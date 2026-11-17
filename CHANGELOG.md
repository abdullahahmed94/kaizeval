# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
See the "Limitations" section of the README and `docs/MVP_ACCEPTANCE.md`.
