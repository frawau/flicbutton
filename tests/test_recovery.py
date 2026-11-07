"""End to end: the flicd host reboots and the buttons recover without an HA restart."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.flicbutton.const import DOMAIN
from custom_components.flicbutton.flicd import ClickType
from tests.fake_flicd import FakeFlicd

pytestmark = pytest.mark.usefixtures("socket_enabled")

A = "80:e4:da:71:2b:3c"
B = "80:e4:da:71:2b:3d"
BUTTON_ENTITIES = (
    ("event", "event"),
    ("binary_sensor", "pressed"),
    ("sensor", "battery"),
)


async def _until(predicate, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def _count(fake: FakeFlicd, name: str) -> int:
    return sum(1 for cmd, _ in fake.commands if cmd == name)


def _conn_id(fake: FakeFlicd, addr: str) -> int:
    return [
        f["conn_id"]
        for cmd, f in fake.commands
        if cmd == "CmdCreateConnectionChannel" and f["bd_addr"] == addr
    ][-1]


def _states(hass: HomeAssistant) -> dict[str, str]:
    ent_reg = er.async_get(hass)
    states = {}
    for addr in (A, B):
        for domain, suffix in BUTTON_ENTITIES:
            entity_id = ent_reg.async_get_entity_id(domain, DOMAIN, f"{addr}_{suffix}")
            assert entity_id is not None, (addr, suffix)
            states[entity_id] = hass.states.get(entity_id).state
    return states


async def test_flicd_reboot_recovers_without_restart(
    hass: HomeAssistant, fake_flicd, caplog
):
    caplog.set_level(logging.DEBUG, logger="custom_components.flicbutton.hub")
    fake_flicd.buttons = {A: {}, B: {}}
    port = fake_flicd.port
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=fake_flicd.bd_addr,
        data={"host": "127.0.0.1", "port": port},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    await _until(lambda: _count(fake_flicd, "CmdCreateBatteryStatusListener") == 2)
    assert STATE_UNAVAILABLE not in _states(hass).values()

    # The Pi goes down: socket closed, port refuses connections.
    await fake_flicd.stop()
    await _until(
        lambda: set(_states(hass).values()) == {STATE_UNAVAILABLE}, timeout=5.0
    )

    # Several refused backoff rounds (1, 2, 4, 8 s) while it reboots.
    now = dt_util.utcnow()
    for attempt, delay in enumerate((1, 2, 4, 8), start=1):
        now += timedelta(seconds=delay)
        async_fire_time_changed(hass, now)
        # The attempt runs as a background task; wait until it was refused.
        await _until(
            lambda n=attempt: caplog.text.count("Reconnect to flicd failed") == n
        )
        assert set(_states(hass).values()) == {STATE_UNAVAILABLE}
    assert caplog.text.count("Reconnect to flicd failed") == 4
    assert entry.state is ConfigEntryState.LOADED

    # flicd is back on the same port with the same buttons.
    await fake_flicd.start(port)
    assert fake_flicd.port == port
    channels = _count(fake_flicd, "CmdCreateConnectionChannel")
    now += timedelta(seconds=16)  # the next backoff step
    async_fire_time_changed(hass, now)
    await _until(lambda: STATE_UNAVAILABLE not in _states(hass).values(), timeout=5.0)
    await _until(lambda: _count(fake_flicd, "CmdCreateBatteryStatusListener") == 4)
    assert _count(fake_flicd, "CmdCreateConnectionChannel") == channels + 2
    assert entry.state is ConfigEntryState.LOADED

    # A legacy automation on flic_click sees exactly what it saw before.
    clicks = async_capture_events(hass, "flic_click")
    await fake_flicd.send(
        "EvtButtonSingleOrDoubleClickOrHold",
        conn_id=_conn_id(fake_flicd, A),
        click_type=ClickType.ButtonDoubleClick,
        was_queued=False,
        time_diff=0,
    )
    await _until(lambda: clicks)
    await hass.async_block_till_done()
    assert [c.data for c in clicks] == [
        {
            "button_name": "flic_80e4da712b3c",
            "button_address": "80:e4:da:71:2b:3c",
            "click_type": "double",
            "queued_time": 0,
        }
    ]

    await hass.config_entries.async_unload(entry.entry_id)


async def test_button_held_during_outage_is_released(hass: HomeAssistant, fake_flicd):
    fake_flicd.buttons = {A: {}, B: {}}
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=fake_flicd.bd_addr,
        data={"host": "127.0.0.1", "port": fake_flicd.port},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _until(lambda: _count(fake_flicd, "CmdCreateBatteryStatusListener") == 2)
    pressed = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{A}_pressed"
    )

    await fake_flicd.send(
        "EvtButtonUpOrDown",
        conn_id=_conn_id(fake_flicd, A),
        click_type=ClickType.ButtonDown,
        was_queued=False,
        time_diff=0,
    )
    await _until(lambda: hass.states.get(pressed).state == "on")
    await fake_flicd.drop_clients()  # the ButtonUp is lost with the link
    await _until(lambda: hass.states.get(pressed).state == STATE_UNAVAILABLE)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await _until(lambda: hass.states.get(pressed).state == STATE_OFF)

    await hass.config_entries.async_unload(entry.entry_id)
