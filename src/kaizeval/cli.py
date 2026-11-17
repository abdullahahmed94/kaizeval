"""Command line interface: serve, worker, sample-agent, demo, ci, migrate."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from . import __version__

EXIT_OK, EXIT_QUALITY_GATE, EXIT_ERROR = 0, 1, 2


def _serve(host: str, port: int) -> None:
    import uvicorn

    from .app import create_app

    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


def _start_thread(app: Any, host: str, port: int) -> Any:
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started:
        if time.time() > deadline or not thread.is_alive():
            raise RuntimeError(f"could not start a server on {host}:{port} (is the port in use?)")
        time.sleep(0.05)
    return server


class Api:
    """Minimal client for the Kaizeval REST API (used by `demo` and `ci`)."""

    def __init__(self, base_url: str, token: str | None = None):
        self.base = base_url.rstrip("/") + "/api/v1"
        self.client = httpx.Client(timeout=120, trust_env=False)
        self.token = token

    def call(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        response = self.client.request(method, self.base + path, headers=headers, **kwargs)
        if response.status_code >= 400:
            try:
                error = response.json().get("error", {})
            except ValueError:
                error = {"code": "HTTP_ERROR", "message": response.text[:200]}
            raise RuntimeError(f"{error.get('code')}: {error.get('message')}")
        return response.json() if response.content else None

    def wait_job(self, ws: str, job: dict[str, Any], timeout: float = 1800) -> dict[str, Any]:
        deadline = time.time() + timeout
        while job["status"] in ("queued", "running"):
            if time.time() > deadline:
                raise RuntimeError("timed out waiting for the job to finish")
            time.sleep(0.4)
            job = self.call("GET", f"/workspaces/{ws}/jobs/{job['id']}")
        if job["status"] == "failed":
            raise RuntimeError(job.get("error") or "job failed")
        return job


# --- demo ---------------------------------------------------------------------------------


def run_demo_flow(api: Api, *, agent_url: str, admin_token: str, email: str, password: str,
                  log=print) -> dict[str, Any]:
    """Drive the complete journey through the public API against the bundled sample agent."""
    from .sample_agent import KB_DIR

    auth = api.call("POST", "/auth/signup", json={"email": email, "password": password, "name": "Demo Owner"})
    api.token = auth["token"]
    ws = api.call("POST", "/workspaces", json={"name": "Demo workspace", "mode": "offline"})["id"]
    base = f"/workspaces/{ws}"
    log("1/8 workspace created (offline mode)")

    provider = api.call("POST", f"{base}/model-providers", json={"name": "Fake provider (SYNTHETIC)", "kind": "fake"})
    preflight = api.call("POST", f"{base}/model-providers/{provider['id']}/verify")
    api.call("PUT", f"{base}/model-role-policies/default", json={"provider_id": provider["id"]})
    log(f"2/8 model provider verified (preflight ok={preflight['ok']}, SYNTHETIC fake provider)")

    agent = api.call("POST", f"{base}/agents", json={
        "name": "Sample support agent", "adapter": "local_config", "config": {"base_url": agent_url},
        "credential": admin_token, "access_level": "candidate_write", "allow_promote": True})
    verified = api.call("POST", f"{base}/agents/{agent['id']}/verify")
    log(f"3/8 agent verified, capabilities: {', '.join(verified['capabilities'])}")

    for path in sorted(KB_DIR.glob("*.md")):
        api.call("POST", f"{base}/documents", files={"file": (path.name, path.read_bytes(), "text/markdown")},
                 data={"kind": "kb"})
    log("4/8 knowledge documents ingested")

    job = api.wait_job(ws, api.call("POST", f"{base}/specifications:build", json={"agent_id": agent["id"], "infer": True}))
    spec_id = job["result"]["specification_id"]
    api.call("POST", f"{base}/specifications/{spec_id}/review", json={"decision": "skip", "note": "demo"})
    log("5/8 specification inferred, review skipped (recorded)")

    generated = api.call("POST", f"{base}/datasets:generate", json={
        "name": "Support knowledge tests", "agent_id": agent["id"], "specification_id": spec_id, "n_cases": 36, "seed": 7})
    api.wait_job(ws, generated["job"])
    dataset = api.call("GET", f"{base}/datasets/{generated['dataset']['id']}")
    log(f"6/8 dataset ready: {dataset['counts'].get('regression', 0)} regression cases, "
        f"{dataset['counts'].get('holdout', 0)} sealed holdout cases")

    started = api.call("POST", f"{base}/evaluation-runs", json={"agent_id": agent["id"], "dataset_id": dataset["id"], "mode": "dual"})
    api.wait_job(ws, started["job"])
    baseline = api.call("GET", f"{base}/evaluation-runs/{started['run']['id']}")
    log(f"7/8 baseline evaluated (dual judges): overall {baseline['summary']['overall']:.3f}, "
        f"pass rate {baseline['summary']['pass_rate']:.3f}")

    api.call("PATCH", base, json={"settings": {"allow_autonomous_optimization": True}})
    opt = api.call("POST", f"{base}/optimization-runs", json={
        "agent_id": agent["id"], "dataset_id": dataset["id"], "mode": "autonomous",
        "policy": {"max_rounds": 6, "stagnation_rounds": 3, "eval_mode": "single"}})
    api.wait_job(ws, opt["job"])
    report = api.call("GET", f"{base}/reports/{opt['optimization_run']['id']}")
    comparison = report.get("regression_comparison") or {}
    overall = comparison.get("overall") or {}
    holdout = report.get("holdout") or {}
    log(f"8/8 optimization finished ({report['stop_reason']}): regression "
        f"{overall.get('delta_percentage_points', 0):+.2f} pp [{overall.get('verdict', 'n/a')}], holdout "
        f"{(holdout.get('overall') or {}).get('delta_percentage_points', 0):+.2f} pp "
        f"[{(holdout.get('overall') or {}).get('verdict', holdout.get('status'))}]")
    return {"workspace_id": ws, "agent_id": agent["id"], "dataset_id": dataset["id"],
            "baseline_run_id": started["run"]["id"], "optimization_run_id": opt["optimization_run"]["id"], "report": report}


def cmd_demo(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["KAIZEVAL_DATA_DIR"] = str(data_dir)
    os.environ.setdefault("KAIZEVAL_OFFLINE", "true")
    from .app import create_app
    from .config import reset_settings
    from .sample_agent import create_sample_agent

    reset_settings()
    token_file = data_dir / "sample-agent-admin.token"
    if not token_file.exists():
        token_file.write_text(secrets.token_urlsafe(24), encoding="utf-8")
    admin_token = token_file.read_text(encoding="utf-8").strip()
    credentials = data_dir / "demo-login.json"
    first_run = not credentials.exists()

    _start_thread(create_sample_agent(admin_token=admin_token, state_file=data_dir / "sample-agent-state.json"),
                  "127.0.0.1", args.agent_port)
    _start_thread(create_app(), args.host, args.port)
    url = f"http://{args.host}:{args.port}"
    print(f"Kaizeval {__version__} demo\n  console : {url}\n  agent   : http://127.0.0.1:{args.agent_port} (bundled sample agent)\n")
    if first_run:
        login = {"email": "demo@kaizeval.local", "password": secrets.token_urlsafe(12)}
        print("Running the full journey against the sample agent. NOTE: the demo uses the deterministic FAKE model\n"
              "provider, so all judge scores are SYNTHETIC. Configure a real model to get real evaluations.\n")
        try:
            run_demo_flow(Api(url), agent_url=f"http://127.0.0.1:{args.agent_port}", admin_token=admin_token, **login)
        except Exception as exc:  # noqa: BLE001
            print(f"demo failed: {exc}", file=sys.stderr)
            return EXIT_ERROR
        credentials.write_text(json.dumps(login), encoding="utf-8")
    else:
        login = json.loads(credentials.read_text(encoding="utf-8"))
        print("Demo data already exists; reusing it.")
    print(f"\nSign in at {url}\n  email   : {login['email']}\n  password: {login['password']}\n")
    if args.no_serve:
        return EXIT_OK
    print("Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return EXIT_OK


# --- ci gate ------------------------------------------------------------------------------


def cmd_ci(args: argparse.Namespace) -> int:
    """Run a regression evaluation and fail the pipeline when quality gates are not met."""
    token = args.token or os.environ.get("KAIZEVAL_TOKEN")
    api = Api(args.url, token)
    try:
        if not api.token:
            email, password = os.environ.get("KAIZEVAL_EMAIL"), os.environ.get("KAIZEVAL_PASSWORD")
            if not email or not password:
                print("set KAIZEVAL_TOKEN, or KAIZEVAL_EMAIL and KAIZEVAL_PASSWORD", file=sys.stderr)
                return EXIT_ERROR
            api.token = api.call("POST", "/auth/login", json={"email": email, "password": password})["token"]
        base = f"/workspaces/{args.workspace}"
        started = api.call("POST", f"{base}/evaluation-runs", json={
            "agent_id": args.agent, "dataset_id": args.dataset, "mode": args.mode, "agent_version": args.agent_version})
        api.wait_job(args.workspace, started["job"], timeout=args.timeout)
        run = api.call("GET", f"{base}/evaluation-runs/{started['run']['id']}")
        summary = run["summary"] or {}
        failures: list[str] = []
        overall = summary.get("overall")
        if overall is None or overall < args.fail_under:
            failures.append(f"overall score {overall} is below --fail-under {args.fail_under}")
        if summary.get("critical_failures", 0) > args.max_critical:
            failures.append(f"{summary.get('critical_failures')} critical failure(s) exceed --max-critical {args.max_critical}")
        comparison = None
        if args.baseline_run:
            comparison = api.call("GET", f"{base}/compare", params={"baseline_run_id": args.baseline_run,
                                                                    "candidate_run_id": run["id"]})
            if comparison["overall"]["verdict"] == "regressed":
                failures.append(f"statistically significant regression vs baseline run "
                                f"({comparison['overall']['delta_percentage_points']:+.2f} pp)")
            if comparison["critical_failures"]["new"] > 0:
                failures.append(f"{comparison['critical_failures']['new']} new critical failure(s) vs baseline run")
    except RuntimeError as exc:
        print(f"kaizeval ci: {exc}", file=sys.stderr)
        return EXIT_ERROR
    result = {"run_id": run["id"], "overall": overall, "pass_rate": summary.get("pass_rate"),
              "critical_failures": summary.get("critical_failures"), "cases": summary.get("cases"),
              "synthetic": run.get("synthetic"), "comparison": (comparison or {}).get("overall"),
              "gate": "failed" if failures else "passed", "reasons": failures}
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"run {run['id']}: overall {overall}, pass rate {summary.get('pass_rate')}, "
              f"critical failures {summary.get('critical_failures')}, cases {summary.get('cases')}")
        if run.get("synthetic"):
            print("WARNING: SYNTHETIC run (fake model provider); not a real quality signal.")
        for reason in failures:
            print(f"GATE FAILED: {reason}")
        if not failures:
            print("quality gate passed")
    return EXIT_QUALITY_GATE if failures else EXIT_OK


# --- entry point --------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    _serve(args.host, args.port)
    return EXIT_OK


def cmd_worker(_: argparse.Namespace) -> int:
    from . import handlers  # noqa: F401
    from .db import upgrade_db
    from .jobs import Worker
    from .security import configure_logging

    configure_logging()
    upgrade_db()
    worker = Worker()
    worker.recover_orphans(only_stale=True)
    print("kaizeval worker running; press Ctrl+C to stop")
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        pass
    return EXIT_OK


def cmd_sample_agent(args: argparse.Namespace) -> int:
    import uvicorn

    from .sample_agent import create_sample_agent

    token = args.admin_token or os.environ.get("SAMPLE_AGENT_ADMIN_TOKEN")
    state = Path(args.state_file) if args.state_file else None
    uvicorn.run(create_sample_agent(admin_token=token, state_file=state), host=args.host, port=args.port, log_level="warning")
    return EXIT_OK


def cmd_migrate(_: argparse.Namespace) -> int:
    from .db import upgrade_db

    upgrade_db()
    print("database is up to date")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kaizeval", description="Evaluate, diagnose and safely optimize AI agents.")
    parser.add_argument("--version", action="version", version=f"kaizeval {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the API and web console")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("worker", help="run a background worker (when KAIZEVAL_WORKER_MODE=external)")
    p.set_defaults(fn=cmd_worker)

    p = sub.add_parser("sample-agent", help="run the bundled deterministic sample agent")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8101)
    p.add_argument("--admin-token", default=None, help="enables the candidate admin API (or SAMPLE_AGENT_ADMIN_TOKEN)")
    p.add_argument("--state-file", default=None)
    p.set_defaults(fn=cmd_sample_agent)

    p = sub.add_parser("demo", help="start everything locally and run the full journey on the sample agent")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--agent-port", type=int, default=8101)
    p.add_argument("--data-dir", default=str(Path.home() / ".kaizeval" / "demo"))
    p.add_argument("--no-serve", action="store_true", help="exit after the journey instead of keeping the console up")
    p.set_defaults(fn=cmd_demo)

    p = sub.add_parser("ci", help="run a regression evaluation and exit non-zero when a quality gate fails")
    p.add_argument("--url", default=os.environ.get("KAIZEVAL_URL", "http://127.0.0.1:8000"))
    p.add_argument("--token", default=None, help="access token (or KAIZEVAL_TOKEN / KAIZEVAL_EMAIL + KAIZEVAL_PASSWORD)")
    p.add_argument("--workspace", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--mode", choices=["deterministic", "single", "dual"], default="deterministic")
    p.add_argument("--agent-version", default="baseline")
    p.add_argument("--fail-under", type=float, default=0.8, help="minimum overall score (0-1)")
    p.add_argument("--max-critical", type=int, default=0, help="maximum allowed critical failures")
    p.add_argument("--baseline-run", default=None, help="fail on a significant regression against this run id")
    p.add_argument("--timeout", type=float, default=1800)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_ci)

    p = sub.add_parser("migrate", help="apply database migrations")
    p.set_defaults(fn=cmd_migrate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
