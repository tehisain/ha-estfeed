"""Tests for statistics helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.estfeed.api import AccountingInterval
from custom_components.estfeed.const import Kind
from custom_components.estfeed.statistics import (
    build_statistic_id,
    compute_statistic_rows,
    eic_suffix,
)


def test_eic_suffix():
    assert eic_suffix("38ZEE-00720089-N") == "089N"
    assert eic_suffix("XYZW-12345678-AB") == "78AB"


def test_build_statistic_id_single_meter():
    assert (
        build_statistic_id("home", Kind.CONSUMPTION, "089N", multi_meter=False)
        == "estfeed:home_consumption_089N"
    )


def test_build_statistic_id_multi_meter():
    # When multiple meters share an entry, suffix is appended even if slug carries it.
    assert (
        build_statistic_id("home", Kind.PRODUCTION, "089N", multi_meter=True)
        == "estfeed:home_production_089N"
    )


def test_compute_statistic_rows_first_chunk():
    intervals = [
        AccountingInterval(
            period_start=datetime(2026, 4, 27, h, tzinfo=UTC),
            consumption_kwh=1.0 + h * 0.1,
            production_kwh=0.0,
            consumption_m3=None,
            production_m3=None,
        )
        for h in range(3)
    ]
    rows = compute_statistic_rows(intervals, Kind.CONSUMPTION, prior_sum=0.0)
    assert len(rows) == 3
    assert rows[0]["start"] == datetime(2026, 4, 27, 0, tzinfo=UTC)
    assert rows[0]["sum"] == 1.0
    assert rows[1]["sum"] == 2.1  # 1.0 + 1.1
    assert rows[2]["sum"] == 3.3  # 2.1 + 1.2
    # state mirrors sum for counter semantics
    assert rows[0]["state"] == rows[0]["sum"]


def test_compute_statistic_rows_continuation_with_prior_sum():
    intervals = [
        AccountingInterval(
            period_start=datetime(2026, 4, 28, 0, tzinfo=UTC),
            consumption_kwh=2.0,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        )
    ]
    rows = compute_statistic_rows(intervals, Kind.CONSUMPTION, prior_sum=100.0)
    assert rows[0]["sum"] == 102.0


def test_compute_statistic_rows_skips_none_values():
    intervals = [
        AccountingInterval(
            period_start=datetime(2026, 4, 27, 0, tzinfo=UTC),
            consumption_kwh=None,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        )
    ]
    rows = compute_statistic_rows(intervals, Kind.CONSUMPTION, prior_sum=0.0)
    assert rows == []


def test_compute_statistic_rows_sorts_by_start():
    """Rows must be ascending by start regardless of input order."""
    intervals = [
        AccountingInterval(
            period_start=datetime(2026, 4, 27, 2, tzinfo=UTC),
            consumption_kwh=0.5,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        ),
        AccountingInterval(
            period_start=datetime(2026, 4, 27, 0, tzinfo=UTC),
            consumption_kwh=1.0,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        ),
        AccountingInterval(
            period_start=datetime(2026, 4, 27, 1, tzinfo=UTC),
            consumption_kwh=2.0,
            production_kwh=None,
            consumption_m3=None,
            production_m3=None,
        ),
    ]
    rows = compute_statistic_rows(intervals, Kind.CONSUMPTION, prior_sum=0.0)
    starts = [r["start"] for r in rows]
    assert starts == sorted(starts)
    sums = [r["sum"] for r in rows]
    assert sums == [1.0, 3.0, 3.5]
