# Estfeed Home Assistant Integration — Design

**Status:** Approved (brainstorming complete, ready for implementation planning)
**Date:** 2026-04-29
**Author:** brainstorming session w/ user

## 1. Goal

A HACS-installable Home Assistant custom integration that ingests electricity (and gas) metering data from the Estfeed public API (`estfeed.elering.ee/api/public/v1`) so Estonian households can:

1. Use the **Energy Dashboard** with their full Estfeed history (consumption + production, per meter), and
2. Read **lagging summary sensors** (yesterday / month-to-date / previous month) for cards and automations.

The integration is *not* real-time — Estfeed data is settled overnight and arrives ~24 hours late. The design is shaped by that constraint.

## 2. Estfeed API summary

Verified live during brainstorming with the user's credentials.

**Authentication** — OAuth2 `client_credentials` against Keycloak:

```
POST https://kc.elering.ee/realms/elering-sso/protocol/openid-connect/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=<UUID>&client_secret=<secret>
```

Returns a Bearer token, `expires_in: 300` (5 min), no usable refresh token (`refresh_expires_in: 0`).

**Endpoints** (`Authorization: Bearer <token>`):

| Method | Path                              | Purpose                                                                                |
|--------|-----------------------------------|----------------------------------------------------------------------------------------|
| GET    | `/api/public/v1/metering-point-eics` | List metering points (EIC + commodity type + access periods) for the API key. Required `startDateTime` / `endDateTime`. |
| GET    | `/api/public/v1/metering-data`    | Hourly/15-min/day/week/month consumption + production per meter. Required `startDateTime`, `endDateTime`, `resolution`. Optional `meteringPointEics` (CSV, ≤10). |

**Constraints:**

- Max 31 days per `metering-data` window.
- Max 10 EICs per request (when given as parameter).
- **Rate limit: 1 request per 5 seconds** (per API key).
- Resolutions observed: `fifteen_min`, `one_hour`, `one_day`, `one_week`, `one_month`. Estonian docs list "15 min, 1 hour, 1 week, 1 month"; example uses `one_day`. Code treats the enum as the source of truth and is verified against the API at startup.
- Data freshness: latest available interval is roughly the previous local-time day's last hour. Confirmed live: at 2026-04-29 the newest hourly interval was `2026-04-28T23:00:00Z`.

**Response shapes:**

`metering-point-eics` → array of `{ eic, commodityType: ELECTRICITY|NATURAL_GAS, periods: [{ from, to? }] }`. `to` is omitted when the grid agreement is still active.

`metering-data` → array of `{ meteringPointEic, accountingIntervals: [{ periodStart (UTC), consumptionKwh, productionKwh, consumptionM3?, productionM3? }], error? }`. Per-meter `error` carries an `ErrorResponseV2` when one meter in a multi-meter request fails — the rest of the response is still valid.

## 3. Decisions made during brainstorming

| #  | Question                              | Decision                                                                                       |
|----|---------------------------------------|------------------------------------------------------------------------------------------------|
| Q1 | Primary use case                      | Both Energy Dashboard *and* lagging summary sensors.                                            |
| Q2 | Distribution                          | HACS-installable custom integration (not core, not personal-only).                              |
| Q3 | Historical backfill                   | Default 12 months on first install; service `estfeed.backfill_history` can extend up to 84 mo. |
| Q4 | Data resolution                       | Configurable per instance, default `one_hour`. (Options flow lets user pick `fifteen_min`.)    |
| Q5 | Lagging sensors to expose             | Yesterday, month-to-date, previous month — for both consumption and production.                |
| Q6 | Polling cadence                       | Once per hour.                                                                                  |
| Q7 | Cost / tariff calculation             | Out of scope; users wire up costs via HA's built-in Energy Dashboard cost configuration.        |

## 4. Architecture

`DataUpdateCoordinator` per config entry → external long-term statistics + in-memory rolling cache → derived sensor entities. This matches the standard HA pattern for delayed external time-series data (Tibber, OctopusEnergy, Forecast.Solar).

```
┌─────────────────────────────────────────────────────────────────┐
│ Config entry (one per API key)                                  │
│                                                                 │
│   ┌──────────────────┐    ┌──────────────────────┐              │
│   │ EstfeedClient    │    │ EstfeedCoordinator   │              │
│   │  - OAuth token   │◄───┤  hourly tick         │              │
│   │  - rate limiter  │    │  ─────────────────   │              │
│   │  - typed errors  │    │  fetch new intervals │              │
│   └──────────────────┘    │  └─► statistics.py ──┼──► HA recorder│
│                           │                      │   (long-term  │
│                           │  update rolling      │    statistics)│
│                           │  cache (62 d)        │              │
│                           └──┬───────────────────┘              │
│                              │                                  │
│                              ▼                                  │
│                       ┌──────────────────┐                      │
│                       │ Sensor entities  │                      │
│                       │  - yesterday     │                      │
│                       │  - MTD           │                      │
│                       │  - prev month    │                      │
│                       │  - diagnostics   │                      │
│                       └──────────────────┘                      │
└─────────────────────────────────────────────────────────────────┘
```

## 5. Repository layout

```
estfeed/
├── custom_components/estfeed/
│   ├── __init__.py            # async_setup_entry, unload, services registration
│   ├── manifest.json          # HACS metadata, dependencies, version
│   ├── const.py               # DOMAIN, defaults, Resolution enum
│   ├── api.py                 # EstfeedClient: OAuth + REST + rate limiting
│   ├── coordinator.py         # EstfeedCoordinator (DataUpdateCoordinator)
│   ├── statistics.py          # external-statistics writer + helpers
│   ├── config_flow.py         # UI setup + reauth + options flow
│   ├── sensor.py              # lagging-value entities + diagnostic timestamp
│   ├── binary_sensor.py       # data_fresh diagnostic
│   ├── diagnostics.py         # async_get_config_entry_diagnostics
│   ├── services.yaml          # backfill_history service definition
│   ├── strings.json           # English UI strings
│   └── translations/
│       ├── en.json
│       └── et.json
├── tests/
│   ├── conftest.py
│   ├── test_api.py
│   ├── test_config_flow.py
│   ├── test_coordinator.py
│   ├── test_sensor.py
│   └── test_statistics.py
├── docs/superpowers/specs/    # this file lives here
├── hacs.json                  # HACS install metadata
├── README.md                  # setup + screenshots + troubleshooting
├── LICENSE                    # MIT
└── .github/workflows/
    ├── validate.yml           # hassfest + HACS action + ruff + mypy
    └── release.yml            # tag-driven release
```

## 6. User-facing setup

**Initial config flow:**

1. Settings → Devices & Services → "+ Add Integration" → "Estfeed".
2. Form: `Client ID` (UUID), `Client Secret`, `Friendly name` (default "Estfeed").
3. Validation: client makes one `metering-point-eics` request with a 7-day window. On 401/403 → "Invalid credentials"; otherwise success screen lists discovered EICs (commodity type + access-period start) and asks for confirmation.
4. On submit: config entry created, devices for each EIC registered, background backfill scheduled.

**Options flow** (re-configurable later):

- `resolution` — `one_hour` (default) | `fifteen_min`.
- `backfill_months` — default 12, range 1–84.

**Reauth flow** triggered when Keycloak returns 401 mid-poll: HA shows a *"Reauthenticate"* button on the config entry → re-opens the credentials form → existing entities, devices, and statistics are preserved.

**Multi-instance:** a second config entry with a different API key just works — separate device tree, separate rate limiter, separate statistics namespaces.

## 7. API client (`api.py`)

`EstfeedClient` is the only module that speaks HTTP. Two public async methods:

- `list_metering_points(start: datetime, end: datetime) -> list[MeteringPoint]`
- `get_metering_data(start, end, resolution: Resolution, eics: list[str] | None = None) -> list[MeterData]`

Both return `dataclass` types defined in this module so callers don't depend on raw JSON keys.

**OAuth token cache:** in-memory token + `expires_at`; refresh when `now >= expires_at - 30s`. Token requests serialized through an `asyncio.Lock` so a burst of API calls doesn't trigger N parallel token fetches. No refresh-token flow (none provided).

**HTTP transport:** uses `homeassistant.helpers.aiohttp_client.async_get_clientsession(hass)` (HA-mandated shared session). 30-second per-request timeout. `User-Agent: ha-estfeed/<manifest version>`.

**Rate limiter:** per-client `asyncio.Lock` + monotonic `last_request_at`; before each request, sleep `max(0, 5.0 - (now - last))`. Per-instance scope means two config entries (two API keys) make calls in parallel without interfering — Estfeed's rate limit is per key.

**Recent-request ring buffer:** the client keeps the last 5 request summaries (`{path, status, duration_ms, at}`, no payloads or tokens) in memory for the diagnostics endpoint to surface. Cleared on unload.

**Typed exceptions:**

| Exception                | Source                                  | Coordinator handling                                       |
|--------------------------|-----------------------------------------|------------------------------------------------------------|
| `EstfeedAuthError`       | Keycloak 401, API 401, API 403          | Raised as `ConfigEntryAuthFailed` → reauth UI              |
| `EstfeedRateLimitError`  | API 429                                 | Sleep `Retry-After` (or 5s), retry once; otherwise fail tick |
| `EstfeedAPIError`        | API 4xx (other), 5xx                    | Fail tick, log WARNING                                     |
| `EstfeedTimeoutError`    | `asyncio.TimeoutError`, `ClientError`   | Fail tick, log WARNING                                     |

**Resolution enum** in `const.py`:

```python
class Resolution(StrEnum):
    QUARTER_HOUR = "fifteen_min"
    HOUR = "one_hour"
    DAY = "one_day"
    WEEK = "one_week"
    MONTH = "one_month"
```

The integration uses only `QUARTER_HOUR` and `HOUR` for ingestion; other values exist for future use.

## 8. Coordinator and statistics ingestion (`coordinator.py`, `statistics.py`)

**Update interval:** 1 hour (Q6).

**Per-tick algorithm:**

1. For each `(eic, kind)` in scope, ask the recorder for the latest existing statistic timestamp via `homeassistant.components.recorder.statistics.get_last_statistics(hass, 1, statistic_id, True, {"sum"})`.
2. Compute fetch window = `(min(latest_seen) + 1 interval) → now`. Cap each chunk at 31 days; loop chunks if needed.
3. Call `EstfeedClient.get_metering_data(...)` with up to 10 EICs per request.
4. For each meter response: skip if `error` populated (log + continue). Otherwise sort intervals by `periodStart` ascending and compute cumulative `sum` by adding per-interval kWh to the previous existing `sum` from step 1.
5. `await async_add_external_statistics(hass, metadata, statistics)` — idempotent on `(statistic_id, start)`.
6. Append the new intervals to the in-memory rolling cache (`dict[eic, deque[Interval]]`, retention 62 days). Trim entries older than 62 days.
7. Notify entity listeners.

**External statistics shape:**

```python
metadata = {
    "source": "estfeed",
    "statistic_id": f"estfeed:{slug}_{kind}_{eic_suffix}",  # e.g. estfeed:home_consumption_n089
    "name": f"{friendly_name} {kind} ({eic})",
    "unit_of_measurement": "kWh",        # or "m³" for gas
    "has_mean": False,
    "has_sum": True,
}
```

`StatisticData` rows: `{ start: hour-aligned UTC datetime, state: cumulative kWh at end of interval, sum: cumulative kWh from series start }`. For metering counters, `state == sum` for every row — both express the running total. HA derives the per-interval delta (the value actually charted in the Energy Dashboard) from consecutive `sum` differences.

**Initial backfill:** `async_setup_entry` checks whether any statistics exist for this entry's streams via `get_last_statistics`. If none, it schedules `hass.async_create_background_task(_initial_backfill(...))` which runs the same fetch loop with `start = now - options.backfill_months`. Estimated ~12 chunks × 5 s ≈ 60 s for one meter at 12 months. The user-visible setup completes in <1 s; data fills in behind the scenes.

**`estfeed.backfill_history` service** (`services.yaml`):

```yaml
estfeed:
  backfill_history:
    fields:
      months:
        default: 24
        selector: { number: { min: 1, max: 84, mode: box } }
      entry_id:
        selector: { config_entry: { integration: estfeed } }
```

Implementation re-runs the chunked fetch for the requested window. Idempotent — replaying produces identical statistics rows.

**Rolling cache repopulation on HA restart:** on `async_setup_entry`, if the in-memory cache is empty (always true at startup), the coordinator does a one-shot fetch of `now - 62 days → now` to warm it. Two API chunks × 5 s ≈ 10 s background warmup.

## 9. Sensors and entity layout (`sensor.py`, `binary_sensor.py`)

**Device per metering point.** `DeviceInfo(identifiers={(DOMAIN, eic)}, name=f"{friendly_name} ({eic})", manufacturer="Elering Estfeed", model=commodity_type)`. Owned by the config entry.

**Lagging sensors** (per meter, six per electricity meter; gas variants use `m³` and `device_class=gas`):

| Entity ID                                | Friendly name                | Unit | device_class | state_class | enabled_by_default |
|------------------------------------------|------------------------------|------|--------------|-------------|--------------------|
| `sensor.<slug>_consumption_yesterday`    | Consumption yesterday        | kWh  | energy       | (none)      | True               |
| `sensor.<slug>_consumption_month_to_date`| Consumption month-to-date    | kWh  | energy       | (none)      | True               |
| `sensor.<slug>_consumption_previous_month`| Consumption previous month  | kWh  | energy       | (none)      | True               |
| `sensor.<slug>_production_yesterday`     | Production yesterday         | kWh  | energy       | (none)      | False              |
| `sensor.<slug>_production_month_to_date` | Production month-to-date     | kWh  | energy       | (none)      | False              |
| `sensor.<slug>_production_previous_month`| Production previous month    | kWh  | energy       | (none)      | False              |

`state_class` intentionally omitted — these are derived totals, and long-term statistics already come from `async_add_external_statistics`. Production sensors are disabled by default for every install (most Estonian households don't generate); one click in the entity registry enables them when relevant.

**Availability.** Lagging sensors and the `latest_interval` diagnostic are `available` whenever the rolling cache contains at least one interval, regardless of the most recent coordinator tick succeeding. This avoids flapping to `unavailable` on a single transient API hiccup; "is data flowing?" is answered by `binary_sensor.<slug>_data_fresh` (intentional separation of "the sensor has a value" from "the value is recent enough"). When the cache is empty (post-restart, before warmup completes), entities report `unavailable`.

**Computation** (all in `hass.config.time_zone`, default `Europe/Tallinn`):
- *Yesterday*: sum of intervals with `local_date(periodStart) == today_local - 1d`.
- *Month-to-date*: sum of intervals with `local(periodStart) ≥ start_of_current_month_local` AND `≤ as_of`.
- *Previous month*: sum of intervals with `local(periodStart) ≥ start_of_previous_month_local` AND `< start_of_current_month_local`.

DST correctness: spring-forward day has 23 hourly intervals, fall-back has 25 — calculations sum what's there, no hour padding/trimming.

**Per-sensor attributes:**
- `meter_eic`, `commodity_type`
- `period_start`, `period_end` (ISO local timestamps describing the value's window)
- `as_of` (UTC timestamp of newest interval included)

**Entity-ID disambiguation:**
- 1 meter per entry → no suffix (`sensor.home_consumption_yesterday`).
- ≥2 meters per entry → append last 4 chars of EIC (`sensor.home_consumption_yesterday_n089`).

**Diagnostic entities** (`EntityCategory.DIAGNOSTIC`, per device):
- `sensor.<slug>_latest_interval` — `device_class=timestamp`, datetime of newest cached interval.
- `binary_sensor.<slug>_data_fresh` — `True` if newest interval is within 30 hours of now.

**Long-term statistic streams** (consumed by Energy Dashboard, not entities):
- `estfeed:<slug>_consumption_<eic_suffix>`
- `estfeed:<slug>_production_<eic_suffix>`

README documents the *Settings → Energy → Add consumption → pick stream* path with screenshots.

## 10. Diagnostics, logging, security

**`async_get_config_entry_diagnostics`** returns:

```python
{
  "entry": { "options": {...}, "title": "Home" },          # client_secret redacted
  "coordinator": {
    "last_update_success": "2026-04-29T07:00:00Z",
    "last_exception": None,
    "intervals_cached_per_meter": { "...089-N": 1488 },
  },
  "meters": [
    {
      "eic": "38ZEE-...REDACTED",
      "commodity_type": "ELECTRICITY",
      "validity_periods": [{ "from": "2019-07-27T21:00:00Z" }],
      "stats": { "newest": "...", "oldest": "..." },
    }
  ],
  "recent_requests": [ /* last 5: path, status, duration_ms */ ],
}
```

`async_redact_data` covers `client_id`, `client_secret`, full EIC bodies (last 4 kept), and any `Authorization` header.

**Logging.** Namespace `custom_components.estfeed`:
- INFO — setup complete, backfill start/end, reauth triggered.
- DEBUG — per-request `path + status + duration` (no payloads, no tokens).
- WARNING — transient failures, per-meter `error` field set.
- ERROR — only unrecoverable.

Tokens, secrets, and full request/response bodies are never logged at any level.

## 11. Error handling

| Failure mode                            | Detection                              | Behavior                                                                   |
|-----------------------------------------|----------------------------------------|----------------------------------------------------------------------------|
| Bad credentials at setup                | Keycloak 401 / `invalid_client`        | Form error: "Invalid client_id / client_secret".                            |
| Credentials revoked after setup         | Keycloak 401 mid-poll                  | `ConfigEntryAuthFailed` → HA reauth UI; entities and statistics preserved.  |
| Rate limit (429)                        | Response status                        | Sleep `Retry-After` (or 5 s), retry once. Else fail this tick.              |
| Network timeout / DNS / 5xx             | `aiohttp.ClientError`, `TimeoutError`  | Fail tick (log WARNING). Sensors keep last cached value; `binary_sensor.data_fresh` flips to `off` once the newest interval ages past 30 h. |
| Per-meter `error` in 200 response       | `ApiKeyMeterDataDto.error` populated   | Skip that meter for this cycle; continue with healthy meters; log WARNING.  |
| Gaps in returned intervals              | Fewer rows than expected               | Write what we got. Backfill service can refetch.                            |

## 12. Testing

**Unit tests** (`pytest` + `pytest-homeassistant-custom-component`):

- `test_api.py` — `EstfeedClient` against `aioresponses`:
  - Token cached + refreshed before expiry; concurrent calls → single token fetch.
  - 401 → `EstfeedAuthError`; 429 → retry; 500 → `EstfeedAPIError`.
  - Rate-limit pacing asserted via `freezegun` (two back-to-back calls separated by ≥5 s).
- `test_coordinator.py` — fake client:
  - Initial backfill writes expected `(start, sum)` series; cumulative sums correct across chunks.
  - Subsequent ticks fetch only new hours (window starts at `latest_seen + 1h`).
  - Recovery from a multi-day gap.
  - Per-meter error skip.
- `test_sensor.py` — DST correctness for yesterday/MTD/previous-month at Europe/Tallinn spring-forward (23-hour day) and fall-back (25-hour day). Production-zero meter still reports `0.0`, not `None`.
- `test_statistics.py` — `async_add_external_statistics` called with correct metadata and ascending start times.
- `test_config_flow.py` — happy path, bad credentials, reauth, options flow.

**Integration tests** stay within HA's mocked-aiohttp environment. Live API is **not** hit in CI; a manual `scripts/smoke.py` exists for end-to-end verification with personal credentials.

**CI gates** (`.github/workflows/validate.yml`):
- `hassfest` (HA's manifest/translations validator)
- `HACS/action@main` (HACS validation)
- `ruff check` and `ruff format --check`
- `mypy --strict custom_components/estfeed`
- `pytest` with ≥85% line coverage on `custom_components/estfeed/`

## 13. Out of scope

1. Cost / tariff calculation (Q7 — users wire up costs via HA Energy Dashboard).
2. Real-time / sub-hourly streaming (Estfeed data is settled overnight).
3. Nord Pool spot price ingestion (separate concern).
4. Read/write operations (Estfeed public API is read-only).
5. Datahub OAuth flow for operators / GDPR-bulk exports (different API, different audience).
6. Multi-language UI beyond English + Estonian.

## 14. Open question deferred to implementation

- **Resolution-enum verification** — the OpenAPI spec doesn't enumerate accepted `resolution` values; the Estonian docs list "15 min, 1 hour, 1 week, 1 month" but the example uses `one_day`. The integration will probe each enum value at startup and skip unsupported ones with a debug log. Only `fifteen_min` and `one_hour` are needed for ingestion regardless.
