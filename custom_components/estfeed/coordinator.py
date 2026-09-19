"""EstfeedCoordinator: fetches data and writes long-term statistics."""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.recorder.statistics import (
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    AccountingInterval,
    EstfeedAuthError,
    EstfeedClient,
    EstfeedError,
    MeteringPoint,
    interval_value,
)
from .const import (
    CONF_BACKFILL_MONTHS,
    CONF_MARGIN_EUR_PER_KWH,
    CONF_RESOLUTION,
    CONF_VAT_PERCENT,
    DEFAULT_MARGIN_EUR_PER_KWH,
    DEFAULT_VAT_PERCENT,
    DOMAIN,
    MAX_DAYS_PER_REQUEST,
    ROLLING_CACHE_DAYS,
    UPDATE_INTERVAL,
    Kind,
    Resolution,
)
from .nps import NPS_PRICE_SETTLE_HOURS, EleringNpsClient, NpsError
from .pricing import make_tariff
from .statistics import (
    CostStream,
    StatisticStream,
    async_write_cost_statistics,
    async_write_cost_statistics_from_hourly,
    async_write_meter_statistics,
    build_statistic_id,
    eic_suffix,
)


@dataclass(frozen=True, slots=True)
class CumulativeBaseline:
    """Anchor timestamp for the cumulative-since-reset sensor.

    The sensor sums raw cache intervals whose ``period_start`` is at or after
    ``reset_at``. Computing from raw intervals rather than the recorder's
    cumulative ``sum`` column shields us from historical sum-column
    corruption (e.g., a force_start backfill that chained off an already-
    inflated prior_sum and pushed the running total up by thousands of kWh).

    ``frozen_sum`` accumulates consumption for intervals that have aged out
    of the 62-day rolling cache, so a long-running baseline reports
    ``frozen_sum + sum(cache slice)`` and stays correct past the cache window.
    ``reset_at`` is surfaced as HA's ``last_reset`` attribute so the Energy
    dashboard tolerates the reset without flagging it as a counter rollback.
    """

    reset_at: datetime
    frozen_sum: float = 0.0


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
        config_entry: ConfigEntry | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{slug}",
            update_interval=UPDATE_INTERVAL,
            config_entry=config_entry,
        )
        self._client = client
        self.slug = slug
        self.options = options
        self.meters: list[MeteringPoint] = []
        # rolling cache: {(eic, kind): deque[AccountingInterval]} sorted by period_start
        self.cache: dict[tuple[str, Kind], deque[AccountingInterval]] = defaultdict(lambda: deque())
        self.last_meter_errors: dict[str, str] = {}
        # Cumulative-since-reset tracking. The sensor sums raw cache intervals
        # from ``baseline.reset_at`` onwards plus ``baseline.frozen_sum`` for
        # intervals that have aged out of the 62-day cache. Baselines are
        # persisted via the optional ``_store`` so they survive HA restarts.
        self.baselines: dict[tuple[str, Kind], CumulativeBaseline] = {}
        self._store: Store[dict[str, Any]] | None = None
        self._baselines_dirty = False
        # NPS price client + last-fetch-error for diagnostics. Injected by
        # __init__.py during setup_entry (real session). Tests can leave this
        # as the default lazy client; production wiring overrides it via
        # ``attach_nps_client`` before any fetch happens.
        self._nps: EleringNpsClient | None = None
        self.last_nps_error: str | None = None

    def attach_nps_client(self, nps: EleringNpsClient) -> None:
        """Inject the NPS price client. Called from setup_entry (production)
        or directly in tests with a mocked client."""
        self._nps = nps

    def attach_store(self, store: Store[dict[str, Any]]) -> None:
        """Attach the HA storage helper used to persist cumulative baselines.

        Kept off the constructor so tests can spin up a coordinator without
        touching HA's storage subsystem.
        """
        self._store = store

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

    @property
    def nps_cache_size(self) -> int:
        """Number of cached NPS hourly prices. Zero when no client is attached."""
        return self._nps.cache_size if self._nps is not None else 0

    def nps_cache_snapshot(self, hours: list[datetime]) -> dict[str, float | None]:
        """Cached EUR/kWh prices for the given hours, keyed by ISO string.

        Public accessor used by diagnostics so it does not need to reach
        into the private ``_nps`` client.
        """
        if self._nps is None:
            return {}
        return self._nps.cache_snapshot(hours)

    def cost_streams_for(self, meter: MeteringPoint) -> list[CostStream]:
        """Cost + compensation streams for one meter; empty list for gas.

        The unit is always EUR: NPS prices are EUR and no conversion is
        applied, so labelling the statistic with ``hass.config.currency``
        would mislabel EUR amounts as e.g. USD for non-EUR installations.
        """
        if meter.commodity_type.value != "ELECTRICITY":
            return []
        suffix = eic_suffix(meter.eic)
        return [
            CostStream(
                statistic_id=f"{DOMAIN}:{self.slug}_cost_{suffix}",
                name=f"{self.slug} cost ({meter.eic})",
                unit="EUR",
                kind=Kind.CONSUMPTION,
            ),
            CostStream(
                statistic_id=f"{DOMAIN}:{self.slug}_compensation_{suffix}",
                name=f"{self.slug} compensation ({meter.eic})",
                unit="EUR",
                kind=Kind.PRODUCTION,
            ),
        ]

    def _build_tariff(self) -> Callable[[float], float]:
        """Construct the curried tariff function from current options."""
        vat = float(self.options.get(CONF_VAT_PERCENT, DEFAULT_VAT_PERCENT))
        margin = float(self.options.get(CONF_MARGIN_EUR_PER_KWH, DEFAULT_MARGIN_EUR_PER_KWH))
        return make_tariff(vat, margin)

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
        except EstfeedAuthError as err:
            # Credentials were rejected mid-flight (key rotated/revoked).
            # Surface the failure and hand the user the reauth flow instead
            # of retrying with the same dead credentials forever.
            if self.config_entry is not None:
                self.config_entry.async_start_reauth(self.hass)
            raise UpdateFailed(str(err)) from err
        except EstfeedError as err:
            raise UpdateFailed(str(err)) from err
        await self.async_ensure_baselines()
        await self._flush_baselines_if_dirty()

    async def async_initial_backfill(self, months: int | None = None) -> None:
        """Run once at setup if no statistics exist for this entry's streams.

        Walks 12 months of history (or whatever `backfill_months` is set to;
        the ``months`` override is used by the backfill service without
        mutating the persisted options). Because no prior stats exist on
        first install, prior_sum starts at 0 and the cumulative counter is
        built correctly from the earliest backfilled interval.
        """
        end = datetime.now(tz=UTC)
        # 12 months ≈ 365 days; backfill_months * 30 keeps things simple and bounded.
        span = months if months is not None else self.backfill_months
        start = end - timedelta(days=span * 30)
        await self._fetch_window(start, end, write_stats=True, force_start=True)
        await self.async_ensure_baselines()
        await self._flush_baselines_if_dirty()
        # Lagging sensors compute from the cache, not from coordinator.data. Fetch
        # paths that bypass _async_update_data (this method and async_warm_cache,
        # plus the backfill_history service) populate the cache without notifying
        # CoordinatorEntity listeners, so the sensor state stays frozen at the
        # value computed before the background fill finished. Nudge listeners.
        self.async_update_listeners()

    async def async_rebuild_cost(self) -> None:
        """Recompute cost/compensation statistics over the configured window.

        Used after VAT/margin option changes and on first run for entries
        upgraded from a pre-cost version. Does not touch energy statistics.

        Cost is derived from the *stored* hourly energy statistics (not a fresh
        Estfeed fetch) so it stays exactly consistent with the consumption the
        user already sees on the Energy dashboard. Re-fetching would let
        Estfeed's still-settling recent intervals drift the cost away from the
        published energy — the bug this method exists to avoid.

        Rows are chained onto the cumulative ``sum`` of the last existing row
        before the window start, so a rebuild over a window shorter than the
        existing history keeps the sum column monotonic (a restart-at-zero
        would read as a counter rollback on the Energy dashboard).
        """
        if not self.meters or self._nps is None:
            return
        # Drop any prior cached prices so a rebuild can never re-use a
        # partial-data mean (Elering's 15-min quarters land progressively;
        # hours fetched before all 4 quarters settle would otherwise live
        # in the cache as a partial mean forever).
        self._nps.clear_cache()
        tariff = self._build_tariff()
        end = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=self.backfill_months * 30)

        # The client chunks the fetch internally (≤31 days per request) so a
        # single call here cannot trigger a months-spanning request. A total
        # failure aborts the rebuild: writing a partially-priced series would
        # advance latest_seen past the unpriced gap, which later ticks cannot
        # backfill. Re-running the rebuild (re-save options) retries cleanly.
        try:
            prices = await self._nps.async_get_prices(start, end)
            self.last_nps_error = None
        except NpsError as err:
            self.last_nps_error = str(err)
            _LOGGER.warning(
                "NPS fetch failed for %s..%s, aborting cost rebuild: %s", start, end, err
            )
            return

        for meter in self.meters:
            cost_streams = self.cost_streams_for(meter)
            if not cost_streams:
                continue
            energy_id_by_kind = {s.kind: s.statistic_id for s in self.streams_for(meter)}
            for cstream in cost_streams:
                energy_id = energy_id_by_kind.get(cstream.kind)
                if energy_id is None:
                    continue
                hourly_energy = await self._hourly_energy_from_stats(energy_id, start, end)
                prior_sum = await self._sum_before_window(cstream.statistic_id, start, end)
                await async_write_cost_statistics_from_hourly(
                    self.hass,
                    cstream,
                    hourly_energy,
                    prices,
                    tariff,
                    prior_sum=prior_sum,
                )
        self.async_update_listeners()

    async def _hourly_energy_from_stats(
        self, statistic_id: str, start: datetime, end: datetime
    ) -> dict[datetime, float]:
        """Read per-hour energy (kWh) for a stored statistic over [start, end).

        Returns top-of-hour UTC → energy delta for that hour, taken from the
        recorder's own ``change`` aggregation so it matches exactly what the
        Energy dashboard shows.
        """
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            end,
            {statistic_id},
            "hour",
            None,
            {"change"},
        )
        result: dict[datetime, float] = {}
        for row in stats.get(statistic_id, []):
            change = row.get("change")
            start_ts = row.get("start")
            if change is None or start_ts is None:
                continue
            hour = datetime.fromtimestamp(float(start_ts), tz=UTC).replace(
                minute=0, second=0, microsecond=0
            )
            result[hour] = float(change)
        return result

    async def _sum_before_window(self, statistic_id: str, start: datetime, end: datetime) -> float:
        """Cumulative ``sum`` of the last row at or before ``start``.

        Rebuilds over a sub-window must chain onto the sum the series
        already had at the window start; restarting from 0 would create a
        mid-series drop that HA reads as a counter rollback. Computed as
        ``latest_sum - sum of change[start, end)`` so both queries stay bounded
        by the rebuild window itself (no scan from the beginning of
        history). Exact for continuous series; degrades gracefully to 0.0
        when no statistics exist at all (fresh install).
        """
        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )
        rows = last_stats.get(statistic_id)
        if not rows:
            return 0.0
        latest_sum = float(rows[0].get("sum") or 0.0)
        changes = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            end,
            {statistic_id},
            "hour",
            None,
            {"change"},
        )
        return latest_sum - sum(
            float(row.get("change") or 0.0) for row in changes.get(statistic_id, [])
        )

    async def async_warm_cache(self) -> None:
        """Populate the rolling 62-day cache after a restart.

        Does NOT write statistics — they already exist from prior runs. Re-writing
        them mid-series with a fresh `prior_sum` lookup would corrupt cumulative
        counter semantics. We just refill `self.cache` for the lagging sensors.
        """
        end = datetime.now(tz=UTC)
        start = end - timedelta(days=ROLLING_CACHE_DAYS)
        await self._fetch_window(start, end, write_stats=False, force_start=True)
        await self.async_ensure_baselines()
        await self._flush_baselines_if_dirty()
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
        cost_streams = self.cost_streams_for(meter)
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
        # to rewrite history for the requested window — chaining off the
        # *current latest* sum would offset every bucket by whatever the
        # cumulative happens to be right now. Instead, seed from the sum the
        # series had just before the window start (0.0 on a fresh install):
        # rewritten rows stay consistent with any older rows outside the
        # window and the sum column never drops mid-series.
        prior_sums: dict[str, float] = {}
        if write_stats:
            for stream in streams:
                prior_sums[stream.statistic_id] = (
                    await self._sum_before_window(stream.statistic_id, start, end)
                    if force_start
                    else await self._prior_sum_for_stream(stream)
                )
        # Per-cost-stream resume points and prior sums (electricity only).
        # Skip the reads entirely when no NPS client is attached — cost writes
        # are gated on ``self._nps is not None`` below, so the reads would
        # produce no observable effect and pay an extra recorder round-trip
        # per stream per fetch.
        cost_per_stream_start: dict[str, datetime | None] = {}
        cost_prior_sums: dict[str, float] = {}
        if write_stats and cost_streams and self._nps is not None:
            for cstream in cost_streams:
                # Reuse _latest_seen_for_stream/_prior_sum_for_stream by passing
                # a StatisticStream-shaped shim — both methods only need
                # ``statistic_id``.
                fake = StatisticStream(
                    statistic_id=cstream.statistic_id,
                    name=cstream.name,
                    unit=cstream.unit,
                    kind=cstream.kind,
                )
                cost_per_stream_start[cstream.statistic_id] = (
                    None if force_start else await self._latest_seen_for_stream(fake)
                )
                cost_prior_sums[cstream.statistic_id] = (
                    await self._sum_before_window(cstream.statistic_id, start, end)
                    if force_start
                    else await self._prior_sum_for_stream(fake)
                )
        tariff = self._build_tariff() if cost_streams else None
        cursor = fetch_start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=MAX_DAYS_PER_REQUEST), end)
            results = await self._client.get_metering_data(
                cursor, chunk_end, self.resolution, eics=[meter.eic]
            )
            # Fetch prices for this chunk once if any cost stream needs them.
            prices: dict[datetime, float] | None = None
            if write_stats and cost_streams and self._nps is not None:
                # Evict hours whose NPS quarters may still be settling so
                # they are re-fetched with complete data — without this, an
                # hour first priced mid-settlement keeps its partial-quarter
                # mean for the process lifetime (the failure mode clear_cache
                # exists for, applied to the regular tick path).
                self._nps.evict_after(
                    datetime.now(tz=UTC) - timedelta(hours=NPS_PRICE_SETTLE_HOURS)
                )
                try:
                    prices = await self._nps.async_get_prices(cursor, chunk_end)
                    self.last_nps_error = None
                except NpsError as err:
                    self.last_nps_error = str(err)
                    _LOGGER.warning("NPS fetch failed for %s..%s: %s", cursor, chunk_end, err)
                    prices = None
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
                # Cost streams (electricity only, prices available)
                if write_stats and cost_streams and prices is not None and tariff is not None:
                    for cstream in cost_streams:
                        threshold = cost_per_stream_start[cstream.statistic_id]
                        relevant_c = (
                            md.intervals
                            if threshold is None
                            else [i for i in md.intervals if i.period_start >= threshold]
                        )
                        cost_prior_sums[cstream.statistic_id] = await async_write_cost_statistics(
                            self.hass,
                            cstream,
                            relevant_c,
                            prices,
                            tariff,
                            prior_sum=cost_prior_sums[cstream.statistic_id],
                        )
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
        baseline = self.baselines.get((eic, kind))
        while bucket and bucket[0].period_start < cutoff:
            expiring = bucket.popleft()
            # If a baseline exists and this interval falls between reset_at
            # and the cache window, preserve its contribution before it ages
            # out — otherwise the cumulative-since-reset sensor would silently
            # lose data once a long-running baseline pushes past 62 days.
            if baseline is None or expiring.period_start < baseline.reset_at:
                continue
            value = interval_value(expiring, kind)
            if value is None:
                continue
            self.baselines[(eic, kind)] = CumulativeBaseline(
                reset_at=baseline.reset_at,
                frozen_sum=baseline.frozen_sum + float(value),
            )
            baseline = self.baselines[(eic, kind)]
            self._baselines_dirty = True

    def cumulative_since_reset(self, eic: str, kind: Kind) -> float | None:
        """Return the cumulative consumption/production since the baseline reset.

        Sums raw cache intervals at or after ``baseline.reset_at`` and adds
        ``baseline.frozen_sum`` for intervals that have already aged out of
        the rolling cache. Returns ``None`` when no baseline exists yet so
        the sensor can report unavailable rather than fabricating a zero.
        """
        baseline = self.baselines.get((eic, kind))
        if baseline is None:
            return None
        total = baseline.frozen_sum
        for ival in self.cache.get((eic, kind), ()):
            if ival.period_start < baseline.reset_at:
                continue
            value = interval_value(ival, kind)
            if value is None:
                continue
            total += float(value)
        return round(total, 3)

    async def async_ensure_baselines(self) -> None:
        """Capture a baseline for every (meter, kind) that lacks one.

        Per the install-time design choice, the cumulative sensor starts at
        zero and counts forward from the install moment. We capture
        ``reset_at = now`` unconditionally — backfilled historical intervals
        are filtered out by the ``period_start >= reset_at`` check in
        ``cumulative_since_reset``, so capturing before data arrives is safe.
        Idempotent: existing baselines (including user-triggered resets) are
        left alone.
        """
        now = datetime.now(tz=UTC)
        for meter in self.meters:
            for kind in (Kind.CONSUMPTION, Kind.PRODUCTION):
                key = (meter.eic, kind)
                if key in self.baselines:
                    continue
                self.baselines[key] = CumulativeBaseline(reset_at=now)
                self._baselines_dirty = True

    async def async_reset_cumulative(self, eic: str, kind: Kind) -> None:
        """Move ``reset_at`` to now so the cumulative sensor reads 0 again."""
        self.baselines[(eic, kind)] = CumulativeBaseline(reset_at=datetime.now(tz=UTC))
        self._baselines_dirty = True
        await self._flush_baselines_if_dirty()
        self.async_update_listeners()

    async def async_set_cumulative_reset_at(
        self,
        reset_at: datetime,
        *,
        kinds: tuple[Kind, ...] = (Kind.CONSUMPTION, Kind.PRODUCTION),
    ) -> None:
        """Service-driven baseline rewind/forward.

        Lets the user restore a previous ``reset_at`` (e.g., after an
        unrelated HA restart unintentionally recreated the baseline) without
        the cumulative jumping back to zero. ``frozen_sum`` is also reset
        since the rebuild walks the cache forward from the new anchor.
        """
        for meter in self.meters:
            for kind in kinds:
                self.baselines[(meter.eic, kind)] = CumulativeBaseline(reset_at=reset_at)
                self._baselines_dirty = True
        await self._flush_baselines_if_dirty()
        self.async_update_listeners()

    async def async_load_baselines(self) -> None:
        """Hydrate ``self.baselines`` from the Store, if one is attached."""
        if self._store is None:
            return
        data = await self._store.async_load()
        if not data:
            _LOGGER.info("No persisted baselines found; starting fresh")
            return
        entries = (data.get("baselines") or {}).items()
        loaded = 0
        for raw_key, raw_val in entries:
            try:
                eic, kind_value = raw_key.rsplit("|", 1)
                kind = Kind(kind_value)
                self.baselines[(eic, kind)] = CumulativeBaseline(
                    reset_at=datetime.fromisoformat(raw_val["reset_at"]),
                    frozen_sum=float(raw_val.get("frozen_sum", 0.0)),
                )
                loaded += 1
            except (KeyError, ValueError) as err:
                _LOGGER.warning("Skipping malformed baseline entry %r: %s", raw_key, err)
        _LOGGER.info("Loaded %d baseline(s) from storage", loaded)

    async def _flush_baselines_if_dirty(self) -> None:
        if not self._baselines_dirty:
            return
        await self._save_baselines()
        self._baselines_dirty = False

    async def _save_baselines(self) -> None:
        if self._store is None:
            return
        payload = {
            "baselines": {
                f"{eic}|{kind.value}": {
                    "reset_at": b.reset_at.isoformat(),
                    "frozen_sum": b.frozen_sum,
                }
                for (eic, kind), b in self.baselines.items()
            }
        }
        await self._store.async_save(payload)
