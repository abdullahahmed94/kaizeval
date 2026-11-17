# Acceptance status for release 0.1.0

This page states, criterion by criterion, what is demonstrated by automated tests, what was only
checked by hand, and what is not done. Run the evidence yourself:

```bash
pip install -e ".[dev]"
pytest                 # 85 tests; strict offline; fails on any non-loopback connection
kaizeval demo          # the whole journey on the bundled sample agent
```

## How to read the demo honestly

The demo and the test-suite use the **fake model provider**. Its judge scores are token-overlap
heuristics and everything it produces is flagged `synthetic`. What the demo *does* show for real:

- a separate agent process is invoked over HTTP for every test;
- deterministic checks (required facts present, source cited, refusal on out-of-scope questions)
  are real measurements of that agent;
- the candidate really is an isolated copy, patches really are applied only to it, the baseline
  really stays byte-identical, and the gates, rollback, holdout and approval flow really run.

What it does **not** show is the quality of a language model as generator, judge or optimizer.
That needs a real model that you configure.

## Acceptance criteria

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | Create a workspace, configure a model or local endpoint, run preflight, register and verify an agent | Done | `test_signup_login_and_system_info`, `test_preflight_is_required_before_ai_steps`, `test_real_http_model_path_with_role_routing_and_usage`, `test_agent_capabilities_reflect_adapter_and_grant` |
| 2 | PDF/DOCX/TXT/MD ingestion with traceable evidence and source citations | Done | `tests/test_documents.py` (file, version, SHA-256, page or section, character offsets, verbatim quote) |
| 3 | Requirements or inferred specification with Review / Edit / Skip and version history | Done | `test_specification_review_edit_skip_and_versions`, `test_requirements_work_without_a_model_but_inference_does_not` |
| 4 | Tagged cases, coverage, regression/holdout split with enforced isolation | Done | `test_dataset_generation_split_and_coverage`, `test_holdout_is_sealed`, `test_holdout_stays_sealed_from_the_optimizer` |
| 5 | A real configured model is routed through the gateway; a clearly flagged mock runs CI without secrets | Partly | The OpenAI-compatible path is tested end to end against a **local mock server** (`tests/test_gateway.py`, `test_real_model_route_is_not_flagged_synthetic`). It was **not** run against a real model server on the release machine |
| 6 | Single and Dual + Arbiter evaluation; disagreement, uncertainty and evidence stay visible | Done | `test_single_evaluation_with_evidence_and_separate_costs`, `test_dual_judges_are_independent_and_arbitrated` |
| 7 | Recommendations with reasons; read-only HTTP agents are not offered auto-patching | Done | `test_findings_and_recommendations`, `test_black_box_agent_gets_recommendations_only` |
| 8 | Immutable A, isolated B, patch only B, paired A/B, full regression, rollback | Done | `test_optimization_improves_candidate_and_leaves_baseline_untouched`, `test_every_round_reruns_full_regression_and_is_paired`, `test_failing_gates_roll_the_candidate_back` |
| 9 | Loop respects rounds, budgets, critical failures, stagnation, cancellation, approval before promotion | Done | `test_rounds_and_budgets_bound_the_loop`, `test_human_guided_flow_pauses_for_each_change`, `test_run_can_be_cancelled_midway`, `test_promotion_needs_explicit_approval_and_can_be_rolled_back` |
| 10 | Reports: per-category scores, percentage-point deltas, evidence, latency, model usage and separate agent cost | Done | `test_report_shows_deltas_usage_and_caveats`, `test_single_evaluation_with_evidence_and_separate_costs` |
| 11 | Workspace isolation, secret redaction, offline no-egress | Done at application level | `test_workspaces_are_isolated`, `test_credentials_are_never_returned`, `tests/test_security_and_network.py`, the socket guard in `tests/conftest.py`. The network-layer variant (`docker-compose.offline.yml`) was not run |
| 12 | A fresh checkout completes an end-to-end demo without a paid key, with labelled synthetic results | Done for the pip path | `tests/test_cli.py` drives the same journey as `kaizeval demo` over HTTP. The Docker path is checked by the CI workflow only |

## Verified by hand, not by automated tests

- The web console. Every page was opened in a browser against the demo data, at desktop and
  phone widths. These flows were clicked through: sign in and out, workspace switch, starting an
  evaluation with live progress, building and editing a specification, generating a dataset,
  a human-guided optimization (approve a change, stop), candidate approve / promote / roll back,
  and opening sealed holdout results through the confirmation dialog.
- These console actions were exercised only at the API level (same requests the console sends),
  not by clicking: file upload and dataset import pickers, member management, adding and
  verifying providers and agents, role-policy edits, recommendation state buttons, case review,
  retiring stale cases, rotating the holdout, cancelling an evaluation, rejecting a patch or a
  candidate.
- There is no automated browser test and no accessibility audit.

## Not verified on the release machine

The machine used to prepare this release had no Docker and no local model server. Therefore:

- **Docker Compose, PostgreSQL and separate worker containers** were not started locally. The CI
  workflow builds the image, starts the stack and checks health. PostgreSQL is reached only
  through SQLAlchemy and Alembic with no dialect-specific SQL, but until CI is green on a
  published repository this path is unproven.
- **S3 / MinIO storage** is implemented (`storage.S3Storage`) and has no test.
- **The network-isolated Compose override** is provided and unvalidated.
- **A real local model** (Ollama, vLLM) was never called. Preflight, routing, retries, JSON
  validation and metering are covered against a mock server that speaks the same protocol; the
  usefulness of any particular model as a judge or generator is not established by this project.

## Deliberate differences from the original blueprint

| Blueprint | This release | Why |
|---|---|---|
| Celery + Redis | Database-backed job queue | One less service; transactional job state; pause/resume. See ADR-2 |
| Next.js | Vite + React static bundle served by the API | No Node.js runtime in production; ships inside the wheel. See ADR-5 |
| OIDC | Local accounts with roles | Keeps the first release self-contained; OIDC is on the roadmap |
| OpenTelemetry | Structured JSON logs with request ids | No exporter by default, consistent with "no telemetry" |
| MinIO required | Filesystem storage by default, S3-compatible optional | A single `pip install` must work |
| Two agents on different runtimes | One sample agent reached through three interfaces (candidate protocol, simple HTTP, OpenAI-compatible) | No second agent framework is bundled |

## Not implemented

- Vendor-specific connectors (for example Salesforce, LangChain/LangGraph wrappers).
- SQL and API ground-truth providers (typed placeholders only).
- The `traces` capability: defined in the contract, implemented by no bundled adapter.
- Language and dialect variants with the fake provider (it cannot translate). The feature works
  only with a real model and is untested with one.
- Calibration of judges against human labels.
- Online experiments on live traffic, scheduled re-runs and monitoring.
- Login rate limiting, multi-factor authentication, password reset, token revocation.
- Editing an agent connection from the console (the API supports it; re-register the agent instead).
- A workspace-wide user directory: ids of removed members are shown as short ids.
- Automatic data retention and deletion.
- OCR for scanned PDFs.
- Kubernetes manifests.
- A hosted service, managed models and billing (out of scope by design).
