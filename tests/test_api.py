"""Tests for the Estfeed API client and data types."""

from __future__ import annotations

import asyncio
import time as time_module
from datetime import UTC, datetime

import aiohttp
import pytest
from aioresponses import aioresponses
from freezegun import freeze_time

from custom_components.estfeed.api import (
    EstfeedAPIError,
    EstfeedAuthError,
    EstfeedClient,
    EstfeedRateLimitError,
    EstfeedTimeoutError,
    MeterData,
    MeteringPoint,
    Period,
)
from custom_components.estfeed.const import KEYCLOAK_TOKEN_URL, CommodityType, Resolution


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


async def test_rate_limit_no_sleep_when_no_prior_request(client, monkeypatch):
    """Fresh client (no prior request) makes the call immediately."""
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
        )
        mocked.get("https://estfeed.elering.ee/api/public/v1/x", payload={"ok": True})
        await client._request("GET", "/api/public/v1/x")

    # _last_request_at starts at 0; elapsed since epoch is huge → no positive sleep needed.
    assert all(s <= 0 for s in sleeps)


async def test_rate_limit_sleeps_when_too_soon(client, monkeypatch):
    """A request made right after a prior one must sleep until ≥5s elapsed."""
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    # Pretend a request just happened a moment ago.
    client._last_request_at = time_module.monotonic()

    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
        )
        mocked.get("https://estfeed.elering.ee/api/public/v1/x", payload={"ok": True})
        await client._request("GET", "/api/public/v1/x")

    # Expect a sleep close to 5s (minus the few ms of test overhead since we set _last_request_at).
    assert any(s >= 4.0 for s in sleeps), f"expected a ~5s sleep, got {sleeps}"


async def test_recent_requests_ring_buffer(client, monkeypatch):
    """The ring buffer keeps the most recent N=5 request summaries."""

    # Patch sleep so the test runs fast despite the 5s rate limit.
    async def fake_sleep(_):
        return

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
        )
        for _ in range(7):
            mocked.get("https://estfeed.elering.ee/api/public/v1/x", payload={"ok": True})

        for _ in range(7):
            await client._request("GET", "/api/public/v1/x")

    summaries = list(client.recent_requests)
    assert len(summaries) == 5
    assert all(s["status"] == 200 for s in summaries)
    assert all(s["path"] == "/api/public/v1/x" for s in summaries)


async def test_401_raises_auth_error(client):
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
        )
        mocked.get(
            "https://estfeed.elering.ee/api/public/v1/x",
            status=401,
            payload={"message": "nope"},
        )
        with pytest.raises(EstfeedAuthError):
            await client._request_json("GET", "/api/public/v1/x")


async def test_429_raises_rate_limit(client):
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
        )
        mocked.get("https://estfeed.elering.ee/api/public/v1/x", status=429, payload={})
        with pytest.raises(EstfeedRateLimitError):
            await client._request_json("GET", "/api/public/v1/x")


async def test_500_raises_api_error(client):
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
        )
        mocked.get("https://estfeed.elering.ee/api/public/v1/x", status=500, payload={})
        with pytest.raises(EstfeedAPIError):
            await client._request_json("GET", "/api/public/v1/x")


async def test_non_json_error_body_maps_to_status_exception(client):
    """Regression: 5xx responses with HTML/empty bodies must map by status,
    not crash with JSONDecodeError."""
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
        )
        mocked.get(
            "https://estfeed.elering.ee/api/public/v1/x",
            status=503,
            body="<html><body>Service Unavailable</body></html>",
            content_type="text/html",
        )
        with pytest.raises(EstfeedAPIError):
            await client._request_json("GET", "/api/public/v1/x")


async def test_empty_body_401_maps_to_auth_error(client):
    """Regression: empty response bodies on 401 should map to EstfeedAuthError."""
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
        )
        mocked.get("https://estfeed.elering.ee/api/public/v1/x", status=401, body="")
        with pytest.raises(EstfeedAuthError):
            await client._request_json("GET", "/api/public/v1/x")


async def test_timeout_raises_timeout_error(client):
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
        )
        mocked.get(
            "https://estfeed.elering.ee/api/public/v1/x",
            exception=TimeoutError(),
        )
        with pytest.raises(EstfeedTimeoutError):
            await client._request_json("GET", "/api/public/v1/x")


async def test_list_metering_points(client):
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
        )
        mocked.get(
            "https://estfeed.elering.ee/api/public/v1/metering-point-eics"
            "?startDateTime=2026-04-01T00:00:00Z&endDateTime=2026-04-29T00:00:00Z",
            payload=[
                {
                    "eic": "38ZEE-00720089-N",
                    "commodityType": "ELECTRICITY",
                    "periods": [{"from": "2019-07-27T21:00:00Z"}],
                }
            ],
        )
        result = await client.list_metering_points(
            datetime(2026, 4, 1, tzinfo=UTC),
            datetime(2026, 4, 29, tzinfo=UTC),
        )
        assert len(result) == 1
        assert result[0].eic == "38ZEE-00720089-N"


async def test_get_metering_data_with_eics(client):
    with aioresponses() as mocked:
        mocked.post(
            KEYCLOAK_TOKEN_URL,
            payload={"access_token": "t", "expires_in": 300, "token_type": "Bearer"},
        )
        mocked.get(
            "https://estfeed.elering.ee/api/public/v1/metering-data"
            "?startDateTime=2026-04-27T00:00:00Z&endDateTime=2026-04-28T00:00:00Z"
            "&resolution=one_hour&meteringPointEics=38ZEE-00720089-N",
            payload=[
                {
                    "meteringPointEic": "38ZEE-00720089-N",
                    "accountingIntervals": [
                        {
                            "periodStart": "2026-04-27T00:00:00Z",
                            "consumptionKwh": 0.348,
                            "productionKwh": 0.0,
                        },
                    ],
                }
            ],
        )
        result = await client.get_metering_data(
            datetime(2026, 4, 27, tzinfo=UTC),
            datetime(2026, 4, 28, tzinfo=UTC),
            Resolution.HOUR,
            eics=["38ZEE-00720089-N"],
        )
        assert len(result) == 1
        assert result[0].eic == "38ZEE-00720089-N"
        assert len(result[0].intervals) == 1
        assert result[0].intervals[0].consumption_kwh == 0.348


async def test_get_metering_data_too_many_eics_raises(client):
    with pytest.raises(ValueError, match="up to 10"):
        await client.get_metering_data(
            datetime(2026, 4, 1, tzinfo=UTC),
            datetime(2026, 4, 2, tzinfo=UTC),
            Resolution.HOUR,
            eics=[f"X{i}" for i in range(11)],
        )
