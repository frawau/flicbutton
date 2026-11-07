"""The Flic button (flicd) integration."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import CONNECT_TIMEOUT, DOMAIN
from .flicd.client import ButtonInfo, FlicdConnectionError
from .hub import FlicHub, HubEvent, HubEventKind, HubOptions

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR]
# The platform modules arrive with the entities; Task 6 forwards PLATFORMS.
_FORWARDED_PLATFORMS: list[Platform] = []

MANUFACTURER = "Shortcut Labs"

type FlicConfigEntry = ConfigEntry[FlicHub]


def _server_identifier(entry: ConfigEntry) -> tuple[str, str]:
    return (DOMAIN, f"server_{entry.unique_id}")


@callback
def async_claim_button_device(
    hass: HomeAssistant,
    entry: FlicConfigEntry,
    info: ButtonInfo,
    fresh_pairing: bool = False,
) -> dr.DeviceEntry | None:
    """Create or update the device of a button for this entry.

    A device owned by another flicbutton entry is moved here, together with its
    entities, only on a fresh pairing. Otherwise (sync of a server that still
    lists the button) it is left untouched and None is returned.
    """
    dev_reg = dr.async_get(hass)
    server = dev_reg.async_get_device_by_identifier(
        _server_identifier(entry), entry.entry_id
    )
    identifier = (DOMAIN, info.bd_addr)

    if dev_reg.async_get_device_by_identifier(identifier, entry.entry_id) is None:
        for other in dev_reg.async_get_devices(identifiers={identifier}):
            owner = hass.config_entries.async_get_entry(other.config_entry_id)
            if owner is None or owner.domain != DOMAIN:
                continue
            if not fresh_pairing:
                return None
            _LOGGER.info(
                "Moving button %s from %s to %s", info.bd_addr, owner.title, entry.title
            )
            # Entities first: moving the device deletes the old entry's entities.
            ent_reg = er.async_get(hass)
            for entity in er.async_entries_for_device(
                ent_reg, other.id, include_disabled_entities=True
            ):
                if entity.config_entry_id == owner.entry_id:
                    ent_reg.async_update_entity(
                        entity.entity_id, config_entry_id=entry.entry_id
                    )
            dev_reg.async_update_device(other.id, new_config_entry_id=entry.entry_id)
            break

    return dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={identifier},
        via_device_id=server.id if server else None,
        manufacturer=MANUFACTURER,
        model=f"Flic {info.flic_version}",
        serial_number=info.serial_number,
        sw_version=str(info.firmware_version),
        hw_version=info.color,
        name=f"Flic {info.bd_addr}",
    )


@callback
def _async_remove_button_device(
    hass: HomeAssistant, entry: FlicConfigEntry, bd_addr: str
) -> None:
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device_by_identifier((DOMAIN, bd_addr), entry.entry_id)
    if device is not None:
        dev_reg.async_remove_device(device.id)


async def async_setup_entry(hass: HomeAssistant, entry: FlicConfigEntry) -> bool:
    hub = FlicHub(
        hass,
        entry.entry_id,
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        HubOptions.from_entry_options(entry.options),
    )
    try:
        await hub.async_start()
    except FlicdConnectionError as err:
        raise ConfigEntryNotReady(f"Cannot connect to flicd: {err}") from err
    entry.async_on_unload(hub.async_stop)
    entry.runtime_data = hub

    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={_server_identifier(entry)},
        manufacturer=MANUFACTURER,
        model="flicd",
        name=f"flicd {entry.data[CONF_HOST]}",
    )
    # No await between here and the subscription: no event can be missed.
    for info in hub.buttons.values():
        async_claim_button_device(hass, entry, info)

    @callback
    def _on_hub_event(event: HubEvent) -> None:
        if event.bd_addr is None:
            return
        if event.kind is HubEventKind.BUTTON_ADDED:
            async_claim_button_device(
                hass, entry, hub.buttons[event.bd_addr], bool(event.value)
            )
        elif event.kind is HubEventKind.BUTTON_REMOVED:
            _async_remove_button_device(hass, entry, event.bd_addr)

    entry.async_on_unload(hub.async_subscribe(_on_hub_event))

    await hass.config_entries.async_forward_entry_setups(entry, _FORWARDED_PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FlicConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, _FORWARDED_PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: FlicConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Unpair the button from flicd; refuse the server device and when offline."""
    bd_addr = next(
        (ident for domain, ident in device.identifiers if domain == DOMAIN), None
    )
    if bd_addr is None or bd_addr.startswith("server_"):
        return False
    hub = getattr(entry, "runtime_data", None)
    if hub is None or not hub.connected:
        _LOGGER.warning("Cannot unpair %s: not connected to flicd", bd_addr)
        return False
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            await hub.async_delete_button(bd_addr)
    except (TimeoutError, FlicdConnectionError):
        _LOGGER.warning("flicd did not confirm unpairing %s", bd_addr)
        return False
    return True
