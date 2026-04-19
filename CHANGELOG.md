# Changelog

## 0.1.3 - 2026-04-19

- On startup, reconstruct the in-progress session start using a three-tier resolver: LG `total_time` / `remaining_time` sensors, then recorder history walk, then `utcnow()`. Prevents truncated sessions across HA restarts and survives recorder purges / HA downtime at cycle start.
- New optional config keys: `total_time_entity`, `remaining_time_entity`.

## 0.1.2 - 2026-04-18

- Attribute overnight cycles by local end-date (matches LG). Hourly rows are laid down across the hours the cycle actually ran, including the prior day; daily totals diverge from the LG app on overnight days in exchange for correct hour-of-use tracking.

## 0.1.1 - 2026-04-17

- Fix duplicate attribution when `energy_yesterday` flaps through `unknown` / `unavailable` and back.

## 0.1.0 - 2026-04-17

- Initial release: session tracking via `dryer_current_status`, proportional attribution of `energy_yesterday` across sessions, backdated hourly rows via `async_add_external_statistics`.
