"""Tests for EstfeedCoordinator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.estfeed.api import (
    AccountingInterval,
    EstfeedAuthError,
    MeterData,
    MeteringPoint,
    Period,
)
from custom_components.estfeed.const import (
    CONF_BACKFILL_MONTHS,
    CONF_MARGIN_EUR_PER_KWH,
    CONF_RESOLUTION,
    CONF_VAT_PERCENT,
    CommodityType,
    Kind,
    Resolution,
)
from custom_components.estfeed.coordinator import CumulativeBaseline, EstfeedCoordinator
from custom_components.estfeed.statistics import CostStream


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
async def test_coordinator_uses_latest_seen_as_start(hass, freezer):
    # Freeze inside the 30-day default window of the mocked latest_seen so
    # the test does not rot as wall-clock time passes.
    freezer.move_to("2026-05-05 12:00:00+00:00")
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
async def test_coordinator_chains_prior_sum_across_chunks_on_regular_tick(hass):
    """Regular tick (force_start=False) reads prior_sum once per stream and
    chains the return value of async_write_meter_statistics across chunks.

    Re-reading get_last_statistics per chunk would race with HA's recorder
    flush — short-term writes may not be visible yet, producing stale priors.
    """
    meter = _make_meter()
    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[meter])
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

    start = datetime(2026, 1, 1, 0, tzinfo=UTC)
    end = datetime(2026, 3, 12, 0, tzinfo=UTC)
    write_mock = AsyncMock(side_effect=[12.0, 24.0, 36.0] * 4)
    prior_mock = AsyncMock(return_value=5.0)
    # No prior intervals so fetch_start = start (matches old test geometry).
    latest_seen_mock = AsyncMock(return_value=None)

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
        patch.object(coordinator, "_latest_seen_for_stream", new=latest_seen_mock),
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=write_mock,
        ),
    ):
        await coordinator._fetch_window(start, end, write_stats=True, force_start=False)

    # _prior_sum_for_stream must be called exactly once per stream (2 streams),
    # NOT once per chunk. Three chunks would otherwise multiply this.
    assert prior_mock.call_count == 2
    # First write per stream uses prior_sum=5.0 (the read-once value).
    first_calls = write_mock.call_args_list[:2]
    for call in first_calls:
        assert call.kwargs["prior_sum"] == 5.0
    # Subsequent writes for the same stream chain off the returned value (12.0),
    # not a re-fetch from get_last_statistics.
    third_call = write_mock.call_args_list[2]
    assert third_call.kwargs["prior_sum"] == 12.0


@pytest.mark.asyncio
async def test_force_start_seeds_prior_sum_from_before_window(hass):
    """force_start=True (initial backfill, manual rebuild service) must seed
    prior_sum from the cumulative sum the series had just BEFORE the window
    start — NOT from the current latest sum (that was the original inflation
    bug: every backfilled hour got offset by whatever the cumulative happens
    to be right now), and NOT hardcoded 0.0 (that creates a mid-series sum
    drop the Energy dashboard reads as a counter rollback when the rebuild
    window is shorter than the existing history).
    """
    meter = _make_meter()
    client = MagicMock()
    client.list_metering_points = AsyncMock(return_value=[meter])
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

    start = datetime(2026, 1, 1, 0, tzinfo=UTC)
    end = datetime(2026, 3, 12, 0, tzinfo=UTC)
    write_mock = AsyncMock(side_effect=[12.0, 24.0, 36.0] * 4)
    # 999.0 simulates the *current* cumulative — the old bug chained off this.
    prior_mock = AsyncMock(return_value=999.0)
    # 7.0 simulates a pre-window row (e.g. older history outside the rebuild).
    sum_before_mock = AsyncMock(return_value=7.0)

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
        patch.object(coordinator, "_sum_before_window", new=sum_before_mock),
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=write_mock,
        ),
    ):
        await coordinator._fetch_window(start, end, write_stats=True, force_start=True)

    # The expensive latest-sum read must not be used on force_start.
    assert prior_mock.call_count == 0
    # First write per stream uses prior_sum=7.0 (seeded from before the window).
    for call in write_mock.call_args_list[:2]:
        assert call.kwargs["prior_sum"] == 7.0
    # Subsequent writes still chain via the returned running sum.
    assert write_mock.call_args_list[2].kwargs["prior_sum"] == 12.0


@pytest.mark.asyncio
async def test_force_start_seeds_zero_when_no_prior_history(hass):
    """Fresh install: nothing exists before the window, so the seed is 0.0
    (delegates to _sum_before_window, which returns 0.0 for empty stats)."""
    meter = _make_meter()
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

    sum_before_mock = AsyncMock(return_value=0.0)

    with (
        patch(
            "custom_components.estfeed.coordinator.get_instance",
            return_value=_fake_recorder(),
        ),
        patch(
            "custom_components.estfeed.coordinator.get_last_statistics",
            new=MagicMock(return_value={}),
        ),
        patch.object(coordinator, "_sum_before_window", new=sum_before_mock),
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=AsyncMock(),
        ),
    ):
        await coordinator._fetch_window(
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 3, 12, tzinfo=UTC),
            write_stats=True,
            force_start=True,
        )

    assert sum_before_mock.call_count == 2  # once per energy stream


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
        patch.object(coordinator, "_sum_before_window", new=AsyncMock(return_value=0.0)),
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


@pytest.mark.asyncio
async def test_warm_cache_notifies_listeners(hass):
    """Regression: lagging sensors compute from the cache, so warm_cache and
    initial_backfill must push to coordinator listeners after the background
    fill completes. Without this, CoordinatorEntity state stays frozen at
    whatever value was computed against the half-populated cache during
    first_refresh and never picks up the historical data."""
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
    listener = MagicMock()
    coordinator.async_add_listener(listener)
    with (
        patch(
            "custom_components.estfeed.coordinator.get_instance",
            return_value=_fake_recorder(),
        ),
        patch(
            "custom_components.estfeed.coordinator.get_last_statistics",
            new=MagicMock(return_value={}),
        ),
    ):
        await coordinator.async_warm_cache()
    listener.assert_called()


@pytest.mark.asyncio
async def test_initial_backfill_notifies_listeners(hass):
    """Same regression as warm_cache, applied to the initial-install path."""
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
    listener = MagicMock()
    coordinator.async_add_listener(listener)
    with (
        patch(
            "custom_components.estfeed.coordinator.get_instance",
            return_value=_fake_recorder(),
        ),
        patch(
            "custom_components.estfeed.coordinator.get_last_statistics",
            new=MagicMock(return_value={}),
        ),
        patch.object(coordinator, "_sum_before_window", new=AsyncMock(return_value=0.0)),
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=AsyncMock(),
        ),
    ):
        await coordinator.async_initial_backfill()
    listener.assert_called()


def test_update_cache_dedupes_overlapping_writes(hass, freezer):
    """Regression: backfill chunks overlap with first_refresh's window. Without
    dedup the bucket double-counts the overlap and lagging-period sensors
    inflate (observed: 620 kWh for a month whose true total was 228 kWh)."""
    # Freeze shortly after the fixture dates so they stay inside the 62-day
    # rolling cache window regardless of when the test runs.
    freezer.move_to("2026-05-10 00:00:00+00:00")
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


def test_update_cache_replaces_null_with_later_value(hass, freezer):
    """Regression: Estfeed returns recent intervals with consumption_kwh=None
    while the hour is still being settled (the API surfaces the row before the
    value lands). A later fetch returns the same period_start with a real
    value. The cache must adopt the newer non-null value, not keep the stale
    null — otherwise today/yesterday sums silently lose hours as they age.
    Observed live as today=0.374 / yesterday=6.383 while recorder stats had
    the same hours at ~0.4 kWh each totalling 11.5 kWh.
    """
    freezer.move_to("2026-05-20 00:00:00+00:00")
    coordinator = EstfeedCoordinator(
        hass=hass,
        client=MagicMock(),
        slug="home",
        options={},
    )
    eic = "38ZEE-00720089-N"
    t = datetime(2026, 5, 16, 0, tzinfo=UTC)
    null_first = [
        AccountingInterval(
            period_start=t,
            consumption_kwh=None,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        )
    ]
    valued_later = [
        AccountingInterval(
            period_start=t,
            consumption_kwh=0.42,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        )
    ]
    coordinator._update_cache(eic, Kind.CONSUMPTION, null_first)
    coordinator._update_cache(eic, Kind.CONSUMPTION, valued_later)
    bucket = coordinator.cache[(eic, Kind.CONSUMPTION)]
    assert len(bucket) == 1
    assert bucket[0].consumption_kwh == 0.42


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
        patch.object(coordinator, "_sum_before_window", new=AsyncMock(return_value=0.0)),
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


@pytest.mark.asyncio
async def test_ensure_baselines_captures_one_per_meter_kind(hass):
    """First refresh after install: every (eic, kind) gets a baseline
    anchored at now. Past intervals brought in by the backfill are filtered
    out by ``cumulative_since_reset`` via the ``period_start >= reset_at``
    check, so the sensor reads 0 until forward-time intervals arrive —
    matches the user's "counts from install" design choice."""
    coordinator = EstfeedCoordinator(hass=hass, client=MagicMock(), slug="home", options={})
    coordinator.meters = [_make_meter()]
    await coordinator.async_ensure_baselines()

    assert ("38ZEE-00720089-N", Kind.CONSUMPTION) in coordinator.baselines
    assert ("38ZEE-00720089-N", Kind.PRODUCTION) in coordinator.baselines
    # New baselines start with no frozen contribution.
    assert coordinator.baselines[("38ZEE-00720089-N", Kind.CONSUMPTION)].frozen_sum == 0.0


@pytest.mark.asyncio
async def test_ensure_baselines_does_not_overwrite_existing(hass):
    """Once a baseline is captured (initial install or user reset), later
    refresh cycles must leave it alone — otherwise every poll would zero
    out the cumulative sensor."""
    coordinator = EstfeedCoordinator(hass=hass, client=MagicMock(), slug="home", options={})
    coordinator.meters = [_make_meter()]
    key = ("38ZEE-00720089-N", Kind.CONSUMPTION)
    original = CumulativeBaseline(reset_at=datetime(2026, 1, 1, tzinfo=UTC), frozen_sum=42.0)
    coordinator.baselines[key] = original

    await coordinator.async_ensure_baselines()

    assert coordinator.baselines[key] is original


@pytest.mark.asyncio
async def test_async_reset_cumulative_moves_reset_at_to_now(hass):
    coordinator = EstfeedCoordinator(hass=hass, client=MagicMock(), slug="home", options={})
    coordinator.meters = [_make_meter()]
    key = ("38ZEE-00720089-N", Kind.CONSUMPTION)
    coordinator.baselines[key] = CumulativeBaseline(
        reset_at=datetime(2026, 1, 1, tzinfo=UTC), frozen_sum=150.0
    )

    await coordinator.async_reset_cumulative("38ZEE-00720089-N", Kind.CONSUMPTION)

    new_baseline = coordinator.baselines[key]
    # reset_at advanced to "now-ish" (after Jan 1) and frozen_sum cleared so
    # the sensor reads 0 until new intervals land past the new reset_at.
    assert new_baseline.reset_at > datetime(2026, 1, 1, tzinfo=UTC)
    assert new_baseline.frozen_sum == 0.0


def test_cumulative_since_reset_sums_cache_from_reset_at(hass):
    """Regression: the cumulative sensor must compute from raw cache intervals,
    not the recorder's cumulative sum column — a force_start backfill chaining
    off an inflated prior_sum once pushed the running total up by thousands of
    kWh, but the per-hour interval values stayed correct."""
    coordinator = EstfeedCoordinator(hass=hass, client=MagicMock(), slug="home", options={})
    eic = "38ZEE-00720089-N"
    reset_at = datetime(2026, 5, 18, 12, tzinfo=UTC)
    coordinator.baselines[(eic, Kind.CONSUMPTION)] = CumulativeBaseline(reset_at=reset_at)
    # Three intervals: one before reset (excluded), two after (included)
    coordinator._update_cache(
        eic,
        Kind.CONSUMPTION,
        [
            AccountingInterval(
                period_start=datetime(2026, 5, 18, 11, tzinfo=UTC),
                consumption_kwh=99.0,
                production_kwh=None,
                consumption_m3=None,
                production_m3=None,
            ),
            AccountingInterval(
                period_start=datetime(2026, 5, 18, 13, tzinfo=UTC),
                consumption_kwh=1.5,
                production_kwh=None,
                consumption_m3=None,
                production_m3=None,
            ),
            AccountingInterval(
                period_start=datetime(2026, 5, 18, 14, tzinfo=UTC),
                consumption_kwh=2.0,
                production_kwh=None,
                consumption_m3=None,
                production_m3=None,
            ),
        ],
    )

    assert coordinator.cumulative_since_reset(eic, Kind.CONSUMPTION) == 3.5


def test_cumulative_since_reset_returns_none_without_baseline(hass):
    coordinator = EstfeedCoordinator(hass=hass, client=MagicMock(), slug="home", options={})
    assert coordinator.cumulative_since_reset("38ZEE-00720089-N", Kind.CONSUMPTION) is None


def test_cumulative_since_reset_includes_frozen_sum(hass):
    """When intervals age out of the 62-day cache, their consumption is
    captured into ``baseline.frozen_sum``. The cumulative sensor must add
    that frozen contribution to whatever is currently in the cache."""
    coordinator = EstfeedCoordinator(hass=hass, client=MagicMock(), slug="home", options={})
    eic = "38ZEE-00720089-N"
    coordinator.baselines[(eic, Kind.CONSUMPTION)] = CumulativeBaseline(
        reset_at=datetime(2026, 1, 1, tzinfo=UTC),
        frozen_sum=500.0,
    )
    coordinator._update_cache(
        eic,
        Kind.CONSUMPTION,
        [
            AccountingInterval(
                period_start=datetime.now(tz=UTC) - timedelta(hours=1),
                consumption_kwh=1.5,
                production_kwh=None,
                consumption_m3=None,
                production_m3=None,
            )
        ],
    )

    assert coordinator.cumulative_since_reset(eic, Kind.CONSUMPTION) == 501.5


def test_update_cache_folds_expiring_intervals_into_frozen_sum(hass):
    """Regression: a long-running baseline must not lose data once intervals
    age past the 62-day cache. Before trimming, the cache stashes the
    eligible expiring intervals (past reset_at, non-null) into the
    baseline's ``frozen_sum`` so the cumulative sensor remains correct."""
    coordinator = EstfeedCoordinator(hass=hass, client=MagicMock(), slug="home", options={})
    eic = "38ZEE-00720089-N"
    coordinator.baselines[(eic, Kind.CONSUMPTION)] = CumulativeBaseline(
        reset_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    now = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    # Build a batch where one interval is *just* old enough to be trimmed
    # (older than ROLLING_CACHE_DAYS) and one is recent enough to stay.
    expiring = AccountingInterval(
        period_start=now - timedelta(days=70),
        consumption_kwh=4.0,
        production_kwh=None,
        consumption_m3=None,
        production_m3=None,
    )
    recent = AccountingInterval(
        period_start=now - timedelta(hours=1),
        consumption_kwh=1.0,
        production_kwh=None,
        consumption_m3=None,
        production_m3=None,
    )
    coordinator._update_cache(eic, Kind.CONSUMPTION, [expiring, recent])

    baseline = coordinator.baselines[(eic, Kind.CONSUMPTION)]
    assert baseline.frozen_sum == 4.0
    # Cumulative = frozen 4 + cached 1 = 5
    assert coordinator.cumulative_since_reset(eic, Kind.CONSUMPTION) == 5.0


# ---- Task 6 tests: cost_streams_for, _build_tariff, last_nps_error ----


def _gas_meter() -> MeteringPoint:
    return MeteringPoint(
        eic="38ZEE-00720099-G",
        commodity_type=CommodityType.NATURAL_GAS,
        periods=[Period(start=datetime(2020, 1, 1, tzinfo=UTC), end=None)],
    )


def test_cost_streams_for_electricity_returns_two_streams(hass):
    coord = EstfeedCoordinator(hass=hass, client=MagicMock(), slug="home", options={})
    coord.meters = [_make_meter()]
    streams = coord.cost_streams_for(_make_meter())
    assert len(streams) == 2
    ids = {s.statistic_id for s in streams}
    assert ids == {"estfeed:home_cost_089n", "estfeed:home_compensation_089n"}
    assert all(isinstance(s, CostStream) for s in streams)
    # hass.config.currency defaults to EUR in HA test fixtures
    assert all(s.unit in {"EUR", hass.config.currency} for s in streams)


def test_cost_streams_for_gas_returns_empty(hass):
    coord = EstfeedCoordinator(hass=hass, client=MagicMock(), slug="home", options={})
    coord.meters = [_gas_meter()]
    assert coord.cost_streams_for(_gas_meter()) == []


def test_build_tariff_applies_configured_vat_and_margin(hass):
    coord = EstfeedCoordinator(
        hass=hass,
        client=MagicMock(),
        slug="home",
        options={CONF_VAT_PERCENT: 22.0, CONF_MARGIN_EUR_PER_KWH: 0.01},
    )
    tariff = coord._build_tariff()
    # 0.05 * 1.22 + 0.01 = 0.071
    assert tariff(0.05) == pytest.approx(0.071)


def test_build_tariff_uses_defaults_when_options_missing(hass):
    coord = EstfeedCoordinator(hass=hass, client=MagicMock(), slug="home", options={})
    tariff = coord._build_tariff()
    # Default VAT=22.0, margin=0.0 -> 0.05 * 1.22 = 0.061
    assert tariff(0.05) == pytest.approx(0.061)


def test_last_nps_error_starts_none(hass):
    coord = EstfeedCoordinator(hass=hass, client=MagicMock(), slug="home", options={})
    assert coord.last_nps_error is None


# ---- Task 7 tests: cost branch + cost_only param ----


def _hour_interval(hour: int, consumption: float) -> AccountingInterval:
    return AccountingInterval(
        period_start=datetime(2026, 5, 21, hour, tzinfo=UTC),
        consumption_kwh=consumption,
        production_kwh=0.0,
        consumption_m3=None,
        production_m3=None,
    )


@pytest.mark.asyncio
async def test_fetch_meter_window_writes_cost_and_compensation_for_electricity(hass):
    client = MagicMock()
    client.get_metering_data = AsyncMock(
        return_value=[
            MeterData(
                eic="38ZEE-00720089-N",
                intervals=[_hour_interval(10, 2.0), _hour_interval(11, 3.0)],
                error=None,
            )
        ]
    )
    coord = EstfeedCoordinator(hass=hass, client=client, slug="home", options={})
    coord.meters = [_make_meter()]
    # Stub NPS so we can predict cost.
    mock_nps = MagicMock()
    mock_nps.async_get_prices = AsyncMock(
        return_value={
            datetime(2026, 5, 21, 10, tzinfo=UTC): 0.05,
            datetime(2026, 5, 21, 11, tzinfo=UTC): 0.05,
        }
    )
    coord.attach_nps_client(mock_nps)
    with (
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=AsyncMock(return_value=5.0),
        ) as mock_energy,
        patch(
            "custom_components.estfeed.coordinator.async_write_cost_statistics",
            new=AsyncMock(return_value=0.305),
        ) as mock_cost,
        patch.object(coord, "_latest_seen_for_stream", new=AsyncMock(return_value=None)),
        patch.object(coord, "_prior_sum_for_stream", new=AsyncMock(return_value=0.0)),
        patch.object(coord, "_sum_before_window", new=AsyncMock(return_value=0.0)),
    ):
        await coord._fetch_meter_window(
            _make_meter(),
            datetime(2026, 5, 21, 10, tzinfo=UTC),
            datetime(2026, 5, 21, 12, tzinfo=UTC),
            write_stats=True,
            force_start=True,
        )
    # 2 energy streams (consumption + production) and 2 cost streams (cost + compensation)
    assert mock_energy.await_count == 2
    assert mock_cost.await_count == 2
    cost_ids = {call.args[1].statistic_id for call in mock_cost.await_args_list}
    assert cost_ids == {"estfeed:home_cost_089n", "estfeed:home_compensation_089n"}


@pytest.mark.asyncio
async def test_fetch_meter_window_skips_cost_for_gas_meter(hass):
    client = MagicMock()
    client.get_metering_data = AsyncMock(
        return_value=[MeterData(eic="38ZEE-00720099-G", intervals=[], error=None)]
    )
    coord = EstfeedCoordinator(hass=hass, client=client, slug="home", options={})
    coord.meters = [_gas_meter()]
    mock_nps = MagicMock()
    mock_nps.async_get_prices = AsyncMock(return_value={})
    coord.attach_nps_client(mock_nps)
    with (
        patch(
            "custom_components.estfeed.coordinator.async_write_cost_statistics",
            new=AsyncMock(),
        ) as mock_cost,
        patch.object(coord, "_latest_seen_for_stream", new=AsyncMock(return_value=None)),
        patch.object(coord, "_prior_sum_for_stream", new=AsyncMock(return_value=0.0)),
        patch.object(coord, "_sum_before_window", new=AsyncMock(return_value=0.0)),
    ):
        await coord._fetch_meter_window(
            _gas_meter(),
            datetime(2026, 5, 21, 10, tzinfo=UTC),
            datetime(2026, 5, 21, 12, tzinfo=UTC),
            write_stats=True,
            force_start=True,
        )
    mock_cost.assert_not_called()
    mock_nps.async_get_prices.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_meter_window_records_nps_error_on_failure(hass):
    client = MagicMock()
    client.get_metering_data = AsyncMock(
        return_value=[
            MeterData(
                eic="38ZEE-00720089-N",
                intervals=[_hour_interval(10, 2.0)],
                error=None,
            )
        ]
    )
    coord = EstfeedCoordinator(hass=hass, client=client, slug="home", options={})
    coord.meters = [_make_meter()]
    mock_nps = MagicMock()
    from custom_components.estfeed.nps import NpsError

    mock_nps.async_get_prices = AsyncMock(side_effect=NpsError("boom"))
    coord.attach_nps_client(mock_nps)
    with (
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=AsyncMock(return_value=2.0),
        ) as mock_energy,
        patch(
            "custom_components.estfeed.coordinator.async_write_cost_statistics",
            new=AsyncMock(),
        ) as mock_cost,
        patch.object(coord, "_latest_seen_for_stream", new=AsyncMock(return_value=None)),
        patch.object(coord, "_prior_sum_for_stream", new=AsyncMock(return_value=0.0)),
        patch.object(coord, "_sum_before_window", new=AsyncMock(return_value=0.0)),
    ):
        await coord._fetch_meter_window(
            _make_meter(),
            datetime(2026, 5, 21, 10, tzinfo=UTC),
            datetime(2026, 5, 21, 12, tzinfo=UTC),
            write_stats=True,
            force_start=True,
        )
    # Energy stats still written; cost skipped; error captured.
    assert mock_energy.await_count == 2
    mock_cost.assert_not_called()
    assert coord.last_nps_error is not None
    assert "boom" in coord.last_nps_error


@pytest.mark.asyncio
async def test_async_rebuild_cost_derives_from_stored_energy_not_api(hass):
    """async_rebuild_cost must price the STORED hourly energy stats, never a
    fresh Estfeed fetch — otherwise still-settling recent intervals drift the
    cost away from the published consumption (the bug this guards against)."""
    client = MagicMock()
    client.get_metering_data = AsyncMock(
        side_effect=AssertionError("rebuild must not re-fetch metering data")
    )
    coord = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={CONF_BACKFILL_MONTHS: 6, CONF_VAT_PERCENT: 22.0, CONF_MARGIN_EUR_PER_KWH: 0.0},
    )
    coord.meters = [_make_meter()]
    h10 = datetime(2026, 5, 21, 10, tzinfo=UTC)
    mock_nps = MagicMock()
    mock_nps.clear_cache = MagicMock()
    mock_nps.async_get_prices = AsyncMock(return_value={h10: 0.05})
    coord.attach_nps_client(mock_nps)

    # Stored consumption stat says 2.0 kWh for hour 10; production empty.
    async def _fake_hourly(statistic_id, start, end):  # noqa: ARG001
        return {h10: 2.0} if "consumption" in statistic_id else {}

    with (
        patch.object(coord, "_hourly_energy_from_stats", new=AsyncMock(side_effect=_fake_hourly)),
        patch.object(coord, "_sum_before_window", new=AsyncMock(return_value=4.0)),
        patch(
            "custom_components.estfeed.coordinator.async_write_cost_statistics_from_hourly",
            new=AsyncMock(return_value=0.122),
        ) as mock_write,
    ):
        await coord.async_rebuild_cost()

    mock_nps.clear_cache.assert_called_once()
    # Consumption cost stream written from the stored 2.0 kWh map.
    consumption_calls = [
        c for c in mock_write.await_args_list if "_cost_" in c.args[1].statistic_id
    ]
    assert len(consumption_calls) == 1
    assert consumption_calls[0].args[2] == {h10: 2.0}  # hourly_energy arg
    # Rebuild chains onto the pre-window sum instead of restarting at 0.
    assert consumption_calls[0].kwargs["prior_sum"] == 4.0


@pytest.mark.asyncio
async def test_async_rebuild_cost_noop_without_nps_client(hass):
    client = MagicMock()
    coord = EstfeedCoordinator(hass=hass, client=client, slug="home", options={})
    coord.meters = [_make_meter()]
    # No NPS client attached → nothing to do, no crash.
    with patch.object(coord, "_hourly_energy_from_stats", new=AsyncMock()) as mock_hourly:
        await coord.async_rebuild_cost()
    mock_hourly.assert_not_called()


# ---- NPS partial-quarter eviction + rebuild hardening ----


@pytest.mark.asyncio
async def test_tick_evicts_unsettled_nps_hours_before_pricing(hass):
    """Regression: Elering publishes 15-min NPS quarters progressively. An hour
    first priced mid-settlement would keep its partial-quarter mean in the
    process-lifetime cache forever. The regular tick must evict hours newer
    than the settle horizon before fetching prices so they are re-priced with
    complete data."""
    client = MagicMock()
    client.get_metering_data = AsyncMock(
        return_value=[MeterData(eic="38ZEE-00720089-N", intervals=[], error=None)]
    )
    coord = EstfeedCoordinator(hass=hass, client=client, slug="home", options={})
    coord.meters = [_make_meter()]
    mock_nps = MagicMock()
    mock_nps.async_get_prices = AsyncMock(return_value={})
    coord.attach_nps_client(mock_nps)

    before = datetime.now(tz=UTC)
    with (
        patch.object(coord, "_latest_seen_for_stream", new=AsyncMock(return_value=None)),
        patch.object(coord, "_prior_sum_for_stream", new=AsyncMock(return_value=0.0)),
        patch.object(coord, "_sum_before_window", new=AsyncMock(return_value=0.0)),
        patch(
            "custom_components.estfeed.coordinator.async_write_meter_statistics",
            new=AsyncMock(return_value=0.0),
        ),
    ):
        await coord._fetch_meter_window(
            _make_meter(),
            datetime(2026, 5, 21, 10, tzinfo=UTC),
            datetime(2026, 5, 21, 12, tzinfo=UTC),
            write_stats=True,
            force_start=True,
        )
    after = datetime.now(tz=UTC)

    mock_nps.evict_after.assert_called_once()
    cutoff = mock_nps.evict_after.call_args.args[0]
    # Cutoff = now - 2h, evaluated inside the tick.
    assert before - timedelta(hours=2) <= cutoff <= after - timedelta(hours=2)


@pytest.mark.asyncio
async def test_async_rebuild_cost_aborts_on_total_nps_failure(hass):
    """A total NPS failure must abort the rebuild rather than write a
    partially-priced series — a partial write would advance latest_seen past
    the unpriced gap, which later ticks cannot backfill."""
    client = MagicMock()
    coord = EstfeedCoordinator(
        hass=hass, client=client, slug="home", options={CONF_BACKFILL_MONTHS: 6}
    )
    coord.meters = [_make_meter()]
    mock_nps = MagicMock()
    mock_nps.clear_cache = MagicMock()
    from custom_components.estfeed.nps import NpsError

    mock_nps.async_get_prices = AsyncMock(side_effect=NpsError("down"))
    coord.attach_nps_client(mock_nps)

    with (
        patch.object(coord, "_hourly_energy_from_stats", new=AsyncMock()) as mock_hourly,
        patch(
            "custom_components.estfeed.coordinator.async_write_cost_statistics_from_hourly",
            new=AsyncMock(),
        ) as mock_write,
    ):
        await coord.async_rebuild_cost()

    mock_hourly.assert_not_called()
    mock_write.assert_not_called()
    assert coord.last_nps_error is not None
    assert "down" in coord.last_nps_error


@pytest.mark.asyncio
async def test_sum_before_window_subtracts_change_over_window(hass):
    """_sum_before_window = latest_sum - sum of change[start, end): the cumulative
    the series had just before the window start, with both queries bounded."""
    coordinator = EstfeedCoordinator(hass=hass, client=MagicMock(), slug="home", options={})

    last_stats = {"estfeed:home_consumption_089n": [{"sum": 100.0}]}
    during = {
        "estfeed:home_consumption_089n": [
            {"start": 1.0, "change": 2.0},
            {"start": 2.0, "change": 3.0},
        ]
    }

    def _fake_during_period(hass, start, end, ids, period, units, types):  # noqa: ARG001
        assert types == {"change"}
        return during

    with (
        patch(
            "custom_components.estfeed.coordinator.get_instance",
            return_value=_fake_recorder(),
        ),
        patch(
            "custom_components.estfeed.coordinator.get_last_statistics",
            new=MagicMock(return_value=last_stats),
        ),
        patch(
            "custom_components.estfeed.coordinator.statistics_during_period",
            new=MagicMock(side_effect=_fake_during_period),
        ),
    ):
        result = await coordinator._sum_before_window(
            "estfeed:home_consumption_089n",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 6, 1, tzinfo=UTC),
        )

    assert result == 95.0  # 100.0 - (2.0 + 3.0)


# ---- Reauth on runtime auth failure ----


@pytest.mark.asyncio
async def test_auth_error_starts_reauth_flow(hass):
    """A mid-flight EstfeedAuthError must surface UpdateFailed and hand the
    user the reauth flow instead of retrying dead credentials forever."""
    # The reauth flow pulls in the integration's recorder dependency.
    from homeassistant.components import recorder
    from homeassistant.config_entries import SOURCE_REAUTH
    from homeassistant.helpers import recorder as recorder_helper
    from homeassistant.helpers.update_coordinator import UpdateFailed
    from homeassistant.setup import async_setup_component
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.estfeed.const import (
        CONF_CLIENT_ID,
        CONF_CLIENT_SECRET,
        CONF_FRIENDLY_NAME,
        DOMAIN,
    )

    with patch("homeassistant.components.recorder.ALLOW_IN_MEMORY_DB", True):
        if recorder.DOMAIN not in hass.data:
            recorder_helper.async_initialize_recorder(hass)
        assert await async_setup_component(
            hass,
            recorder.DOMAIN,
            {recorder.DOMAIN: {"db_url": "sqlite://", "commit_interval": 0}},
        )
        await hass.async_block_till_done()

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CLIENT_ID: "c", CONF_CLIENT_SECRET: "s", CONF_FRIENDLY_NAME: "Home"},
        options={},
        unique_id="c",
    )
    entry.add_to_hass(hass)

    client = MagicMock()
    client.get_metering_data = AsyncMock(side_effect=EstfeedAuthError("401: revoked"))

    coordinator = EstfeedCoordinator(
        hass=hass,
        client=client,
        slug="home",
        options={},
        config_entry=entry,
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
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()

    # async_start_reauth spawns the flow in a background task.
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"].get("source") == SOURCE_REAUTH for flow in flows)
