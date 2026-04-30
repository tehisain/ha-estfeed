"""Estfeed API client and data types."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Self

import aiohttp

from .const import (
    KEYCLOAK_TOKEN_URL,
    REQUEST_TIMEOUT_SECONDS,
    TOKEN_REFRESH_MARGIN_SECONDS,
    CommodityType,
)

_LOGGER = logging.getLogger(__name__)


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


class EstfeedClient:
    """HTTP client for the Estfeed public API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        client_id: str,
        client_secret: str,
    ) -> None:
        self._session = session
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._token_expires_at: float = 0.0  # monotonic
        self._token_lock = asyncio.Lock()

    async def _ensure_token(self) -> str:
        """Return a valid bearer token, fetching/refreshing as needed."""
        async with self._token_lock:
            now = time.monotonic()
            if self._token and now < self._token_expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
                return self._token
            self._token, self._token_expires_at = await self._fetch_token(now)
            return self._token

    async def _fetch_token(self, now_monotonic: float) -> tuple[str, float]:
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        async with self._session.post(
            KEYCLOAK_TOKEN_URL,
            data=data,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
        ) as resp:
            payload = await resp.json()
            if resp.status != 200 or "access_token" not in payload:
                raise EstfeedAuthError(
                    f"Token request failed: {resp.status} {payload.get('error', '')}"
                )
            return payload["access_token"], now_monotonic + float(payload.get("expires_in", 60))


class EstfeedAuthError(Exception):
    """Authentication / authorisation failure."""
