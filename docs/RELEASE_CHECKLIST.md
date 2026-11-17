# Release checklist

Work through this list for every release. Do not tag a release with an unchecked item unless the
release notes say so explicitly.

## Ownership and license (first public release, and whenever contributors change)

- [ ] The copyright holder named in `NOTICE` is correct and has the right to publish the code
      (work done for an employer may belong to the employer).
- [ ] Every third-party file in the repository is compatible with Apache-2.0 and attributed.
- [ ] `LICENSE` is the unmodified Apache License 2.0 text
      (SHA-256 `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`).
- [ ] `docs/THIRD_PARTY_LICENSES.md` is regenerated and contains no license that is incompatible
      with distribution (for example a copyleft license that would bind the whole work).

## Quality

- [ ] `ruff check src tests` passes.
- [ ] `mypy` passes.
- [ ] `pytest` passes (strict offline; the socket guard reports no external connection).
- [ ] `cd web && npm ci && npm run build` passes.
- [ ] `python -m build` produces a wheel that contains `kaizeval/webui/index.html`.
- [ ] `pip install dist/*.whl` in a clean environment, then `kaizeval demo --no-serve` completes.
- [ ] CI is green, including the Docker Compose smoke test.
- [ ] `python scripts/gen_api_reference.py` leaves `docs/API.md` unchanged.

## Verification that needs infrastructure

- [ ] Preflight and one evaluation against a real local model server.
- [ ] The Compose stack: sign up, run the journey, restart containers, data persists.
- [ ] The network-isolated override: the API and worker containers cannot reach an outside host.

## Hygiene

- [ ] No secret, token, private address or customer data anywhere in the tree or in git history
      (`git log -p | grep -iE "api[_-]?key|secret|password|token"` reviewed; a secret scanner run).
- [ ] No `.env`, database file, `secret.key` or data directory is tracked.
- [ ] Sample data describes fictional entities only.
- [ ] No fabricated benchmark, score or certification claim in the README or docs. Demo numbers
      are described as synthetic.
- [ ] Git author name and email on every commit are the real, intended identity.

## Publish

- [ ] `CHANGELOG.md` is updated and the version in `pyproject.toml` and
      `src/kaizeval/__init__.py` match.
- [ ] Tag `vX.Y.Z` on the commit that CI validated.
- [ ] Create the GitHub release, attach the wheel and the sdist, paste the changelog section.
- [ ] Open the public URL in a signed-out browser and confirm the README renders and the release
      assets download.
