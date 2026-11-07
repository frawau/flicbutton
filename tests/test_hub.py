"""Tests for FlicHub against the fake flicd server."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.helpers import event as ha_event
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.flicbutton.const import (
    CONF_AUTO_PAIR,
    CONF_IGNORED_CLICK_TYPES,
    CONF_QUEUE_TIMEOUT,
)
from custom_components.flicbutton.flicd import (
    BluetoothControllerState,
    ClickType,
    ScanWizardResult,
)
from custom_components.flicbutton.flicd.client import (
    ClickStream,
    FlicdClient,
    FlicdConnectionError,
    ScanResult,
)
from custom_components.flicbutton.hub import (
    FlicHub,
    HubEvent,
    HubEventKind,
    HubOptions,
    PairingInProgress,
    click_stream_for,
)

pytestmark = pytest.mark.usefixtures("socket_enabled")

A = "80:e4:da:71:2b:3c"
B = "80:e4:da:71:2b:3d"
C = "80:e4:da:71:2b:3e"

CLICK = "EvtButtonSingleOrDoubleClickOrHold"
CLICK_OR_HOLD = "EvtButtonClickOrHold"
SINGLE_OR_DOUBLE = "EvtButtonSingleOrDoubleClick"
UPDOWN = "EvtButtonUpOrDown"


async def _until(predicate, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def _kinds(events: list[HubEvent], kind: HubEventKind) -> list[HubEvent]:
    return [e for e in events if e.kind is kind]


def _count(fake, name: str) -> int:
    return sum(1 for cmd, _ in fake.commands if cmd == name)


def _conn_id(fake, addr: str) -> int:
    """conn_id of the latest channel the hub opened for ``addr``."""
    return [
        f["conn_id"]
        for cmd, f in fake.commands
        if cmd == "CmdCreateConnectionChannel" and f["bd_addr"] == addr
    ][-1]


def _listener_id(fake, addr: str) -> int:
    return [
        f["listener_id"]
        for cmd, f in fake.commands
        if cmd == "CmdCreateBatteryStatusListener" and f["bd_addr"] == addr
    ][-1]


async def _fire(hass, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


@pytest.fixture
async def make_hub(hass, fake_flicd):
    hubs: list[FlicHub] = []

    async def _make(
        *, start: bool = True, port: int | None = None, **options
    ) -> tuple[FlicHub, list[HubEvent]]:
        hub = FlicHub(
            hass,
            "entry-1",
            "127.0.0.1",
            fake_flicd.port if port is None else port,
            HubOptions.from_entry_options(options),
        )
        events: list[HubEvent] = []
        hub.async_subscribe(events.append)
        hubs.append(hub)
        if start:
            await hub.async_start()
            # The fake records commands on receipt; wait for the last setup one.
            await _until(
                lambda: _count(fake_flicd, "CmdCreateBatteryStatusListener")
                >= len(hub.buttons)
            )
        return hub, events

    yield _make
    for hub in hubs:
        await hub.async_stop()


async def _click(fake, addr, click_type, was_queued=False, time_diff=0, event=CLICK):
    await fake.send(
        event,
        conn_id=_conn_id(fake, addr),
        click_type=click_type,
        was_queued=was_queued,
        time_diff=time_diff,
    )


async def _updown(fake, addr, click_type, was_queued=False, time_diff=0):
    await fake.send(
        UPDOWN,
        conn_id=_conn_id(fake, addr),
        click_type=click_type,
        was_queued=was_queued,
        time_diff=time_diff,
    )


def test_options_defaults():
    assert HubOptions.from_entry_options({}) == HubOptions(
        queue_timeout=3, ignored_click_types=[], auto_pair=False
    )
    assert HubOptions.from_entry_options(
        {CONF_QUEUE_TIMEOUT: 5, CONF_IGNORED_CLICK_TYPES: ["hold"], CONF_AUTO_PAIR: 1}
    ) == HubOptions(queue_timeout=5, ignored_click_types=["hold"], auto_pair=True)


async def test_start_syncs_buttons(fake_flicd, make_hub):
    fake_flicd.buttons = {A: {"serial_number": "AA11"}, B: {}}
    hub, events = await make_hub()
    assert set(hub.buttons) == {A, B}
    assert hub.buttons[A].serial_number == "AA11"
    added = _kinds(events, HubEventKind.BUTTON_ADDED)
    assert [e.bd_addr for e in added] == [A, B]
    assert [e.value for e in added] == [False, False]
    assert events[-1].kind is HubEventKind.AVAILABILITY
    assert hub.connected is True
    assert hub.available is True
    assert hub.server_info is not None
    assert hub.server_info.verified_buttons == [A, B]
    assert hub.battery == {A: None, B: None}
    assert _count(fake_flicd, "CmdCreateConnectionChannel") == 2
    assert _count(fake_flicd, "CmdCreateBatteryStatusListener") == 2


async def test_start_unreachable_raises(fake_flicd, make_hub):
    port = fake_flicd.port
    await fake_flicd.stop()
    hub, _ = await make_hub(start=False, port=port)
    with pytest.raises(FlicdConnectionError):
        await hub.async_start()
    assert hub.connected is False
    assert hub.available is False


async def test_click_fires_legacy_event(hass, fake_flicd, make_hub):
    fake_flicd.buttons = {A: {}}
    clicks = async_capture_events(hass, "flic_click")
    _, events = await make_hub()
    await _click(fake_flicd, A, ClickType.ButtonHold)
    await _until(lambda: clicks)
    await hass.async_block_till_done()
    assert len(clicks) == 1
    assert clicks[0].data == {
        "button_name": "flic_80e4da712b3c",
        "button_address": "80:e4:da:71:2b:3c",
        "click_type": "hold",
        "queued_time": 0,
    }
    assert [(e.bd_addr, e.value) for e in _kinds(events, HubEventKind.CLICK)] == [
        (A, ("hold", 0))
    ]


@pytest.mark.parametrize(
    ("ignored", "stream"),
    [
        ([], ClickStream.SINGLE_OR_DOUBLE_CLICK_OR_HOLD),
        (["single"], ClickStream.SINGLE_OR_DOUBLE_CLICK_OR_HOLD),
        (["double"], ClickStream.CLICK_OR_HOLD),
        (["double", "hold"], ClickStream.CLICK_OR_HOLD),
        (["single", "double"], ClickStream.CLICK_OR_HOLD),
        (["hold"], ClickStream.SINGLE_OR_DOUBLE_CLICK),
        (["single", "hold"], ClickStream.SINGLE_OR_DOUBLE_CLICK),
        (["single", "double", "hold"], None),
    ],
)
def test_click_stream_mirrors_legacy(ignored, stream):
    assert click_stream_for(ignored) is stream


async def test_ignore_double_uses_click_or_hold_stream(hass, fake_flicd, make_hub):
    fake_flicd.buttons = {A: {}}
    clicks = async_capture_events(hass, "flic_click")
    _, events = await make_hub(ignored_click_types=["double"])
    # Not the subscribed stream: dropped by the client.
    await _click(fake_flicd, A, ClickType.ButtonDoubleClick)
    await _updown(fake_flicd, A, ClickType.ButtonDown)
    await _click(fake_flicd, A, ClickType.ButtonClick, event=CLICK_OR_HOLD)
    await _click(fake_flicd, A, ClickType.ButtonHold, event=CLICK_OR_HOLD)
    await _until(lambda: len(clicks) == 2)
    await hass.async_block_till_done()
    assert clicks[0].data == {
        "button_name": "flic_80e4da712b3c",
        "button_address": A,
        "click_type": "single",
        "queued_time": 0,
    }
    assert [c.data["click_type"] for c in clicks] == ["single", "hold"]
    assert [e.value for e in _kinds(events, HubEventKind.CLICK)] == [
        ("single", 0),
        ("hold", 0),
    ]
    assert [e.value for e in _kinds(events, HubEventKind.PRESSED)] == [True]


async def test_ignore_hold_uses_single_or_double_stream(hass, fake_flicd, make_hub):
    fake_flicd.buttons = {A: {}}
    clicks = async_capture_events(hass, "flic_click")
    await make_hub(ignored_click_types=["hold"])
    await _click(fake_flicd, A, ClickType.ButtonHold)  # not subscribed
    await _click(fake_flicd, A, ClickType.ButtonHold, event=CLICK_OR_HOLD)
    await _click(fake_flicd, A, ClickType.ButtonSingleClick, event=SINGLE_OR_DOUBLE)
    await _click(fake_flicd, A, ClickType.ButtonDoubleClick, event=SINGLE_OR_DOUBLE)
    await _until(lambda: len(clicks) == 2)
    await hass.async_block_till_done()
    assert [c.data["click_type"] for c in clicks] == ["single", "double"]


async def test_ignore_filter_applies_on_top_of_stream(hass, fake_flicd, make_hub):
    fake_flicd.buttons = {A: {}}
    clicks = async_capture_events(hass, "flic_click")
    await make_hub(ignored_click_types=["double", "hold"])
    await _click(fake_flicd, A, ClickType.ButtonHold, event=CLICK_OR_HOLD)
    await _click(fake_flicd, A, ClickType.ButtonClick, event=CLICK_OR_HOLD)
    await _until(lambda: clicks)
    await hass.async_block_till_done()
    assert [c.data["click_type"] for c in clicks] == ["single"]


async def test_queued_click_beyond_timeout_dropped_everywhere(
    hass, fake_flicd, make_hub, caplog
):
    fake_flicd.buttons = {A: {}}
    clicks = async_capture_events(hass, "flic_click")
    _, events = await make_hub(queue_timeout=3)
    await _click(fake_flicd, A, ClickType.ButtonSingleClick, True, 4)
    await _updown(fake_flicd, A, ClickType.ButtonDown, True, 4)
    await _updown(fake_flicd, A, ClickType.ButtonUp, True, 3)
    await _click(fake_flicd, A, ClickType.ButtonHold, True, 3)
    await _until(lambda: clicks)
    await hass.async_block_till_done()
    assert [c.data["click_type"] for c in clicks] == ["hold"]
    assert clicks[0].data["queued_time"] == 3
    assert [e.value for e in _kinds(events, HubEventKind.CLICK)] == [("hold", 3)]
    assert [e.value for e in _kinds(events, HubEventKind.PRESSED)] == [False]
    assert "Queued ButtonSingleClick dropped for 80:e4:da:71:2b:3c" in caplog.text
    assert "Time in queue was 4 seconds" in caplog.text


async def test_drop_marks_unavailable_and_reconnects(hass, fake_flicd, make_hub):
    fake_flicd.buttons = {A: {}, B: {}}
    hub, events = await make_hub()
    for _ in range(2):  # twice: the backoff resets after a successful sync
        events.clear()
        channels = _count(fake_flicd, "CmdCreateConnectionChannel")
        await fake_flicd.drop_clients()
        await _until(lambda: not hub.connected)
        assert hub.available is False
        # Every button is released first, so none stays "pressed" after the outage.
        assert [(e.kind, e.bd_addr, e.value) for e in events] == [
            (HubEventKind.PRESSED, A, False),
            (HubEventKind.PRESSED, B, False),
            (HubEventKind.AVAILABILITY, None, None),
        ]

        await _fire(hass, 1)
        await _until(lambda: hub.connected)
        assert hub.available is True
        await _until(
            lambda c=channels: _count(fake_flicd, "CmdCreateConnectionChannel") == c + 2
        )
        assert _kinds(events, HubEventKind.BUTTON_ADDED) == []
        assert events[-1].kind is HubEventKind.AVAILABILITY
    assert set(hub.buttons) == {A, B}


async def test_resync_removes_buttons_gone_from_server(hass, fake_flicd, make_hub):
    fake_flicd.buttons = {A: {}, B: {}}
    hub, events = await make_hub()
    await fake_flicd.drop_clients()
    await _until(lambda: not hub.connected)
    del fake_flicd.buttons[B]
    events.clear()
    await _fire(hass, 1)
    await _until(lambda: hub.available)
    assert set(hub.buttons) == {A}
    assert B not in hub.battery
    assert [e.bd_addr for e in _kinds(events, HubEventKind.BUTTON_REMOVED)] == [B]
    assert _kinds(events, HubEventKind.BUTTON_ADDED) == []


async def test_backoff_doubles_and_caps(hass, fake_flicd, make_hub):
    delays: list[float] = []
    real_call_later = ha_event.async_call_later

    def _recording_call_later(hass_, delay, action):
        delays.append(delay)
        return real_call_later(hass_, delay, action)

    hub, _ = await make_hub()
    with patch(
        "custom_components.flicbutton.hub.async_call_later", _recording_call_later
    ):
        await fake_flicd.stop()
        await _until(lambda: delays)
        now = dt_util.utcnow()
        while len(delays) < 8:
            attempts = len(delays)
            now += timedelta(seconds=delays[-1])
            async_fire_time_changed(hass, now)
            await _until(lambda n=attempts: len(delays) > n)
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60]
    assert hub.connected is False


async def test_ping_timeout_marks_unavailable_and_reconnects(
    hass, fake_flicd, make_hub
):
    fake_flicd.buttons = {A: {}}
    hub, _ = await make_hub()
    fake_flicd.answer_pings = False
    with patch("custom_components.flicbutton.hub.PING_TIMEOUT", 0.1):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await fake_flicd.wait_for_command("CmdPing")
        await _until(lambda: not hub.available)
    assert hub.connected is False
    fake_flicd.answer_pings = True
    await _fire(hass, 1)  # the first backoff step reconnects
    await _until(lambda: hub.available)


async def test_reconnect_survives_unexpected_error(hass, fake_flicd, make_hub, caplog):
    fake_flicd.buttons = {A: {}}
    hub, _ = await make_hub()
    real_connect = FlicdClient.connect
    attempts: list[int] = []

    async def _flaky_connect(self, timeout=10.0):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        await real_connect(self, timeout)

    with patch.object(FlicdClient, "connect", _flaky_connect):
        await fake_flicd.drop_clients()
        await _until(lambda: not hub.connected)
        await _fire(hass, 1)
        await _until(lambda: attempts)
        assert hub.connected is False
        assert "Unexpected error reconnecting to flicd" in caplog.text
        await _fire(hass, 2)  # the next backoff step
        await _until(lambda: hub.available)
    assert len(attempts) == 2
    assert set(hub.buttons) == {A}


async def test_disconnect_handler_error_still_reconnects(
    hass, fake_flicd, make_hub, caplog
):
    fake_flicd.buttons = {A: {}}
    hub, _ = await make_hub()
    with patch.object(hub, "_async_connection_lost", side_effect=RuntimeError("boom")):
        await fake_flicd.drop_clients()
        await _until(lambda: not hub.connected)
    assert "Error handling loss of connection to flicd" in caplog.text
    await _fire(hass, 1)
    await _until(lambda: hub.available)


async def test_auto_pair_survives_unexpected_error(fake_flicd, make_hub, caplog):
    real_wizard = FlicdClient.run_scan_wizard
    calls: list[int] = []

    async def _flaky_wizard(self, on_progress=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return await real_wizard(self, on_progress)

    with (
        patch.object(FlicdClient, "run_scan_wizard", _flaky_wizard),
        patch("custom_components.flicbutton.hub.BACKOFF_MAX", 0),
    ):
        await make_hub(auto_pair=True)
        await fake_flicd.wait_for_command("CmdCreateScanWizard")
    assert len(calls) == 2
    assert "Auto-pair scan wizard failed" in caplog.text


async def test_ping_answered_keeps_connection(hass, fake_flicd, make_hub):
    hub, _ = await make_hub()
    await _fire(hass, 30)
    await fake_flicd.wait_for_command("CmdPing")
    assert hub.available is True


async def test_controller_detached_makes_buttons_unavailable(fake_flicd, make_hub):
    hub, events = await make_hub()
    events.clear()
    await fake_flicd.send(
        "EvtBluetoothControllerStateChange", state=BluetoothControllerState.Detached
    )
    await _until(lambda: not hub.available)
    assert hub.connected is True
    assert [e.value for e in _kinds(events, HubEventKind.CONTROLLER_STATE)] == [
        BluetoothControllerState.Detached
    ]
    assert _kinds(events, HubEventKind.AVAILABILITY)
    await fake_flicd.send(
        "EvtBluetoothControllerStateChange", state=BluetoothControllerState.Attached
    )
    await _until(lambda: hub.available)


async def test_new_verified_button_added(fake_flicd, make_hub):
    fake_flicd.buttons = {A: {}}
    hub, events = await make_hub()
    fake_flicd.buttons[C] = {"color": "black"}
    await fake_flicd.send("EvtNewVerifiedButton", bd_addr=C)
    await _until(lambda: C in hub.buttons)
    assert hub.buttons[C].color == "black"
    added = _kinds(events, HubEventKind.BUTTON_ADDED)
    assert [e.bd_addr for e in added] == [A, C]
    assert [e.value for e in added] == [False, True]
    await _until(lambda: _count(fake_flicd, "CmdCreateBatteryStatusListener") == 2)
    assert _conn_id(fake_flicd, C)


async def test_button_deleted_by_flicd_removed(fake_flicd, make_hub):
    fake_flicd.buttons = {A: {}, B: {}}
    hub, events = await make_hub()
    await fake_flicd.send("EvtButtonDeleted", bd_addr=B, deleted_by_this_client=False)
    await _until(lambda: B not in hub.buttons)
    assert B not in hub.battery
    assert [e.bd_addr for e in _kinds(events, HubEventKind.BUTTON_REMOVED)] == [B]


async def test_delete_button_normalises_address(fake_flicd, make_hub):
    fake_flicd.buttons = {A: {}}
    hub, events = await make_hub()
    await asyncio.wait_for(hub.async_delete_button(A.upper()), 2)
    assert ("CmdDeleteButton", {"bd_addr": A}) in fake_flicd.commands
    assert A not in hub.buttons
    assert [e.bd_addr for e in _kinds(events, HubEventKind.BUTTON_REMOVED)] == [A]


async def test_battery_updates(fake_flicd, make_hub):
    fake_flicd.buttons = {A: {}}
    hub, events = await make_hub()
    await fake_flicd.send(
        "EvtBatteryStatus",
        listener_id=_listener_id(fake_flicd, A),
        battery_percentage=87,
        timestamp=1762500000,
    )
    await _until(lambda: hub.battery[A] == 87)
    assert [(e.bd_addr, e.value) for e in _kinds(events, HubEventKind.BATTERY)] == [
        (A, 87)
    ]


async def test_pair_success_reports_progress(fake_flicd, make_hub):
    fake_flicd.scan_wizard_script = [
        ("EvtScanWizardFoundPublicButton", {"bd_addr": C, "name": "F0"}),
        ("EvtScanWizardButtonConnected", {}),
        ("EvtScanWizardCompleted", {"result": ScanWizardResult.WizardSuccess}),
    ]
    hub, _ = await make_hub()
    progress: list[str] = []
    result = await asyncio.wait_for(hub.async_pair(progress.append), 2)
    assert result == ScanResult(ScanWizardResult.WizardSuccess, C, "F0")
    assert progress == ["found_public", "connected"]


async def test_pair_in_progress_rejected(fake_flicd, make_hub):
    hub, _ = await make_hub()
    first = asyncio.ensure_future(hub.async_pair())
    await fake_flicd.wait_for_command("CmdCreateScanWizard")
    with pytest.raises(PairingInProgress):
        await hub.async_pair()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


async def test_pair_cancelled_sends_cancel_scan_wizard(fake_flicd, make_hub):
    hub, _ = await make_hub()
    task = asyncio.ensure_future(hub.async_pair())
    cmd = await fake_flicd.wait_for_command("CmdCreateScanWizard")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cancel = await fake_flicd.wait_for_command("CmdCancelScanWizard")
    assert cancel["scan_wizard_id"] == cmd["scan_wizard_id"]
    # A new pairing is possible once the cancelled one is gone.
    fake_flicd.scan_wizard_script = [
        ("EvtScanWizardCompleted", {"result": ScanWizardResult.WizardFailedTimeout})
    ]
    result = await asyncio.wait_for(hub.async_pair(), 2)
    assert result.result is ScanWizardResult.WizardFailedTimeout


async def test_pair_when_disconnected_raises(fake_flicd, make_hub):
    hub, _ = await make_hub()
    await fake_flicd.drop_clients()
    await _until(lambda: not hub.connected)
    with pytest.raises(FlicdConnectionError):
        await hub.async_pair()


async def test_auto_pair_restarts_wizard(hass, fake_flicd, make_hub):
    hub, _ = await make_hub(auto_pair=True)
    first = await fake_flicd.wait_for_command("CmdCreateScanWizard")
    await fake_flicd.send(
        "EvtScanWizardCompleted",
        scan_wizard_id=first["scan_wizard_id"],
        result=ScanWizardResult.WizardFailedTimeout,
    )
    await _until(lambda: _count(fake_flicd, "CmdCreateScanWizard") == 2)
    with pytest.raises(PairingInProgress):
        await hub.async_pair()
    # The loop stops on disconnect and is restarted by the next sync.
    await fake_flicd.drop_clients()
    await _until(lambda: not hub.connected)
    await _fire(hass, 1)
    await _until(lambda: _count(fake_flicd, "CmdCreateScanWizard") == 3)


async def test_subscriber_exception_does_not_break_hub(fake_flicd, make_hub, caplog):
    fake_flicd.buttons = {A: {}}
    hub, events = await make_hub()

    def _boom(event: HubEvent) -> None:
        raise RuntimeError("boom")

    unsub = hub.async_subscribe(_boom)
    later: list[HubEvent] = []
    hub.async_subscribe(later.append)
    with caplog.at_level(logging.ERROR):
        await _updown(fake_flicd, A, ClickType.ButtonDown)
        await _until(lambda: later)
    assert "boom" in caplog.text
    assert _kinds(events, HubEventKind.PRESSED)
    unsub()
    await _updown(fake_flicd, A, ClickType.ButtonUp)
    await _until(lambda: len(later) == 2)


async def test_stop_cancels_everything(hass, fake_flicd, make_hub):
    hub, _ = await make_hub(auto_pair=True)
    await fake_flicd.wait_for_command("CmdCreateScanWizard")
    await hub.async_stop()
    assert hub.connected is False
    await fake_flicd.drop_clients()
    await _fire(hass, 120)
    assert hub.connected is False


async def test_sync_timeout_raises_and_closes(fake_flicd, make_hub):
    fake_flicd.answer_get_info = False
    hub, _ = await make_hub(start=False)
    with (
        patch("custom_components.flicbutton.hub.CONNECT_TIMEOUT", 0.1),
        pytest.raises(FlicdConnectionError),
    ):
        await hub.async_start()
    assert hub.connected is False
    await _until(lambda: not fake_flicd._writers)
