# Kaizeval

**Evaluate. Diagnose. Optimize. Repeat.**

Kaizeval tests your AI agent against its own knowledge base, tells you where and why it fails,
and, when the agent allows it, improves an isolated copy and proves the gain on questions the
optimizer never saw. It runs entirely on your own machines, and works with no internet access.

[![CI](https://github.com/abdullahahmed94/kaizeval/actions/workflows/ci.yml/badge.svg)](https://github.com/abdullahahmed94/kaizeval/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/kaizeval.svg)](https://pypi.org/project/kaizeval/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)

## How it works

```mermaid
flowchart LR
    A["Connect agent<br/>and model"] --> B["Upload<br/>knowledge"]
    B --> C["Generate tests<br/>from evidence"]
    C --> D["Evaluate<br/>Baseline A"]
    D --> E["Diagnose and<br/>recommend"]
    E --> F["Patch isolated<br/>Candidate B"]
    F --> G["Paired A/B on<br/>every cycle"]
    G -->|"keep or roll back,<br/>until 3 cycles change nothing"| F
    G --> H["Sealed holdout<br/>validation"]
    H --> I{"You decide"}
    I -->|continue| F
    I --> J["Release<br/>or reject"]
```

Agents that cannot be patched safely stop after "Diagnose and recommend": you get the report and
the recommendations, and Kaizeval says why nothing more is offered. The full journey, the scoring
pipeline and one optimization round are drawn step by step in [docs/WORKFLOW.md](docs/WORKFLOW.md).

### Architecture

```mermaid
flowchart TB
    subgraph CLIENTS["Clients"]
        UI["Web console"]
        CLI["CLI: project file,<br/>CI gate"]
        MCP["MCP tools for<br/>coding assistants"]
        REST["Any REST client"]
    end

    subgraph KZ["Kaizeval, on your own machines"]
        API["API<br/>accounts, roles, workspace isolation"]
        JOBS[("Job queue")]
        WORK["Workers"]
        subgraph CORE["Core services"]
            direction LR
            SPEC["Specification"]
            DATA["Datasets"]
            EVAL["Evaluation"]
            DIAG["Diagnostics"]
            OPT["Optimization"]
            HOLD["Holdout<br/>validation"]
        end
        GW["LLM Gateway<br/>role routing, preflight, budgets, metering"]
        CONN["Agent connectors<br/>capability discovery"]
        NET{{"Network policy<br/>offline by default"}}
        DB[("SQLite or PostgreSQL")]
        FILES[("Files or S3-compatible")]
    end

    MODEL["Model endpoint<br/>local, or one you approved"]
    AGENT["Your agent<br/>Baseline A and Candidate B"]

    UI --> API
    CLI --> API
    MCP --> API
    REST --> API
    API --> JOBS --> WORK
    WORK --> EVAL
    SPEC --> GW
    DATA --> GW
    EVAL --> GW
    DIAG --> GW
    OPT --> GW
    EVAL --> CONN
    OPT --> CONN
    OPT -->|"aggregates only"| HOLD
    GW --> NET
    CONN --> NET
    NET --> MODEL
    NET --> AGENT
    API --> DB
    WORK --> DB
    API --> FILES
```

One Python package runs as a single process on a laptop, or as API and worker containers with
PostgreSQL. Details and design decisions: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Why Kaizeval

Most evaluation tools stop at a score. Kaizeval is built around what happens next:

- **A closed, controlled loop.** Findings become recommendations; recommendations become
  allowlisted changes to a *candidate copy*; every change re-runs the whole regression set and is
  kept only if it passes your gates. Your production agent is never touched and nothing is
  promoted without an explicit approval.
- **Results you can defend.** Deterministic checks and human-approved references outrank model
  judges. Every A/B comparison comes with a confidence interval and a plain verdict:
  *improved*, *regressed* or *inconclusive*. A final holdout set stays sealed from the optimizer.
- **Private by default.** Self-hosted, no telemetry, no license check. Offline mode is on by
  default and refuses any non-local destination before a DNS lookup is even made. Use a local
  model (Ollama, vLLM, anything OpenAI-compatible), or opt in to a remote provider per workspace.
- **Honest about what it cannot do.** A black-box agent gets recommendations only, with the
  reason shown. Unobservable things are reported as "not scored", not guessed.

Kaizeval is not a hosted service, a vector database or an observability platform, and it
works alongside the evaluation and tracing tools you already use.

## Try it in a minute

Requires Python 3.10+. No Docker, no API key, no account.

```bash
pip install kaizeval
kaizeval demo
```

`kaizeval demo` starts the console at <http://127.0.0.1:8000> and a small sample support agent,
then runs the complete journey against it and prints a login.

> **About the demo numbers.** The demo uses a built-in **fake model provider** so it can run
> without any model. Scores that come from it are text-overlap heuristics and are labelled
> **SYNTHETIC** everywhere. What is real in the demo: a separate agent process is called for every
> test, the deterministic checks measure its actual answers, the candidate is a real isolated
> copy, and the baseline stays byte-identical. Connect a real model to get real judgments.

## What it looks like

Screenshots of the bundled console running the demo. The purple banners are the SYNTHETIC label
that every fake-provider result carries.

| Journey overview | Optimization run | Paired A/B comparison |
|---|---|---|
| ![Overview page with the journey checklist](docs/images/console-overview.jpg) | ![Optimization run: result, the continue-or-release decision and the timeline](docs/images/console-optimization.jpg) | ![Paired A/B comparison with confidence interval and verdict](docs/images/console-ab-comparison.jpg) |

## Test your own agent in three commands

```bash
kaizeval init        # writes kaizeval.yaml
# edit kaizeval.yaml: your model, your agent's URL, your documents and requirements
kaizeval run
```

```yaml
model:
  base_url: http://localhost:11434/v1   # any OpenAI-compatible endpoint, local or hosted
  model: your-model-name
agent:
  url: http://127.0.0.1:8200/chat       # the agent you built
requirements: |
  Answer customer questions using only the company documents.
  Say you do not know when the documents do not contain the answer.
knowledge: [docs/**/*.md]
tests:
  cases: 30
  testers: 3          # independent tester agents: each writes and runs its own share of the tests
evaluation:
  reviewers: 2        # independent reviewer agents per answer (0 = rules only, up to 7)
  target: 1.0         # keep improving until the score reaches 100%
```

`kaizeval run` starts a local engine (no server, no account), lets the testers write and run the
tests with part of them sealed as a holdout, lets the reviewers score the answers, and prints the
score, the failed cases, the recommendations and a report. Run it again after changing your agent
and it compares the new run with the first one, case by case:

```text
[5/7] tests ready: 24 regression, 12 sealed holdout (3 tester(s))
[6/7] evaluated with 3 reviewer(s): overall 39.8%, pass rate 8.3%, critical failures 3
[7/7] optimization completed (the optimizer found no further safe change): best 85.9% after 5 round(s)
    A/B on regression: +45.86 pp -> improved
    sealed holdout:    +26.38 pp -> inconclusive
  next: The loop stopped (the optimizer found no further safe change). Candidate B is waiting
        for your decision; nothing was changed in production. Review the report and apply the
        recommendations you agree with, then choose: `kaizeval run --continue` to keep
        improving from Candidate B, `kaizeval release approve` to release it as it is, or
        `kaizeval release reject`.
  result: target not met
```

(That output is from `examples/kaizeval.yaml` with the built-in fake model, so it is SYNTHETIC.)
The loop keeps going until your target is reached, or until three cycles in a row leave the score
unchanged; then it exits with the recommendations for what still fails and a report. 100% is the
goal, not a promise.

**Then it is your call.** Read the report, apply the recommendations you agree with, and choose:

```bash
kaizeval run --continue     # keep improving from the improved version, not from scratch
kaizeval release approve    # release the improved version as it is
kaizeval release reject     # keep what you had
```

Nothing is released, and the loop is never restarted, without that decision.

A plain HTTP agent can be tested and scored but not changed from outside, so for such an agent you
(or your coding assistant) apply the recommendations and run again. To let Kaizeval close the loop
itself, add the candidate protocol to your agent with a few lines of
[`kaizeval.agentkit`](docs/PROJECT_MODE.md#making-your-own-agent-automatic).
Full reference: [docs/PROJECT_MODE.md](docs/PROJECT_MODE.md).

## Use it from a coding assistant

Any assistant that can run a terminal command can drive the loop: it runs
`kaizeval run --json --no-input`, reads `failures` and `recommendations`, edits your agent, and
runs again until `target_met` is true or the loop stops (`stalled`, or `decision_options` is not
empty). At that point it asks you whether to continue or to release. For assistants that speak the Model Context Protocol there
is a built-in server:

```bash
pip install "kaizeval[mcp]"
```

```json
{ "mcpServers": { "kaizeval": { "command": "kaizeval", "args": ["mcp"] } } }
```

It exposes `init_project`, `evaluate_agent`, `last_result` and `release_candidate`, and instructs
the assistant to ask you before any release.

## Use it through the API

Everything the console does is a REST call, so the whole journey can be scripted. A complete
walkthrough that needs only the Python standard library is in
[`examples/api_quickstart.py`](examples/api_quickstart.py):

```bash
kaizeval serve                         # terminal 1: API on http://127.0.0.1:8000
kaizeval sample-agent --port 8101      # terminal 2: something to evaluate (or use your own agent)
python examples/api_quickstart.py      # terminal 3
```

```text
2. Register the agent as a black box and verify the connection
  POST   /workspaces/{ws}/agents                      ->  201
  POST   /workspaces/{ws}/agents/{id}/verify          ->  200
4. Run a deterministic evaluation (no model needed) and wait for the job
  POST   /workspaces/{ws}/evaluation-runs             ->  202
  GET    /workspaces/{ws}/jobs/{id}                   ->  200
     overall 57.1%  pass rate 50.0%  critical failures 0  cases 6
5. Read per-case results, findings and recommendations
     FAIL  How long is the compressor covered under warranty?  ->  correctness=0.0 (0/1 key facts present (approved reference))
     - Add an explicit rule to decline when no supporting evidence is found [instruction_prompt, 2 case(s)]
       auto-apply: False  reason: UNSUPPORTED_CAPABILITY: a black-box HTTP agent cannot be patched; apply this change yourself
```

That run uses no model at all: the scores come from deterministic checks of the sample agent's
real answers against approved references. All routes, error codes and payloads are in
[docs/API.md](docs/API.md); the OpenAPI document is served at `/api/openapi.json`.

## Use it with your own agent and model

```bash
kaizeval serve          # console and API on http://127.0.0.1:8000
```

1. **Model.** Models → add an *OpenAI-compatible* provider, for example a local Ollama server at
   `http://localhost:11434/v1` with your model's name → **Verify** → set as default.
   (Deterministic evaluation works with no model at all.)
2. **Agent.** Agents → *Generic HTTP chat endpoint*. Point it at any endpoint that accepts a
   message and returns an answer, or at an OpenAI-style `/chat/completions` endpoint.
3. **Knowledge.** Upload the PDF, DOCX, Markdown or text files your agent answers from.
4. **Tests.** Generate evidence-linked cases, and/or import your own approved Q&A or real
   conversations as CSV or JSON Lines.
5. **Evaluate.** Choose deterministic, single judge, or dual judges with an arbiter.
6. **Improve.** Read the findings and recommendations. If your agent implements the
   [candidate protocol](docs/CONNECTOR_SDK.md) (a handful of HTTP endpoints), let Kaizeval
   optimize a candidate under your rules, then approve or reject the result.

## Use it in CI

Fail a pull request when your agent gets worse:

```bash
kaizeval ci --url https://kaizeval.internal --workspace $WS --agent $AGENT --dataset $DATASET \
  --fail-under 0.85 --max-critical 0 --baseline-run $LAST_GOOD_RUN
```

Exit code `0` passes, `1` means a quality gate failed, `2` is an operational error. As a GitHub
Action:

```yaml
- uses: abdullahahmed94/kaizeval@v0.2.0
  with:
    url: ${{ vars.KAIZEVAL_URL }}
    email: ${{ secrets.KAIZEVAL_EMAIL }}
    password: ${{ secrets.KAIZEVAL_PASSWORD }}
    workspace: ${{ vars.KAIZEVAL_WORKSPACE }}
    agent: ${{ vars.KAIZEVAL_AGENT }}
    dataset: ${{ vars.KAIZEVAL_DATASET }}
    fail-under: "0.85"
```

## What is included

| Area | What you get |
|---|---|
| Agents | Generic HTTP and OpenAI-compatible connectors (black box); a candidate protocol for agents that can be safely patched, with `kaizeval.agentkit` to add it to your own agent in a few lines; capability discovery with reasons |
| Models | Any OpenAI-compatible endpoint, local or hosted; one gateway for eight roles (generator, reference drafter, two judges, arbiter, root cause, optimizer, spec inference); preflight; adapts to endpoints that refuse optional request fields; budgets; per-role usage; no silent fallback |
| Knowledge | PDF, DOCX, Markdown, text; versioned; every passage traceable to file, page or section and offsets |
| Specification | From your requirements, inferred, or both; versioned; Review & Edit, Approve or Skip |
| Tests | Written by 1 to 8 independent tester agents, each with its own angle; knowledge, citation, edge-case, multi-turn and unanswerable cases; language variants (needs a real model); import of approved Q&A; coverage report; stale-case detection when a document changes |
| Evaluation | Deterministic-first scoring; 1 to 7 independent reviewer agents with an arbiter on disagreement; testers run in parallel; critical-failure gates; "needs human review" and "not scored" as first-class outcomes |
| Diagnosis | Findings that separate observation from hypothesis; prioritised recommendations with a review workflow |
| Optimization | Human-guided or autonomous; allowlisted patches on an isolated candidate; full regression each cycle; rollback; stops at the target or after three unchanged cycles; budgets and cancellation; after a stop you continue from the candidate or release it |
| Validation | Paired A/B with bootstrap confidence interval; sealed, rotatable holdout; explicit approval, promotion and rollback |
| Operations | One-file project mode (`kaizeval init` / `run`); MCP server for coding assistants; roles (owner, editor, reviewer, viewer); audit trail; encrypted credentials; JSON and Markdown reports; REST API; CLI; GitHub Action |

Console pages: Overview, Models, Agents, Knowledge, Specification, Datasets, Evaluations,
Recommendations, Optimization, Candidates, Audit, Workspace settings.

## Deploy

- **Single node:** `pip install` + `kaizeval serve` (SQLite, local files).
- **Docker Compose:** API, workers and PostgreSQL; optional MinIO; a network-isolated variant for
  air-gapped sites. See [docs/INSTALL.md](docs/INSTALL.md).

## Limitations

Kaizeval is **alpha** software. Please read these before relying on it.

- A score on generated tests describes behaviour on those tests. It is not real-world accuracy.
- Reaching the target score is a goal, not a guarantee. The improvement loop stops when three cycles in a row change nothing, which can be below the target; you can then continue it or release what it reached.
- Model judges are estimates and can be wrong or biased. Two judges agreeing is not proof.
- Automatic optimization works only for agents that implement the candidate protocol (a few lines
  with `kaizeval.agentkit`). No vendor-specific connector is included; other agents are evaluated as
  black boxes and get recommendations.
- It has been run end to end against one real local model (`qwen2.5:7b` on Ollama), including a
  model-backed agent improved through its system prompt, and against one hosted provider with an
  API key; what happened, what broke and what was fixed is in
  [docs/REAL_MODEL_VALIDATION.md](docs/REAL_MODEL_VALIDATION.md). Other models and other
  providers may behave differently. CI runs the test-suite on SQLite and on PostgreSQL and health-checks
  the Docker Compose stack on every push. The S3 backend is tested against a local S3 emulator,
  not against a real object store.
- Model reviewers make mistakes. One claimed a refusal that was not there; such claims are now
  verified against the answer, but treat reviewer scores as estimates, and review language
  variants yourself.
- An agent that is itself a model answers differently each time. Use `evaluation.repeats` and do
  not read small differences as real.
- Sign-in is local accounts only (no single sign-on yet). Holdout isolation is enforced in code,
  not by a process boundary. No independent security audit has been done.
- Evaluation sends real requests to your agent. Use a test deployment if its tools have side
  effects.

## Documentation

| | |
|---|---|
| [Project mode: one file, one command](docs/PROJECT_MODE.md) | [Install, configure, upgrade](docs/INSTALL.md) |
| [Architecture](docs/ARCHITECTURE.md) | [Examples](examples/README.md) |
| [Workflow, step by step](docs/WORKFLOW.md) | [API quickstart script](examples/api_quickstart.py) |
| [REST API](docs/API.md) | [LLM Gateway](docs/LLM_GATEWAY.md) |
| [Connector SDK](docs/CONNECTOR_SDK.md) | [Security notes](docs/SECURITY.md) |
| [Validation against a real model](docs/REAL_MODEL_VALIDATION.md) | [Acceptance status](docs/MVP_ACCEPTANCE.md) |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | [Changelog](CHANGELOG.md) |
| [Implementation plan and roadmap](docs/IMPLEMENTATION_PLAN.md) | [Release checklist](docs/RELEASE_CHECKLIST.md) |

## Roadmap

S3 against a real object store · validation with more models and providers · judge calibration against human
labels · knowledge-base health report · connectors for common agent frameworks · service tokens
and scheduled runs · single sign-on · adversarial test packs · SQL and API ground truth.

## Contributing

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md); report security
problems as described in [SECURITY.md](SECURITY.md).

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). You may use, modify and
distribute Kaizeval, including commercially, under the terms of the license. The Kaizeval name is
not covered by the license grant (section 6).
