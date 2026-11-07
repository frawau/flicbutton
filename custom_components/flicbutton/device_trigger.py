"""Device triggers for Flic buttons."""

from __future__ import annotations

import probatio
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import CLICK_TYPES, DOMAIN, EVENT_FLIC_CLICK
from .hub import HubOptions

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {probatio.Required(CONF_TYPE): probatio.In(CLICK_TYPES)}
)


def _button_address(device: dr.DeviceEntry) -> str | None:
    """Return the bd_addr of a button device, None for any other device."""
    for domain, ident in device.identifiers:
        if domain == DOMAIN and not ident.startswith("server_"):
            return ident
    return None


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str]]:
    """List the triggers of a button: the click types that are not ignored."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None or _button_address(device) is None:
        return []
    entry = hass.config_entries.async_get_entry(device.config_entry_id)
    if entry is None:
        return []
    # The hub holds the options in force; fall back to the stored ones if the
    # entry is not loaded.
    runtime = getattr(entry, "runtime_data", None)
    options = (
        runtime.options if runtime else HubOptions.from_entry_options(entry.options)
    )
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: click_type,
        }
        for click_type in CLICK_TYPES
        if click_type not in options.ignored_click_types
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach an event trigger on the post-filter flic_click event."""
    device = dr.async_get(hass).async_get(config[CONF_DEVICE_ID])
    bd_addr = _button_address(device) if device else None
    if bd_addr is None:
        raise ValueError(f"Device {config[CONF_DEVICE_ID]} is not a Flic button")
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: EVENT_FLIC_CLICK,
            event_trigger.CONF_EVENT_DATA: {
                "button_address": bd_addr,
                "click_type": config[CONF_TYPE],
            },
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
