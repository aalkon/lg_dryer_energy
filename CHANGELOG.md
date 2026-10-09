# Changelog

## 0.2.0 - 2026-10-08

- Address PR review: avoid unchanged-total and no-op status writes, ignore provisional decreases, validate the native today source, and match compaction rounding to recorder accumulation.
- Defer yesterday reconciliation until 6 AM local time and revalidate hourly even with an unchanged native value. Warn once when the configured yesterday entity is missing.
- Capture status changes during startup, release the session lock during LG requests, bound recorder waits, and preserve deferred accounting writes during shutdown.
- Add explicit `migration_last_processed_date` recovery for legacy storage without a marker, with hourly retry and a single diagnostic. Prune sessions independently of successful attribution and discard invalid sessions once.
- Fix review findings: defer imports until HA has started, verify atomic accounting writes by reading the storage file, and preserve status-event timestamps through lock delays.
- Verify yesterday through a read-only, date-specific detailed LG API response using the native entity's existing connection. Cached state timestamps cannot establish a reporting date. Missing or mismatched rows never become zero corrections; retry/recheck requests are capped at once an hour per running instance.
- Use the optional `energy_today_entity` (default `sensor.dryer_energy_today`) for provisional same-day energy estimates after a cycle completes. Missing or unusable today data falls back to yesterday-only attribution.
- Allocate positive daily-total increases over newly completed sessions. Reconcile yesterday's reported total, including downward and zero revisions, by replacing prior allocations and rebuilding subsequent cumulative sums.
- Preserve the statistic ID and migrate existing session storage to a durable, bounded reconciliation ledger. Verify recorder writes and replay unfinished imports after restart without double-counting.
- Handle identical daily totals, unavailable-state recovery, local-date resets, UTC hour boundaries and DST. Preserve running sessions through connectivity flaps and persist session starts immediately.
- Preserve legacy statistics through long idle periods and overlapping overnight hours. Fail safely if statistics exist but their accounting storage has been lost.
- Add current recorder metadata (`mean_type` and `unit_class`). Target Home Assistant 2026.7 or newer.
- Document that hourly allocation is estimated, that cycle-end-day attribution remains an assumption, and that missed historical daily totals cannot be recovered from the native sensors alone.

## 0.1.3 - 2026-04-19

- On startup, reconstruct the in-progress session start using a three-tier resolver: LG `total_time` / `remaining_time` sensors, then recorder history walk, then `utcnow()`. Prevents truncated sessions across HA restarts and survives recorder purges / HA downtime at cycle start.
- New optional config keys: `total_time_entity`, `remaining_time_entity`.

## 0.1.2 - 2026-04-18

- Attribute overnight cycles by local end-date (matches LG). Hourly rows are laid down across the hours the cycle actually ran, including the prior day; daily totals diverge from the LG app on overnight days in exchange for correct hour-of-use tracking.

## 0.1.1 - 2026-04-17

- Fix duplicate attribution when `energy_yesterday` flaps through `unknown` / `unavailable` and back.

## 0.1.0 - 2026-04-17

- Initial release: session tracking via `dryer_current_status`, proportional attribution of `energy_yesterday` across sessions, backdated hourly rows via `async_add_external_statistics`.
