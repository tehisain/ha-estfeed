"""Tests for the Elering gas exchange price client."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

import aiohttp
import pytest
from aioresponses import aioresponses

from custom_components.estfeed.gas_price import EleringGasPriceClient, GasPriceError, gas_day_for

GAS_URL_RE = re.compile(r"^https://dashboard\.elering\.ee/api/gas-trade")


@pytest.fixture
async def session():
    connector = aiohttp.TCPConnector(force_close=True)
    async with aiohttp.ClientSession(connector=connector) as s:
        yield s


def _stub(prices_by_day: dict[date, float | None]) -> dict:
    """Shape of /api/gas-trade: rows stamped at UTC midnight of the gas day."""
    return {
        "success": True,
        "data": {
            "common": [
                {
                    "timestamp": int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp()),
                    "price": price,
                    "quantity": None,
                }
                for d, price in prices_by_day.items()
            ],
            "ee": [],
        },
    }


def test_gas_day_starts_at_0700_tallinn_in_summer_and_winter():
    # Summer (EEST, UTC+3): 07:00 local = 04:00 UTC.
    assert gas_day_for(datetime(2026, 7, 2, 3, tzinfo=UTC)) == date(2026, 7, 1)
    assert gas_day_for(datetime(2026, 7, 2, 4, tzinfo=UTC)) == date(2026, 7, 2)
    # Winter (EET, UTC+2): 07:00 local = 05:00 UTC.
    assert gas_day_for(datetime(2026, 1, 2, 4, tzinfo=UTC)) == date(2026, 1, 1)
    assert gas_day_for(datetime(2026, 1, 2, 5, tzinfo=UTC)) == date(2026, 1, 2)


@pytest.mark.asyncio
async def test_hours_get_their_gas_day_price_in_eur_per_kwh(session):
    client = EleringGasPriceClient(session)
    with aioresponses() as mocked:
        mocked.get(GAS_URL_RE, payload=_stub({date(2026, 7, 1): 46.052, date(2026, 7, 2): 46.648}))
        prices = await client.async_get_prices(
            datetime(2026, 7, 2, 2, tzinfo=UTC), datetime(2026, 7, 2, 6, tzinfo=UTC)
        )
    assert prices == {
        datetime(2026, 7, 2, 2, tzinfo=UTC): pytest.approx(0.046052),
        datetime(2026, 7, 2, 3, tzinfo=UTC): pytest.approx(0.046052),
        datetime(2026, 7, 2, 4, tzinfo=UTC): pytest.approx(0.046648),
        datetime(2026, 7, 2, 5, tzinfo=UTC): pytest.approx(0.046648),
    }


@pytest.mark.asyncio
async def test_unpublished_gas_day_is_omitted_and_retried(session):
    """The newest gas day has no final price yet: its hours are left out, and
    a later call asks again instead of caching the absence."""
    client = EleringGasPriceClient(session)
    start = datetime(2026, 10, 2, 3, tzinfo=UTC)  # gas day Oct 1, last hour
    end = datetime(2026, 10, 2, 6, tzinfo=UTC)  # gas day Oct 2, first two hours
    with aioresponses() as mocked:
        mocked.get(GAS_URL_RE, payload=_stub({date(2026, 10, 1): 74.892, date(2026, 10, 2): None}))
        mocked.get(GAS_URL_RE, payload=_stub({date(2026, 10, 2): 76.041}))
        first = await client.async_get_prices(start, end)
        second = await client.async_get_prices(start, end)
    assert list(first) == [start]
    assert second[datetime(2026, 10, 2, 5, tzinfo=UTC)] == pytest.approx(0.076041)
    assert second[start] == pytest.approx(0.074892)


@pytest.mark.asyncio
async def test_cached_days_do_not_refetch(session):
    client = EleringGasPriceClient(session)
    start, end = datetime(2026, 7, 1, 4, tzinfo=UTC), datetime(2026, 7, 1, 10, tzinfo=UTC)
    with aioresponses() as mocked:
        mocked.get(GAS_URL_RE, payload=_stub({date(2026, 7, 1): 46.052}))
        await client.async_get_prices(start, end)
        # A second identical call must be served from cache (no stub left).
        prices = await client.async_get_prices(start, end)
    assert len(prices) == 6
    assert client.cache_size == 1


@pytest.mark.asyncio
async def test_http_error_raises(session):
    client = EleringGasPriceClient(session)
    with aioresponses() as mocked:
        mocked.get(GAS_URL_RE, status=503)
        with pytest.raises(GasPriceError):
            await client.async_get_prices(
                datetime(2026, 7, 1, 4, tzinfo=UTC), datetime(2026, 7, 1, 5, tzinfo=UTC)
            )
