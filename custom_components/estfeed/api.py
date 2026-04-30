"""Estfeed API client and data types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Self

from .const import CommodityType


def _parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 datetime string from the Estfeed API."""
    # Estfeed returns "...Z"; Python's fromisoformat handles "+00:00".
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


@dataclass(frozen=True, slots=True)
class Period:
    """An access-validity period for a metering point."""

    start: datetime
    end: datetime | None


@dataclass(frozen=True, slots=True)
class MeteringPoint:
    """A metering point exposed by the API key."""

    eic: str
    commodity_type: CommodityType
    periods: list[Period]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return cls(
            eic=raw["eic"],
            commodity_type=CommodityType(raw["commodityType"]),
            periods=[
                Period(
                    start=_parse_iso(p["from"]),
                    end=_parse_iso(p["to"]) if p.get("to") else None,
                )
                for p in raw.get("periods", [])
            ],
        )


@dataclass(frozen=True, slots=True)
class AccountingInterval:
    """One accounting interval from the metering-data response."""

    period_start: datetime
    consumption_kwh: float | None
    production_kwh: float | None
    consumption_m3: float | None
    production_m3: float | None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return cls(
            period_start=_parse_iso(raw["periodStart"]),
            consumption_kwh=raw.get("consumptionKwh"),
            production_kwh=raw.get("productionKwh"),
            consumption_m3=raw.get("consumptionM3"),
            production_m3=raw.get("productionM3"),
        )


@dataclass(frozen=True, slots=True)
class MeterError:
    """Per-meter error embedded in a 200 response."""

    id: str
    message: str
    code: str
    trace_id: str
    args: list[Any]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return cls(
            id=raw["id"],
            message=raw["message"],
            code=raw["code"],
            trace_id=raw["traceId"],
            args=list(raw.get("args", [])),
        )


@dataclass(frozen=True, slots=True)
class MeterData:
    """Metering data for one meter."""

    eic: str
    intervals: list[AccountingInterval]
    error: MeterError | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return cls(
            eic=raw["meteringPointEic"],
            intervals=[
                AccountingInterval.from_dict(it) for it in raw.get("accountingIntervals", [])
            ],
            error=MeterError.from_dict(raw["error"]) if raw.get("error") else None,
        )
