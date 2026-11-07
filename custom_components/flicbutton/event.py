"""Click event entity of a Flic button."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FlicConfigEntry
from .const import CLICK_TYPES
from .entity import FlicButtonEntity, async_setup_button_entities
from .hub import FlicHub, HubEvent, HubEventKind


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FlicConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_setup_button_entities(
        hass,
        entry,
        async_add_entities,
        lambda hub, bd_addr: [FlicClickEvent(hub, bd_addr)],
    )


class FlicClickEvent(FlicButtonEntity, EventEntity):
    _attr_translation_key = "click"

    def __init__(self, hub: FlicHub, bd_addr: str) -> None:
        super().__init__(hub, bd_addr)
        self._attr_unique_id = f"{bd_addr}_event"
        self._attr_event_types = [
            t for t in CLICK_TYPES if t not in hub.options.ignored_click_types
        ]

    @callback
    def _handle_hub_event(self, event: HubEvent) -> None:
        if event.kind is HubEventKind.CLICK and event.bd_addr == self.bd_addr:
            click_type, queued_time = event.value
            self._trigger_event(click_type, {"queued_time": queued_time})
            self.async_write_ha_state()
        else:
            super()._handle_hub_event(event)
