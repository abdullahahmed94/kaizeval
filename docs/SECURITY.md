# Security and privacy notes

This document describes what Kaizeval protects, how, and where the protection ends. It is written
for operators. Kaizeval has **not** been independently audited and carries no certification.

## Data Kaizeval handles

| Data | Where it is stored |
|---|---|
| Knowledge documents (originals) | Object storage, under `ws/{workspace_id}/…` |
| Parsed passages, test cases, agent answers, judgments, findings | Database |
| Model and agent credentials | Database, encrypted (`secrets.ciphertext`) |
| Account passwords | Database, scrypt hashes |
| Audit events | Database |

Nothing is sent anywhere except to the model endpoints and agent endpoints that a workspace
configures. There is no telemetry, update check or license check.

## Threats considered and the controls in place

### 1. Cross-workspace access
- Every table carries `workspace_id`; every API route resolves the caller's membership first.
  Non-members receive `404` for the whole workspace.
- Objects are loaded through `scoped()`, which re-checks the workspace of each id supplied by the
  client. Storage keys are prefixed with the workspace and path traversal is rejected.
- Tests: `test_workspaces_are_isolated`.
- Limit: isolation is logical. All workspaces of one instance share a database and a process.
  Use separate instances for parties that must not share infrastructure.

### 2. Credential exposure
- Model API keys and agent credentials are encrypted with a key derived from
  `KAIZEVAL_SECRET_KEY` (Fernet: AES-128-CBC with HMAC-SHA256). They are write-only in the API.
- Log lines pass through a redaction filter; request and response bodies are never logged; error
  messages stored on jobs are redacted.
- Tests: `test_credentials_are_never_returned`, `test_redaction_masks_credentials`,
  `test_real_http_model_path_with_role_routing_and_usage`.
- Limits: anyone who has both the database and `KAIZEVAL_SECRET_KEY` can decrypt credentials.
  Losing the key makes stored credentials unusable (re-enter them). There is no key rotation
  command yet. Access tokens are kept in the browser's local storage, so a cross-site scripting
  flaw in the console would expose a session; the console renders API data as text only.

### 3. Unwanted outbound traffic and SSRF
- All requests to model and agent endpoints go through `netguard`:
  - offline mode (default) reaches only loopback, private ranges, single-label names and
    explicitly allowlisted hosts, and refuses dotted names **before resolving them**;
  - link-local, multicast and reserved addresses (cloud metadata endpoints) are refused always;
  - a public destination needs a connected workspace **and** an explicit per-endpoint opt-in by
    an owner;
  - redirects are not followed; proxy environment variables are ignored.
- The policy is checked when an endpoint is registered, at preflight, and again on every request.
- Tests: `tests/test_security_and_network.py`, `test_offline_refuses_external_provider_everywhere`,
  and the suite-wide socket guard in `tests/conftest.py`, which fails the run if any test opens a
  non-loopback connection.
- Limits: in a self-hosted deployment, reaching internal addresses is intended (your agents and
  model servers live there). Any workspace editor can therefore make the server send requests to
  internal HTTP endpoints. Give accounts only to people you would allow to do that, and place the
  instance in a network segment that matches that trust. DNS rebinding between the check and the
  connection is not defended against. For a network-layer guarantee use
  `deploy/docker-compose.offline.yml`, which puts the containers on a network with no outside
  route.

### 4. Prompt injection through documents and agent output
- Documents and agent answers are data. They are passed to models only inside a delimited JSON
  block, under a system instruction to treat that block as untrusted material.
- Model output is never executed and never used as an instruction. It is parsed as JSON and
  validated against a fixed schema; scores are clamped to 0..1.
- An optimizer proposal can only set keys from the connector's allowlist, within declared types
  and ranges, and only on a candidate. It cannot reach the baseline, production or any tool.
- Passages that look like instructions are flagged (`possible_injection`) for reviewers.
- Limits: a hostile document can still bias a judge or a generated test (for example by
  persuading a model that an answer is correct). Deterministic checks, approved references and
  human review are the mitigation. Flagging is a keyword heuristic and will miss attempts.

### 5. Unauthorized change to an agent
- The baseline has no write path. Patches go to a candidate created through the connector; the
  baseline digest is re-verified after every patch.
- Autonomous optimization is off until an owner enables it for the workspace.
- Promotion needs the connector capability, an owner-granted `allow_promote`, and an unused,
  explicit approval for that exact candidate. Approval and promotion are separate calls and both
  are audited.
- Tests: `tests/test_optimization.py`.

### 6. Leaking the holdout into tuning
See "Holdout isolation" in [ARCHITECTURE.md](ARCHITECTURE.md). The isolation is enforced by code
and tests, not by a process boundary; a database administrator can read holdout rows.

### 7. Side effects during evaluation
Kaizeval only sends chat messages to the agent. If your agent calls tools that write data, send
messages or move money, evaluate a test deployment with mocked or read-only tools. Kaizeval cannot
see or block what an agent does internally.

### 8. Account security
- Passwords: scrypt (N=2^14, r=8, p=1), minimum length 8.
- Tokens: HS256, default lifetime 12 hours. There is no server-side revocation list; rotate
  `KAIZEVAL_SECRET_KEY` to invalidate every token (this also invalidates stored credentials).
- Not implemented: rate limiting of login attempts, multi-factor authentication, single sign-on,
  password reset by email. Put the instance behind your own access controls.

## Operator checklist

- [ ] Serve the console over TLS (terminate it at a reverse proxy). The built-in server speaks
      plain HTTP and binds to `127.0.0.1` by default.
- [ ] Set `KAIZEVAL_SECRET_KEY` explicitly and store it in your secret manager.
- [ ] Set `KAIZEVAL_ALLOW_SIGNUP=false` after creating the first accounts.
- [ ] Keep `KAIZEVAL_OFFLINE=true` unless you need a remote provider.
- [ ] Back up the database and the object storage together.
- [ ] Restrict who can reach the instance, and review the audit log (`/audit`).
- [ ] Evaluate agents with mocked or read-only tools.
- [ ] Use a dedicated, least-privilege admin token for agents that expose the candidate protocol.

## Retention

Kaizeval does not delete data on a schedule in this release. Remove a workspace's data by deleting
its rows and its `ws/{workspace_id}/` object prefix. Superseded document versions are kept so that
past runs stay reproducible.
