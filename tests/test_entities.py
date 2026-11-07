"""Tests for the flicbutton event, binary_sensor and sensor entities."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.flicbutton.const import DOMAIN
from custom_components.flicbutton.flicd import BluetoothControllerState, ClickType
from tests.fake_flicd import FakeFlicd

pytestmark = pytest.mark.usefixtures("socket_enabled")

A = "80:e4:da:71:2b:3c"
B = "80:e4:da:71:2b:3d"
C = "80:e4:da:71:2b:3e"

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


def _listener_id(fake: FakeFlicd, addr: str) -> int:
    return [
        f["listener_id"]
        for cmd, f in fake.commands
        if cmd == "CmdCreateBatteryStatusListener" and f["bd_addr"] == addr
    ][-1]


def _entry(fake: FakeFlicd, **options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=fake.bd_addr,
        data={"host": "127.0.0.1", "port": fake.port},
        options=options,
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, fake: FakeFlicd) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    # The fake records commands on receipt; wait for the last setup one.
    await _until(
        lambda: _count(fake, "CmdCreateBatteryStatusListener") >= len(fake.buttons)
    )


def _eid(hass: HomeAssistant, domain: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)


def _state(hass: HomeAssistant, domain: str, unique_id: str):
    entity_id = _eid(hass, domain, unique_id)
    assert entity_id is not None, unique_id
    return hass.states.get(entity_id)


async def _send_channel(fake: FakeFlicd, event: str, addr: str, click_type) -> None:
    await fake.send(
        event,
        conn_id=_conn_id(fake, addr),
        click_type=click_type,
        was_queued=False,
        time_diff=0,
    )


async def _click(fake: FakeFlicd, addr: str, click_type: ClickType) -> None:
    await _send_channel(fake, "EvtButtonSingleOrDoubleClickOrHold", addr, click_type)


async def _updown(fake: FakeFlicd, addr: str, click_type: ClickType) -> None:
    await _send_channel(fake, "EvtButtonUpOrDown", addr, click_type)


async def _unload_all(hass: HomeAssistant, *entries: MockConfigEntry) -> None:
    for entry in entries:
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)


@pytest.fixture
async def loaded(hass: HomeAssistant, fake_flicd):
    fake_flicd.buttons = {A: {}, B: {}}
    entry = _entry(fake_flicd)
    await _setup(hass, entry, fake_flicd)
    yield entry
    await _unload_all(hass, entry)


async def test_entities_created_per_button(hass, loaded, fake_flicd):
    ent_reg = er.async_get(hass)
    for addr in (A, B):
        for domain, suffix in BUTTON_ENTITIES:
            entity_id = _eid(hass, domain, f"{addr}_{suffix}")
            assert entity_id is not None, (addr, suffix)
            assert ent_reg.async_get(entity_id).config_entry_id == loaded.entry_id
    server = fake_flicd.bd_addr
    connected = _state(hass, "binary_sensor", f"{server}_connected")
    assert connected.state == STATE_ON
    assert connected.attributes["device_class"] == "connectivity"
    controller = _state(hass, "sensor", f"{server}_controller_state")
    assert controller.state == "attached"
    assert controller.attributes["options"] == ["detached", "resetting", "attached"]
    entries = er.async_entries_for_config_entry(ent_reg, loaded.entry_id)
    assert len(entries) == 2 * 3 + 2
    for unique_id in (f"{server}_connected", f"{server}_controller_state"):
        entry = next(e for e in entries if e.unique_id == unique_id)
        assert entry.entity_category == "diagnostic"
    battery = next(e for e in entries if e.unique_id == f"{A}_battery")
    assert battery.entity_category == "diagnostic"


async def test_event_entity_fires(hass, loaded, fake_flicd):
    await _click(fake_flicd, A, ClickType.ButtonHold)
    await _until(
        lambda: _state(hass, "event", f"{A}_event").attributes.get("event_type")
        == "hold"
    )
    state = _state(hass, "event", f"{A}_event")
    assert state.attributes["queued_time"] == 0
    assert state.attributes["event_types"] == ["single", "double", "hold"]
    assert _state(hass, "event", f"{B}_event").attributes.get("event_type") is None


async def test_event_types_exclude_ignored(hass, fake_flicd):
    fake_flicd.buttons = {A: {}}
    entry = _entry(fake_flicd, ignored_click_types=["hold"])
    await _setup(hass, entry, fake_flicd)
    try:
        state = _state(hass, "event", f"{A}_event")
        assert state.attributes["event_types"] == ["single", "double"]
    finally:
        await _unload_all(hass, entry)


async def test_pressed_binary_sensor(hass, loaded, fake_flicd):
    await _updown(fake_flicd, A, ClickType.ButtonDown)
    await _until(lambda: _state(hass, "binary_sensor", f"{A}_pressed").state == "on")
    await _updown(fake_flicd, A, ClickType.ButtonUp)
    await _until(lambda: _state(hass, "binary_sensor", f"{A}_pressed").state == "off")
    assert (
        "device_class" not in _state(hass, "binary_sensor", f"{A}_pressed").attributes
    )


async def test_battery_sensor(hass, loaded, fake_flicd):
    async def _battery(level: int) -> None:
        await fake_flicd.send(
            "EvtBatteryStatus",
            listener_id=_listener_id(fake_flicd, A),
            battery_percentage=level,
            timestamp=1762500000,
        )

    await _battery(87)
    await _until(lambda: _state(hass, "sensor", f"{A}_battery").state == "87")
    state = _state(hass, "sensor", f"{A}_battery")
    assert state.attributes["unit_of_measurement"] == "%"
    assert state.attributes["device_class"] == "battery"
    assert state.attributes["state_class"] == "measurement"
    await _battery(-1)
    await _until(lambda: _state(hass, "sensor", f"{A}_battery").state == STATE_UNKNOWN)


async def test_unavailable_on_drop_and_back(hass, loaded, fake_flicd):
    server = fake_flicd.bd_addr
    hub = loaded.runtime_data

    def _button_states() -> list[str]:
        return [
            _state(hass, domain, f"{addr}_{suffix}").state
            for addr in (A, B)
            for domain, suffix in BUTTON_ENTITIES
        ]

    assert STATE_UNAVAILABLE not in _button_states()
    await fake_flicd.drop_clients()
    await _until(lambda: not hub.connected)
    await hass.async_block_till_done()
    assert set(_button_states()) == {STATE_UNAVAILABLE}
    assert _state(hass, "binary_sensor", f"{server}_connected").state == STATE_OFF
    assert _state(hass, "sensor", f"{server}_controller_state").state == STATE_UNKNOWN

    # The hub reconnects after its 1 s backoff.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1.1))
    await _until(lambda: hub.connected)
    await hass.async_block_till_done()
    assert STATE_UNAVAILABLE not in _button_states()
    assert _state(hass, "binary_sensor", f"{server}_connected").state == STATE_ON
    assert _state(hass, "sensor", f"{server}_controller_state").state == "attached"


async def test_controller_state_change(hass, loaded, fake_flicd):
    server = fake_flicd.bd_addr
    await fake_flicd.send(
        "EvtBluetoothControllerStateChange", state=BluetoothControllerState.Detached
    )
    await _until(
        lambda: _state(hass, "sensor", f"{server}_controller_state").state == "detached"
    )
    assert _state(hass, "event", f"{A}_event").state == STATE_UNAVAILABLE
    assert _state(hass, "binary_sensor", f"{server}_connected").state == STATE_ON


async def test_new_button_entities_added_at_runtime(hass, loaded, fake_flicd):
    fake_flicd.buttons[C] = {}
    await fake_flicd.send("EvtNewVerifiedButton", bd_addr=C)
    await _until(
        lambda: all(
            _eid(hass, domain, f"{C}_{suffix}") is not None
            for domain, suffix in BUTTON_ENTITIES
        )
    )
    await hass.async_block_till_done()
    for domain, suffix in BUTTON_ENTITIES:
        state = _state(hass, domain, f"{C}_{suffix}")
        assert state is not None
        assert state.state != STATE_UNAVAILABLE


async def test_button_removed_removes_entities(hass, loaded, fake_flicd):
    entity_ids = [
        _eid(hass, domain, f"{B}_{suffix}") for domain, suffix in BUTTON_ENTITIES
    ]
    assert all(hass.states.get(e) is not None for e in entity_ids)
    await fake_flicd.send("EvtButtonDeleted", bd_addr=B, deleted_by_this_client=False)
    await _until(lambda: B not in loaded.runtime_data.buttons)
    await hass.async_block_till_done()
    ent_reg = er.async_get(hass)
    for entity_id in entity_ids:
        assert ent_reg.async_get(entity_id) is None
        assert hass.states.get(entity_id) is None
    assert _state(hass, "event", f"{A}_event").state != STATE_UNAVAILABLE


async def test_moved_button_entities_served_by_new_entry(hass, fake_flicd, caplog):
    caplog.set_level(logging.ERROR)
    fake_flicd.buttons = {A: {}}
    entry1 = _entry(fake_flicd)
    fake2 = FakeFlicd()
    fake2.bd_addr = "00:11:22:33:44:66"
    await fake2.start()
    entry2 = _entry(fake2)
    try:
        await _setup(hass, entry1, fake_flicd)
        await _setup(hass, entry2, fake2)
        entity_ids = [
            _eid(hass, domain, f"{A}_{suffix}") for domain, suffix in BUTTON_ENTITIES
        ]
        assert None not in entity_ids

        # The button is re-paired to the second server.
        fake2.buttons[A] = {}
        await fake2.send("EvtNewVerifiedButton", bd_addr=A)
        await _until(lambda: _count(fake2, "CmdCreateBatteryStatusListener") == 1)
        await hass.async_block_till_done()
        ent_reg = er.async_get(hass)
        for entity_id in entity_ids:
            assert ent_reg.async_get(entity_id).config_entry_id == entry2.entry_id
            assert hass.states.get(entity_id).state != STATE_UNAVAILABLE

        await _click(fake2, A, ClickType.ButtonSingleClick)
        await _until(
            lambda: hass.states.get(entity_ids[0]).attributes.get("event_type")
            == "single"
        )

        # entry1 no longer serves the moved button's entities.
        assert await hass.config_entries.async_unload(entry1.entry_id)
        await hass.async_block_till_done()
        for entity_id in entity_ids:
            assert hass.states.get(entity_id).state != STATE_UNAVAILABLE
        await _updown(fake2, A, ClickType.ButtonDown)
        await _until(lambda: hass.states.get(entity_ids[1]).state == STATE_ON)
        assert "already exists" not in caplog.text
        assert "already used" not in caplog.text
    finally:
        await _unload_all(hass, entry1, entry2)
        await fake2.drop_clients()
        await fake2.stop()
