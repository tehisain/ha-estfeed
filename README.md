# Estfeed — Home Assistant Integration

Home Assistant integration for Elering's [Estfeed](https://estfeed.elering.ee/) metering data API. Brings Estonian electricity (and gas) meter data into the **Energy Dashboard** with full historical backfill, plus lagging summary sensors for cards and automations.

> **Note:** Estfeed data is settled overnight and arrives ~24 hours late. This integration is built around that — it is not real-time.

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

## Entities created

For each metering point:
- `sensor.<name>_consumption_yesterday` (kWh)
- `sensor.<name>_consumption_month_to_date` (kWh)
- `sensor.<name>_consumption_previous_month` (kWh)
- `sensor.<name>_production_yesterday` / `_month_to_date` / `_previous_month` (kWh, **disabled by default** — enable in entity registry if you generate)
- `sensor.<name>_latest_interval` (timestamp, diagnostic)
- `binary_sensor.<name>_data_fresh` (diagnostic — `on` if newest interval is < 30 h old)

## Services

- `estfeed.backfill_history(months=24, entry_id=<uuid>)` — re-fetch and re-publish the last N months of statistics. Idempotent.

## Limitations

- Data is delayed ~24 hours.
- Cost calculation is intentionally not included; use HA's built-in Energy Dashboard cost configuration with a price entity.
- API rate limit: 1 request per 5 seconds (per API key) — handled internally.
