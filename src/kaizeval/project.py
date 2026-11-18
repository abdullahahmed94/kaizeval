"""Project mode: run the whole journey from one `kaizeval.yaml` file.

This is the shortest path to using Kaizeval from a terminal, a CI job or a coding assistant:

    kaizeval init          # writes kaizeval.yaml
    kaizeval run           # connect, generate tests, evaluate, recommend, compare with the last run

`run` is idempotent. It reuses the workspace, the tests and the baseline it created before, so
running it again after changing the agent gives a paired A/B comparison with the first run.
Everything goes through the public REST API, either of an embedded local engine or of a server
given with `--url`.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import secrets
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from .client import Api, ApiError

CONFIG_NAME = "kaizeval.yaml"
STATE_DIR = ".kaizeval"

TEMPLATE = """\
# Kaizeval project file. Run `kaizeval run` in this folder.
project: my-agent

# offline   = only local/private endpoints can be reached (default)
# connected = also allows a public model provider that you explicitly mark with allow_public
network: offline

# --- 1. The model Kaizeval uses to write tests, review answers and propose fixes ----------
# Remove this whole block to run without any model: only imported tests and deterministic
# checks are used then.
model:
  base_url: http://localhost:11434/v1     # any OpenAI-compatible endpoint (local or hosted)
  model: your-model-name
  # api_key_env: MY_PROVIDER_API_KEY      # name of the environment variable that holds the key
  # allow_public: true                    # required for a hosted provider (with network: connected)

# --- 2. The agent you want to test --------------------------------------------------------
agent:
  url: http://127.0.0.1:8200/chat
  style: simple            # simple: POST {"message": ...} -> {"answer": ..., "citations": [...]}
                           # openai: an OpenAI-style /chat/completions endpoint
  # response_path: answer
  # citations_path: citations
  # api_key_env: MY_AGENT_API_KEY
  # candidate_protocol: true        # the agent implements the candidate admin API (enables auto-optimization)
  # admin_token_env: MY_AGENT_ADMIN_TOKEN

# --- 3. What the agent is supposed to do, and what it should know -------------------------
requirements: |
  Answer customer questions using only the company documents.
  Always name the source document.
  Say you do not know when the documents do not contain the answer.
# requirements_file: docs/requirements.md
# instructions_file: prompts/system.md   # the agent's own instructions / system prompt
knowledge:
  - docs/**/*.md
  # - kb/*.pdf

# --- 4. Tests -----------------------------------------------------------------------------
tests:
  cases: 30                # how many test cases to generate
  testers: 3               # independent tester agents (1-8): each writes and runs its own share
  holdout_fraction: 0.3    # share sealed away for the final independent check
  # languages: [Arabic, French]          # also ask the same questions in these languages
  # import: [tests/approved-qa.csv]      # your own approved questions (CSV or JSONL)

# --- 5. Review ----------------------------------------------------------------------------
evaluation:
  reviewers: 2             # independent reviewer agents per answer (0 = rules only, 1-7)
  target: 1.0              # keep improving until the regression score reaches this (1.0 = 100%)
  require_citations: true
  # repeats: 3             # ask every question this many times and average (for agents whose answers vary)

# --- 6. Automatic improvement (only for agents with candidate_protocol: true) --------------
# The loop stops when the target is reached, or when two cycles in a row leave the score
# unchanged. It then exits with the remaining recommendations and a report.
optimize:
  enabled: false
  # max_rounds: 20         # optional extra limit on the number of cycles
"""

DEFAULTS: dict[str, Any] = {
    "project": "my-agent", "network": "offline", "model": None, "agent": {}, "knowledge": [],
    "requirements": "", "requirements_file": None, "instructions_file": None,
    "tests": {"cases": 30, "testers": 3, "holdout_fraction": 0.3, "languages": [], "import": [], "seed": 0},
    "evaluation": {"reviewers": 2, "target": 1.0, "require_citations": True, "repeats": 1,
                   "max_agent_calls": None, "max_model_calls": None},
    "optimize": {"enabled": False, "max_rounds": None, "stagnation_rounds": 2},
}

Log = Callable[[str], None]


class ProjectError(Exception):
    pass


def write_template(directory: Path, *, force: bool = False) -> Path:
    path = directory / CONFIG_NAME
    if path.exists() and not force:
        raise ProjectError(f"{path} already exists (use --force to overwrite it)")
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    return path


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ProjectError(f"{path} not found. Create one with `kaizeval init`.")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ProjectError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProjectError(f"{path} must contain a mapping of settings")
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise ProjectError(f"unknown setting(s) in {path.name}: {', '.join(unknown)}")
    config: dict[str, Any] = {}
    for key, default in DEFAULTS.items():
        value = raw.get(key, default)
        if isinstance(default, dict) and default:
            if value is not None and not isinstance(value, dict):
                raise ProjectError(f"'{key}' must be a mapping")
            extra = sorted(set(value or {}) - set(default))
            if extra:
                raise ProjectError(f"unknown setting(s) under '{key}': {', '.join(extra)}")
            value = {**default, **(value or {})}
        config[key] = value
    if not (config["agent"] or {}).get("url"):
        raise ProjectError("agent.url is required: the HTTP endpoint of the agent you want to test")
    if config["network"] not in ("offline", "connected"):
        raise ProjectError("network must be 'offline' or 'connected'")
    if isinstance(config["knowledge"], str):
        config["knowledge"] = [config["knowledge"]]
    if not 0 <= int(config["evaluation"]["reviewers"]) <= 7:
        raise ProjectError("evaluation.reviewers must be between 0 and 7")
    if not 1 <= int(config["tests"]["testers"]) <= 8:
        raise ProjectError("tests.testers must be between 1 and 8")
    return config


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Engine:
    """An API endpoint to talk to: a remote server, or a local engine started for this project."""

    def __init__(self, config: dict[str, Any], root: Path, url: str | None, log: Log):
        self.config, self.root, self.url, self.log = config, root, url, log
        self.state_dir = root / STATE_DIR
        self.state_file = self.state_dir / "project-state.json"
        self.state: dict[str, Any] = {}
        self.server: Any = None
        self.api: Api

    def __enter__(self) -> Engine:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.state_file.exists():
            self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
        if self.url:
            self._connect_remote()
        else:
            self._start_embedded()
        return self

    def __exit__(self, *_: Any) -> None:
        self.save()
        if self.server is not None:
            self.server.should_exit = True

    def save(self) -> None:
        self.state_file.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def _connect_remote(self) -> None:
        self.api = Api(self.url or "")
        token = os.environ.get("KAIZEVAL_TOKEN")
        if token:
            self.api.token = token
            return
        email, password = os.environ.get("KAIZEVAL_EMAIL"), os.environ.get("KAIZEVAL_PASSWORD")
        if not email or not password:
            raise ProjectError("with --url, set KAIZEVAL_TOKEN, or KAIZEVAL_EMAIL and KAIZEVAL_PASSWORD")
        self.api.token = self.api.call("POST", "/auth/login", json={"email": email, "password": password})["token"]

    def _start_embedded(self) -> None:
        os.environ["KAIZEVAL_DATA_DIR"] = str(self.state_dir)
        os.environ["KAIZEVAL_OFFLINE"] = "false" if self.config["network"] == "connected" else "true"
        os.environ.setdefault("KAIZEVAL_LOG_LEVEL", "WARNING")
        import threading
        import time

        import uvicorn

        from .app import create_app
        from .config import reset_settings

        reset_settings()
        port = _free_port()
        self.server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=self.server.run, daemon=True)
        thread.start()
        deadline = time.time() + 30
        while not self.server.started:
            if time.time() > deadline or not thread.is_alive():
                raise ProjectError("could not start the local engine")
            time.sleep(0.05)
        self.url = f"http://127.0.0.1:{port}"
        self.api = Api(self.url)
        account = self.state.get("account")
        if account:
            self.api.token = self.api.call("POST", "/auth/login", json=account)["token"]
        else:
            account = {"email": "local@kaizeval.local", "password": secrets.token_urlsafe(18)}
            self.api.token = self.api.call("POST", "/auth/signup", json={**account, "name": "Local owner"})["token"]
            self.state["account"] = account
            self.save()


def _read(root: Path, name: str | None) -> str:
    if not name:
        return ""
    path = (root / name)
    if not path.exists():
        raise ProjectError(f"file not found: {path}")
    return path.read_text(encoding="utf-8")


def _secret(env_name: str | None, what: str) -> str | None:
    if not env_name:
        return None
    value = os.environ.get(env_name)
    if not value:
        raise ProjectError(f"{what}: environment variable {env_name} is not set")
    return value


def _mode(reviewers: int) -> str:
    return {0: "deterministic", 1: "single", 2: "dual"}.get(reviewers, "panel")


def run_project(config_path: Path, *, url: str | None = None, holdout: bool = False, regenerate: bool = False,
                reset_baseline: bool = False, log: Log = print) -> dict[str, Any]:
    """Run the journey described by the project file and return a machine-readable result."""
    config_path = config_path.resolve()
    root = config_path.parent
    config = load_config(config_path)
    with Engine(config, root, url, log) as engine:
        try:
            return _run(engine, config, root, holdout=holdout, regenerate=regenerate, reset_baseline=reset_baseline, log=log)
        except ApiError as exc:
            raise ProjectError(f"{exc.code}: {exc.message}") from exc


def _run(engine: Engine, config: dict[str, Any], root: Path, *, holdout: bool, regenerate: bool,
         reset_baseline: bool, log: Log) -> dict[str, Any]:
    api, state = engine.api, engine.state
    notes: list[str] = []

    # 1. workspace
    ws = state.get("workspace_id")
    if ws:
        try:
            api.call("GET", f"/workspaces/{ws}")
        except ApiError:
            ws = None
    if not ws:
        ws = api.call("POST", "/workspaces", json={"name": config["project"], "mode": config["network"]})["id"]
        # keep the local account; everything that lived in the old workspace is recreated below
        for key in ("provider", "agent", "spec", "dataset", "baseline_run_id", "candidate_id"):
            state.pop(key, None)
        state["workspace_id"] = ws
    base = f"/workspaces/{ws}"
    log(f"[1/7] workspace ready ({config['network']} mode)")

    # 2. model
    model = config["model"]
    reviewers = int(config["evaluation"]["reviewers"])
    if model:
        fingerprint = _fingerprint(model)
        if (state.get("provider") or {}).get("fingerprint") != fingerprint:
            kind = model.get("kind", "openai_compatible")
            provider = api.call("POST", f"{base}/model-providers", json={
                "name": f"{config['project']} model", "kind": kind, "base_url": model.get("base_url"),
                "default_model": model.get("model", ""), "allow_egress": bool(model.get("allow_public")),
                "api_key": _secret(model.get("api_key_env"), "model.api_key_env"), "params": model.get("params") or {}})
            state["provider"] = {"id": provider["id"], "fingerprint": fingerprint}
        provider_id = state["provider"]["id"]
        preflight = api.call("POST", f"{base}/model-providers/{provider_id}/verify")
        if not preflight["ok"]:
            failed = "; ".join(f"{c['name']}: {c['detail']}" for c in preflight["checks"] if not c["ok"])
            state.pop("provider", None)
            raise ProjectError(f"the model did not pass preflight ({failed})")
        api.call("PUT", f"{base}/model-role-policies/default", json={"provider_id": provider_id})
        log(f"[2/7] model verified{' (FAKE provider: results are SYNTHETIC)' if preflight.get('synthetic') else ''}")
    else:
        if reviewers:
            notes.append("No model is configured, so reviewers were set to 0: only deterministic checks are used.")
        reviewers = 0
        log("[2/7] no model configured: deterministic checks only")

    # 3. agent
    agent_cfg = config["agent"]
    fingerprint = _fingerprint(agent_cfg)
    if (state.get("agent") or {}).get("fingerprint") != fingerprint:
        candidate_protocol = bool(agent_cfg.get("candidate_protocol"))
        if candidate_protocol:
            body = {"adapter": "local_config", "config": {"base_url": agent_cfg["url"]},
                    "credential": _secret(agent_cfg.get("admin_token_env"), "agent.admin_token_env"),
                    "access_level": "candidate_write", "allow_promote": True}
        else:
            http = {k: agent_cfg[k] for k in ("url", "style", "response_path", "citations_path", "timeout_s",
                                              "cost_per_call", "model", "allow_public") if agent_cfg.get(k) is not None}
            body = {"adapter": "http_chat", "config": http,
                    "credential": _secret(agent_cfg.get("api_key_env"), "agent.api_key_env")}
        agent = api.call("POST", f"{base}/agents", json={"name": config["project"], **body})
        state["agent"] = {"id": agent["id"], "fingerprint": fingerprint}
        for key in ("baseline_run_id", "candidate_id"):
            state.pop(key, None)
    agent_id = state["agent"]["id"]
    verified = api.call("POST", f"{base}/agents/{agent_id}/verify")
    modes = api.call("GET", f"{base}/agents/{agent_id}/capabilities")["modes"]
    log(f"[3/7] agent connected (capabilities: {', '.join(verified['capabilities'])})")

    # 4. knowledge, requirements and instructions
    files: list[Path] = []
    for pattern in config["knowledge"] or []:
        files.extend(Path(p) for p in sorted(glob.glob(str(root / pattern), recursive=True)) if Path(p).is_file())
    shas = []
    for path in files:
        document = api.call("POST", f"{base}/documents", files={"file": (path.name, path.read_bytes())}, data={"kind": "kb"})
        if document["status"] != "parsed":
            notes.append(f"{path.name} could not be parsed: {document.get('error')}")
        shas.append(document["sha256"])
    requirements = "\n".join(part for part in [
        (config["requirements"] or "").strip(), _read(root, config["requirements_file"]).strip()] if part)
    instructions = _read(root, config["instructions_file"]).strip()
    if instructions:
        requirements += "\n\nThe agent's own instructions (it must follow them):\n" + instructions
    spec_fp = _fingerprint([requirements, shas, bool(model)])
    if (requirements or (model and files)) and (state.get("spec") or {}).get("fingerprint") != spec_fp:
        job = api.wait_job(ws, api.call("POST", f"{base}/specifications:build", json={
            "agent_id": agent_id, "requirements_text": requirements or None, "infer": bool(model and files)}))
        spec_id = job["result"]["specification_id"]
        api.call("POST", f"{base}/specifications/{spec_id}/review", json={
            "decision": "approve" if requirements else "skip", "note": "from the project file"})
        state["spec"] = {"id": spec_id, "fingerprint": spec_fp}
    log(f"[4/7] {len(files)} knowledge file(s), requirements {'provided' if requirements else 'not provided'}")

    # 5. tests
    tests = config["tests"]
    imports = [Path(p) for pattern in tests["import"] or [] for p in sorted(glob.glob(str(root / pattern)))]
    dataset_fp = _fingerprint([tests, shas, [hashlib.sha256(p.read_bytes()).hexdigest() for p in imports], bool(model)])
    if regenerate or (state.get("dataset") or {}).get("fingerprint") != dataset_fp:
        dataset_id = None
        if model and files:
            generated = api.call("POST", f"{base}/datasets:generate", json={
                "name": f"{config['project']} tests", "agent_id": agent_id,
                "specification_id": (state.get("spec") or {}).get("id"), "n_cases": int(tests["cases"]),
                "testers": int(tests["testers"]), "holdout_fraction": float(tests["holdout_fraction"]),
                "seed": int(tests.get("seed") or 0), "languages": tests["languages"] or []})
            api.wait_job(ws, generated["job"])
            dataset_id = generated["dataset"]["id"]
        for path in imports:
            data = {"approved": "true", "holdout_fraction": str(tests["holdout_fraction"] if dataset_id is None else 0.0),
                    "name": f"{config['project']} tests"}
            if dataset_id:
                data["dataset_id"] = dataset_id
            dataset_id = api.call("POST", f"{base}/datasets:import", files={"file": (path.name, path.read_bytes())},
                                  data=data)["dataset"]["id"]
        if dataset_id is None:
            raise ProjectError("there is nothing to test with: configure a model and knowledge files so tests can be "
                               "generated, or list your own questions under tests.import")
        state["dataset"] = {"id": dataset_id, "fingerprint": dataset_fp}
        for key in ("baseline_run_id", "candidate_id"):
            state.pop(key, None)
    dataset_id = state["dataset"]["id"]
    counts = api.call("GET", f"{base}/datasets/{dataset_id}")["counts"]
    log(f"[5/7] tests ready: {counts.get('regression', 0)} regression, {counts.get('holdout', 0)} sealed holdout "
        f"({tests['testers']} tester(s))")

    # 6. evaluate
    mode = _mode(reviewers)
    evaluation = config["evaluation"]
    limits = {k: v for k, v in (("max_agent_calls", evaluation.get("max_agent_calls")),
                                ("max_model_calls", evaluation.get("max_model_calls"))) if v is not None}
    eval_config = {"reviewers": reviewers, "testers": int(tests["testers"]), "repeats": max(1, int(evaluation.get("repeats") or 1)),
                   "require_citations": bool(evaluation["require_citations"]), "limits": limits}
    started = api.call("POST", f"{base}/evaluation-runs", json={
        "agent_id": agent_id, "dataset_id": dataset_id, "mode": mode, "config": eval_config})
    api.wait_job(ws, started["job"])
    run = api.call("GET", f"{base}/evaluation-runs/{started['run']['id']}")
    summary = run["summary"]
    target = float(evaluation["target"])
    log(f"[6/7] evaluated with {reviewers} reviewer(s): overall {summary['overall']:.1%}, pass rate "
        f"{summary['pass_rate']:.1%}, critical failures {summary['critical_failures']}")

    failures = []
    for item in api.call("GET", f"{base}/evaluation-runs/{run['id']}/results", params={"verdict": "fail", "limit": 8})["items"]:
        scores = item["final"].get("scores") or {}
        failures.append({"question": item["case"]["turns"][0]["content"], "answer": item["response"].get("text", "")[:300],
                         "category": item["case"]["category"],
                         "why": "; ".join(f"{d}={v['score']} ({v.get('detail') or v['method']})" for d, v in scores.items())})
    recommendations = [
        {"title": r["title"], "detail": r["detail"], "category": r["category"], "target_component": r["target_component"],
         "cases_affected": r["impact"].get("cases_affected"), "risk": r["risk"],
         "auto_apply_available": r["auto_apply_available"], "suggested_patch": r["suggested_patch"],
         "why_not_automatic": r["auto_apply_blocker"]}
        for r in api.call("GET", f"{base}/recommendations", params={"run_id": run["id"]})["items"]]

    # A/B against the first run of this project (the baseline)
    comparison = None
    baseline_id = None if reset_baseline else state.get("baseline_run_id")
    if baseline_id and baseline_id != run["id"]:
        try:
            compared = api.call("GET", f"{base}/compare", params={"baseline_run_id": baseline_id, "candidate_run_id": run["id"]})
            comparison = {**compared["overall"], "critical_failures": compared["critical_failures"],
                          "by_category": compared["by_category"], "baseline_run_id": baseline_id}
        except ApiError as exc:
            notes.append(f"No comparison with the baseline run: {exc.message}")
            baseline_id = None
    if not baseline_id:
        state["baseline_run_id"] = run["id"]
    # Two runs in a row with the same score mean the last change made no difference.
    previous_overall = state.get("last_overall")
    stalled = (previous_overall is not None and summary["overall"] is not None
               and abs(previous_overall - summary["overall"]) < 1e-9 and state.get("last_dataset_id") == dataset_id)
    state["last_run_id"], state["last_overall"], state["last_dataset_id"] = run["id"], summary["overall"], dataset_id

    # 7. automatic improvement of an isolated candidate, when the agent supports it
    optimization = None
    final_overall = summary["overall"]
    report_id = run["id"]
    wants_optimization = bool(config["optimize"]["enabled"])
    if wants_optimization and not modes["human_guided_optimization"]:
        notes.append("optimize.enabled is set, but this agent cannot be patched through its connector: "
                     "recommendations only. Apply them, then run again.")
    elif wants_optimization and final_overall >= target:
        notes.append("The target is already met; no optimization was needed.")
    elif wants_optimization:
        if mode == "deterministic":
            raise ProjectError("automatic optimization needs a model (the optimizer role); add a model block")
        api.call("PATCH", base, json={"settings": {"allow_autonomous_optimization": True}})
        body = {"agent_id": agent_id, "dataset_id": dataset_id, "mode": "autonomous", "policy": {
            "max_rounds": config["optimize"]["max_rounds"], "stagnation_rounds": int(config["optimize"]["stagnation_rounds"]),
            "target_score": target, "eval_mode": mode, "eval_config": eval_config, "limits": limits, "run_holdout": True}}
        try:
            opt = api.call("POST", f"{base}/optimization-runs", json=body)
        except ApiError as exc:
            if exc.code != "HOLDOUT_SPENT":
                raise
            api.wait_job(ws, api.call("POST", f"{base}/datasets/{dataset_id}/holdout:rotate", json={}))
            notes.append("The previous holdout had already informed a decision, so a fresh one was generated.")
            opt = api.call("POST", f"{base}/optimization-runs", json=body)
        api.wait_job(ws, opt["job"])
        report = api.call("GET", f"{base}/reports/{opt['optimization_run']['id']}")
        report_id = report["id"]
        best = report.get("best") or {}
        final_overall = best.get("overall") if best.get("overall") is not None else final_overall
        # The optimization measures the same baseline again. If the two measurements differ, the
        # agent's answers vary from run to run, and small score changes cannot be trusted.
        measured_again = (report.get("baseline") or {}).get("overall")
        if measured_again is not None and summary["overall"] is not None and abs(measured_again - summary["overall"]) >= 0.02:
            noise = abs(measured_again - summary["overall"])
            notes.append(f"The agent's answers vary between identical runs: the same baseline version scored "
                         f"{summary['overall']:.1%} and then {measured_again:.1%}. Treat changes smaller than about "
                         f"{noise * 100:.0f} percentage points as noise, and set evaluation.repeats to 2 or more to average it out.")
        candidate = report.get("candidate") or {}
        state["candidate_id"] = candidate.get("id")
        ab = (report.get("regression_comparison") or {}).get("overall")
        optimization = {
            "status": report["status"], "stop_reason": report["stop_reason"], "rounds": report["rounds"],
            "baseline_overall": (report.get("baseline") or {}).get("overall"), "best_overall": best.get("overall"),
            "regression_ab": ab, "holdout": report.get("holdout"),
            "changes": [{"round": c["round"], "patch": c["patch"], "state": c["state"],
                         "reasons": (c.get("outcome") or {}).get("reasons", [])} for c in report.get("changes") or []],
            "remaining_recommendations": [
                {"title": r["title"], "detail": r["detail"], "category": r["category"],
                 "cases_affected": r["impact"].get("cases_affected"), "suggested_patch": r["suggested_patch"],
                 "why_not_automatic": r["auto_apply_blocker"]} for r in report.get("remaining_recommendations") or []],
            "candidate": {"id": candidate.get("id"), "status": candidate.get("status"),
                          "diff_from_baseline": candidate.get("diff_from_baseline")},
        }
        log(f"[7/7] optimization {report['status']} ({report['stop_reason']}): best {final_overall:.1%} "
            f"after {report['rounds']} round(s)")

    holdout_result = None
    if holdout and not optimization:
        check = api.call("POST", f"{base}/evaluation-runs", json={
            "agent_id": agent_id, "dataset_id": dataset_id, "mode": mode, "split": "holdout", "config": eval_config})
        api.wait_job(ws, check["job"])
        checked = api.call("GET", f"{base}/evaluation-runs/{check['run']['id']}")["summary"]
        holdout_result = {"overall": checked["overall"], "pass_rate": checked["pass_rate"], "cases": checked["cases"],
                          "critical_failures": checked["critical_failures"],
                          "note": "Aggregate result on the sealed holdout. This holdout is now spent; "
                                  "it is rotated automatically the next time it is needed."}

    report_path = root / "kaizeval-report.md"
    report_path.write_text(api.call("GET", f"{base}/reports/{report_id}", params={"format": "md"}, raw=True), encoding="utf-8")

    target_met = final_overall is not None and final_overall >= target - 1e-9
    next_steps: list[str] = []
    if optimization and (optimization["candidate"] or {}).get("status") == "awaiting_decision":
        next_steps.append("Candidate B is waiting for your decision. Nothing was changed in production. "
                          "Run `kaizeval release approve` to promote it, or `kaizeval release reject`.")
    elif not target_met and stalled:
        next_steps.append("Two runs in a row scored the same, so the loop stops here: the last change made no "
                          "difference. The report and the recommendations describe what still fails.")
    elif not target_met:
        if recommendations:
            next_steps.append("Apply the recommendations to your agent, then run `kaizeval run` again: the next run "
                              "is compared pairwise with the baseline run.")
        else:
            next_steps.append("No recommendation was produced; inspect the failed cases in the report.")
    if target_met and not optimization and not holdout_result and counts.get("holdout"):
        next_steps.append("The regression target is met. Run `kaizeval run --holdout` once for the final independent check.")
    if comparison and comparison.get("verdict") == "inconclusive":
        notes.append("The difference from the baseline is statistically inconclusive at this number of cases.")

    result = {
        "project": config["project"], "workspace_id": ws, "agent_id": agent_id, "dataset_id": dataset_id,
        "run_id": run["id"], "synthetic": bool(run["synthetic"]), "mode": mode, "reviewers": reviewers,
        "testers": int(tests["testers"]), "cases": summary["cases"], "holdout_cases": counts.get("holdout", 0),
        "overall": summary["overall"], "pass_rate": summary["pass_rate"], "critical_failures": summary["critical_failures"],
        "by_category": summary["by_category"], "by_dimension": summary["by_dimension"],
        "target": target, "final_overall": final_overall, "target_met": target_met, "stalled": bool(stalled),
        "failures": failures, "recommendations": recommendations, "comparison_with_baseline": comparison,
        "optimization": optimization, "holdout": holdout_result, "report": str(report_path),
        "console": engine.url, "notes": notes, "next_steps": next_steps,
    }
    (root / STATE_DIR / "last-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def release(config_path: Path, decision: str, *, url: str | None = None, note: str | None = None,
            log: Log = print) -> dict[str, Any]:
    """Record the owner's release decision for the candidate of the last optimization, and promote it if approved."""
    if decision not in ("approve", "reject"):
        raise ProjectError("decision must be 'approve' or 'reject'")
    config_path = config_path.resolve()
    config = load_config(config_path)
    with Engine(config, config_path.parent, url, log) as engine:
        ws, candidate_id = engine.state.get("workspace_id"), engine.state.get("candidate_id")
        if not ws or not candidate_id:
            raise ProjectError("there is no candidate waiting for a decision; run `kaizeval run` with optimize.enabled first")
        base = f"/workspaces/{ws}"
        try:
            decided = engine.api.call("POST", f"{base}/candidates/{candidate_id}/approve", json={
                "decision": "approved" if decision == "approve" else "rejected", "note": note})
            if decision == "reject":
                engine.state.pop("candidate_id", None)
                return {"decision": "rejected", "candidate_status": decided["candidate"]["status"], "production": "unchanged"}
            promoted = engine.api.call("POST", f"{base}/candidates/{candidate_id}/promote")
        except ApiError as exc:
            raise ProjectError(f"{exc.code}: {exc.message}") from exc
        engine.state.pop("candidate_id", None)
        return {"decision": "approved", "candidate_status": promoted["candidate"]["status"], "production": promoted["result"]}


def format_result(result: dict[str, Any]) -> str:
    """A short human-readable summary of a run."""
    pct = lambda v: "n/a" if v is None else f"{v:.1%}"  # noqa: E731
    lines = ["", f"Kaizeval: {result['project']}"]
    if result["synthetic"]:
        lines.append("  SYNTHETIC: the fake model provider was used; reviewer scores are not a real evaluation.")
    lines.append(f"  score {pct(result['overall'])} (target {pct(result['target'])})   pass rate {pct(result['pass_rate'])}   "
                 f"critical failures {result['critical_failures']}   cases {result['cases']}   "
                 f"testers {result['testers']}   reviewers {result['reviewers']}")
    for name, values in (result["by_category"] or {}).items():
        lines.append(f"    {name:<18} {pct(values.get('mean')):>7}   ({values['n']} cases)")
    comparison = result.get("comparison_with_baseline")
    if comparison:
        lines.append(f"  vs baseline run: {comparison['delta_percentage_points']:+.2f} pp over {comparison['n']} paired cases "
                     f"-> {comparison['verdict']}")
    optimization = result.get("optimization")
    if optimization:
        lines.append(f"  optimization: {optimization['status']} after {optimization['rounds']} round(s), "
                     f"{pct(optimization['baseline_overall'])} -> {pct(optimization['best_overall'])} ({optimization['stop_reason']})")
        ab = optimization.get("regression_ab")
        if ab:
            lines.append(f"    A/B on regression: {ab['delta_percentage_points']:+.2f} pp -> {ab['verdict']}")
        if optimization.get("remaining_recommendations"):
            lines.append("    still recommended for the best version:")
            lines += [f"      - {r['title']} ({r['cases_affected']} case(s))" for r in optimization["remaining_recommendations"]]
        hold = (optimization.get("holdout") or {})
        if hold.get("status") == "completed":
            lines.append(f"    sealed holdout:    {hold['overall']['delta_percentage_points']:+.2f} pp -> {hold['overall']['verdict']}")
    if result.get("holdout"):
        lines.append(f"  sealed holdout: {pct(result['holdout']['overall'])} over {result['holdout']['cases']} cases")
    if result["failures"]:
        lines.append("  failed cases:")
        lines += [f"    - {f['question'][:70]}  [{f['why'][:90]}]" for f in result["failures"][:5]]
    if result["recommendations"]:
        lines.append("  recommendations:")
        lines += [f"    - {r['title']} ({r['cases_affected']} case(s))" for r in result["recommendations"]]
    lines += [f"  note: {note}" for note in result["notes"]]
    lines += [f"  next: {step}" for step in result["next_steps"]]
    lines.append(f"  report: {result['report']}")
    lines.append(f"  result: {'TARGET MET' if result['target_met'] else 'target not met'}")
    return "\n".join(lines)
