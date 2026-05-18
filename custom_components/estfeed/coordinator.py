"""EstfeedCoordinator: fetches data and writes long-term statistics."""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.storage import Store
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


@dataclass(frozen=True, slots=True)
class CumulativeBaseline:
    """Recorded cumulative-sum value at the moment the user pressed reset.

    The cumulative-since-reset sensor reports ``latest_sum - baseline.sum``.
    ``reset_at`` is surfaced as the HA ``last_reset`` attribute so the Energy
    dashboard handles the reset gracefully when the value drops back to zero.
    """

    sum: float
    reset_at: datetime

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
        # Cumulative-since-reset tracking. ``latest_sum`` mirrors the most-recent
        # cumulative sum from external statistics (one per stream); it stays
        # absent until the first fetch writes/reads a real value. ``baselines``
        # records the cumulative value at the moment the user pressed reset.
        # Sensor value = latest_sum - baseline.sum. Baselines are persisted via
        # the optional ``_store`` so they survive HA restarts.
        self.latest_sum: dict[tuple[str, Kind], float] = {}
        self.baselines: dict[tuple[str, Kind], CumulativeBaseline] = {}
        self._store: Store | None = None

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
        await self.async_refresh_latest_sums()
        await self.async_ensure_baselines()

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
        await self.async_refresh_latest_sums()
        await self.async_ensure_baselines()
        # Lagging sensors compute from the cache, not from coordinator.data. Fetch
        # paths that bypass _async_update_data (this method and async_warm_cache,
        # plus the backfill_history service) populate the cache without notifying
        # CoordinatorEntity listeners, so the sensor state stays frozen at the
        # value computed before the background fill finished. Nudge listeners.
        self.async_update_listeners()

    async def async_warm_cache(self) -> None:
        """Populate the rolling 62-day cache after a restart.

        Does NOT write statistics — they already exist from prior runs. Re-writing
        them mid-series with a fresh `prior_sum` lookup would corrupt cumulative
        counter semantics. We just refill `self.cache` for the lagging sensors.
        """
        end = datetime.now(tz=UTC)
        start = end - timedelta(days=ROLLING_CACHE_DAYS)
        await self._fetch_window(start, end, write_stats=False, force_start=True)
        await self.async_refresh_latest_sums()
        await self.async_ensure_baselines()
        self.async_update_listeners()

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
        # force_start callers (initial backfill, manual rebuild service) want
        # to rebuild history from scratch — chaining off the current latest
        # sum would offset every historical bucket by whatever the cumulative
        # happens to be right now. Reset to 0.0 so the rewrite is clean.
        prior_sums: dict[str, float] = {}
        if write_stats:
            for stream in streams:
                prior_sums[stream.statistic_id] = (
                    0.0 if force_start else await self._prior_sum_for_stream(stream)
                )
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

    async def _latest_sum_or_none(self, stream: StatisticStream) -> float | None:
        """Return the latest cumulative sum, or ``None`` when no stats exist.

        Unlike ``_prior_sum_for_stream`` (which folds the no-rows case to 0.0
        so the cumulative write loop can keep advancing), the cumulative
        sensor needs to distinguish "no data yet" from "data exists and the
        running sum happens to be 0" — otherwise a production stream that
        legitimately read 0 would be permanently treated as unavailable.
        """
        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, stream.statistic_id, True, {"sum"}
        )
        rows = last_stats.get(stream.statistic_id)
        if not rows:
            return None
        raw = rows[0].get("sum")
        return float(raw) if raw is not None else None

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
        # Newer fetch wins per period_start. Estfeed returns recent intervals
        # with `consumption_kwh=None` while the hour is still being settled, and
        # later fetches replace the null with the real value. A period_start-only
        # dedup that *skips* duplicates would freeze the original null and the
        # lagging sensors would silently lose hours as today→yesterday rolls
        # over (observed live: today=0.374 / yesterday=6.383 while recorder
        # stats had the same window at 11.5 kWh). Resort after replace because
        # backfill chunks can land older rows behind newer ones — the trim
        # below relies on the deque being ascending by period_start.
        bucket = self.cache[(eic, kind)]
        by_start: dict[datetime, AccountingInterval] = {i.period_start: i for i in bucket}
        for i in intervals:
            by_start[i.period_start] = i
        ordered = sorted(by_start.values(), key=lambda i: i.period_start)
        bucket.clear()
        bucket.extend(ordered)
        cutoff = datetime.now(tz=UTC) - timedelta(days=ROLLING_CACHE_DAYS)
        while bucket and bucket[0].period_start < cutoff:
            bucket.popleft()

    async def async_refresh_latest_sums(self) -> None:
        """Pull the most-recent cumulative sum per stream from the recorder.

        Called after each fetch path completes. We read from the recorder
        rather than tracking writes locally because warm_cache and the
        diagnostics service intentionally bypass the write loop, and the
        cumulative sensor must stay accurate across those flows too.
        """
        for meter in self.meters:
            for stream in self.streams_for(meter):
                latest = await self._latest_sum_or_none(stream)
                if latest is not None:
                    self.latest_sum[(meter.eic, stream.kind)] = latest

    async def async_ensure_baselines(self) -> None:
        """Capture an initial baseline for any (eic, kind) that lacks one.

        Per the install-time design choice, the cumulative sensor starts at
        zero and counts forward from the install moment — we capture the
        cumulative sum at the first refresh that produces real stats. This
        is idempotent: streams that already have a baseline are left alone,
        so user-triggered resets are not overwritten by a later refresh.
        """
        new_keys: list[tuple[str, Kind]] = []
        now = datetime.now(tz=UTC)
        for key, latest in self.latest_sum.items():
            if key in self.baselines:
                continue
            self.baselines[key] = CumulativeBaseline(sum=latest, reset_at=now)
            new_keys.append(key)
        if new_keys:
            await self._save_baselines()

    async def async_reset_cumulative(self, eic: str, kind: Kind) -> None:
        """Capture the current cumulative sum as the new baseline.

        If the stream has no statistics yet (latest_sum missing), the reset
        is a no-op — there is nothing meaningful to capture, and we would
        rather the user retry once data has arrived than silently anchor to
        zero and then jump on the next refresh.
        """
        key = (eic, kind)
        if key not in self.latest_sum:
            _LOGGER.warning(
                "Reset requested for %s/%s before any statistics exist; ignored",
                eic,
                kind.value,
            )
            return
        self.baselines[key] = CumulativeBaseline(
            sum=self.latest_sum[key], reset_at=datetime.now(tz=UTC)
        )
        await self._save_baselines()
        self.async_update_listeners()

    async def async_load_baselines(self) -> None:
        """Hydrate ``self.baselines`` from the Store, if one is attached."""
        if self._store is None:
            return
        data = await self._store.async_load()
        if not data:
            return
        for raw_key, raw_val in (data.get("baselines") or {}).items():
            try:
                eic, kind_value = raw_key.rsplit("|", 1)
                kind = Kind(kind_value)
                self.baselines[(eic, kind)] = CumulativeBaseline(
                    sum=float(raw_val["sum"]),
                    reset_at=datetime.fromisoformat(raw_val["reset_at"]),
                )
            except (KeyError, ValueError) as err:
                _LOGGER.warning("Skipping malformed baseline entry %r: %s", raw_key, err)

    async def _save_baselines(self) -> None:
        if self._store is None:
            return
        payload = {
            "baselines": {
                f"{eic}|{kind.value}": {
                    "sum": b.sum,
                    "reset_at": b.reset_at.isoformat(),
                }
                for (eic, kind), b in self.baselines.items()
            }
        }
        await self._store.async_save(payload)
