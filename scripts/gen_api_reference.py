"""Regenerate the route table in docs/API.md from the application itself.

Usage: python scripts/gen_api_reference.py
"""

from __future__ import annotations

import inspect
import os
import re
import tempfile
from pathlib import Path

os.environ.setdefault("KAIZEVAL_DATA_DIR", tempfile.mkdtemp(prefix="kaizeval-docs-"))

from fastapi.routing import APIRoute  # noqa: E402

from kaizeval.api import auth, knowledge, runs, setup  # noqa: E402

START, END = "<!-- routes:start -->", "<!-- routes:end -->"
GROUPS = [
    ("Authentication and workspaces", ("/auth", "/system", "/workspaces", "/workspaces/{ws}", "/members")),
    ("Model providers and usage", ("/model-providers", "/model-role-policies", "/model-usage", "/runs/{run_id}/model-usage")),
    ("Agents", ("/agent-adapters", "/agents")),
    ("Knowledge and specifications", ("/documents", "/evidence", "/specifications")),
    ("Datasets", ("/datasets",)),
    ("Evaluation and recommendations", ("/evaluation-runs", "/compare", "/recommendations", "/runs/{run_id}/cancel")),
    ("Optimization, candidates and release", ("/optimization-runs", "/candidate-patches", "/candidates", "/experiments")),
    ("Reports, audit and jobs", ("/reports", "/audit", "/jobs")),
]


def describe(route: APIRoute) -> tuple[str, str]:
    source = inspect.getsource(route.endpoint)
    roles = re.findall(r'workspace_ctx\("(\w+)"\)', source) + re.findall(r'ctx\.require\("(\w+)"\)', source)
    if "workspace_ctx(" in source and not re.search(r'workspace_ctx\("', source):
        roles.insert(0, "viewer")
    if not roles:
        role = "signed in" if "current_user" in source else "public"
    else:
        role = roles[0] + (f" ({', '.join(sorted(set(roles[1:])))} for some options)" if len(set(roles)) > 1 else "")
    doc = (inspect.getdoc(route.endpoint) or route.name.replace("_", " ").capitalize()).split("\n")[0]
    return role, doc.rstrip(".")


def main() -> None:
    routes = [r for module in (auth, setup, knowledge, runs) for r in module.router.routes if isinstance(r, APIRoute)]
    rows: dict[str, list[str]] = {title: [] for title, _ in GROUPS}
    for route in routes:
        short = route.path.replace("{workspace_id}", "{ws}")
        local = short.removeprefix("/workspaces/{ws}") or "/workspaces/{ws}"
        title = next((t for t, prefixes in GROUPS if any(local.startswith(p) or short == p for p in prefixes)), GROUPS[-1][0])
        role, doc = describe(route)
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            rows[title].append(f"| `{method}` | `{short}` | {role} | {doc} |")
    lines = [START, ""]
    for title, _ in GROUPS:
        if rows[title]:
            lines += [f"### {title}", "", "| Method | Path (under `/api/v1`) | Minimum role | Purpose |", "|---|---|---|---|", *rows[title], ""]
    lines.append(END)
    path = Path(__file__).resolve().parents[1] / "docs" / "API.md"
    text = path.read_text(encoding="utf-8")
    before, rest = text.split(START, 1)
    path.write_text(before + "\n".join(lines) + rest.split(END, 1)[1], encoding="utf-8")
    print(f"wrote {sum(len(v) for v in rows.values())} routes to {path}")


if __name__ == "__main__":
    main()
