"""Use Kaizeval purely through its REST API. Standard library only, no SDK needed.

This script signs up, creates a workspace, registers an agent as a black box, imports approved
questions, runs a deterministic evaluation (no model required) and reads back the results,
findings, recommendations and the report. Every HTTP call is printed as it happens.

Start a server and something to evaluate, then run the script:

    kaizeval serve                                  # API on http://127.0.0.1:8000
    kaizeval sample-agent --port 8101               # or point --agent-url at your own agent
    python examples/api_quickstart.py

Options: --url, --agent-url, --email, --password, --questions (CSV or JSONL file).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


class Client:
    def __init__(self, base_url: str, log=print):
        self.base = base_url.rstrip("/") + "/api/v1"
        self.token: str | None = None
        self.log = log

    def call(self, method: str, path: str, body: Any = None, *, files: dict[str, tuple[str, bytes]] | None = None,
             fields: dict[str, str] | None = None, raw: bool = False) -> Any:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if files is not None:
            boundary = uuid.uuid4().hex
            parts = []
            for name, value in (fields or {}).items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
            for name, (filename, content) in files.items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                             f"Content-Type: application/octet-stream\r\n\r\n".encode() + content + b"\r\n")
            data = b"".join(parts) + f"--{boundary}--\r\n".encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - URL supplied by the operator
                status, payload = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, payload = exc.code, exc.read()
        self.log(f"  {method:<6} {path}  ->  {status}")
        if status >= 400:
            try:
                error = json.loads(payload)["error"]
            except (ValueError, KeyError):
                error = {"code": "HTTP_ERROR", "message": payload[:200].decode(errors="replace")}
            raise SystemExit(f"API error {status} {error['code']}: {error['message']}")
        if raw:
            return payload.decode()
        return json.loads(payload) if payload else None

    def wait(self, workspace: str, job: dict[str, Any]) -> dict[str, Any]:
        while job["status"] in ("queued", "running"):
            time.sleep(0.5)
            job = self.call("GET", f"/workspaces/{workspace}/jobs/{job['id']}")
        if job["status"] != "completed":
            raise SystemExit(f"job {job['status']}: {job.get('error')}")
        return job


def main(argv: list[str] | None = None, log=print) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--agent-url", default="http://127.0.0.1:8101/chat")
    parser.add_argument("--email", default=f"api-{uuid.uuid4().hex[:8]}@example.test")
    parser.add_argument("--password", default=uuid.uuid4().hex)
    parser.add_argument("--questions", default=str(HERE / "approved-qa.csv"))
    args = parser.parse_args(argv)
    api = Client(args.url, log)

    log("1. Sign up and create a workspace")
    api.token = api.call("POST", "/auth/signup", {"email": args.email, "password": args.password, "name": "API user"})["token"]
    ws = api.call("POST", "/workspaces", {"name": "API quickstart", "mode": "offline"})["id"]
    base = f"/workspaces/{ws}"

    log("2. Register the agent as a black box and verify the connection")
    agent = api.call("POST", f"{base}/agents", {"name": "My agent", "adapter": "http_chat", "config": {"url": args.agent_url}})
    verified = api.call("POST", f"{base}/agents/{agent['id']}/verify")
    modes = api.call("GET", f"{base}/agents/{agent['id']}/capabilities")["modes"]
    log(f"     capabilities: {verified['capabilities']}  optimization available: {modes['human_guided_optimization']}")

    log("3. Import approved questions and answers")
    questions = Path(args.questions)
    imported = api.call("POST", f"{base}/datasets:import", files={"file": (questions.name, questions.read_bytes())},
                        fields={"name": "Approved Q&A", "approved": "true"})
    dataset_id = imported["dataset"]["id"]
    log(f"     imported {imported['imported']} cases")

    log("4. Run a deterministic evaluation (no model needed) and wait for the job")
    started = api.call("POST", f"{base}/evaluation-runs", {"agent_id": agent["id"], "dataset_id": dataset_id, "mode": "deterministic"})
    api.wait(ws, started["job"])
    run = api.call("GET", f"{base}/evaluation-runs/{started['run']['id']}")
    summary = run["summary"]
    log(f"     overall {summary['overall']:.1%}  pass rate {summary['pass_rate']:.1%}  "
        f"critical failures {summary['critical_failures']}  cases {summary['cases']}")
    for category, values in summary["by_category"].items():
        log(f"     {category:<14} n={values['n']}  mean={values['mean']}")

    log("5. Read per-case results, findings and recommendations")
    failed = api.call("GET", f"{base}/evaluation-runs/{run['id']}/results?verdict=fail&limit=3")
    for item in failed["items"]:
        question = item["case"]["turns"][0]["content"]
        detail = "; ".join(f"{dim}={score['score']} ({score.get('detail', score['method'])})" for dim, score in item["final"]["scores"].items())
        log(f"     FAIL  {question[:60]}  ->  {detail[:110]}")
    findings = api.call("GET", f"{base}/evaluation-runs/{run['id']}/findings")
    recommendations = api.call("GET", f"{base}/recommendations?run_id={run['id']}")["items"]
    log(f"     {findings['total']} finding(s), {len(recommendations)} recommendation(s)")
    for rec in recommendations:
        log(f"     - {rec['title']} [{rec['category']}, {rec['impact']['cases_affected']} case(s)]")
        log(f"       auto-apply: {rec['auto_apply_available']}  reason: {rec['auto_apply_blocker']}")

    log("6. Download the report as Markdown")
    report = api.call("GET", f"{base}/reports/{run['id']}?format=md", raw=True)
    for line in report.splitlines()[:6]:
        log(f"     {line}")
    return {"workspace_id": ws, "agent_id": agent["id"], "dataset_id": dataset_id, "run": run,
            "recommendations": recommendations, "report": report}


if __name__ == "__main__":
    main(sys.argv[1:])
