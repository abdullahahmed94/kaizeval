# Contributing to Kaizeval

Thank you for considering a contribution. Bug reports, documentation fixes, new connectors and
model-provider adapters are all welcome.

## Ground rules

- Be respectful. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- Report security problems privately, as described in [SECURITY.md](SECURITY.md).
- Never include secrets, customer documents, real agent transcripts or private network addresses in
  issues, tests or fixtures. Use synthetic data.
- Do not add telemetry, license checks or any outbound call that is not requested by the user.

## Development setup

Requirements: Python 3.10+ and, for the web console, Node.js 20+.

```bash
git clone <your fork>
cd kaizeval
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cd web && npm ci && npm run build && cd ..   # builds the console into src/kaizeval/webui

kaizeval demo                   # starts everything and runs the full journey on the sample agent
```

## Checks that must pass

```bash
ruff check src tests
mypy
pytest
cd web && npm run build
```

The test-suite runs in strict offline mode and fails on any non-loopback network connection. Tests
must not need credentials, a real model or internet access. Use the fake provider or the local mock
model server in `tests/conftest.py`.

## Design constraints reviewers will check

These are the properties the project exists to protect. A change that weakens one will not be merged.

1. **Holdout isolation.** Only `services/holdout.py` and audited human review may obtain a
   `HoldoutAccess`. Optimization, diagnostics and recommendation code must never read holdout items
   or per-case holdout results.
2. **Baseline immutability.** Optimization writes only to a candidate created through the
   connector. Nothing promotes to production without a recorded, explicit approval.
3. **Honest capabilities.** A connector advertises only what it really implements. Unsupported
   operations raise `UNSUPPORTED_CAPABILITY`; nothing is simulated.
4. **No silent fallback.** A model-dependent step fails visibly when its model is missing or
   unreachable. It never switches to another provider.
5. **Network policy.** Every outbound request to a model or agent goes through `netguard`.
6. **Honest results.** Anything produced by the fake provider stays flagged `synthetic`. No
   invented scores, costs or benchmarks, in code or in documentation.
7. **Evidence over opinion.** Deterministic checks and approved references outrank model
   judgments. Model-judged scores are labelled as estimates.

## Adding a connector or a model provider

See [docs/CONNECTOR_SDK.md](docs/CONNECTOR_SDK.md) and [docs/LLM_GATEWAY.md](docs/LLM_GATEWAY.md).
Include tests that run against a local stub of the target system, and link to the vendor
documentation that proves each capability you advertise.

## Commits and pull requests

- Keep pull requests focused; describe what changed and how you tested it.
- Add or update tests and documentation with the code.
- Update `CHANGELOG.md` under "Unreleased".

## Licensing of contributions

Kaizeval is licensed under the Apache License 2.0. As stated in section 5 of the license, any
contribution you intentionally submit for inclusion is provided under the same license, without
additional terms. You confirm that you have the right to submit it (for example, that your
employer permits it).

There is currently no contributor license agreement. If the project ever needs one (for example
to offer the code under additional terms), it will be proposed publicly and will apply only to
contributions made after it is adopted; past contributions will not be relicensed without their
authors' consent.
