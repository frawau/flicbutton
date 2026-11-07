"""Sensors: button battery, and the flicd Bluetooth controller state."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FlicConfigEntry
from .const import CONTROLLER_STATES
from .entity import FlicButtonEntity, FlicServerEntity, async_setup_button_entities
from .hub import FlicHub, HubEvent, HubEventKind


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FlicConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([FlicControllerStateSensor(entry.runtime_data, entry)])
    async_setup_button_entities(
        hass,
        entry,
        async_add_entities,
        lambda hub, bd_addr: [FlicBatterySensor(hub, bd_addr)],
    )


class FlicBatterySensor(FlicButtonEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: FlicHub, bd_addr: str) -> None:
        super().__init__(hub, bd_addr)
        self._attr_unique_id = f"{bd_addr}_battery"

    @property
    def native_value(self) -> int | None:
        return self.hub.battery.get(self.bd_addr)

    @callback
    def _handle_hub_event(self, event: HubEvent) -> None:
        if event.kind is HubEventKind.BATTERY and event.bd_addr == self.bd_addr:
            self.async_write_ha_state()
        else:
            super()._handle_hub_event(event)


class FlicControllerStateSensor(FlicServerEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CONTROLLER_STATES
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "controller_state"

    def __init__(self, hub: FlicHub, entry: FlicConfigEntry) -> None:
        super().__init__(hub, entry)
        self._attr_unique_id = f"{entry.unique_id}_controller_state"

    @property
    def native_value(self) -> str | None:
        state = self.hub.controller_state
        return None if state is None else state.name.lower()
