"""Tests for the asyncio flicd client against the fake server."""

from __future__ import annotations

import asyncio
import socket
import struct
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from custom_components.flicbutton.flicd import (
    BluetoothControllerState,
    ClickType,
    ConnectionStatus,
    DisconnectReason,
    LatencyMode,
    RemovedReason,
    ScanWizardResult,
)
from custom_components.flicbutton.flicd.client import (
    ClickStream,
    FlicdClient,
    FlicdConnectionError,
    ScanResult,
)

pytestmark = pytest.mark.usefixtures("socket_enabled")

A = "80:e4:da:71:2b:3c"
B = "80:e4:da:71:2b:3d"


@pytest.fixture
async def client(fake_flicd):
    client = FlicdClient("127.0.0.1", fake_flicd.port)
    await client.connect()
    yield client
    await client.close()


async def _until(predicate, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_connect_refused_raises():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    client = FlicdClient("127.0.0.1", port)
    with pytest.raises(FlicdConnectionError):
        await client.connect(timeout=2.0)
    assert client.connected is False


async def test_get_info_lists_verified_buttons(fake_flicd, client):
    fake_flicd.buttons = {A: {}}
    info = await client.get_info()
    assert info.verified_buttons == [A]
    assert info.controller_state is BluetoothControllerState.Attached
    assert client.connected is True


async def test_button_info_fifo(fake_flicd, client):
    fake_flicd.buttons = {
        A: {"serial_number": "AA11", "color": "white"},
        B: {"serial_number": "BB22"},
    }
    first, second = await asyncio.gather(
        client.get_button_info(A), client.get_button_info(B)
    )
    assert (first.bd_addr, first.serial_number, first.color) == (A, "AA11", "white")
    assert (second.bd_addr, second.serial_number, second.uuid) == (B, "BB22", None)


async def test_ping_timeout(fake_flicd, client):
    fake_flicd.answer_pings = False
    with pytest.raises(TimeoutError):
        await client.ping(0.2)


async def test_ping_ok(client):
    await client.ping(1.0)


async def test_channel_click_dispatch(fake_flicd, client):
    listener = MagicMock()
    conn_id = client.open_channel(A, listener)
    cmd = await fake_flicd.wait_for_command("CmdCreateConnectionChannel")
    assert cmd["conn_id"] == conn_id
    assert cmd["bd_addr"] == A
    assert cmd["latency_mode"] == LatencyMode.NormalLatency
    assert cmd["auto_disconnect_time"] == 511

    await fake_flicd.send(
        "EvtButtonSingleOrDoubleClickOrHold",
        conn_id=conn_id,
        click_type=ClickType.ButtonHold,
        was_queued=False,
        time_diff=0,
    )
    await fake_flicd.send(
        "EvtButtonUpOrDown",
        conn_id=conn_id,
        click_type=ClickType.ButtonDown,
        was_queued=True,
        time_diff=4,
    )
    # Ignored opcodes, then a sentinel to know all were processed.
    await fake_flicd.send(
        "EvtButtonClickOrHold",
        conn_id=conn_id,
        click_type=ClickType.ButtonClick,
        was_queued=False,
        time_diff=0,
    )
    await fake_flicd.send(
        "EvtButtonSingleOrDoubleClick",
        conn_id=conn_id,
        click_type=ClickType.ButtonSingleClick,
        was_queued=False,
        time_diff=0,
    )
    await fake_flicd.send(
        "EvtConnectionStatusChanged",
        conn_id=conn_id,
        connection_status=ConnectionStatus.Ready,
        disconnect_reason=DisconnectReason.Unspecified,
    )
    await fake_flicd.send(
        "EvtConnectionChannelRemoved",
        conn_id=conn_id,
        removed_reason=RemovedReason.RemovedByThisClient,
    )
    await fake_flicd.send(
        "EvtButtonUpOrDown",
        conn_id=999,
        click_type=ClickType.ButtonUp,
        was_queued=False,
        time_diff=0,
    )
    await client.ping(1.0)

    listener.on_click.assert_called_once_with(ClickType.ButtonHold, False, 0)
    listener.on_up_down.assert_called_once_with(ClickType.ButtonDown, True, 4)
    listener.on_status.assert_called_once_with(
        ConnectionStatus.Ready, DisconnectReason.Unspecified
    )
    listener.on_removed.assert_called_once_with(RemovedReason.RemovedByThisClient)


_CLICK_EVENTS = (
    ("EvtButtonClickOrHold", ClickType.ButtonClick),
    ("EvtButtonSingleOrDoubleClick", ClickType.ButtonSingleClick),
    ("EvtButtonSingleOrDoubleClickOrHold", ClickType.ButtonHold),
)


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        (ClickStream.CLICK_OR_HOLD, ClickType.ButtonClick),
        (ClickStream.SINGLE_OR_DOUBLE_CLICK, ClickType.ButtonSingleClick),
        (ClickStream.SINGLE_OR_DOUBLE_CLICK_OR_HOLD, ClickType.ButtonHold),
        (None, None),
    ],
)
async def test_channel_routes_only_its_click_stream(
    fake_flicd, client, stream, expected
):
    listener = MagicMock()
    conn_id = client.open_channel(A, listener, click_stream=stream)
    await fake_flicd.wait_for_command("CmdCreateConnectionChannel")
    for event, click_type in _CLICK_EVENTS:
        await fake_flicd.send(
            event, conn_id=conn_id, click_type=click_type, was_queued=False, time_diff=0
        )
    await client.ping(1.0)  # sentinel: all events above were processed
    if expected is None:
        listener.on_click.assert_not_called()
    else:
        listener.on_click.assert_called_once_with(expected, False, 0)


async def test_close_channel_sends_remove(fake_flicd, client):
    conn_id = client.open_channel(A, MagicMock())
    client.close_channel(conn_id)
    cmd = await fake_flicd.wait_for_command("CmdRemoveConnectionChannel")
    assert cmd["conn_id"] == conn_id


async def test_battery_listener_utc(fake_flicd, client):
    callback = MagicMock()
    listener_id = client.add_battery_listener(A, callback)
    cmd = await fake_flicd.wait_for_command("CmdCreateBatteryStatusListener")
    assert (cmd["listener_id"], cmd["bd_addr"]) == (listener_id, A)

    await fake_flicd.send(
        "EvtBatteryStatus",
        listener_id=listener_id,
        battery_percentage=87,
        timestamp=1762500000,
    )
    await fake_flicd.send(
        "EvtBatteryStatus",
        listener_id=listener_id,
        battery_percentage=-1,
        timestamp=1762500000,
    )
    await fake_flicd.send(
        "EvtBatteryStatus", listener_id=12345, battery_percentage=5, timestamp=0
    )
    await client.ping(1.0)

    expected = datetime(2025, 11, 7, 7, 20, tzinfo=UTC)
    assert callback.call_args_list[0].args == (87, expected)
    assert callback.call_args_list[1].args == (None, expected)
    assert callback.call_args_list[0].args[1].tzinfo is UTC
    assert callback.call_count == 2


async def test_scan_wizard_success(fake_flicd, client):
    fake_flicd.scan_wizard_script = [
        ("EvtScanWizardFoundPrivateButton", {}),
        ("EvtScanWizardFoundPublicButton", {"bd_addr": A, "name": "F0"}),
        ("EvtScanWizardButtonConnected", {}),
        ("EvtScanWizardCompleted", {"result": ScanWizardResult.WizardSuccess}),
    ]
    progress: list[str] = []
    result = await client.run_scan_wizard(progress.append)
    assert result == ScanResult(ScanWizardResult.WizardSuccess, A, "F0")
    assert progress == ["found_private", "found_public", "connected"]


async def test_scan_wizard_cancel(fake_flicd, client):
    task = asyncio.create_task(client.run_scan_wizard())
    cmd = await fake_flicd.wait_for_command("CmdCreateScanWizard")
    with pytest.raises(RuntimeError):
        await client.run_scan_wizard()
    await client.cancel_scan_wizard()
    cancel = await fake_flicd.wait_for_command("CmdCancelScanWizard")
    assert cancel["scan_wizard_id"] == cmd["scan_wizard_id"]
    await fake_flicd.send(
        "EvtScanWizardCompleted",
        scan_wizard_id=cmd["scan_wizard_id"],
        result=ScanWizardResult.WizardCancelledByUser,
    )
    result = await task
    assert result.result is ScanWizardResult.WizardCancelledByUser
    assert result.bd_addr is None


async def test_delete_button(fake_flicd, client):
    fake_flicd.buttons = {A: {}}
    await client.delete_button(A)
    assert ("CmdDeleteButton", {"bd_addr": A}) in fake_flicd.commands
    assert A not in fake_flicd.buttons


async def test_server_events_forwarded(fake_flicd, client):
    seen: list[tuple[str, dict]] = []
    client.on_server_event = lambda name, fields: seen.append((name, fields))
    await fake_flicd.send("EvtNewVerifiedButton", bd_addr=B)
    await fake_flicd.send(
        "EvtBluetoothControllerStateChange", state=BluetoothControllerState.Detached
    )
    await fake_flicd.send(
        "EvtNoSpaceForNewConnection", max_concurrently_connected_buttons=1
    )
    await client.ping(1.0)
    assert [n for n, _ in seen] == [
        "EvtNewVerifiedButton",
        "EvtBluetoothControllerStateChange",
        "EvtNoSpaceForNewConnection",
    ]
    assert seen[0][1] == {"bd_addr": B}


async def test_unsolicited_button_deleted_forwarded(fake_flicd, client):
    seen: list[tuple[str, dict]] = []
    client.on_server_event = lambda name, fields: seen.append((name, fields))
    await fake_flicd.send("EvtButtonDeleted", bd_addr=A, deleted_by_this_client=False)
    await client.ping(1.0)
    assert seen == [
        ("EvtButtonDeleted", {"bd_addr": A, "deleted_by_this_client": False})
    ]


async def test_drop_calls_on_disconnect_and_fails_pending(fake_flicd, client):
    fake_flicd.answer_get_info = False
    on_disconnect = MagicMock()
    client.on_disconnect = on_disconnect
    pending = asyncio.create_task(client.get_info())
    await fake_flicd.wait_for_command("CmdGetInfo")
    await fake_flicd.drop_clients()
    with pytest.raises(FlicdConnectionError):
        await pending
    await _until(lambda: not client.connected)
    on_disconnect.assert_called_once_with()
    with pytest.raises(FlicdConnectionError):
        await client.get_info()


async def test_close_does_not_call_on_disconnect(fake_flicd):
    client = FlicdClient("127.0.0.1", fake_flicd.port)
    await client.connect()
    on_disconnect = MagicMock()
    client.on_disconnect = on_disconnect
    await client.close()
    await client.close()
    assert client.connected is False
    on_disconnect.assert_not_called()


async def test_malformed_packet_ignored_connection_survives(fake_flicd, client):
    # Valid opcode (EvtButtonUpOrDown = 4) but the body is truncated.
    await fake_flicd.send_raw(struct.pack("<HB", 3, 4) + b"\x01\x02")
    await fake_flicd.send_raw(b"\x01\x00\xff")  # unknown opcode
    await client.ping(1.0)
    assert client.connected is True


async def test_cancelled_button_info_late_response_not_given_to_next(
    fake_flicd, client
):
    fake_flicd.answer_button_info = False
    first = asyncio.create_task(client.get_button_info(A))
    await _until(lambda: len(fake_flicd.commands) == 1)
    first.cancel()
    second = asyncio.create_task(client.get_button_info(B))
    await _until(lambda: len(fake_flicd.commands) == 2)
    for addr in (A, B):
        await fake_flicd.send(
            "EvtGetButtonInfoResponse",
            bd_addr=addr,
            uuid=None,
            color=None,
            serial_number=None,
            flic_version=2,
            firmware_version=1,
        )
    assert (await second).bd_addr == B


async def test_cancelled_get_info_late_response_not_given_to_next(fake_flicd, client):
    fake_flicd.answer_get_info = False
    first = asyncio.create_task(client.get_info())
    await _until(lambda: len(fake_flicd.commands) == 1)
    first.cancel()
    second = asyncio.create_task(client.get_info())
    await _until(lambda: len(fake_flicd.commands) == 2)
    for addrs in ([A], [B]):
        await fake_flicd.send(
            "EvtGetInfoResponse",
            bluetooth_controller_state=BluetoothControllerState.Attached,
            my_bd_addr="00:11:22:33:44:55",
            my_bd_addr_type=0,
            max_pending_connections=3,
            max_concurrently_connected_buttons=20,
            current_pending_connections=0,
            currently_no_space_for_new_connection=0,
            bd_addr_of_verified_buttons=addrs,
        )
    assert (await second).verified_buttons == [B]
