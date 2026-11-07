"""Tests for flicbutton diagnostics."""

from __future__ import annotations

import json

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.flicbutton.const import DOMAIN
from tests.fake_flicd import FakeFlicd

pytestmark = pytest.mark.usefixtures("socket_enabled")

A = "80:e4:da:71:2b:3c"
B = "80:e4:da:71:2b:3d"


async def test_diagnostics(hass: HomeAssistant, hass_client, fake_flicd: FakeFlicd):
    fake_flicd.buttons = {A: {"flic_version": 1, "serial_number": "AA11"}, B: {}}
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=fake_flicd.bd_addr,
        data={"host": "127.0.0.1", "port": fake_flicd.port},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert await async_setup_component(hass, "diagnostics", {})
    try:
        diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
        json.dumps(diag)
        assert diag["entry"]["data"]["host"] == "**REDACTED**"
        assert diag["entry"]["data"]["port"] == fake_flicd.port
        assert diag["connected"] is True
        assert diag["available"] is True
        assert diag["server_info"]["controller_state"] == "Attached"
        assert diag["server_info"]["bd_addr_type"] == "PublicBdAddrType"
        assert {b["bd_addr"] for b in diag["buttons"]} == {A, B}
        by_addr = {b["bd_addr"]: b for b in diag["buttons"]}
        assert by_addr[A]["serial_number"] == "AA11"
        assert "battery" in by_addr[A]
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await fake_flicd.drop_clients()
