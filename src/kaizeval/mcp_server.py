"""Kaizeval as MCP tools, so a coding assistant can test and improve an agent from inside the editor.

Start it with `kaizeval mcp` (stdio transport). Needs the optional dependency:
`pip install "kaizeval[mcp]"`.

By default every call starts a local engine next to the project file. Set KAIZEVAL_URL (with
KAIZEVAL_TOKEN, or KAIZEVAL_EMAIL and KAIZEVAL_PASSWORD) to use a shared Kaizeval server instead.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .project import CONFIG_NAME, STATE_DIR, ProjectError, release, run_project, write_template

INSTRUCTIONS = """\
Kaizeval tests an AI agent against its own documents and requirements and reports where it fails.

Typical loop:
1. `init_project` once, then edit kaizeval.yaml (agent.url, model, knowledge, requirements).
2. `evaluate_agent`. Read `failures` and `recommendations`.
3. Apply the recommendations to the agent's code or configuration, restart the agent, and call
   `evaluate_agent` again. `comparison_with_baseline` tells you whether the change really helped
   ("improved"), hurt ("regressed") or made no demonstrable difference ("inconclusive").
4. Repeat until `target_met` is true, then call `evaluate_agent` once with final_holdout_check=true.
5. Stop when the loop stops: `stalled` is true (three runs in a row with the same score), or
   `decision_options` is not empty (the automatic improvement loop ended after three cycles
   without progress). Show the user the report and the remaining recommendations, and ask what
   they want: continue, or release as it is. Do not decide for them.
   - continue: apply the recommendations they agree with, then call `evaluate_agent` with
     continue_improvement=true. The loop goes on from the improved candidate, not from scratch.
   - release: call `release_candidate` with decision "approve" (or "reject").
6. Never release on the user's behalf: ask the user, then call `release_candidate` with their decision.

Results flagged `synthetic: true` come from the built-in fake model and are not real evaluations.
"""


def _log(message: str) -> None:
    # stdout carries the protocol; progress must go to stderr
    print(message, file=sys.stderr)


def _url() -> str | None:
    return os.environ.get("KAIZEVAL_URL") or None


def _server_class() -> Any:
    import importlib

    try:
        return importlib.import_module("mcp.server.mcpserver").MCPServer  # MCP SDK 2.x
    except ImportError:
        return importlib.import_module("mcp.server.fastmcp").FastMCP  # MCP SDK 1.x


def build_server() -> Any:
    server = _server_class()("kaizeval", instructions=INSTRUCTIONS)

    @server.tool()
    def init_project(directory: str = ".", overwrite: bool = False) -> dict[str, Any]:
        """Create a kaizeval.yaml project file in a folder. Edit it before the first evaluation."""
        try:
            path = write_template(Path(directory), force=overwrite)
        except ProjectError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": str(path),
                "next": "Set agent.url to the agent's HTTP endpoint and model to your model, then call evaluate_agent."}

    @server.tool()
    def evaluate_agent(config_path: str = CONFIG_NAME, final_holdout_check: bool = False,
                       regenerate_tests: bool = False, reset_baseline: bool = False,
                       continue_improvement: bool = False) -> dict[str, Any]:
        """Run the journey in kaizeval.yaml: connect, generate or reuse tests, let the testers call the
        agent, let the reviewers score the answers, and return scores, failed cases, recommendations and
        a paired comparison with the baseline run. Safe to call repeatedly.

        Set continue_improvement=true only after the USER chose to keep improving when the loop had
        stopped: the improvement loop then goes on from its candidate instead of starting over."""
        try:
            return {"ok": True, **run_project(Path(config_path), url=_url(), holdout=final_holdout_check,
                                              regenerate=regenerate_tests, reset_baseline=reset_baseline,
                                              continue_=continue_improvement, log=_log)}
        except ProjectError as exc:
            return {"ok": False, "error": str(exc)}

    @server.tool()
    def last_result(config_path: str = CONFIG_NAME) -> dict[str, Any]:
        """Return the result of the most recent evaluate_agent call for this project, without running anything."""
        path = Path(config_path).resolve().parent / STATE_DIR / "last-result.json"
        if not path.exists():
            return {"ok": False, "error": "no result yet; call evaluate_agent first"}
        return {"ok": True, **json.loads(path.read_text(encoding="utf-8"))}

    @server.tool()
    def release_candidate(decision: str, config_path: str = CONFIG_NAME, note: str = "") -> dict[str, Any]:
        """Record the USER's release decision ("approve" or "reject") for the candidate produced by the last
        optimization. Approving promotes it through the agent's connector. Only call this after the
        user has explicitly decided."""
        try:
            return {"ok": True, **release(Path(config_path), decision, url=_url(), note=note or None, log=_log)}
        except ProjectError as exc:
            return {"ok": False, "error": str(exc)}

    return server


def main() -> None:
    build_server().run("stdio")
