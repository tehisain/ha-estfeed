"""Constants for the Estfeed integration."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Final

DOMAIN: Final = "estfeed"

CONF_CLIENT_ID: Final = "client_id"
CONF_CLIENT_SECRET: Final = "client_secret"
CONF_FRIENDLY_NAME: Final = "friendly_name"
CONF_RESOLUTION: Final = "resolution"
CONF_BACKFILL_MONTHS: Final = "backfill_months"

DEFAULT_FRIENDLY_NAME: Final = "Estfeed"
DEFAULT_BACKFILL_MONTHS: Final = 12
MAX_BACKFILL_MONTHS: Final = 84
MIN_BACKFILL_MONTHS: Final = 1

CONF_VAT_PERCENT: Final = "vat_percent"
CONF_MARGIN_EUR_PER_KWH: Final = "margin_eur_per_kwh"
DEFAULT_VAT_PERCENT: Final = 22.0
DEFAULT_MARGIN_EUR_PER_KWH: Final = 0.0
# Gas cost pricing: off, a fixed price, or the Elering gas exchange index.
CONF_GAS_PRICE_MODE: Final = "gas_price_mode"
# Fixed gas price (EUR/kWh, excl. VAT), used in fixed mode.
CONF_GAS_PRICE_EUR_PER_KWH: Final = "gas_price_eur_per_kwh"
DEFAULT_GAS_PRICE_EUR_PER_KWH: Final = 0.0
# Seller margin over the exchange index (EUR/kWh, excl. VAT), used in exchange mode.
CONF_GAS_MARGIN_EUR_PER_KWH: Final = "gas_margin_eur_per_kwh"
DEFAULT_GAS_MARGIN_EUR_PER_KWH: Final = 0.0
# Trailing days of gas cost every tick re-prices: the exchange index for a
# gas day is published about a day after Estfeed delivers its usage.
GAS_REPRICE_DAYS: Final = 7

UPDATE_INTERVAL: Final = timedelta(hours=1)
ROLLING_CACHE_DAYS: Final = 62
DATA_FRESH_THRESHOLD: Final = timedelta(hours=30)

API_BASE_URL: Final = "https://estfeed.elering.ee"
KEYCLOAK_TOKEN_URL: Final = "https://kc.elering.ee/realms/elering-sso/protocol/openid-connect/token"
RATE_LIMIT_SECONDS: Final = 5.0
TOKEN_REFRESH_MARGIN_SECONDS: Final = 30
REQUEST_TIMEOUT_SECONDS: Final = 30
MAX_EICS_PER_REQUEST: Final = 10
MAX_DAYS_PER_REQUEST: Final = 31
RECENT_REQUESTS_BUFFER_SIZE: Final = 5

ATTRIBUTION: Final = "Data provided by Elering Estfeed"


class Resolution(StrEnum):
    """API resolution values."""

    QUARTER_HOUR = "fifteen_min"
    HOUR = "one_hour"
    DAY = "one_day"
    WEEK = "one_week"
    MONTH = "one_month"


class Kind(StrEnum):
    """Metering data kind."""

    CONSUMPTION = "consumption"
    PRODUCTION = "production"


class GasPriceMode(StrEnum):
    """How the gas cost statistic is priced."""

    OFF = "off"
    FIXED = "fixed"
    EXCHANGE = "exchange"


class CommodityType(StrEnum):
    """Estfeed commodity types."""

    ELECTRICITY = "ELECTRICITY"
    NATURAL_GAS = "NATURAL_GAS"


UNIT_KWH: Final = "kWh"
UNIT_M3: Final = "m³"


def unit_for(commodity: CommodityType) -> str:
    """Unit published for a commodity: kWh for electricity, m³ for gas."""
    return UNIT_KWH if commodity == CommodityType.ELECTRICITY else UNIT_M3
