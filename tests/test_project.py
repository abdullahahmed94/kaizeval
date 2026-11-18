"""Project mode: `kaizeval init` / `kaizeval run` / `kaizeval release`, and the MCP tools."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml

from conftest import ADMIN_TOKEN, new_user
from kaizeval import cli
from kaizeval.project import ProjectError, load_config, release, run_project
from kaizeval.sample_agent import KB_DIR

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _write(tmp_path: Path, config: dict) -> Path:
    path = tmp_path / "kaizeval.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def _quiet(_: str) -> None:
    pass


@pytest.fixture()
def remote(client, live_server, monkeypatch):
    monkeypatch.setenv("KAIZEVAL_TOKEN", new_user(client, workspace=False).token)
    return live_server


def test_init_creates_a_valid_project_file(tmp_path, capsys):
    assert cli.main(["init", str(tmp_path)]) == cli.EXIT_OK
    assert "kaizeval run" in capsys.readouterr().out
    config = load_config(tmp_path / "kaizeval.yaml")
    assert config["agent"]["url"] and config["tests"]["testers"] == 3 and config["evaluation"]["reviewers"] == 2
    assert config["evaluation"]["target"] == 1.0 and config["network"] == "offline"
    assert cli.main(["init", str(tmp_path)]) == cli.EXIT_ERROR
    assert cli.main(["init", str(tmp_path), "--force"]) == cli.EXIT_OK


@pytest.mark.parametrize("config,message", [
    ({"agent": {}}, "agent.url is required"),
    ({"agent": {"url": "http://x"}, "typo": 1}, "unknown setting"),
    ({"agent": {"url": "http://x"}, "tests": {"testerz": 2}}, "unknown setting(s) under 'tests'"),
    ({"agent": {"url": "http://x"}, "evaluation": {"reviewers": 9}}, "between 0 and 7"),
    ({"agent": {"url": "http://x"}, "tests": {"testers": 0}}, "between 1 and 8"),
    ({"agent": {"url": "http://x"}, "network": "cloud"}, "network must be"),
])
def test_project_file_is_validated(tmp_path, config, message):
    with pytest.raises(ProjectError) as err:
        load_config(_write(tmp_path, config))
    assert message in str(err.value)


def test_model_block_sets_the_role_limits_and_rejects_typos(tmp_path, remote, sample_agent, client):
    """`model.max_tokens` reaches the role policy (reasoning models need room), and a typo is an error, not silence."""
    shutil.copy(EXAMPLES / "approved-qa.csv", tmp_path / "approved-qa.csv")
    base = {"project": "limits", "agent": {"url": sample_agent["url"] + "/chat"}, "tests": {"import": ["approved-qa.csv"]},
            "evaluation": {"reviewers": 0}}
    with pytest.raises(ProjectError) as err:
        run_project(_write(tmp_path, {**base, "model": {"kind": "fake", "max_token": 9000}}), url=remote, log=_quiet)
    assert "unknown setting(s) under 'model': max_token" in str(err.value)
    result = run_project(_write(tmp_path, {**base, "model": {"kind": "fake", "max_tokens": 9000, "timeout_s": 90}}),
                         url=remote, log=_quiet)
    token = os.environ["KAIZEVAL_TOKEN"]
    policies = client.get(f"/api/v1/workspaces/{result['workspace_id']}/model-role-policies",
                          headers={"Authorization": f"Bearer {token}"}).json()
    assert policies["default"]["max_tokens"] == 9000 and policies["default"]["timeout_s"] == 90
    assert cli.main(["run", "-c", str(tmp_path / "missing.yaml")]) == cli.EXIT_ERROR


def test_black_box_agent_without_any_model(tmp_path, remote, sample_agent, capsys):
    """Rules only: imported questions, no model, a black-box agent. Re-running gives an A/B against the first run."""
    shutil.copy(EXAMPLES / "approved-qa.csv", tmp_path / "approved-qa.csv")
    path = _write(tmp_path, {"project": "blackbox", "agent": {"url": sample_agent["url"] + "/chat"},
                             "tests": {"import": ["approved-qa.csv"], "testers": 2},
                             "evaluation": {"reviewers": 2, "require_citations": False}})
    first = run_project(path, url=remote, log=_quiet)
    assert first["mode"] == "deterministic" and first["reviewers"] == 0 and first["synthetic"] is False
    assert any("No model is configured" in note for note in first["notes"])
    # 6 imported questions: part of them is sealed away as the holdout, like generated tests
    assert first["cases"] + first["holdout_cases"] == 6 and first["holdout_cases"] >= 1
    assert first["testers"] == 2 and 0 < first["overall"] < 1 and first["target_met"] is False
    assert first["failures"] and first["failures"][0]["why"]
    assert first["recommendations"]
    assert all(r["why_not_automatic"].startswith("UNSUPPORTED_CAPABILITY") for r in first["recommendations"])
    assert first["comparison_with_baseline"] is None and "Apply the recommendations" in first["next_steps"][0]
    assert Path(first["report"]).read_text(encoding="utf-8").startswith("# Kaizeval evaluation report")

    assert cli.main(["run", "-c", str(path), "--url", remote, "--json", "--no-input"]) == cli.EXIT_QUALITY_GATE
    second = json.loads(capsys.readouterr().out)
    assert second["dataset_id"] == first["dataset_id"] and second["agent_id"] == first["agent_id"]  # reused
    comparison = second["comparison_with_baseline"]
    assert comparison["baseline_run_id"] == first["run_id"] and comparison["n"] == first["cases"]
    assert comparison["delta_percentage_points"] == 0 and comparison["verdict"] == "inconclusive"
    # one unchanged run is not enough to stop
    assert first["stalled"] is False and second["stalled"] is False and second["unchanged_runs"] == 1
    assert second["decision_options"] == [] and "Apply the recommendations" in second["next_steps"][0]
    state = json.loads((tmp_path / ".kaizeval" / "project-state.json").read_text(encoding="utf-8"))
    assert state["baseline_run_id"] == first["run_id"] and state["last_run_id"] == second["run_id"]

    assert cli.main(["run", "-c", str(path), "--url", remote, "--no-input"]) == cli.EXIT_QUALITY_GATE
    text = capsys.readouterr().out
    assert "vs baseline run" in text and "target not met" in text and "recommendations:" in text

    # three runs in a row with an unchanged score: the loop says so and stops instead of going on blindly
    fourth = run_project(path, url=remote, log=_quiet)
    assert fourth["stalled"] is True and fourth["unchanged_runs"] == 3 and fourth["decision_options"] == ["continue"]
    assert any("did not change in 3 runs in a row" in step and "kaizeval run --continue" in step for step in fourth["next_steps"])
    # the user may still choose to go on; the count starts again
    assert cli.main(["run", "-c", str(path), "--url", remote, "--json", "--no-input", "--continue"]) == cli.EXIT_QUALITY_GATE
    fifth = json.loads(capsys.readouterr().out)
    assert fifth["stalled"] is False and fifth["unchanged_runs"] == 1


def test_full_loop_with_testers_reviewers_and_optimization(tmp_path, remote, sample_agent, monkeypatch):
    """The whole journey from one file: model, 3 testers, 3 reviewers, requirements, knowledge, loop to the target."""
    (tmp_path / "docs").mkdir()
    for source in KB_DIR.glob("*.md"):
        shutil.copy(source, tmp_path / "docs" / source.name)
    (tmp_path / "system.md").write_text("You are a support assistant. Cite the source document.", encoding="utf-8")
    monkeypatch.setenv("TEST_AGENT_ADMIN_TOKEN", ADMIN_TOKEN)
    path = _write(tmp_path, {
        "project": "support-agent", "model": {"kind": "fake"},
        "agent": {"url": sample_agent["url"], "candidate_protocol": True, "admin_token_env": "TEST_AGENT_ADMIN_TOKEN"},
        "requirements": "Always cite the source document.\nDecline when the documents do not contain the answer.",
        "instructions_file": "system.md", "knowledge": ["docs/*.md"],
        "tests": {"cases": 24, "testers": 3, "holdout_fraction": 0.3},
        "evaluation": {"reviewers": 3, "target": 1.0},
        "optimize": {"enabled": True, "max_rounds": 2},
    })
    result = run_project(path, url=remote, log=_quiet)
    assert result["synthetic"] is True and result["mode"] == "panel"
    assert result["reviewers"] == 3 and result["testers"] == 3 and result["holdout_cases"] >= 3
    optimization = result["optimization"]
    assert optimization["status"] == "completed" and optimization["rounds"] >= 1
    assert optimization["best_overall"] > optimization["baseline_overall"]
    assert optimization["regression_ab"]["verdict"] == "improved"
    assert optimization["holdout"]["status"] == "completed" and "paired_cases" in optimization["holdout"]
    assert any(c["state"] == "kept" for c in optimization["changes"])
    assert optimization["candidate"]["status"] == "awaiting_decision" and optimization["candidate"]["diff_from_baseline"]
    assert result["final_overall"] == optimization["best_overall"] and result["target"] == 1.0
    assert result["target_met"] is (result["final_overall"] >= 1.0)  # 100% is a target, never a promise
    # the loop has stopped: the user reviews the report, then continues or releases
    assert optimization["decision_options"] == result["decision_options"] == ["continue", "release", "reject"]
    step = next(step for step in result["next_steps"] if "waiting for your decision" in step)
    assert "kaizeval run --continue" in step and "kaizeval release approve" in step and optimization["stop_reason"] in step
    report_text = Path(result["report"]).read_text(encoding="utf-8")
    assert "Independent holdout validation" in report_text and "## Your decision" in report_text

    # "continue": the same candidate goes on improving; it does not start again from the baseline
    more = run_project(path, url=remote, continue_=True, log=_quiet)
    continued = more["optimization"]
    assert continued["continuations"] == 1 and continued["candidate"]["id"] == optimization["candidate"]["id"]
    assert continued["rounds"] > optimization["rounds"] and continued["best_overall"] > continued["baseline_overall"]
    # the fresh holdout retired the old one into the regression set: more cases, so the scores are not comparable
    assert continued["scores_comparable_with_previous_run"] is False
    assert any("the regression set grew" in note and "not comparable" in note for note in more["notes"])
    # a different score on a larger set is not "the agent's answers vary"
    assert not any("vary between identical runs" in note for note in more["notes"])
    assert [c["patch"] for c in continued["changes"]][:len(optimization["changes"])] == [c["patch"] for c in optimization["changes"]]
    assert continued["holdout"]["status"] == "completed"  # validated again, on a fresh holdout
    assert any("fresh one was generated" in note for note in more["notes"])
    assert continued["candidate"]["status"] == "awaiting_decision"
    assert "Continued after round" in Path(more["report"]).read_text(encoding="utf-8")

    # nothing was promoted by the run; the release decision is a separate, explicit step
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    pointer = sample_agent["url"] + "/admin/production"
    assert httpx.get(pointer, headers=admin, trust_env=False).json()["production"] == "baseline"
    outcome = release(path, "approve", url=remote, note="reviewed", log=_quiet)
    assert outcome["decision"] == "approved" and outcome["candidate_status"] == "promoted"
    assert httpx.get(pointer, headers=admin, trust_env=False).json()["production"].startswith("cand-")
    with pytest.raises(ProjectError):
        release(path, "approve", url=remote, log=_quiet)  # nothing is waiting any more
    # after a release there is nothing to continue, so a new loop is started and says so
    fresh = run_project(path, url=remote, continue_=True, log=_quiet)
    assert any("no stopped improvement loop to continue" in note for note in fresh["notes"])
    assert fresh["optimization"] is None or fresh["optimization"]["continuations"] == 0
    # put the shared sample agent back so other tests see an untouched production pointer
    httpx.post(sample_agent["url"] + "/admin/promote", headers=admin, trust_env=False,
               json={"candidate_id": "baseline", "approval_id": "test-cleanup"}).raise_for_status()


def test_optimization_is_refused_for_agents_that_cannot_be_patched(tmp_path, remote, sample_agent):
    (tmp_path / "docs").mkdir()
    shutil.copy(next(KB_DIR.glob("*.md")), tmp_path / "docs" / "kb.md")
    path = _write(tmp_path, {"model": {"kind": "fake"},
                             "agent": {"url": sample_agent["url"] + "/v1/chat/completions", "style": "openai"},
                             "knowledge": ["docs/*.md"], "tests": {"cases": 8, "testers": 2},
                             "evaluation": {"reviewers": 1}, "optimize": {"enabled": True}})
    result = run_project(path, url=remote, log=_quiet)
    assert result["optimization"] is None and result["mode"] == "single"
    assert any("cannot be patched" in note for note in result["notes"])
    with pytest.raises(ProjectError):
        release(path, "approve", url=remote, log=_quiet)


def test_embedded_engine_runs_in_a_fresh_process(tmp_path, sample_agent):
    """`kaizeval run` with no server at all: it starts its own local engine and keeps its state between runs."""
    shutil.copy(EXAMPLES / "approved-qa.csv", tmp_path / "approved-qa.csv")
    path = _write(tmp_path, {"agent": {"url": sample_agent["url"] + "/chat"}, "tests": {"import": ["approved-qa.csv"]},
                             "evaluation": {"reviewers": 0}})
    env = {k: v for k, v in os.environ.items() if not k.startswith("KAIZEVAL_")}
    command = [sys.executable, "-m", "kaizeval.cli", "run", "-c", str(path), "--json", "--no-input"]
    outputs = []
    stderr = ""
    for _ in range(2):
        done = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=240, check=False)
        assert done.returncode == cli.EXIT_QUALITY_GATE, done.stderr[-800:]
        outputs.append(json.loads(done.stdout))
        stderr = done.stderr
    assert outputs[0]["cases"] + outputs[0]["holdout_cases"] == 6 and outputs[0]["comparison_with_baseline"] is None
    assert outputs[1]["comparison_with_baseline"]["baseline_run_id"] == outputs[0]["run_id"]
    assert (tmp_path / ".kaizeval" / "kaizeval.db").exists() and (tmp_path / "kaizeval-report.md").exists()
    assert "[6/7] evaluated" in stderr  # progress goes to stderr so stdout stays pure JSON


def test_mcp_tools(tmp_path, remote, sample_agent, monkeypatch):
    from mcp import Client

    from kaizeval.mcp_server import build_server

    monkeypatch.setenv("KAIZEVAL_URL", remote)
    shutil.copy(EXAMPLES / "approved-qa.csv", tmp_path / "approved-qa.csv")
    path = _write(tmp_path, {"agent": {"url": sample_agent["url"] + "/chat"}, "tests": {"import": ["approved-qa.csv"]}})

    def payload(result):
        structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
        return structured if structured else json.loads(result.content[0].text)

    async def session():
        async with Client(build_server()) as mcp_client:
            tools = {tool.name: tool for tool in (await mcp_client.list_tools()).tools}
            empty = payload(await mcp_client.call_tool("last_result", {"config_path": str(path)}))
            ran = payload(await mcp_client.call_tool("evaluate_agent", {"config_path": str(path)}))
            last = payload(await mcp_client.call_tool("last_result", {"config_path": str(path)}))
            refused = payload(await mcp_client.call_tool("release_candidate", {"decision": "approve", "config_path": str(path)}))
            created = payload(await mcp_client.call_tool("init_project", {"directory": str(tmp_path / "new")}))
            return tools, empty, ran, last, refused, created

    tools, empty, ran, last, refused, created = asyncio.run(session())
    assert set(tools) == {"init_project", "evaluate_agent", "last_result", "release_candidate"}
    assert "USER" in tools["release_candidate"].description
    schema = getattr(tools["evaluate_agent"], "input_schema", None) or getattr(tools["evaluate_agent"], "inputSchema", {})
    assert "continue_improvement" in schema["properties"] and "USER" in tools["evaluate_agent"].description
    assert empty["ok"] is False
    assert ran["ok"] is True and ran["cases"] + ran["holdout_cases"] == 6 and ran["target_met"] is False
    assert last["run_id"] == ran["run_id"]
    assert refused["ok"] is False and "no candidate" in refused["error"]
    assert created["ok"] is True and (tmp_path / "new" / "kaizeval.yaml").exists()


@pytest.mark.parametrize("answers,expected_runs,expected_release", [
    (["c", "r"], [False, True], ["approve"]),      # continue once, then release what came out
    (["x"], [False], ["reject"]),
    ([""], [False], []),                           # Enter: decide later, nothing is recorded
    (["c", ""], [False, True], []),
])
def test_the_terminal_asks_continue_or_release(monkeypatch, capsys, answers, expected_runs, expected_release):
    """When the loop has stopped, `kaizeval run` asks the person what to do and never decides by itself."""
    import kaizeval.project as project

    runs, releases, pending = [], [], list(answers)
    result = {"project": "p", "synthetic": False, "overall": 0.8, "target": 1.0, "pass_rate": 0.8, "critical_failures": 0,
              "cases": 10, "testers": 1, "reviewers": 1, "by_category": {}, "failures": [], "recommendations": [], "notes": [],
              "next_steps": [], "report": "kaizeval-report.md", "target_met": False,
              "decision_options": ["continue", "release", "reject"]}

    def fake_run(path, **kwargs):
        runs.append(kwargs["continue_"])
        assert not (kwargs["continue_"] and kwargs["regenerate"])  # a continuation never regenerates the tests
        return result

    def fake_release(path, decision, **kwargs):
        releases.append(decision)
        return {"decision": decision, "candidate_status": "promoted" if decision == "approve" else "rejected"}

    monkeypatch.setattr(project, "run_project", fake_run)
    monkeypatch.setattr(project, "release", fake_release)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": pending.pop(0))
    assert cli.main(["run", "--regenerate"]) == cli.EXIT_QUALITY_GATE
    assert runs == expected_runs and releases == expected_release and pending == []
    out = capsys.readouterr().out
    assert ("decision recorded: " in out) is bool(expected_release)
    if not expected_release:
        assert "kaizeval run --continue" in out and "kaizeval release approve" in out
    # with --no-input, or when stdin is not a terminal, nothing is asked
    runs.clear()
    assert cli.main(["run", "--no-input"]) == cli.EXIT_QUALITY_GATE and runs == [False]
