# Implementation plan and status

Status of release 0.3.0 against the product blueprint. For per-criterion evidence see
[MVP_ACCEPTANCE.md](MVP_ACCEPTANCE.md).

## Starting point

The project began from a specification only (product overview, customer journey, technical
architecture, MVP scope). There was no existing code. The specification contained a few
inconsistencies, resolved as follows:

| Inconsistency | Resolution |
|---|---|
| API paths written both as `/v1/…` and `/api/v1/…`; `preflight` vs `verify` | `/api/v1`, workspace-scoped paths, `…/verify` runs the preflight |
| Capability names differed between documents (`apply_config_patch` / `apply_patch`, `rollback` / `restore_candidate`) | `invoke, inspect, traces, create_candidate, apply_patch, restore_candidate, promote` |
| Some tables still offered platform-managed models to the user | Not offered: this edition has local and customer-provided endpoints only |
| "Holdout hidden from the optimizer" without saying how in a monolith | Enforced by an access token (`HoldoutAccess`), module boundaries and tests; documented as logical isolation |
| "Prove no egress" without a mechanism | Application policy (`netguard`) with tests, plus an optional network-isolated Compose override |
| Where requirements conflicted, the stricter security or data-isolation rule was chosen | e.g. offline is the default and cannot be overridden per workspace |

## Milestones

### A. Foundation and first vertical slice (done)
- Package layout, settings, migrations, health checks.
- Accounts, workspaces, roles, audit trail, encrypted secrets.
- Job queue with retries, cancellation and pause/resume.
- LLM Gateway: OpenAI-compatible provider, fake provider, role policies, preflight, usage, budgets, offline policy.
- Document ingestion and evidence; generic HTTP connector; sample agent.
- Test generation, sealed split, single evaluator, report, end-to-end demo.

### B. Evaluation reliability and user experience (done)
- Dual judges with conditional arbiter; deterministic-first precedence; `n/a` and human-review outcomes.
- Diagnostics, recommendations with review states.
- Multi-turn cases; import of approved Q&A; coverage; stale detection; holdout rotation.
- Web console covering the whole journey.
- Failure handling, pagination, timeouts, cancellation, idempotency keys.

### C. Controlled improvement (done)
- Candidate creation through the connector, allowlisted patches, baseline digest verification, rollback.
- Bounded loop with gates; human-guided and autonomous modes.
- Paired A/B with a bootstrap confidence interval and sign test.
- Independent holdout validation; explicit approval, promotion and production rollback.
- Recommendations-only fallback for agents that cannot be patched.

### D. Hardening and handoff (done, with the gaps listed below)
- CI workflow (lint, types, tests on three Python versions, console build, wheel build with the
  demo journey, Compose smoke test).
- Documentation set, community files, license and notices.
- CLI quality gate and GitHub Action.

## Additions beyond the blueprint

- Project mode (`kaizeval init` / `run` / `release`): the whole journey from one file with an
  embedded engine, usable from a terminal, CI or a coding assistant.
- Selectable numbers of independent testers (1-8) and reviewers (0-7).
- An MCP server exposing the journey as tools.
- `kaizeval ci` and a GitHub Action: fail a pipeline when the agent's score drops or regresses.
- Statistical verdict on every A/B comparison (`improved` / `regressed` / `inconclusive`).
- Refusal to compare runs that were scored with different evaluation modes.
- Import of real conversations as test cases.
- Stale-case detection when a knowledge document changes.
- OpenAI-compatible preset for the generic HTTP connector.
- Single-command, no-Docker start (`pip install` + `kaizeval demo`).

## Open items

Verified by CI: the test-suite on three Python versions, the console build, the wheel with the
demo journey, and the Docker Compose stack starting on PostgreSQL. Verified by hand against one real local model and one hosted provider: every model-assisted
step and the improvement loop ([REAL_MODEL_VALIDATION.md](REAL_MODEL_VALIDATION.md)). Still
unverified: S3 against a real object store (it is tested against an emulator), the
network-isolated override, other models and other hosted providers. The test-suite also passes
on PostgreSQL.
Not implemented: see the list at the end of [MVP_ACCEPTANCE.md](MVP_ACCEPTANCE.md).

## Next milestones

1. **Validate what is unverified**: S3 against a real object store; the network-isolated
   override; runs against more models and providers.
2. **Trust in scores**: judge calibration against a human-labelled sample; knowledge-base health
   report (contradictions, duplicates, ambiguous passages).
3. **Reach**: connectors for common agent frameworks (invoke + traces first); service tokens for
   CI; scheduled regression runs.
4. **Enterprise basics**: OIDC sign-in, login rate limiting, retention policies, key rotation.
5. **More ground truth**: approved-dataset management, then SQL and API providers.
6. **Adversarial test packs**: prompt injection, data leakage and out-of-scope behaviour.
