"""Tests for flicbutton device triggers."""

from __future__ import annotations

import pytest
from homeassistant.components import automation
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_get_device_automations,
    async_mock_service,
)

from custom_components.flicbutton.const import DOMAIN
from custom_components.flicbutton.flicd import ClickType
from tests.test_entities import (
    A,
    B,
    _click,
    _entry,
    _setup,
    _unload_all,
    _until,
)

pytestmark = pytest.mark.usefixtures("socket_enabled")


def _device_id(hass: HomeAssistant, entry: MockConfigEntry, ident: str) -> str:
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, ident), entry.entry_id
    )
    assert device is not None
    return device.id


async def _triggers(hass: HomeAssistant, device_id: str) -> list[dict]:
    """Triggers of this integration; entity-based ones come from the entities."""
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, device_id
    )
    return [t for t in triggers if "entity_id" not in t]


async def test_get_triggers_lists_three_types(hass, fake_flicd):
    fake_flicd.buttons = {A: {}}
    entry = _entry(fake_flicd)
    await _setup(hass, entry, fake_flicd)
    try:
        device_id = _device_id(hass, entry, A)
        triggers = await _triggers(hass, device_id)
        assert {t["type"] for t in triggers} == {"single", "double", "hold"}
        assert all(
            t["platform"] == "device"
            and t["domain"] == DOMAIN
            and t["device_id"] == device_id
            for t in triggers
        )
    finally:
        await _unload_all(hass, entry)


async def test_get_triggers_skips_ignored_types(hass, fake_flicd):
    fake_flicd.buttons = {A: {}}
    entry = _entry(fake_flicd, ignored_click_types=["hold"])
    await _setup(hass, entry, fake_flicd)
    try:
        triggers = await _triggers(hass, _device_id(hass, entry, A))
        assert {t["type"] for t in triggers} == {"single", "double"}
    finally:
        await _unload_all(hass, entry)


async def test_get_triggers_entry_not_loaded(hass, fake_flicd):
    fake_flicd.buttons = {A: {}}
    entry = _entry(fake_flicd, ignored_click_types=["double"])
    await _setup(hass, entry, fake_flicd)
    device_id = _device_id(hass, entry, A)
    await _unload_all(hass, entry)
    assert entry.state is ConfigEntryState.NOT_LOADED
    triggers = await _triggers(hass, device_id)
    assert {t["type"] for t in triggers} == {"single", "hold"}


async def test_server_device_has_no_triggers(hass, fake_flicd):
    fake_flicd.buttons = {A: {}}
    entry = _entry(fake_flicd)
    await _setup(hass, entry, fake_flicd)
    try:
        server_id = _device_id(hass, entry, f"server_{fake_flicd.bd_addr}")
        assert await _triggers(hass, server_id) == []
    finally:
        await _unload_all(hass, entry)


async def test_trigger_fires_automation(hass, fake_flicd):
    fake_flicd.buttons = {A: {}, B: {}}
    entry = _entry(fake_flicd)
    await _setup(hass, entry, fake_flicd)
    calls = async_mock_service(hass, "test", "automation")
    try:
        assert await async_setup_component(
            hass,
            automation.DOMAIN,
            {
                automation.DOMAIN: {
                    "trigger": {
                        "platform": "device",
                        "domain": DOMAIN,
                        "device_id": _device_id(hass, entry, A),
                        "type": "double",
                    },
                    "action": {
                        "service": "test.automation",
                        "data": {"trigger": "{{ trigger.event.data.click_type }}"},
                    },
                }
            },
        )
        await _click(fake_flicd, B, ClickType.ButtonDoubleClick)
        await _click(fake_flicd, A, ClickType.ButtonSingleClick)
        await hass.async_block_till_done()
        assert calls == []
        await _click(fake_flicd, A, ClickType.ButtonDoubleClick)
        await _until(lambda: calls)
        await hass.async_block_till_done()
        assert len(calls) == 1
        assert calls[0].data["trigger"] == "double"
    finally:
        await _unload_all(hass, entry)
