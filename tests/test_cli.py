"""The demo journey and the CI quality gate, driven over real HTTP (acceptance 12)."""

from __future__ import annotations

import uuid

import pytest

from conftest import ADMIN_TOKEN, serve
from kaizeval import cli
from kaizeval.app import create_app


@pytest.fixture(scope="module")
def live_api():
    app = create_app()
    from kaizeval import handlers  # noqa: F401
    from kaizeval.db import upgrade_db

    upgrade_db()
    url, server = serve(app)
    yield url
    server.should_exit = True


@pytest.fixture(scope="module")
def demo(live_api, sample_agent):
    api = cli.Api(live_api)
    lines: list[str] = []
    result = cli.run_demo_flow(api, agent_url=sample_agent["url"], admin_token=ADMIN_TOKEN,
                               email=f"demo-{uuid.uuid4().hex[:8]}@example.test", password="demo-password-123", log=lines.append)
    return {"api": api, "lines": lines, **result}


def test_demo_runs_the_whole_journey_without_any_api_key(demo):
    assert len(demo["lines"]) == 9 and "SYNTHETIC" in demo["lines"][1]
    # it ends by handing the decision to the person: continue, or release
    assert "Your decision" in demo["lines"][8] and "Continue improving" in demo["lines"][8]
    report = demo["report"]
    assert report["status"] == "completed" and report["synthetic"] is True and report["banner"]
    assert report["regression_comparison"]["overall"]["verdict"] == "improved"
    assert report["holdout"]["status"] == "completed" and report["holdout"]["overall"]["delta_percentage_points"] > 0
    assert report["candidate"]["status"] == "awaiting_decision"  # the demo never promotes
    assert report["next_decision"]["options"] == ["continue", "release", "reject"]


def test_ci_gate_exit_codes(demo, live_api, monkeypatch, capsys):
    monkeypatch.setenv("KAIZEVAL_TOKEN", demo["api"].token)
    common = ["ci", "--url", live_api, "--workspace", demo["workspace_id"], "--agent", demo["agent_id"],
              "--dataset", demo["dataset_id"]]
    assert cli.main([*common, "--fail-under", "0.1", "--max-critical", "99"]) == cli.EXIT_OK
    assert "quality gate passed" in capsys.readouterr().out
    assert cli.main([*common, "--fail-under", "0.99"]) == cli.EXIT_QUALITY_GATE
    assert "GATE FAILED" in capsys.readouterr().out
    # a baseline version compared with itself is never a regression
    compare = [*common, "--fail-under", "0.1", "--max-critical", "99", "--baseline-run", demo["baseline_run_id"], "--json"]
    assert cli.main([*compare, "--mode", "dual"]) == cli.EXIT_OK
    assert '"gate": "passed"' in capsys.readouterr().out
    # runs scored with different evaluation modes are not comparable, and the gate says so instead of guessing
    assert cli.main(compare) == cli.EXIT_ERROR
    assert "NOT_COMPARABLE" in capsys.readouterr().err
    monkeypatch.setenv("KAIZEVAL_TOKEN", "invalid")
    assert cli.main([*common]) == cli.EXIT_ERROR
    assert cli.main(["ci", "--url", live_api, "--workspace", "nope", "--agent", "a", "--dataset", "d"]) == cli.EXIT_ERROR


def test_console_route_and_api_404(live_api):
    import httpx

    with httpx.Client(trust_env=False) as client:
        assert client.get(live_api + "/").status_code == 200
        missing = client.get(live_api + "/api/v1/does-not-exist")
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "NOT_FOUND"
        assert client.get(live_api + "/api/openapi.json").json()["info"]["title"] == "Kaizeval API"
        # no bundled "try it" page: such pages pull scripts from a public CDN
        assert client.get(live_api + "/api/docs").status_code == 404
        headers = client.get(live_api + "/api/v1/system").headers
        assert headers["x-content-type-options"] == "nosniff" and headers["x-request-id"]
