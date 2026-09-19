# Estfeed — Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?logo=homeassistant&logoColor=white)](https://github.com/hacs/integration)
[![Validate](https://img.shields.io/github/actions/workflow/status/tehisain/ha-estfeed/validate.yml?branch=main&label=validate&logo=github)](https://github.com/tehisain/ha-estfeed/actions/workflows/validate.yml)
[![License: MIT](https://img.shields.io/github/license/tehisain/ha-estfeed?color=blue)](LICENSE)
[![Last commit](https://img.shields.io/github/last-commit/tehisain/ha-estfeed?color=blueviolet)](https://github.com/tehisain/ha-estfeed/commits/main)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg?logo=ruff)](https://github.com/astral-sh/ruff)

Home Assistant integration for Elering's [Estfeed](https://estfeed.elering.ee/) metering data API. Brings Estonian electricity (and gas) meter data into the **Energy Dashboard** with full historical backfill, plus lagging summary sensors for cards and automations.

> **Note:** Estfeed publishes intervals throughout the day, but each hour goes through a settling period before the kWh value is finalised — the integration polls hourly and replaces unsettled placeholders with real values once the grid operator confirms them. Most hours land within a few hours of midnight that closed them; some may take longer. Expect today's running total to grow in chunks rather than minute-by-minute, and don't treat it as real-time.

## Installation

### HACS (recommended)

1. Add this repository as a custom HACS repository (category: Integration).
2. Install "Estfeed" from HACS.
3. Restart Home Assistant.
4. Settings → Devices & Services → "+ Add Integration" → search "Estfeed".

## Configuration

You'll need a `client_id` and `client_secret` from your e-Elering customer portal:
1. Log in to https://kliendiportaal.elering.ee
2. Generate an API key. The portal shows you the `client_id` (UUID) and `client_secret`.
3. Paste both into the Estfeed integration setup form.

## Energy Dashboard wiring

After setup completes (and the backfill finishes — usually within 1–2 minutes), open Settings → Energy → Electricity grid → "Add consumption" and pick `estfeed:<your_name>_consumption_<eic_suffix>`. If you have solar, add the matching `_production_` stream as "Return to grid".

The integration writes external statistics with proper cumulative-sum semantics and a `last_reset` attribute on the cumulative-since-reset sensor, so HA's Energy dashboard handles resets without flagging them as counter rollbacks.

### Cost & compensation statistics

For electricity meters, the integration also publishes two derived external statistics in EUR:

- `estfeed:<your_name>_cost_<eic_suffix>` — cumulative cost of consumed energy
- `estfeed:<your_name>_compensation_<eic_suffix>` — cumulative compensation for produced energy

Both are computed by multiplying each hour's consumption/production by the matching Nord Pool spot price for the EE bidding zone (fetched from the Elering NPS API), then applying a configurable tariff: `spot × (1 + VAT%/100) + margin`. Defaults: VAT 22 %, margin 0 €/kWh — adjust in the integration options. Prices are denominated in **EUR** (the NPS API's native currency); the statistics are labelled EUR regardless of your Home Assistant currency setting.

To wire them into the Energy dashboard:

1. Open Settings → Dashboards → Energy → "Grid consumption" for the existing `estfeed:<your_name>_consumption_<eic_suffix>` row.
2. Under "Use an entity tracking the total costs", select `estfeed:<your_name>_cost_<eic_suffix>`.
3. Repeat for "Return to grid" → pair `estfeed:<your_name>_production_<eic_suffix>` with `estfeed:<your_name>_compensation_<eic_suffix>`.

Changing VAT or margin in the integration options automatically rebuilds the cost/compensation history over the configured backfill window, so the dashboard reflects the new tariff retroactively.

Gas meters do not publish cost statistics (no spot-price source).

## Entities created

For each metering point:
- `sensor.<name>_consumption_today` (kWh — running total for the current local day; grows as new hourly intervals settle)
- `sensor.<name>_consumption_yesterday` (kWh)
- `sensor.<name>_consumption_month_to_date` (kWh)
- `sensor.<name>_consumption_previous_month` (kWh)
- `sensor.<name>_consumption_cumulative` (kWh — total since the last reset; baseline is captured at install so the sensor starts at 0 and counts forward)
- `sensor.<name>_production_today` / `_yesterday` / `_month_to_date` / `_previous_month` / `_cumulative` (kWh, **disabled by default** — enable in entity registry if you generate)
- `sensor.<name>_latest_interval` (timestamp, diagnostic)
- `binary_sensor.<name>_data_fresh` (diagnostic — `on` if newest interval is < 30 h old)
- `button.<name>_consumption_cumulative_reset` (re-captures the current cumulative as the new baseline, so the cumulative sensor returns to 0; the matching production button exists too and is disabled by default)

## Services

- `estfeed.backfill_history(months=24, entry_id=<uuid>)` — re-fetch and re-publish the last N months of statistics. Rebuilds chain onto the cumulative sum at the window start, so history outside the window stays consistent.
- `estfeed.set_cumulative_reset_at(reset_at=..., entry_id=<uuid>)` — move the cumulative-since-reset baseline to a specific timestamp (e.g. to restore a previous anchor after an accidental reset). Restores both consumption and production baselines.

## Limitations

- Not real-time: hours need to settle before their kWh value is final (see the note at the top).
- Cost/compensation statistics are a spot-price estimate (`spot × (1 + VAT%) + margin`); network fees, renewable levies and time-windowed margins are not modelled. For anything fancier, unpair the cost statistic and use HA's built-in Energy cost configuration instead.
- Cost statistics are denominated in EUR (NPS prices are EUR; no conversion is applied).
- The cumulative-since-reset sensor recomputes from a 62-day rolling cache plus a frozen sum; if Home Assistant is offline for more than ~62 days, consumption from the outage window beyond those 62 days is not recovered into the cumulative total.
- API rate limit: 1 request per 5 seconds (per API key) — handled internally.

## Development

The `editable_mode=compat` flag avoids a setuptools/HA loader incompatibility where the default editable install creates a virtual path entry that HA's `async_get_custom_components` cannot iterate.

~~~bash
pip install -e . --config-settings editable_mode=compat
pip install pytest pytest-asyncio pytest-cov pytest-homeassistant-custom-component homeassistant aioresponses freezegun ruff mypy
pytest tests --cov=custom_components/estfeed
ruff check custom_components tests
mypy
~~~

For a live end-to-end check against your own API key:

~~~bash
ESTFEED_CLIENT_ID=... ESTFEED_CLIENT_SECRET=... python scripts/smoke.py
~~~
