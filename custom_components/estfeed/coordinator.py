"""EstfeedCoordinator: fetches data and writes long-term statistics."""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    AccountingInterval,
    EstfeedClient,
    EstfeedError,
    MeteringPoint,
)
from .const import (
    CONF_BACKFILL_MONTHS,
    CONF_RESOLUTION,
    DOMAIN,
    MAX_DAYS_PER_REQUEST,
    ROLLING_CACHE_DAYS,
    UPDATE_INTERVAL,
    Kind,
    Resolution,
)
from .statistics import (
    StatisticStream,
    async_write_meter_statistics,
    build_statistic_id,
    eic_suffix,
)

_LOGGER = logging.getLogger(__name__)

# Kind / unit mapping for electricity vs gas
_ELECTRICITY_KINDS = (Kind.CONSUMPTION, Kind.PRODUCTION)
_GAS_KINDS = (Kind.CONSUMPTION, Kind.PRODUCTION)


def _snap_to_resolution(dt: datetime, resolution: Resolution) -> datetime:
    """Snap a timestamp down to the nearest boundary the API expects."""
    if resolution == Resolution.QUARTER_HOUR:
        return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)
    if resolution == Resolution.DAY:
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    # HOUR (default), WEEK, MONTH all snap to top of hour.
    return dt.replace(minute=0, second=0, microsecond=0)


class EstfeedCoordinator(DataUpdateCoordinator[None]):
    """Hourly poller + statistics ingester."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: EstfeedClient,
        slug: str,
        options: dict[str, Any],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{slug}",
            update_interval=UPDATE_INTERVAL,
        )
        self._client = client
        self.slug = slug
        self.options = options
        self.meters: list[MeteringPoint] = []
        # rolling cache: {(eic, kind): deque[AccountingInterval]} sorted by period_start
        self.cache: dict[tuple[str, Kind], deque[AccountingInterval]] = defaultdict(lambda: deque())
        self.last_meter_errors: dict[str, str] = {}

    @property
    def resolution(self) -> Resolution:
        return Resolution(self.options.get(CONF_RESOLUTION, Resolution.HOUR.value))

    @property
    def backfill_months(self) -> int:
        return int(self.options.get(CONF_BACKFILL_MONTHS, 12))

    @property
    def recent_requests(self) -> deque[dict[str, Any]]:
        """Expose the underlying client's recent-request ring buffer for diagnostics."""
        return self._client.recent_requests

    def streams_for(self, meter: MeteringPoint) -> list[StatisticStream]:
        suffix = eic_suffix(meter.eic)
        unit = "kWh" if meter.commodity_type.value == "ELECTRICITY" else "m³"
        kinds = _ELECTRICITY_KINDS if meter.commodity_type.value == "ELECTRICITY" else _GAS_KINDS
        return [
            StatisticStream(
                statistic_id=build_statistic_id(
                    self.slug, k, suffix, multi_meter=len(self.meters) > 1
                ),
                name=f"{self.slug} {k.value} ({meter.eic})",
                unit=unit,
                kind=k,
            )
            for k in kinds
        ]

    async def _async_update_data(self) -> None:
        """One hourly tick: fetch new intervals per meter, write statistics, update cache."""
        if not self.meters:
            return
        try:
            await self._fetch_window(
                self._compute_default_start(),
                datetime.now(tz=UTC),
                write_stats=True,
                force_start=False,
            )
        except EstfeedError as err:
            raise UpdateFailed(str(err)) from err

    async def async_initial_backfill(self) -> None:
        """Run once at setup if no statistics exist for this entry's streams.

        Walks 12 months of history (or whatever `backfill_months` is set to). Because
        no prior stats exist on first install, prior_sum starts at 0 and the cumulative
        counter is built correctly from the earliest backfilled interval.
        """
        end = datetime.now(tz=UTC)
        # 12 months ≈ 365 days; backfill_months * 30 keeps things simple and bounded.
        start = end - timedelta(days=self.backfill_months * 30)
        await self._fetch_window(start, end, write_stats=True, force_start=True)

    async def async_warm_cache(self) -> None:
        """Populate the rolling 62-day cache after a restart.

        Does NOT write statistics — they already exist from prior runs. Re-writing
        them mid-series with a fresh `prior_sum` lookup would corrupt cumulative
        counter semantics. We just refill `self.cache` for the lagging sensors.
        """
        end = datetime.now(tz=UTC)
        start = end - timedelta(days=ROLLING_CACHE_DAYS)
        await self._fetch_window(start, end, write_stats=False, force_start=True)

    async def _fetch_window(
        self,
        start: datetime,
        end: datetime,
        *,
        write_stats: bool,
        force_start: bool,
    ) -> None:
        """Fetch [start, end] in 31-day chunks, optionally writing stats per chunk."""
        # Estfeed anchors hourly intervals to the requested start_datetime — if
        # we send a non-aligned timestamp, the API returns intervals at the
        # same minute/second offset, which HA's recorder rejects. Snap to the
        # resolution boundary so the API returns clean top-of-hour buckets.
        start = _snap_to_resolution(start, self.resolution)
        end = _snap_to_resolution(end, self.resolution)
        for meter in self.meters:
            await self._fetch_meter_window(
                meter, start, end, write_stats=write_stats, force_start=force_start
            )

    async def _fetch_meter_window(
        self,
        meter: MeteringPoint,
        start: datetime,
        end: datetime,
        *,
        write_stats: bool,
        force_start: bool,
    ) -> None:
        streams = self.streams_for(meter)
        # Per-stream resume point: each kind tracks its own latest-seen.
        # Sharing one chunk_start across kinds caused the leading kind's
        # historical rows to be re-written with an inflated prior_sum every
        # tick whenever a sibling kind lagged (e.g., production for a
        # consume-only meter that briefly reported a non-null value).
        # `None` means "no prior data — accept everything we fetch".
        per_stream_start: dict[str, datetime | None] = {}
        if force_start:
            for stream in streams:
                per_stream_start[stream.statistic_id] = None
        else:
            for stream in streams:
                per_stream_start[stream.statistic_id] = await self._latest_seen_for_stream(stream)
        # One API call covers all kinds. Fetch from the earliest seen so a
        # lagging stream can backfill its gap, but bound by the caller's
        # `start` on regular ticks — otherwise a stream stuck far in the
        # past (e.g., a single non-null production reading from 12 months
        # ago for a consume-only meter that has been null since) would
        # force every tick to download a year of data and exceed HA's
        # bootstrap stage-2 timeout. force_start callers (initial backfill,
        # warm cache, manual service) still get the full window.
        seen = [s for s in per_stream_start.values() if s is not None]
        fetch_start = start if force_start or not seen else max(start, min(seen))
        # Read prior_sum ONCE before the chunk loop (per stream) and advance
        # locally as we write. HA's recorder may not flush statistics writes
        # synchronously, so re-reading get_last_statistics inside the loop
        # would risk seeing stale data for chunk N+1 after chunk N's write.
        prior_sums: dict[str, float] = {}
        if write_stats:
            for stream in streams:
                prior_sums[stream.statistic_id] = await self._prior_sum_for_stream(stream)
        cursor = fetch_start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=MAX_DAYS_PER_REQUEST), end)
            results = await self._client.get_metering_data(
                cursor, chunk_end, self.resolution, eics=[meter.eic]
            )
            for md in results:
                if md.error is not None:
                    self.last_meter_errors[md.eic] = md.error.code
                    _LOGGER.warning(
                        "Estfeed returned error for meter %s: %s (traceId=%s)",
                        md.eic,
                        md.error.code,
                        md.error.trace_id,
                    )
                    continue
                # Successful response for this meter — clear any stale error
                # state so consumers see the meter as healthy again (M8).
                self.last_meter_errors.pop(md.eic, None)
                for stream in streams:
                    threshold = per_stream_start[stream.statistic_id]
                    # Filter intervals per stream so a leading kind never
                    # overwrites its already-stored rows. `_latest_seen_for_stream`
                    # returns the previous row's `end`, which equals the next
                    # row's `start` — no +1h offset needed.
                    if threshold is None:
                        relevant = md.intervals
                    else:
                        relevant = [i for i in md.intervals if i.period_start >= threshold]
                    if write_stats:
                        prior_sums[stream.statistic_id] = await async_write_meter_statistics(
                            self.hass,
                            stream,
                            relevant,
                            prior_sum=prior_sums[stream.statistic_id],
                        )
                    self._update_cache(meter.eic, stream.kind, relevant)
            cursor = chunk_end

    async def _latest_seen_for_stream(self, stream: StatisticStream) -> datetime | None:
        # `get_last_statistics` is a synchronous DB query; HA expects callers to
        # offload it to the recorder's executor.
        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, stream.statistic_id, True, set()
        )
        rows = last_stats.get(stream.statistic_id)
        if not rows:
            return None
        end_ms = rows[0].get("end")
        if end_ms is None:
            return None
        return datetime.fromtimestamp(end_ms / 1000.0, tz=UTC)

    async def _prior_sum_for_stream(self, stream: StatisticStream) -> float:
        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, stream.statistic_id, True, {"sum"}
        )
        rows = last_stats.get(stream.statistic_id)
        if not rows:
            return 0.0
        return float(rows[0].get("sum") or 0.0)

    def _compute_default_start(self) -> datetime:
        """For the regular hourly tick, start window = now - 30 days as a fallback.

        Only used when no prior statistics exist (meter was just added). The
        initial-backfill flow uses backfill_months instead, computed by callers.
        """
        return datetime.now(tz=UTC) - timedelta(days=30)

    def _update_cache(
        self,
        eic: str,
        kind: Kind,
        intervals: list[AccountingInterval],
    ) -> None:
        bucket = self.cache[(eic, kind)]
        for ival in sorted(intervals, key=lambda i: i.period_start):
            bucket.append(ival)
        # Trim entries older than ROLLING_CACHE_DAYS
        cutoff = datetime.now(tz=UTC) - timedelta(days=ROLLING_CACHE_DAYS)
        while bucket and bucket[0].period_start < cutoff:
            bucket.popleft()
