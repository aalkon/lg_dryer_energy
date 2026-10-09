# Tests

Install `pytest`, `pytest-asyncio`, and (on Windows) `tzdata`, then run from this repository root:

```sh
python -m pytest tests -q
```

`conftest.py` supplies lightweight Home Assistant stubs, file-backed persistence
which logs write failures without raising (matching HA's Store contract), startup
callbacks, and an in-memory recorder with historical row upserts and range queries.

Coverage includes a 543 Wh example cycle; incremental allocation; next-day
upward/downward/zero reconciliation; identical consecutive totals; missing
sources; stale dates; UTC/local midnight; DST; overlapping overnight hours;
restart and failed-write replay; concurrent callbacks; v2 migration and long
idle gaps; bounded ledger compaction; and session reconstruction. Review regressions
cover dirty replay before HA starts, silent storage failure during migration,
status callbacks delayed across midnight, and republished stale yesterday values.
Detailed API tests require the requested date, distinguish missing data from zero,
and check the native entity/SDK call path and hourly request throttling.
PR review regressions also cover unchanged split totals without rewrites,
provisional decreases, morning settling and unchanged-hint revalidation,
status persistence during API waits, recorder timeout/replay, startup events,
shutdown cancellation/deferred saves, explicit legacy marker recovery/backoff,
native today validation, bounded session retention and warning counts,
no-op status events, and exact compaction accumulation order.

These tests validate accounting and lifecycle behavior without a running HA
instance. Full HA/recorder integration and real-device midnight reporting still
need deployment validation.
