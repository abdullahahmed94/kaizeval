# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0]

### Added
- `kaizeval.agentkit.CandidateStore`: adds the candidate protocol to any FastAPI agent in a few
  lines, so Kaizeval can improve it automatically. Settings can be booleans, numbers or text such
  as a system prompt.
- `examples/llm_agent.py`: a model-backed retrieval agent built with the kit, with a project file
  to test and improve it.
- `scripts/real_model_check.py`: exercises every model-assisted step against a real
  OpenAI-compatible model and records validity, retries, latency and sanity checks.
- A `Publish to PyPI` workflow using Trusted Publishing.
- Reports and `kaizeval run` now end with the recommendations for what still fails in the best
  version, and `kaizeval run` reports `stalled` when three runs in a row leave the score unchanged.
- Continue or release after a stop. When the loop has stopped, the owner reviews the report and
  chooses: `kaizeval run --continue` (API: `POST /optimization-runs/{id}/continue`, console:
  "Continue improving", MCP: `continue_improvement`) keeps improving from the candidate, or
  `kaizeval release approve` releases it as it is. A continuation measures baseline and candidate
  again, rebuilds the candidate if the agent was changed or restarted, and voids any release
  approval given before it.
- Results carry `decision_options`, and reports a "Your decision" section.

- `evaluation.repeats` in the project file, and a warning with the measured variation when the
  same baseline scores differently in two runs.
- [docs/REAL_MODEL_VALIDATION.md](docs/REAL_MODEL_VALIDATION.md): what happened when every step
  was run against a real local model.

- Hosted endpoints that accept less than the usual request: a refused `response_format` or
  `temperature` is left out and remembered per provider (`params.send_temperature`), a reply cut
  at the output limit fails as `OUTPUT_TRUNCATED` instead of being retried, and `model.max_tokens`,
  `model.temperature` and `model.timeout_s` in the project file set the limits of every call.
  Unknown keys under `model` are now an error.

- Tests for the S3 storage backend (against a local S3 emulator) and a CI job that runs the whole
  test-suite on PostgreSQL.

### Changed
- Console polish: a tinted navigation rail with the product mark, a timeline with status markers
  instead of coloured side stripes, hairline alerts, themed selection, caret, scrollbars and
  focus ring, tabular numerals, a readable text measure, larger touch targets, and reduced-motion
  support. No behaviour changed.
- The improvement loop has no fixed number of rounds any more. It runs until the target is
  reached or until three cycles in a row leave the score unchanged (`stagnation_rounds: 3`).
  `max_rounds` is now an optional extra limit.
- On a terminal, `kaizeval run` now asks "continue, release, reject or decide later" instead of a
  yes/no release question; pressing Enter records no decision (it used to reject).
- A proposed change that was already tried and not kept is not evaluated again; it counts as an
  unchanged cycle.
- The interactive `/api/docs` page was removed because it loaded scripts from a public CDN; the
  OpenAPI document is still served at `/api/openapi.json`.

### Fixed (found by running against a real model)
- A reviewer's claim that the agent declined is accepted only when it quotes a sentence that is in
  the answer and reads like a refusal; a real model had scored an unrelated answer as a refusal.
- Test generation keeps at most the requested number of questions per passage, drops
  near-duplicates, and leaves out questions whose reference cannot be linked to verbatim evidence.
- Proposed out-of-scope questions that the knowledge base can in fact answer are discarded.
- Language variants with mixed or foreign scripts are rejected.
- The optimizer prompt now leads from the most frequent failure to a concrete setting change.
- The second reviewer has its own sceptical brief, and runs warn when all reviewers use one model.
- A source named inside the answer text counts as a citation.
- Key facts no longer decide correctness for questions asked in another language.
- Preflight and call timeouts allow for a local model that needs minutes to load.
- Rotating the holdout failed with a real model, which repeated the questions the dataset already
  had. The tester is now told what each passage was already asked, passages with the fewest
  questions are preferred, near-repeats of existing questions are dropped, and generation is
  retried on other passages. If a fresh holdout still cannot be generated, `kaizeval run`
  continues without the final holdout check and says so, instead of failing.

### Fixed
- The fake optimizer no longer proposes again a change that a reviewer rejected.
- Per-case holdout results now need the same explicit acknowledgement as holdout cases.
- Timestamps in API responses always carry an explicit UTC offset.

## [0.1.0] - Initial release

First public, self-hosted release.

### Added
- Local accounts, workspaces and four roles (owner, editor, reviewer, viewer) with
  workspace-scoped data access.
- LLM Gateway with role-based routing for eight roles, preflight, structured-output validation,
  retries, budgets and usage metering. Providers: any OpenAI-compatible endpoint (for example a
  local Ollama or vLLM server) and a deterministic fake provider for demos and CI.
- Strict offline mode (default) and an outbound network policy with SSRF protections.
- Document ingestion for PDF, DOCX, Markdown and plain text with versioned, traceable evidence.
- Versioned specifications from business requirements and/or inference, with Review & Edit,
  Approve and Skip & Continue.
- Evidence-linked test generation, import of approved Q&A (CSV, JSONL, JSON), stratified
  regression/holdout split, coverage report, stale-case detection and holdout rotation.
- Project mode: `kaizeval init` and `kaizeval run` drive the whole journey from one
  `kaizeval.yaml` file with an embedded local engine, a paired comparison with the baseline run
  on every re-run, and `kaizeval release approve|reject` for the release decision.
- A chosen number of independent tester agents (1-8) that write the tests and call the agent in
  parallel, and of independent reviewer agents (0-7) with an arbiter on disagreement.
- An MCP server (`kaizeval mcp`) so coding assistants can evaluate and improve an agent.
- Evaluation in deterministic, single-judge and dual-judge-with-arbiter modes; per-dimension and
  per-category scores; critical-failure gates; cancellation and call budgets.
- Rule-based diagnostics with optional model hypotheses, and prioritised recommendations with a
  review workflow.
- Controlled optimization of an isolated candidate: allowlisted patches, full regression re-run
  each round, paired A/B comparison with a bootstrap confidence interval, quality gates, rollback,
  human-guided and autonomous modes, and sealed holdout validation.
- Explicit approval and promotion flow, production rollback, and an audit trail.
- Connectors: generic HTTP chat (including OpenAI-compatible chat endpoints) and the local
  candidate protocol; a bundled deterministic sample agent.
- Reports in JSON and Markdown; a web console; a CLI with `serve`, `worker`, `demo`, `ci` and
  `sample-agent`; a GitHub Action for CI quality gates.
- Docker Compose deployment files, including a network-isolated variant.

### Known limitations
See `docs/MVP_ACCEPTANCE.md`.
