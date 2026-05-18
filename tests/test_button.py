"""Tests for the Estfeed reset button."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.estfeed.api import MeteringPoint, Period
from custom_components.estfeed.button import CumulativeResetButton
from custom_components.estfeed.const import CommodityType, Kind


def _meter() -> MeteringPoint:
    return MeteringPoint(
        eic="38ZEE-00720089-N",
        commodity_type=CommodityType.ELECTRICITY,
        periods=[Period(start=datetime(2019, 7, 27, 21, tzinfo=UTC), end=None)],
    )


@pytest.mark.asyncio
async def test_reset_button_calls_coordinator():
    coordinator = MagicMock()
    coordinator.async_reset_cumulative = AsyncMock()
    coordinator.slug = "home"

    button = CumulativeResetButton(coordinator, _meter(), Kind.CONSUMPTION)
    await button.async_press()

    coordinator.async_reset_cumulative.assert_awaited_once_with(
        "38ZEE-00720089-N", Kind.CONSUMPTION
    )
    assert button.unique_id == "estfeed_home_consumption_cumulative_reset_089n"


def test_production_reset_button_disabled_by_default():
    """Mirrors the production sensor convention: most users don't generate,
    so the production reset button shouldn't clutter the entity list."""
    coordinator = MagicMock()
    coordinator.slug = "home"

    consumption_btn = CumulativeResetButton(coordinator, _meter(), Kind.CONSUMPTION)
    production_btn = CumulativeResetButton(coordinator, _meter(), Kind.PRODUCTION)

    assert consumption_btn.entity_registry_enabled_default is True
    assert production_btn.entity_registry_enabled_default is False
