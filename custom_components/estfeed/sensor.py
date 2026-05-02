"""Estfeed sensor entities and lagging-period helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .api import AccountingInterval
from .const import Kind

if TYPE_CHECKING:
    from collections import deque


class LaggingPeriod(StrEnum):
    YESTERDAY = "yesterday"
    MONTH_TO_DATE = "month_to_date"
    PREVIOUS_MONTH = "previous_month"


def window_for_period(
    period: LaggingPeriod, *, now: datetime, tz: ZoneInfo
) -> tuple[datetime, datetime]:
    """Return [start, end) for the given period in the given tz."""
    local_now = now.astimezone(tz)
    today_local = datetime(local_now.year, local_now.month, local_now.day, tzinfo=tz)
    if period == LaggingPeriod.YESTERDAY:
        return today_local - timedelta(days=1), today_local
    if period == LaggingPeriod.MONTH_TO_DATE:
        month_start = today_local.replace(day=1)
        return month_start, local_now
    if period == LaggingPeriod.PREVIOUS_MONTH:
        first_of_this_month = today_local.replace(day=1)
        last_month_end = first_of_this_month
        last_month_start = (first_of_this_month - timedelta(days=1)).replace(day=1)
        return last_month_start, last_month_end
    raise ValueError(f"unknown period: {period}")


def _interval_value(ival: AccountingInterval, kind: Kind) -> float | None:
    if kind == Kind.CONSUMPTION:
        return ival.consumption_kwh if ival.consumption_kwh is not None else ival.consumption_m3
    return ival.production_kwh if ival.production_kwh is not None else ival.production_m3


def sum_for_period(
    intervals: list[AccountingInterval] | deque[AccountingInterval],
    kind: Kind,
    period: LaggingPeriod,
    *,
    now: datetime,
    tz: ZoneInfo,
) -> float:
    """Sum kWh/m³ values that fall within the given local period."""
    start_local, end_local = window_for_period(period, now=now, tz=tz)
    start_utc = start_local.astimezone(now.tzinfo or ZoneInfo("UTC"))
    end_utc = end_local.astimezone(now.tzinfo or ZoneInfo("UTC"))
    total = 0.0
    for ival in intervals:
        if start_utc <= ival.period_start < end_utc:
            v = _interval_value(ival, kind)
            if v is not None:
                total += float(v)
    return total
