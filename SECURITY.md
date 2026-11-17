# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Report it privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability** (private vulnerability reporting). Include:

- the affected version (`kaizeval --version`) and how it is deployed;
- a description of the issue and its impact;
- steps to reproduce, ideally with a minimal example that uses synthetic data.

You will receive an acknowledgement, and a fix or mitigation plan will be shared with you before
any public disclosure. Please give the maintainers reasonable time to release a fix.

## Supported versions

Kaizeval is in early (0.x) development. Security fixes are applied to the latest released version.

## What is in scope

- Authentication or workspace-isolation bypass (reading or changing another workspace's data).
- Exposure of stored credentials or of secrets in logs, API responses or reports.
- Bypass of the outbound network policy (offline mode, SSRF protections).
- Any path that lets automated optimization read sealed holdout items, change a baseline, or
  promote a candidate without an explicit approval.
- Prompt-injection through documents or agent output that leads to one of the above.

## Notes for operators

Kaizeval has not undergone an independent security audit and holds no security certification.
Read [docs/SECURITY.md](docs/SECURITY.md) for the threat model, the protections that exist, their
limits, and the hardening steps that are your responsibility (TLS termination, network placement,
backups, key management).
