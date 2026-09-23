"""Button platform for Tariff Tracker."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_INTERVAL_SOURCE_ENTITY, DOMAIN
from .runtime import PlanRuntime


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime: PlanRuntime = hass.data[DOMAIN][entry.entry_id]
    entities = [
        ResetCostHistoryButton(runtime, entry),
        ResetMonthlyCostButton(runtime, entry),
    ]
    if runtime.options.get(CONF_INTERVAL_SOURCE_ENTITY):
        entities.append(ClearIntervalBackfillButton(runtime, entry))
    async_add_entities(entities)


class _BaseResetButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, runtime: PlanRuntime, entry: ConfigEntry, key: str, name: str) -> None:
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=runtime.plan_name,
            manufacturer="Tariff Tracker",
            model="Tariff Plan",
        )


class ResetCostHistoryButton(_BaseResetButton):
    def __init__(self, runtime: PlanRuntime, entry: ConfigEntry) -> None:
        super().__init__(runtime, entry, "reset_cost_history", "Reset cost history")

    async def async_press(self) -> None:
        await self._runtime.async_reset_costs()


class ResetMonthlyCostButton(_BaseResetButton):
    """Zero just this month's running cost/credit, leaving today's,
    billing period's, and tier/power tracking totals untouched."""

    def __init__(self, runtime: PlanRuntime, entry: ConfigEntry) -> None:
        super().__init__(runtime, entry, "reset_monthly_cost", "Reset monthly cost")

    async def async_press(self) -> None:
        await self._runtime.async_reset_costs(
            reset_today=False,
            reset_month=True,
            reset_billing_period=False,
            reset_power_tracking=False,
            reset_tier_usage=False,
        )


class ClearIntervalBackfillButton(_BaseResetButton):
    """Testing/debug aid, only present when a GloBird interval source is
    configured: forgets every interval slot seen so far AND fully resets
    this plan's cost/energy totals in the same action, so the next
    interval-array update reprocesses everything as brand new. Not part of
    normal operation - use it to watch the backfill mechanism run again
    without waiting for GloBird to publish new or revised data.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:refresh-circle"

    def __init__(self, runtime: PlanRuntime, entry: ConfigEntry) -> None:
        super().__init__(
            runtime,
            entry,
            "clear_interval_backfill",
            "Clear interval backfill history (testing)",
        )

    async def async_press(self) -> None:
        await self._runtime.async_reset_interval_backfill()
