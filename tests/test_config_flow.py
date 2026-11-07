"""Tests for the flicbutton config flow."""

from __future__ import annotations

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.flicbutton.const import DOMAIN

pytestmark = pytest.mark.usefixtures("socket_enabled")


async def _start(hass: HomeAssistant, host: str, port: int):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: host, CONF_PORT: port}
    )


async def _unload_all(hass: HomeAssistant) -> None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)


async def test_user_flow_creates_entry(hass: HomeAssistant, fake_flicd) -> None:
    result = await _start(hass, "127.0.0.1", fake_flicd.port)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "flicd 127.0.0.1"
    assert result["data"] == {"host": "127.0.0.1", "port": fake_flicd.port}
    assert result["options"] == {}
    entry = result["result"]
    assert entry.unique_id == fake_flicd.bd_addr.lower()
    await hass.async_block_till_done()
    await _unload_all(hass)


async def test_user_flow_cannot_connect(hass: HomeAssistant, fake_flicd) -> None:
    # Reserve a port that nothing listens on.
    dead = fake_flicd.port
    await fake_flicd.stop()
    result = await _start(hass, "127.0.0.1", dead)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    await fake_flicd.start()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: fake_flicd.port}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await _unload_all(hass)


async def test_user_flow_already_configured_updates_host(
    hass: HomeAssistant, fake_flicd
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=fake_flicd.bd_addr.lower(),
        data={CONF_HOST: "192.0.2.1", CONF_PORT: 5551},
    )
    entry.add_to_hass(hass)

    result = await _start(hass, "127.0.0.1", fake_flicd.port)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data == {CONF_HOST: "127.0.0.1", CONF_PORT: fake_flicd.port}
