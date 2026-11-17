**What this changes and why**

**How it was tested**
- [ ] `ruff check src tests`, `mypy` and `pytest` pass locally
- [ ] New behaviour has a test
- [ ] `npm run build` passes (if `web/` changed)

**Checklist**
- [ ] No secret, customer data or private address is included
- [ ] Nothing reads holdout items outside `services/holdout.py` and audited human review
- [ ] No new outbound network call bypasses `netguard`
- [ ] A connector only advertises capabilities it really implements
- [ ] Documentation updated where behaviour changed
- [ ] I have the right to contribute this code under the Apache-2.0 license
