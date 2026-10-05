"""Elering gas exchange price client.

Wraps the public dashboard endpoint at
``https://dashboard.elering.ee/api/gas-trade``, which republishes the
GET Baltic / EEX NGP daily index for the common Finnish-Baltic balancing zone
(Estonia's gas price). Only final values are published, one per gas day, so
the price for gas day D appears around D+1.

A gas day runs 07:00-07:00 Estonian local time (05:00 UTC in winter, 04:00 UTC
in summer). Prices are returned per top-of-hour UTC so callers can multiply
them against hourly consumption exactly like NPS electricity prices.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp

GAS_PRICE_URL = "https://dashboard.elering.ee/api/gas-trade"
GAS_PRICE_TIMEOUT_SECONDS = 30
# The endpoint returns one row per day, so a year fits comfortably in one call;
# chunking still bounds a multi-year backfill.
GAS_PRICE_MAX_FETCH_DAYS = 366
# Common Finnish-Baltic balancing zone; the per-country "ee" series is empty.
GAS_PRICE_ZONE = "common"
GAS_DAY_START_HOUR = 7
GAS_DAY_TZ = ZoneInfo("Europe/Tallinn")

_LOGGER = logging.getLogger(__name__)


class GasPriceError(Exception):
    """Raised when the gas price endpoint returns a non-200 status or fails."""


def gas_day_for(hour: datetime) -> date:
    """Gas day an hour belongs to: the local date of the 07:00 that started it."""
    local = hour.astimezone(GAS_DAY_TZ)
    return (local - timedelta(hours=GAS_DAY_START_HOUR)).date()


class EleringGasPriceClient:
    """Async client for Elering's public gas exchange price endpoint.

    No auth required. Returns prices in EUR/kWh (converted from the
    endpoint's native EUR/MWh). Final daily prices never change, so they are
    cached for the process lifetime; days not yet published are simply
    re-requested on the next call.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        # Gas day → EUR/kWh
        self._cache: dict[date, float] = {}

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    async def async_get_prices(self, start: datetime, end: datetime) -> dict[datetime, float]:
        """Return hourly gas prices in [start, end) keyed by top-of-hour UTC.

        Hours whose gas day has no published price yet are omitted.
        """
        start = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        end = end.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        if end <= start:
            return {}

        hours: list[datetime] = []
        cursor = start
        while cursor < end:
            hours.append(cursor)
            cursor += timedelta(hours=1)

        missing = sorted({gas_day_for(h) for h in hours} - self._cache.keys())
        if missing:
            await self._fetch_days(missing[0], missing[-1])

        result: dict[datetime, float] = {}
        for hour in hours:
            price = self._cache.get(gas_day_for(hour))
            if price is not None:
                result[hour] = price
        return result

    async def _fetch_days(self, first: date, last: date) -> None:
        """Fetch gas days [first, last] in bounded chunks, merging into the cache."""
        cursor = first
        while cursor <= last:
            chunk_last = min(cursor + timedelta(days=GAS_PRICE_MAX_FETCH_DAYS - 1), last)
            await self._fetch_chunk(cursor, chunk_last)
            cursor = chunk_last + timedelta(days=1)

    async def _fetch_chunk(self, first: date, last: date) -> None:
        # Rows are stamped at UTC midnight of their gas day's date.
        params = {
            "start": f"{first.isoformat()}T00:00:00.000Z",
            "end": f"{(last + timedelta(days=1)).isoformat()}T00:00:00.000Z",
        }
        try:
            async with self._session.get(
                GAS_PRICE_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=GAS_PRICE_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status != 200:
                    raise GasPriceError(f"Gas price endpoint returned status {resp.status}")
                payload = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise GasPriceError(f"Gas price request failed: {err}") from err
        except TimeoutError as err:
            raise GasPriceError(f"Gas price request timed out: {err}") from err

        for row in (payload or {}).get("data", {}).get(GAS_PRICE_ZONE, []):
            ts = row.get("timestamp")
            price_eur_per_mwh = row.get("price")
            if ts is None or price_eur_per_mwh is None:
                continue
            day = datetime.fromtimestamp(int(ts), tz=UTC).date()
            if first <= day <= last:
                self._cache[day] = float(price_eur_per_mwh) / 1000.0
