"""Tests for the Estfeed API client and data types."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import aiohttp
import pytest
from aioresponses import aioresponses
from freezegun import freeze_time

from custom_components.estfeed.api import (
    EstfeedClient,
    MeterData,
    MeteringPoint,
    Period,
)
from custom_components.estfeed.const import KEYCLOAK_TOKEN_URL, CommodityType


def test_metering_point_from_dict():
    raw = {
        "eic": "38ZEE-00720089-N",
        "commodityType": "ELECTRICITY",
        "periods": [{"from": "2019-07-27T21:00:00Z"}],
    }
    mp = MeteringPoint.from_dict(raw)
    assert mp.eic == "38ZEE-00720089-N"
    assert mp.commodity_type == CommodityType.ELECTRICITY
    assert mp.periods == [Period(start=datetime(2019, 7, 27, 21, 0, tzinfo=UTC), end=None)]


def test_metering_point_from_dict_with_period_end():
    raw = {
        "eic": "X",
        "commodityType": "NATURAL_GAS",
        "periods": [{"from": "2020-01-01T00:00:00Z", "to": "2024-01-01T00:00:00Z"}],
    }
    mp = MeteringPoint.from_dict(raw)
    assert mp.periods[0].end == datetime(2024, 1, 1, 0, 0, tzinfo=UTC)


def test_meter_data_from_dict():
    raw = {
        "meteringPointEic": "38ZEE-00720089-N",
        "accountingIntervals": [
            {"periodStart": "2026-04-27T00:00:00Z", "consumptionKwh": 0.348, "productionKwh": 0.0},
            {"periodStart": "2026-04-27T01:00:00Z", "consumptionKwh": 0.338, "productionKwh": 0.0},
        ],
    }
    md = MeterData.from_dict(raw)
    assert md.eic == "38ZEE-00720089-N"
    assert md.error is None
    assert len(md.intervals) == 2
    assert md.intervals[0].period_start == datetime(2026, 4, 27, 0, 0, tzinfo=UTC)
    assert md.intervals[0].consumption_kwh == 0.348
    assert md.intervals[0].production_kwh == 0.0


def test_meter_data_with_per_meter_error():
    raw = {
        "meteringPointEic": "X",
        "accountingIntervals": [],
        "error": {
            "id": "abc",
            "message": "boom",
            "code": "error.boom",
            "traceId": "t1",
            "args": [],
        },
    }
    md = MeterData.from_dict(raw)
    assert md.error is not None
    assert md.error.code == "error.boom"


@pytest.fixture
async def session():
    connector = aiohttp.TCPConnector(force_close=True)
    async with aiohttp.ClientSession(connector=connector) as s:
        yield s


@pytest.fixture
def client(session):
    return EstfeedClient(session, client_id="cid", client_secret="csec")


async def test_token_fetched_and_cached(client):
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "tok-1", "expires_in": 300, "token_type": "Bearer"},
        )
        token1 = await client._ensure_token()
        token2 = await client._ensure_token()
        assert token1 == "tok-1"
        assert token2 == "tok-1"  # cached, no second POST
        # aioresponses raises if a mocked URL is requested more times than registered
        # (it's registered once); reaching this line proves the second call did not POST.


async def test_token_refresh_before_expiry(client):
    """Token must be refreshed when within the safety margin of expiry."""
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "tok-1", "expires_in": 300, "token_type": "Bearer"},
        )
        with freeze_time("2026-04-29T00:00:00Z") as frozen:
            await client._ensure_token()
            # advance to within 30s of expiry: 300 - 30 = 270s in
            frozen.tick(delta=271)
            mocked.post(
                KEYCLOAK_TOKEN_URL,
                payload={"access_token": "tok-2", "expires_in": 300, "token_type": "Bearer"},
            )
            token = await client._ensure_token()
            assert token == "tok-2"


async def test_concurrent_token_fetch_serialised(client):
    """Ten concurrent _ensure_token calls trigger only one HTTP POST."""
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "tok-1", "expires_in": 300, "token_type": "Bearer"},
        )
        results = await asyncio.gather(*[client._ensure_token() for _ in range(10)])
        assert all(r == "tok-1" for r in results)
        # Only one mock was registered — if more than one POST happened, aioresponses
        # would raise on the unmatched extras.
