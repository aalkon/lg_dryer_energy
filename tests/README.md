# lg_dryer_energy tests

Unit tests for the attribution logic. These intentionally do **not** require
a live Home Assistant instance — `conftest.py` installs lightweight stubs in
`sys.modules` for the handful of HA symbols the integration imports, so
`pytest` can run the module's logic directly.

## Requirements

```bash
pip install pytest pytest-asyncio
```

## Run

From the repository root:

```bash
pytest ha-integrations/lg-dryer-energy/tests -v
```

## Coverage

| Brief test # | Covered by |
|---|---|
| 1 (normal single-run day) | `test_1_normal_single_run_day` |
| 2 (unknown-flap does not re-trigger) | `test_2_unknown_flap_does_not_retrigger` |
| 3 (explicit replay safety) | `test_3_explicit_replay_safety` |
| 4 (replay with idempotency disabled → identical rows) | `test_4_replay_with_idempotency_bypass_produces_identical_rows` |
| 5 (no-sessions fallback writes once) | `test_5_no_sessions_fallback_writes_once` |
| 6 (midnight-crossing session) | `test_6_midnight_crossing_session` |
| 8 (in-progress session at attribution time) | `test_8_in_progress_session_at_attribution_time` |
| Regression — non-monotonic sum on replay | `test_non_monotonic_sum_never_occurs_on_replay` |

## Not covered by these unit tests

- **Test 7 (gap-day recovery)** — exercised implicitly by the session-GC
  log-and-drop behavior in `_async_attribute_energy`, but a dedicated test
  with the stub harness would add little over reading the code.
- **Test 9 (storage migration v1 → v2)** — requires the real HA `Store`
  class. Add this in a parallel test module using
  `pytest-homeassistant-custom-component` if needed.

## Configuration note

`pytest-asyncio` must be configured in asyncio mode. Either add to
`pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

or decorate each async test with `@pytest.mark.asyncio` (already done here).
