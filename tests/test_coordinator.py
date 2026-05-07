"""Tests for EstfeedCoordinator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.estfeed.api import (
    AccountingInterval,
    MeterData,
    MeteringPoint,
    Period,
)
from custom_components.estfeed.const import (
    CONF_BACKFILL_MONTHS,
    CONF_RESOLUTION,
    CommodityType,
    Kind,
    Resolution,
)
from custom_components.estfeed.coordinator import EstfeedCoordinator


def _make_meter(eic: str = "38ZEE-00720089-N") -> MeteringPoint:
    return MeteringPoint(
        eic=eic,
        commodity_type=CommodityType.ELECTRICITY,
        periods=[Period(start=datetime(2019, 7, 27, 21, tzinfo=UTC), end=None)],
    )


def _hourly(start: datetime, hours: int, kwh: float = 0.5) -> list[AccountingInterval]:
    return [
        AccountingInterval(
            period_start=start + timedelta(hours=h),
            consumption_kwh=kwh,
            production_kwh=0.0,
            consumption_m3=None,
            production_m3=None,
        )
        for h in range(hours)
    ]


def _fake_recorder():
    """Stand-in for ``get_instance(hass)`` whose executor calls funcs directly.

    The real ``get_instance`` looks up the recorder from ``hass.data``; tests
    bypass the recorder entirely by patching the symbol so the executor just
    invokes the callable inline.
    """
    recorder = MagicMock()

    async def _exec(func, *args, **kwargs):
        return func(*args, **kwargs)

    recorder.async_add_executor_job = _exec
    return recorder


@pytest.mark.asyncio
async def test_coordinator_first_update_fetches_and_writes(hass):
    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[_make_meter()])
    client.get_metering_data = AsyncMock(
        return_value=[
            MeterData(
                eic="38ZEE-00720089-N",
                intervals=_hourly(datetime(2026, 4, 28, 0, tzinfo=UTC), 24),
            )
        ]
    )

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={CONF_RESOLUTION: Resolution.HOUR.value, CONF_BACKFILL_MONTHS: 12},
    )
    coordinator.meters = [_make_meter()]

    with (
        patch(
            "custom_components.estfeed.coordinator.get_instance",
            return_value=_fake_recorder(),
        ),
        patch(
            "custom_components.estfeed.coordinator.get_last_statistics",
            new=MagicMock(return_value={}),
        ),
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=AsyncMock(),
        ) as mock_write,
    ):
        await coordinator._async_update_data()

    # One call per (eic, kind) pair = 2 (consumption + production)
    assert mock_write.call_count == 2


@pytest.mark.asyncio
async def test_coordinator_uses_latest_seen_as_start(hass):
    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[_make_meter()])
    client.get_metering_data = AsyncMock(
        return_value=[MeterData(eic="38ZEE-00720089-N", intervals=[])]
    )

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={CONF_RESOLUTION: Resolution.HOUR.value, CONF_BACKFILL_MONTHS: 12},
    )
    coordinator.meters = [_make_meter()]

    last_seen_ts = datetime(2026, 4, 28, 23, tzinfo=UTC).timestamp()
    fake_last_stats = {
        "estfeed:home_consumption_089n": [{"end": last_seen_ts * 1000}],  # ms
    }

    with (
        patch(
            "custom_components.estfeed.coordinator.get_instance",
            return_value=_fake_recorder(),
        ),
        patch(
            "custom_components.estfeed.coordinator.get_last_statistics",
            new=MagicMock(return_value=fake_last_stats),
        ),
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=AsyncMock(),
        ),
    ):
        await coordinator._async_update_data()

    # First call should request from latest_seen onwards (NOT +1h). The
    # stored row's `end` equals the next bucket's `start`, so adding an
    # extra hour would skip a bucket.
    args, _ = client.get_metering_data.call_args
    assert args[0] == datetime(2026, 4, 28, 23, tzinfo=UTC)


@pytest.mark.asyncio
async def test_coordinator_per_meter_error_is_skipped(hass):
    """A meter returning an `error` field is skipped without crashing the tick."""
    from custom_components.estfeed.api import MeterError

    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[_make_meter()])
    client.get_metering_data = AsyncMock(
        return_value=[
            MeterData(
                eic="38ZEE-00720089-N",
                intervals=[],
                error=MeterError(id="x", message="m", code="c", trace_id="t", args=[]),
            )
        ]
    )

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={CONF_RESOLUTION: Resolution.HOUR.value, CONF_BACKFILL_MONTHS: 12},
    )
    coordinator.meters = [_make_meter()]

    with (
        patch(
            "custom_components.estfeed.coordinator.get_instance",
            return_value=_fake_recorder(),
        ),
        patch(
            "custom_components.estfeed.coordinator.get_last_statistics",
            new=MagicMock(return_value={}),
        ),
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=AsyncMock(),
        ) as mock_write,
    ):
        await coordinator._async_update_data()

    mock_write.assert_not_called()


@pytest.mark.asyncio
async def test_coordinator_clears_stale_error_on_success(hass):
    """A successful MeterData should clear any prior error code for that EIC (M8)."""
    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[_make_meter()])
    client.get_metering_data = AsyncMock(
        return_value=[
            MeterData(
                eic="38ZEE-00720089-N",
                intervals=_hourly(datetime(2026, 4, 28, 0, tzinfo=UTC), 3),
            )
        ]
    )

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={CONF_RESOLUTION: Resolution.HOUR.value, CONF_BACKFILL_MONTHS: 12},
    )
    coordinator.meters = [_make_meter()]
    # Pre-populate stale error state from a prior failed tick.
    coordinator.last_meter_errors["38ZEE-00720089-N"] = "OLD_CODE"

    with (
        patch(
            "custom_components.estfeed.coordinator.get_instance",
            return_value=_fake_recorder(),
        ),
        patch(
            "custom_components.estfeed.coordinator.get_last_statistics",
            new=MagicMock(return_value={}),
        ),
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=AsyncMock(return_value=0.0),
        ),
    ):
        await coordinator._async_update_data()

    assert "38ZEE-00720089-N" not in coordinator.last_meter_errors


@pytest.mark.asyncio
async def test_coordinator_carries_prior_sum_across_chunks(hass):
    """prior_sum must be tracked locally across chunks within a single fetch.

    Re-reading get_last_statistics per chunk would race with HA's recorder
    flush. The coordinator should read prior_sum once and advance it from the
    return value of async_write_meter_statistics.
    """
    meter = _make_meter()
    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[meter])
    # Two chunks of 31 days each → triggers the multi-chunk loop.
    client.get_metering_data = AsyncMock(
        return_value=[
            MeterData(
                eic="38ZEE-00720089-N",
                intervals=_hourly(datetime(2026, 1, 1, 0, tzinfo=UTC), 24),
            )
        ]
    )

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={CONF_RESOLUTION: Resolution.HOUR.value, CONF_BACKFILL_MONTHS: 12},
    )
    coordinator.meters = [meter]

    # Force a multi-chunk window: 70 days back → ~3 chunks of 31 days.
    start = datetime(2026, 1, 1, 0, tzinfo=UTC)
    end = datetime(2026, 3, 12, 0, tzinfo=UTC)
    # async_write_meter_statistics returns running sum; simulate +12.0 per call.
    write_returns = [12.0, 24.0, 36.0]
    write_mock = AsyncMock(side_effect=write_returns * 4)  # plenty for both kinds
    prior_mock = AsyncMock(return_value=5.0)

    with (
        patch(
            "custom_components.estfeed.coordinator.get_instance",
            return_value=_fake_recorder(),
        ),
        patch(
            "custom_components.estfeed.coordinator.get_last_statistics",
            new=MagicMock(return_value={}),
        ),
        patch.object(coordinator, "_prior_sum_for_stream", new=prior_mock),
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=write_mock,
        ),
    ):
        await coordinator._fetch_window(start, end, write_stats=True, force_start=True)

    # _prior_sum_for_stream must be called exactly once per stream (2 streams),
    # NOT once per chunk. Three chunks would otherwise multiply this.
    assert prior_mock.call_count == 2
    # First write per stream uses prior_sum=5.0 (the read-once value).
    first_calls = write_mock.call_args_list[:2]
    for call in first_calls:
        assert call.kwargs["prior_sum"] == 5.0
    # Subsequent writes for the same stream should chain off the returned value
    # (12.0), not re-fetch from get_last_statistics.
    third_call = write_mock.call_args_list[2]
    assert third_call.kwargs["prior_sum"] == 12.0


@pytest.mark.asyncio
async def test_initial_backfill_uses_backfill_months(hass):
    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[_make_meter()])
    client.get_metering_data = AsyncMock(
        return_value=[MeterData(eic="38ZEE-00720089-N", intervals=[])]
    )

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={CONF_RESOLUTION: Resolution.HOUR.value, CONF_BACKFILL_MONTHS: 12},
    )
    coordinator.meters = [_make_meter()]

    with (
        patch(
            "custom_components.estfeed.coordinator.get_instance",
            return_value=_fake_recorder(),
        ),
        patch(
            "custom_components.estfeed.coordinator.get_last_statistics",
            new=MagicMock(return_value={}),
        ),
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=AsyncMock(),
        ),
    ):
        await coordinator.async_initial_backfill()

    # First fetch starts ~12 months back. Allow some tolerance.
    args = client.get_metering_data.call_args_list[0].args
    assert args[0] < datetime.now(tz=UTC) - timedelta(days=350)


@pytest.mark.asyncio
async def test_cache_warmup_populates_rolling_cache(hass):
    intervals = _hourly(datetime.now(tz=UTC) - timedelta(days=30), 24 * 5)
    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[_make_meter()])
    client.get_metering_data = AsyncMock(
        return_value=[MeterData(eic="38ZEE-00720089-N", intervals=intervals)]
    )

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={CONF_RESOLUTION: Resolution.HOUR.value, CONF_BACKFILL_MONTHS: 12},
    )
    coordinator.meters = [_make_meter()]

    with (
        patch(
            "custom_components.estfeed.coordinator.get_instance",
            return_value=_fake_recorder(),
        ),
        patch(
            "custom_components.estfeed.coordinator.get_last_statistics",
            new=MagicMock(return_value={}),
        ),
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=AsyncMock(),
        ),
    ):
        await coordinator.async_warm_cache()

    cached = coordinator.cache[("38ZEE-00720089-N", Kind.CONSUMPTION)]
    # The mock returns the same intervals for every chunk; the warmup window is split
    # into 31-day chunks so the cache gets called multiple times. _update_cache must
    # dedupe, so the bucket holds at most the unique intervals once.
    assert 0 < len(cached) <= len(intervals)
    assert len(coordinator.cache[("38ZEE-00720089-N", Kind.PRODUCTION)]) <= len(intervals)


def test_update_cache_dedupes_overlapping_writes(hass):
    """Regression: backfill chunks overlap with first_refresh's window. Without
    dedup the bucket double-counts the overlap and lagging-period sensors
    inflate (observed: 620 kWh for a month whose true total was 228 kWh)."""
    coordinator = EstfeedCoordinator(
        hass=hass,
        client=MagicMock(),
        slug="home",
        options={},
    )
    eic = "38ZEE-00720089-N"
    base = datetime(2026, 4, 7, 11, tzinfo=UTC)  # mimics first_refresh start
    first_refresh_data = _hourly(base, 24 * 30)  # 30 days, Apr 7 - May 7
    coordinator._update_cache(eic, Kind.CONSUMPTION, first_refresh_data)
    assert len(coordinator.cache[(eic, Kind.CONSUMPTION)]) == 24 * 30

    # Backfill chunk overlapping the start of first_refresh's window: Apr 1 - Apr 18.
    # Apr 7 - Apr 18 overlaps; only Apr 1 - Apr 7 (144 hours) is genuinely new.
    overlap_start = datetime(2026, 4, 1, 0, tzinfo=UTC)
    backfill_chunk = _hourly(overlap_start, 24 * 17)
    coordinator._update_cache(eic, Kind.CONSUMPTION, backfill_chunk)

    bucket = coordinator.cache[(eic, Kind.CONSUMPTION)]
    starts = [i.period_start for i in bucket]
    # No duplicates — bucket holds each unique period_start at most once.
    assert len(starts) == len(set(starts))
    # Bucket is sorted ascending so the trim-from-left loop works.
    assert starts == sorted(starts)
    # 720 (first write) + 408 (second write) - 253 overlap = 875 unique hours.
    assert len(bucket) == 875


def test_update_cache_trim_works_after_unsorted_appends(hass):
    """Regression: the trim loop only pops while bucket[0] is older than the
    62-day cutoff. If older intervals are appended behind newer ones (as
    backfill does after first_refresh seeded the bucket) the bucket becomes
    unsorted and the trim silently leaves stale data behind. _update_cache
    must resort before trimming."""
    coordinator = EstfeedCoordinator(
        hass=hass,
        client=MagicMock(),
        slug="home",
        options={},
    )
    eic = "38ZEE-00720089-N"
    now = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    # First write: a recent slice that the trim should keep.
    recent = _hourly(now - timedelta(days=10), 24)
    coordinator._update_cache(eic, Kind.CONSUMPTION, recent)
    # Second write: very old data, far older than ROLLING_CACHE_DAYS=62.
    stale = _hourly(now - timedelta(days=200), 24)
    coordinator._update_cache(eic, Kind.CONSUMPTION, stale)

    bucket = coordinator.cache[(eic, Kind.CONSUMPTION)]
    # All stale (>62d) intervals must be gone; only the recent slice remains.
    assert len(bucket) == 24
    cutoff = datetime.now(tz=UTC) - timedelta(days=62)
    assert all(i.period_start >= cutoff for i in bucket)


@pytest.mark.asyncio
async def test_async_setup_entry_creates_coordinator_and_meters(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.estfeed import async_setup_entry, async_unload_entry
    from custom_components.estfeed.const import (
        CONF_CLIENT_ID,
        CONF_CLIENT_SECRET,
        CONF_FRIENDLY_NAME,
        DOMAIN,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "c", CONF_CLIENT_SECRET: "s", CONF_FRIENDLY_NAME: "Home"},
        options={},
        unique_id="c",
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.estfeed.EstfeedClient.list_metering_points",
            new=AsyncMock(return_value=[_make_meter()]),
        ),
        patch(
            "custom_components.estfeed.get_instance",
            return_value=_fake_recorder(),
        ),
        patch(
            "custom_components.estfeed.get_last_statistics",
            new=MagicMock(return_value={}),
        ),
        patch(
            "custom_components.estfeed.EstfeedCoordinator.async_initial_backfill",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.estfeed.EstfeedCoordinator.async_warm_cache",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.estfeed.EstfeedCoordinator.async_config_entry_first_refresh",
            new=AsyncMock(),
        ),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new=AsyncMock(return_value=True)
        ),
        patch.object(
            hass.config_entries, "async_unload_platforms", new=AsyncMock(return_value=True)
        ),
    ):
        assert await async_setup_entry(hass, entry)
        coord = hass.data[DOMAIN][entry.entry_id]
        assert coord.slug == "home"
        assert len(coord.meters) == 1

        assert await async_unload_entry(hass, entry)
        assert entry.entry_id not in hass.data[DOMAIN]


@pytest.mark.asyncio
async def test_coordinator_filters_intervals_per_stream(hass):
    """Regression: when production lags consumption (e.g., a consume-only
    meter that reported a non-null production value once long ago), the
    leading consumption stream must NOT receive intervals before its own
    latest_seen. Otherwise its historical rows get rewritten with prior_sum
    chained from the *current* latest sum, inflating past-month consumption
    every tick.
    """
    meter = _make_meter()
    consumption_seen = datetime(2026, 5, 1, 0, tzinfo=UTC)
    production_seen = datetime(2026, 4, 25, 0, tzinfo=UTC)
    fetch_start = datetime(2026, 4, 1, 0, tzinfo=UTC)
    fetch_end = datetime(2026, 5, 2, 0, tzinfo=UTC)

    # API returns 7 days of hourly intervals starting at production_seen,
    # covering the gap that production needs to backfill plus the new hours
    # consumption needs.
    intervals = _hourly(production_seen, hours=24 * 7)
    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[meter])
    client.get_metering_data = AsyncMock(
        return_value=[MeterData(eic="38ZEE-00720089-N", intervals=intervals)]
    )

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={CONF_RESOLUTION: Resolution.HOUR.value, CONF_BACKFILL_MONTHS: 12},
    )
    coordinator.meters = [meter]

    async def fake_latest_seen(stream):
        return consumption_seen if stream.kind == Kind.CONSUMPTION else production_seen

    write_mock = AsyncMock(return_value=0.0)

    with (
        patch(
            "custom_components.estfeed.coordinator.get_instance",
            return_value=_fake_recorder(),
        ),
        patch(
            "custom_components.estfeed.coordinator.get_last_statistics",
            new=MagicMock(return_value={}),
        ),
        patch.object(coordinator, "_latest_seen_for_stream", new=fake_latest_seen),
        patch.object(coordinator, "_prior_sum_for_stream", new=AsyncMock(return_value=0.0)),
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=write_mock,
        ),
    ):
        await coordinator._fetch_window(fetch_start, fetch_end, write_stats=True, force_start=False)

    # API fetch starts at the *earliest* seen (production_seen) so the lagging
    # stream can backfill its gap.
    assert client.get_metering_data.call_args.args[0] == production_seen

    consumption_calls = [c for c in write_mock.call_args_list if c.args[1].kind == Kind.CONSUMPTION]
    production_calls = [c for c in write_mock.call_args_list if c.args[1].kind == Kind.PRODUCTION]
    assert consumption_calls and production_calls

    # Consumption: every interval handed to the writer must be >= consumption_seen.
    # If anything earlier slipped through, the writer would chain prior_sum
    # (= current consumption sum) onto an already-recorded bucket, inflating it.
    for call in consumption_calls:
        for ival in call.args[2]:
            assert ival.period_start >= consumption_seen, (
                f"consumption received {ival.period_start} < {consumption_seen} — "
                "this would re-write a historical bucket with an inflated sum"
            )

    # Production: must receive intervals from production_seen onwards, including
    # ones older than consumption_seen (the gap to backfill).
    for call in production_calls:
        for ival in call.args[2]:
            assert ival.period_start >= production_seen
    assert any(
        any(i.period_start < consumption_seen for i in c.args[2]) for c in production_calls
    ), "production should backfill the gap between production_seen and consumption_seen"


@pytest.mark.asyncio
async def test_coordinator_fetch_start_bounded_by_caller_start(hass):
    """Regression: when a stream is stuck far in the past (e.g., a
    consume-only meter with a single non-null production reading from
    12 months ago), the regular tick must NOT fetch from that stuck
    point. Fetching ~12 months of intervals every hour exceeds HA's
    bootstrap stage-2 timeout (300s). The fetch is bounded by the
    caller's `start`; deep history is reserved for force_start callers
    (initial backfill, warm cache, the backfill service).
    """
    meter = _make_meter()
    consumption_seen = datetime(2026, 5, 5, 0, tzinfo=UTC)
    stale_production_seen = datetime(2025, 5, 5, 0, tzinfo=UTC)  # 12 months stale
    fetch_start = datetime(2026, 4, 5, 0, tzinfo=UTC)  # caller window: 30 days
    fetch_end = datetime(2026, 5, 5, 0, tzinfo=UTC)

    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[meter])
    client.get_metering_data = AsyncMock(
        return_value=[MeterData(eic="38ZEE-00720089-N", intervals=[])]
    )

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={CONF_RESOLUTION: Resolution.HOUR.value, CONF_BACKFILL_MONTHS: 12},
    )
    coordinator.meters = [meter]

    async def fake_latest_seen(stream):
        return consumption_seen if stream.kind == Kind.CONSUMPTION else stale_production_seen

    with (
        patch(
            "custom_components.estfeed.coordinator.get_instance",
            return_value=_fake_recorder(),
        ),
        patch(
            "custom_components.estfeed.coordinator.get_last_statistics",
            new=MagicMock(return_value={}),
        ),
        patch.object(coordinator, "_latest_seen_for_stream", new=fake_latest_seen),
        patch.object(coordinator, "_prior_sum_for_stream", new=AsyncMock(return_value=0.0)),
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=AsyncMock(return_value=0.0),
        ),
    ):
        await coordinator._fetch_window(fetch_start, fetch_end, write_stats=True, force_start=False)

    # API fetch must be clamped to the caller's `fetch_start`, not the
    # 12-month-stale production_seen. A 12-month per-tick fetch would
    # blow HA's bootstrap timeout.
    assert client.get_metering_data.call_args.args[0] == fetch_start


@pytest.mark.asyncio
async def test_coordinator_force_start_ignores_bound(hass):
    """force_start callers (initial backfill, warm cache, manual service)
    must still fetch from `start` regardless of any existing latest_seen.
    The bound only protects regular ticks."""
    meter = _make_meter()
    fetch_start = datetime(2025, 5, 5, 0, tzinfo=UTC)  # 12 months back
    fetch_end = datetime(2026, 5, 5, 0, tzinfo=UTC)

    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[meter])
    client.get_metering_data = AsyncMock(
        return_value=[MeterData(eic="38ZEE-00720089-N", intervals=[])]
    )

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={CONF_RESOLUTION: Resolution.HOUR.value, CONF_BACKFILL_MONTHS: 12},
    )
    coordinator.meters = [meter]

    # Even with a recent latest_seen, force_start must override and pull
    # from the requested start.
    recent_seen = datetime(2026, 5, 4, 23, tzinfo=UTC)

    async def fake_latest_seen(_stream):
        return recent_seen

    with (
        patch(
            "custom_components.estfeed.coordinator.get_instance",
            return_value=_fake_recorder(),
        ),
        patch(
            "custom_components.estfeed.coordinator.get_last_statistics",
            new=MagicMock(return_value={}),
        ),
        patch.object(coordinator, "_latest_seen_for_stream", new=fake_latest_seen),
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=AsyncMock(return_value=0.0),
        ),
    ):
        await coordinator._fetch_window(fetch_start, fetch_end, write_stats=True, force_start=True)

    # force_start: fetch begins at the caller's `start`, not at recent_seen.
    assert client.get_metering_data.call_args_list[0].args[0] == fetch_start


@pytest.mark.asyncio
async def test_coordinator_snaps_request_start_to_top_of_hour(hass):
    """Regression: API anchors hourly intervals to the requested start.
    If we send a non-aligned timestamp, intervals come back at HH:32:13
    and HA's recorder rejects them. Coordinator must snap before fetching."""
    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[_make_meter()])
    client.get_metering_data = AsyncMock(
        return_value=[MeterData(eic="38ZEE-00720089-N", intervals=[])]
    )

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={CONF_RESOLUTION: Resolution.HOUR.value, CONF_BACKFILL_MONTHS: 12},
    )
    coordinator.meters = [_make_meter()]

    with (
        patch(
            "custom_components.estfeed.coordinator.get_instance",
            return_value=_fake_recorder(),
        ),
        patch(
            "custom_components.estfeed.coordinator.get_last_statistics",
            new=MagicMock(return_value={}),
        ),
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=AsyncMock(),
        ),
    ):
        await coordinator._async_update_data()

    # Every call to get_metering_data must use a top-of-hour start.
    assert client.get_metering_data.call_args_list, "client should have been called"
    for call in client.get_metering_data.call_args_list:
        start = call.args[0]
        assert start.minute == 0 and start.second == 0 and start.microsecond == 0, (
            f"non-aligned start {start!r}"
        )
