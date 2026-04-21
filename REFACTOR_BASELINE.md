# Refactor Baseline — 2026-04-10

## Test Suite

```
Ran 337 tests in 431.050s
OK (skipped=1)
```

- **337 passed**, 0 failures, 0 errors, 1 skipped
- Test files: test_phase1.py, test_phase2.py, test_phase3.py, test_workflows.py, test_dma_features.py, test_openwisp.py, test_signup_fee.py
- Runner: `tests/test_runner.py` (SabiWiFiTestRunner — creates RADIUS tables in test DB)
- Command: `DJANGO_SETTINGS_MODULE=config.settings.base venv/bin/python manage.py test tests/ --keepdb --verbosity=2`
- Settings: `config/settings/base.py` (TEST_RUNNER = 'tests.test_runner.SabiWiFiTestRunner')

## Linter

No linter configured (no flake8, ruff, mypy, pyproject.toml, or tox.ini present).

## Build

Django 5.1 + DRF. No build step beyond `manage.py collectstatic`.
`DJANGO_SETTINGS_MODULE=config.settings.base venv/bin/python -c "import django; django.setup()"` exits 0.

## Codebase Stats

- ~130 Python source files (excluding venv, migrations, __pycache__)
- ~18,700 lines of Python
- Largest files: portal/views.py (1443), dashboard/views.py (1258), routers/views.py (863)

## Known Noise

- Every test that creates a Reseller emits: `Could not create OpenWISP org for reseller ...: OpenWISP not configured.`
  This is expected — the OpenWISP signal fires but the integration isn't configured in test env.
- Some tests hit the live Termii SMS API (error logged but tests pass):
  `SMS failed to ... Phone number is expected in international format.`
