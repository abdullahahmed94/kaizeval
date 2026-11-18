"""Minimal HTTP client for the Kaizeval REST API (used by the CLI and by project mode)."""

from __future__ import annotations

import time
from typing import Any

import httpx


class ApiError(RuntimeError):
    """An error returned by the API, with its stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Api:
    def __init__(self, base_url: str, token: str | None = None):
        self.base = base_url.rstrip("/") + "/api/v1"
        self.client = httpx.Client(timeout=300, trust_env=False)
        self.token = token

    def call(self, method: str, path: str, *, raw: bool = False, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            response = self.client.request(method, self.base + path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError("UNREACHABLE", f"cannot reach {self.base} ({type(exc).__name__})") from exc
        if response.status_code >= 400:
            try:
                error = response.json().get("error", {})
            except ValueError:
                error = {"code": "HTTP_ERROR", "message": response.text[:200]}
            raise ApiError(str(error.get("code") or "HTTP_ERROR"), str(error.get("message") or response.status_code))
        if raw:
            return response.text
        return response.json() if response.content else None

    def wait_job(self, ws: str, job: dict[str, Any], timeout: float = 3600) -> dict[str, Any]:
        deadline = time.time() + timeout
        while job["status"] in ("queued", "running"):
            if time.time() > deadline:
                raise ApiError("TIMEOUT", "timed out waiting for the job to finish")
            time.sleep(0.4)
            job = self.call("GET", f"/workspaces/{ws}/jobs/{job['id']}")
        if job["status"] == "failed":
            raise ApiError("JOB_FAILED", job.get("error") or "job failed")
        return job
