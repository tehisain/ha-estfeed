"""EstfeedCoordinator: fetches data and writes long-term statistics."""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.core import HomeAssistant
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

    async def _fetch_window(
        self,
        start: datetime,
        end: datetime,
        *,
        write_stats: bool,
        force_start: bool,
    ) -> None:
        """Fetch [start, end] in 31-day chunks, optionally writing stats per chunk."""
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
        # Single fetch per (meter, chunk) covers all kinds — the API returns
        # consumption and production together. Use the latest-seen across kinds
        # so we don't re-fetch already-recorded data.
        chunk_start = start if force_start else await self._chunk_start_for_meter(streams, start)
        # Read prior_sum ONCE before the chunk loop (per stream) and advance
        # locally as we write. HA's recorder may not flush statistics writes
        # synchronously, so re-reading get_last_statistics inside the loop
        # would risk seeing stale data for chunk N+1 after chunk N's write.
        prior_sums: dict[str, float] = {}
        if write_stats:
            for stream in streams:
                prior_sums[stream.statistic_id] = await self._prior_sum_for_stream(stream)
        cursor = chunk_start
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
                    if write_stats:
                        prior_sums[stream.statistic_id] = await async_write_meter_statistics(
                            self.hass,
                            stream,
                            md.intervals,
                            prior_sum=prior_sums[stream.statistic_id],
                        )
                    self._update_cache(meter.eic, stream.kind, md.intervals)
            cursor = chunk_end

    async def _chunk_start_for_meter(
        self, streams: list[StatisticStream], default_start: datetime
    ) -> datetime:
        """Pick the start of the next fetch window for a meter.

        Uses the latest seen statistic across all of the meter's streams so we
        avoid re-fetching data we've already recorded for any kind.
        """
        latest_per_stream: list[datetime] = []
        for stream in streams:
            seen = await self._latest_seen_for_stream(stream)
            if seen is not None:
                latest_per_stream.append(seen)
        if not latest_per_stream:
            return default_start
        # TODO: revisit if partial-kind failures observed — switch to min() with overlap window.
        return max(latest_per_stream) + timedelta(hours=1)

    async def _latest_seen_for_stream(self, stream: StatisticStream) -> datetime | None:
        last_stats = await get_last_statistics(self.hass, 1, stream.statistic_id, True, {"end"})
        rows = last_stats.get(stream.statistic_id)
        if not rows:
            return None
        end_ms = rows[0].get("end")
        if end_ms is None:
            return None
        return datetime.fromtimestamp(end_ms / 1000.0, tz=UTC)

    async def _prior_sum_for_stream(self, stream: StatisticStream) -> float:
        last_stats = await get_last_statistics(self.hass, 1, stream.statistic_id, True, {"sum"})
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
