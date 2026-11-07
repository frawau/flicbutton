"""Diagnostics for the Flic button (flicd) integration."""

from __future__ import annotations

from dataclasses import asdict
from enum import IntEnum
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .hub import FlicHub

TO_REDACT = {"host", "title"}


def _plain(value: Any) -> Any:
    """Make a dataclass dict JSON-safe: enums by name."""
    if isinstance(value, IntEnum):
        return value.name
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    hub: FlicHub = entry.runtime_data
    server_info: dict[str, Any] | None = None
    if hub.server_info is not None:
        server_info = _plain(asdict(hub.server_info))
        # server_info is a connect-time snapshot; the hub tracks live changes.
        live = hub.controller_state
        if live is not None:
            server_info["controller_state"] = live.name
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "connected": hub.connected,
        "available": hub.available,
        "server_info": server_info,
        "buttons": [
            {**_plain(asdict(info)), "battery": hub.battery.get(addr)}
            for addr, info in hub.buttons.items()
        ],
    }
