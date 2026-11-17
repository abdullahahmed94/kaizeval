"""Shared fixtures.

The whole suite runs with strict offline mode on and with a socket guard that fails any attempt
to open a non-loopback connection, so a passing run is itself evidence of no outbound traffic.
"""

from __future__ import annotations

import json
import os
import re
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

_TMP = tempfile.mkdtemp(prefix="kaizeval-tests-")
os.environ.update({
    "KAIZEVAL_DATA_DIR": _TMP,
    "KAIZEVAL_ENV": "test",
    "KAIZEVAL_OFFLINE": "true",
    "KAIZEVAL_WORKER_MODE": "eager",
    "KAIZEVAL_SECRET_KEY": "test-only-secret-key-not-used-anywhere-else",
    "KAIZEVAL_LOG_LEVEL": "WARNING",
})

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from kaizeval.app import create_app  # noqa: E402
from kaizeval.gateway import core as gateway_core  # noqa: E402
from kaizeval.sample_agent import KB_DIR, create_sample_agent  # noqa: E402

gateway_core.RETRY_BASE_SECONDS = 0.0
ADMIN_TOKEN = "test-admin-token-" + uuid.uuid4().hex[:8]

_real_connect = socket.socket.connect
EXTERNAL_ATTEMPTS: list[str] = []


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) else str(address)
    if host not in ("127.0.0.1", "::1", "localhost"):
        EXTERNAL_ATTEMPTS.append(str(address))
        raise AssertionError(f"test attempted an external network connection to {address}")
    return _real_connect(self, address)


@pytest.fixture(autouse=True, scope="session")
def no_external_network():
    socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
    yield
    socket.socket.connect = _real_connect  # type: ignore[method-assign]
    assert not EXTERNAL_ATTEMPTS, f"external connections were attempted: {EXTERNAL_ATTEMPTS}"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(app: Any) -> tuple[str, uvicorn.Server]:
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 20
    while not server.started:
        assert time.time() < deadline, "server did not start"
        time.sleep(0.02)
    return f"http://127.0.0.1:{port}", server


@pytest.fixture(scope="session")
def sample_agent() -> dict[str, Any]:
    app = create_sample_agent(admin_token=ADMIN_TOKEN)
    url, server = serve(app)
    yield {"url": url, "token": ADMIN_TOKEN, "state": app.state.agent_state}
    server.should_exit = True


@pytest.fixture(scope="session")
def client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c


class Session:
    """A signed-in API user bound to one workspace."""

    def __init__(self, client: TestClient, token: str, ws: str | None = None, email: str = ""):
        self.client, self.token, self.ws, self.email = client, token, ws, email

    def request(self, method: str, path: str, expect: int | None = None, **kwargs: Any) -> Any:
        if path.startswith("/ws/"):
            path = f"/workspaces/{self.ws}/" + path[4:]
        headers = {"Authorization": f"Bearer {self.token}", **kwargs.pop("headers", {})}
        response = self.client.request(method, "/api/v1" + path, headers=headers, **kwargs)
        if expect is not None:
            assert response.status_code == expect, f"{method} {path} -> {response.status_code}: {response.text[:500]}"
        elif response.status_code >= 400:
            raise AssertionError(f"{method} {path} -> {response.status_code}: {response.text[:500]}")
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response.text

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> Any:
        return self.request("PUT", path, **kw)

    def patch(self, path: str, **kw: Any) -> Any:
        return self.request("PATCH", path, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self.request("DELETE", path, **kw)


def new_user(client: TestClient, *, workspace: bool = True, mode: str = "offline") -> Session:
    email = f"user-{uuid.uuid4().hex[:10]}@example.test"
    response = client.post("/api/v1/auth/signup", json={"email": email, "password": "correct-horse-battery", "name": "Tester"})
    assert response.status_code == 201, response.text
    session = Session(client, response.json()["token"], email=email)
    if workspace:
        session.ws = session.post("/workspaces", json={"name": "Test workspace", "mode": mode})["id"]
    return session


def add_member(owner: Session, role: str) -> Session:
    member = new_user(owner.client, workspace=False)
    owner.put("/ws/members", json={"email": member.email, "role": role})
    member.ws = owner.ws
    return member


def setup_fake_model(s: Session) -> str:
    provider = s.post("/ws/model-providers", json={"name": "fake", "kind": "fake"})
    assert s.post(f"/ws/model-providers/{provider['id']}/verify")["ok"] is True
    s.put("/ws/model-role-policies/default", json={"provider_id": provider["id"]})
    return provider["id"]


def setup_sample_agent(s: Session, agent: dict[str, Any], *, access_level: str = "candidate_write",
                       allow_promote: bool = True) -> str:
    created = s.post("/ws/agents", json={
        "name": "sample", "adapter": "local_config", "config": {"base_url": agent["url"], "cost_per_call": 0.001},
        "credential": agent["token"], "access_level": access_level, "allow_promote": allow_promote})
    s.post(f"/ws/agents/{created['id']}/verify")
    return created["id"]


def upload_kb(s: Session) -> list[str]:
    ids = []
    for path in sorted(KB_DIR.glob("*.md")):
        doc = s.post("/ws/documents", files={"file": (path.name, path.read_bytes(), "text/markdown")}, data={"kind": "kb"})
        assert doc["status"] == "parsed"
        ids.append(doc["id"])
    return ids


def generate_dataset(s: Session, agent_id: str, *, n_cases: int = 24, seed: int = 3, **extra: Any) -> str:
    out = s.post("/ws/datasets:generate", json={"name": "tests", "agent_id": agent_id, "n_cases": n_cases, "seed": seed, **extra})
    assert out["job"]["status"] == "completed", out["job"]
    return out["dataset"]["id"]


def run_eval(s: Session, agent_id: str, dataset_id: str, mode: str = "single", **extra: Any) -> dict[str, Any]:
    out = s.post("/ws/evaluation-runs", json={"agent_id": agent_id, "dataset_id": dataset_id, "mode": mode, **extra})
    return s.get(f"/ws/evaluation-runs/{out['run']['id']}")


@pytest.fixture(scope="module")
def ready(client: TestClient, sample_agent: dict[str, Any]) -> dict[str, Any]:
    """A workspace with a verified fake model, the sample agent, the knowledge base and a generated dataset."""
    s = new_user(client)
    provider_id = setup_fake_model(s)
    agent_id = setup_sample_agent(s, sample_agent)
    upload_kb(s)
    dataset_id = generate_dataset(s, agent_id)
    return {"s": s, "provider_id": provider_id, "agent_id": agent_id, "dataset_id": dataset_id}


# --- a local OpenAI-compatible mock model server ------------------------------------------


class MockModel:
    """Scriptable OpenAI-compatible endpoint running on loopback."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.script: list[Any] = []  # queued overrides: int status code, or str raw content
        self.reject_response_format = False
        app = FastAPI()

        @app.post("/v1/chat/completions")
        async def completions(request: Request):
            from fastapi.responses import JSONResponse

            body = await request.json()
            self.requests.append(body)
            self.headers.append(dict(request.headers))
            if self.reject_response_format and "response_format" in body:
                return JSONResponse(status_code=400, content={"error": "response_format unsupported"})
            if self.script:
                step = self.script.pop(0)
                if isinstance(step, int):
                    return JSONResponse(status_code=step, content={"error": "scripted failure"})
                content = step
            else:
                content = json.dumps(self.answer(body))
            return {"model": body.get("model"), "choices": [{"message": {"role": "assistant", "content": content}}],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 30}}

        self.url, self.server = serve(app)

    @staticmethod
    def answer(body: dict[str, Any]) -> dict[str, Any]:
        text = body["messages"][1]["content"]
        match = re.search(r"<input>\n(.*)\n</input>", text, re.S)
        inputs = json.loads(match.group(1)) if match else {}
        if "nonce" in inputs:
            return {"ok": True, "echo": inputs["nonce"]}
        if "judgments" in inputs:
            return {"scores": {d: 0.5 for d in inputs["dimensions"]}, "verdict": "uncertain", "confidence": 0.4,
                    "rationale": "mock arbiter", "needs_human_review": True}
        if "dimensions" in inputs:
            return {"scores": {d: 0.9 for d in inputs["dimensions"]}, "verdict": "pass", "confidence": 0.8,
                    "rationale": "mock judge"}
        if "failures" in inputs:
            return {"findings": [{"case_ref": f["case_ref"], "category": "retrieval_grounding",
                                  "hypothesis": "mock hypothesis", "confidence": 0.5} for f in inputs["failures"]]}
        return {}


@pytest.fixture(scope="session")
def mock_model() -> MockModel:
    model = MockModel()
    yield model
    model.server.should_exit = True


def make_pdf(pages: list[str]) -> bytes:
    """Build a minimal valid PDF with one text line per page (no external dependency)."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pages_id = len(objects) + 2 * len(pages) + 1
    page_ids = []
    for text in pages:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode()
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_ids.append(add(
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] /Contents {content} 0 R "
            f"/Resources << /Font << /F1 {font} 0 R >> >> >>".encode()))
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    assert add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()) == pages_id
    catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())
    out, offsets = b"%PDF-1.4\n", []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


DATA_DIR = Path(_TMP)
