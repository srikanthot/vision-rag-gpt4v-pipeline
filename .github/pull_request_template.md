## Summary
<!-- What changed and why. -->

## Checklist
- [ ] `python tests/test_unit.py` passes
- [ ] `python tests/test_e2e_simulator.py` passes
- [ ] `ruff check function_app tests scripts` is clean
- [ ] Bumped `skillVersion` in `deploy.config.example.json` if record shape changed

## Deploy impact
- [ ] No redeploy needed
- [ ] Requires `scripts/deploy_function.sh` (code or app-setting change)
- [ ] Requires `python scripts/deploy_search.py` (search artifact change)
- [ ] Requires full `--run-indexer` re-index
