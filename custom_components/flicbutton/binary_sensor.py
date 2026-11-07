"""Binary sensors: button pressed, and flicd server connectivity."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FlicConfigEntry
from .entity import FlicButtonEntity, FlicServerEntity, async_setup_button_entities
from .hub import FlicHub, HubEvent, HubEventKind


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FlicConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([FlicConnectedSensor(entry.runtime_data, entry)])
    async_setup_button_entities(
        hass,
        entry,
        async_add_entities,
        lambda hub, bd_addr: [FlicPressedSensor(hub, bd_addr)],
    )


class FlicPressedSensor(FlicButtonEntity, BinarySensorEntity):
    _attr_translation_key = "pressed"

    def __init__(self, hub: FlicHub, bd_addr: str) -> None:
        super().__init__(hub, bd_addr)
        self._attr_unique_id = f"{bd_addr}_pressed"

    @callback
    def _handle_hub_event(self, event: HubEvent) -> None:
        if event.kind is HubEventKind.PRESSED and event.bd_addr == self.bd_addr:
            self._attr_is_on = bool(event.value)
            self.async_write_ha_state()
        else:
            super()._handle_hub_event(event)


class FlicConnectedSensor(FlicServerEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: FlicHub, entry: FlicConfigEntry) -> None:
        super().__init__(hub, entry)
        self._attr_unique_id = f"{entry.unique_id}_connected"

    @property
    def is_on(self) -> bool:
        return self.hub.connected
