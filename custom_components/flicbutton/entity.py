"""Base entities for the Flic button (flicd) integration."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FlicConfigEntry, server_identifier
from .const import DOMAIN, signal_button_owned, signal_button_released
from .hub import FlicHub, HubEvent, HubEventKind


@callback
def async_setup_button_entities(
    hass: HomeAssistant,
    entry: FlicConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[FlicHub, str], Iterable[Entity]],
) -> None:
    """Add entities for the buttons this entry owns, now and when it claims more.

    Ownership is decided by the claim in __init__ (the button's device belongs to
    this entry); platforms never claim themselves.
    """
    hub = entry.runtime_data
    dev_reg = dr.async_get(hass)
    async_add_entities(
        entity
        for bd_addr in hub.buttons
        if dev_reg.async_get_device_by_identifier((DOMAIN, bd_addr), entry.entry_id)
        for entity in factory(hub, bd_addr)
    )

    @callback
    def _on_owned(bd_addr: str) -> None:
        async_add_entities(factory(hub, bd_addr))

    entry.async_on_unload(
        async_dispatcher_connect(hass, signal_button_owned(entry.entry_id), _on_owned)
    )


class _FlicEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: FlicHub) -> None:
        self.hub = hub

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.hub.async_subscribe(self._handle_hub_event))

    @callback
    def _handle_hub_event(self, event: HubEvent) -> None:
        # The hub also emits AVAILABILITY after every controller state change.
        if event.kind is HubEventKind.AVAILABILITY:
            self.async_write_ha_state()


class FlicButtonEntity(_FlicEntity):
    """An entity of one button; subclasses handle their own event kinds."""

    def __init__(self, hub: FlicHub, bd_addr: str) -> None:
        super().__init__(hub)
        self.bd_addr = bd_addr
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, bd_addr)})

    @property
    def available(self) -> bool:
        return self.hub.available and self.bd_addr in self.hub.buttons

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_button_released(self.hub.entry_id),
                self._on_released,
            )
        )

    @callback
    def _on_released(self, bd_addr: str) -> None:
        """The device moved to another entry: free the entity_id, keep the registry.

        HA keeps a live entity running when its registry entry is re-pointed to
        another config entry, so the new owner could not add its entity. The
        removal runs eagerly to completion (nothing in it yields), so the entity_id
        is free before the new owner adds its entities.
        """
        if bd_addr == self.bd_addr:
            self.hass.async_create_task(self.async_remove(), eager_start=True)


class FlicServerEntity(_FlicEntity):
    """An entity of the flicd server; always available so it can report off."""

    _attr_available = True

    def __init__(self, hub: FlicHub, entry: FlicConfigEntry) -> None:
        super().__init__(hub)
        self._attr_device_info = DeviceInfo(identifiers={server_identifier(entry)})
