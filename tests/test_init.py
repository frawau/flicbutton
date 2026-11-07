"""Tests for flicbutton entry setup, devices and device removal."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.flicbutton.const import DOMAIN
from tests.fake_flicd import FakeFlicd

pytestmark = pytest.mark.usefixtures("socket_enabled")

A = "80:e4:da:71:2b:3c"
B = "80:e4:da:71:2b:3d"


async def _until(predicate, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def _entry(fake: FakeFlicd) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=fake.bd_addr,
        data={"host": "127.0.0.1", "port": fake.port},
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _device(hass: HomeAssistant, entry, ident: str) -> dr.DeviceEntry | None:
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, ident), entry.entry_id
    )


async def _remove_via_ws(hass, hass_ws_client, entry, device) -> dict:
    assert await async_setup_component(hass, "config", {})
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "config/device_registry/remove_config_entry",
            "config_entry_id": entry.entry_id,
            "device_id": device.id,
        }
    )
    return await client.receive_json()


@pytest.fixture
async def loaded(hass: HomeAssistant, fake_flicd):
    """A loaded entry with two buttons; unloaded again before the fake stops."""
    fake_flicd.buttons = {A: {"flic_version": 1, "serial_number": "AA11"}, B: {}}
    entry = _entry(fake_flicd)
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    yield entry
    if entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_creates_server_and_button_devices(hass, loaded, fake_flicd):
    server = _device(hass, loaded, f"server_{fake_flicd.bd_addr}")
    assert server is not None
    assert server.name == "flicd 127.0.0.1"
    a = _device(hass, loaded, A)
    b = _device(hass, loaded, B)
    assert a and b
    for device in (a, b):
        assert device.via_device_id == server.id
        assert device.manufacturer == "Shortcut Labs"
    assert a.model == "Flic 1"
    assert a.serial_number == "AA11"
    assert a.sw_version == "1"
    assert a.name == f"Flic {A}"
    assert (
        len(dr.async_entries_for_config_entry(dr.async_get(hass), loaded.entry_id)) == 3
    )


async def test_setup_not_ready_when_unreachable(hass, fake_flicd):
    port = fake_flicd.port
    await fake_flicd.stop()
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="x", data={"host": "127.0.0.1", "port": port}
    )
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    await fake_flicd.start()  # fixture teardown stops it again


async def test_unload(hass, loaded, fake_flicd):
    assert await hass.config_entries.async_unload(loaded.entry_id)
    assert loaded.state is ConfigEntryState.NOT_LOADED
    await _until(lambda: not fake_flicd._writers)
    # Devices stay registered across an unload.
    assert _device(hass, loaded, A) is not None


async def test_remove_device_unpairs(hass, hass_ws_client, loaded, fake_flicd):
    device = _device(hass, loaded, A)
    response = await _remove_via_ws(hass, hass_ws_client, loaded, device)
    assert response["success"], response
    cmd = await fake_flicd.wait_for_command("CmdDeleteButton")
    assert cmd["bd_addr"] == A
    assert _device(hass, loaded, A) is None
    assert loaded.runtime_data.buttons.keys() == {B}


async def test_remove_device_refused_when_offline(
    hass, hass_ws_client, loaded, fake_flicd
):
    await fake_flicd.drop_clients()
    await _until(lambda: not loaded.runtime_data.connected)
    device = _device(hass, loaded, A)
    response = await _remove_via_ws(hass, hass_ws_client, loaded, device)
    assert not response["success"]
    assert _device(hass, loaded, A) is not None
    assert not [c for c, _ in fake_flicd.commands if c == "CmdDeleteButton"]


async def test_remove_device_refused_on_timeout(
    hass, hass_ws_client, loaded, fake_flicd
):
    async def _never(_bd_addr: str) -> None:
        await asyncio.Event().wait()

    device = _device(hass, loaded, A)
    with (
        patch("custom_components.flicbutton.CONNECT_TIMEOUT", 0.05),
        patch.object(loaded.runtime_data, "async_delete_button", _never),
    ):
        response = await _remove_via_ws(hass, hass_ws_client, loaded, device)
    assert not response["success"]
    assert _device(hass, loaded, A) is not None


async def test_remove_server_device_refused(hass, hass_ws_client, loaded, fake_flicd):
    server = _device(hass, loaded, f"server_{fake_flicd.bd_addr}")
    response = await _remove_via_ws(hass, hass_ws_client, loaded, server)
    assert not response["success"]
    assert _device(hass, loaded, f"server_{fake_flicd.bd_addr}") is not None
    assert not [c for c, _ in fake_flicd.commands if c == "CmdDeleteButton"]


async def test_new_button_gets_device_at_runtime(hass, loaded, fake_flicd):
    c = "80:e4:da:71:2b:3e"
    fake_flicd.buttons[c] = {"color": "black"}
    await fake_flicd.send("EvtNewVerifiedButton", bd_addr=c)
    await _until(lambda: _device(hass, loaded, c) is not None)
    device = _device(hass, loaded, c)
    server = _device(hass, loaded, f"server_{fake_flicd.bd_addr}")
    assert device.via_device_id == server.id
    assert device.hw_version == "black"


async def test_button_deleted_by_flicd_removes_device(hass, loaded, fake_flicd):
    await fake_flicd.send("EvtButtonDeleted", bd_addr=B, deleted_by_this_client=False)
    await _until(lambda: _device(hass, loaded, B) is None)
    assert _device(hass, loaded, A) is not None


async def test_button_moves_between_entries(hass, fake_flicd):
    fake_flicd.buttons = {A: {"flic_version": 1}}
    entry1 = _entry(fake_flicd)
    fake2 = FakeFlicd()
    fake2.bd_addr = "00:11:22:33:44:66"
    await fake2.start()
    entry2 = _entry(fake2)
    try:
        await _setup(hass, entry1)
        await _setup(hass, entry2)
        assert entry1.state is ConfigEntryState.LOADED
        assert entry2.state is ConfigEntryState.LOADED

        device = _device(hass, entry1, A)
        ent_reg = er.async_get(hass)
        entity_ids = sorted(
            e.entity_id for e in er.async_entries_for_device(ent_reg, device.id)
        )
        assert len(entity_ids) == 3  # event, pressed, battery

        # The button is re-paired to the second server.
        fake2.buttons[A] = {"flic_version": 1}
        await fake2.send("EvtNewVerifiedButton", bd_addr=A)
        await _until(lambda: _device(hass, entry2, A) is not None)

        moved = _device(hass, entry2, A)
        assert moved.id == device.id
        assert _device(hass, entry1, A) is None
        server2 = _device(hass, entry2, f"server_{fake2.bd_addr}")
        assert moved.via_device_id == server2.id
        assert (
            sorted(e.entity_id for e in er.async_entries_for_device(ent_reg, moved.id))
            == entity_ids
        )
        for entity_id in entity_ids:
            assert ent_reg.async_get(entity_id).config_entry_id == entry2.entry_id

        # Reloading the first entry (it still lists the button) must not move it back.
        assert await hass.config_entries.async_reload(entry1.entry_id)
        await hass.async_block_till_done()
        assert entry1.state is ConfigEntryState.LOADED
        assert A in entry1.runtime_data.buttons
        assert _device(hass, entry1, A) is None
        assert _device(hass, entry2, A).id == device.id
        for entity_id in entity_ids:
            assert ent_reg.async_get(entity_id).config_entry_id == entry2.entry_id

        # The first server now drops the button: nothing of the move is undone.
        await fake_flicd.send(
            "EvtButtonDeleted", bd_addr=A, deleted_by_this_client=False
        )
        await _until(lambda: A not in entry1.runtime_data.buttons)
        assert _device(hass, entry2, A) is not None
        assert ent_reg.async_get(entity_ids[0]) is not None
    finally:
        for entry in (entry1, entry2):
            if entry.state is ConfigEntryState.LOADED:
                await hass.config_entries.async_unload(entry.entry_id)
        await fake2.stop()


async def test_delete_first_then_pair_elsewhere_creates_new_device(hass, fake_flicd):
    """Observed behaviour: flicd deleting a button removes its device.

    The device, and with it its entities, is gone when the button is later paired
    on another server, so entry2 gets a fresh device (new id) with new entities.
    Keeping entities across a re-pair needs the announce on the new server to
    come before the delete on the old one.
    """
    fake_flicd.buttons = {A: {"flic_version": 1}}
    entry1 = _entry(fake_flicd)
    fake2 = FakeFlicd()
    fake2.bd_addr = "00:11:22:33:44:66"
    await fake2.start()
    entry2 = _entry(fake2)
    try:
        await _setup(hass, entry1)
        await _setup(hass, entry2)
        old = _device(hass, entry1, A)
        ent_reg = er.async_get(hass)
        assert len(er.async_entries_for_device(ent_reg, old.id)) == 3

        await fake_flicd.send(
            "EvtButtonDeleted", bd_addr=A, deleted_by_this_client=False
        )
        await _until(lambda: _device(hass, entry1, A) is None)
        assert er.async_entries_for_device(ent_reg, old.id) == []

        fake2.buttons[A] = {"flic_version": 1}
        await fake2.send("EvtNewVerifiedButton", bd_addr=A)
        await _until(lambda: _device(hass, entry2, A) is not None)

        new = _device(hass, entry2, A)
        assert new.id != old.id
        await hass.async_block_till_done()
        new_entities = er.async_entries_for_device(ent_reg, new.id)
        assert len(new_entities) == 3
        assert {e.config_entry_id for e in new_entities} == {entry2.entry_id}
    finally:
        for entry in (entry1, entry2):
            if entry.state is ConfigEntryState.LOADED:
                await hass.config_entries.async_unload(entry.entry_id)
        await fake2.stop()
