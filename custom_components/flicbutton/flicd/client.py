"""Asyncio client for the flicd daemon. No Home Assistant imports."""

from __future__ import annotations

import asyncio
import logging
import struct
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from ..const import CONNECT_TIMEOUT
from .packets import (
    BdAddrType,
    BluetoothControllerState,
    ClickType,
    ConnectionStatus,
    DisconnectReason,
    FrameReader,
    LatencyMode,
    RemovedReason,
    ScanWizardResult,
    decode_event,
    encode_command,
)

_LOGGER = logging.getLogger(__name__)

AUTO_DISCONNECT_TIME = 511
_SERVER_EVENTS = frozenset(
    {
        "EvtNewVerifiedButton",
        "EvtButtonDeleted",
        "EvtBluetoothControllerStateChange",
        "EvtNoSpaceForNewConnection",
        "EvtGotSpaceForNewConnection",
    }
)
_SCAN_PROGRESS = {
    "EvtScanWizardFoundPrivateButton": "found_private",
    "EvtScanWizardFoundPublicButton": "found_public",
    "EvtScanWizardButtonConnected": "connected",
}


class FlicdConnectionError(Exception):
    """The connection to flicd failed or was lost."""


@dataclass(frozen=True)
class ServerInfo:
    controller_state: BluetoothControllerState
    bd_addr: str
    bd_addr_type: BdAddrType
    max_pending_connections: int
    max_concurrent_buttons: int
    verified_buttons: list[str]


@dataclass(frozen=True)
class ButtonInfo:
    bd_addr: str
    uuid: str | None
    color: str | None
    serial_number: str | None
    flic_version: int
    firmware_version: int


@dataclass(frozen=True)
class ScanResult:
    result: ScanWizardResult
    bd_addr: str | None
    name: str | None


class ChannelListener(Protocol):
    def on_up_down(
        self, click_type: ClickType, was_queued: bool, time_diff: int
    ) -> None: ...

    def on_click(
        self, click_type: ClickType, was_queued: bool, time_diff: int
    ) -> None: ...

    def on_status(self, status: ConnectionStatus, reason: DisconnectReason) -> None: ...

    def on_removed(self, reason: RemovedReason) -> None: ...


class _Protocol(asyncio.Protocol):
    def __init__(self, client: FlicdClient) -> None:
        self._client = client
        self._reader = FrameReader()

    def data_received(self, data: bytes) -> None:
        for payload in self._reader.feed(data):
            self._client._handle_payload(payload)

    def connection_lost(self, exc: Exception | None) -> None:
        self._client._connection_lost(self)


class FlicdClient:
    """Talk to flicd over TCP; all callbacks run in the event loop."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._transport: asyncio.Transport | None = None
        self._protocol: _Protocol | None = None
        self.on_disconnect: Callable[[], None] | None = None
        self.on_server_event: Callable[[str, dict[str, Any]], None] | None = None

        self._next_id = 0
        self._info_waiters: deque[asyncio.Future[ServerInfo]] = deque()
        self._button_info_waiters: deque[asyncio.Future[ButtonInfo]] = deque()
        self._ping_waiters: dict[int, asyncio.Future[None]] = {}
        self._delete_waiters: dict[str, list[asyncio.Future[None]]] = {}
        self._channels: dict[int, ChannelListener] = {}
        self._battery: dict[int, Callable[[int | None, datetime], None]] = {}
        self._wizard_id: int | None = None
        self._wizard_future: asyncio.Future[ScanResult] | None = None
        self._wizard_progress: Callable[[str], None] | None = None
        self._wizard_found: dict[str, Any] = {}

    @property
    def connected(self) -> bool:
        return self._transport is not None

    async def connect(self, timeout: float = CONNECT_TIMEOUT) -> None:
        loop = asyncio.get_running_loop()
        try:
            transport, protocol = await asyncio.wait_for(
                loop.create_connection(lambda: _Protocol(self), self._host, self._port),
                timeout,
            )
        except (OSError, TimeoutError) as err:
            raise FlicdConnectionError(
                f"Cannot connect to flicd at {self._host}:{self._port}: {err!r}"
            ) from err
        self._transport = transport  # type: ignore[assignment]
        self._protocol = protocol

    async def close(self) -> None:
        """Close the connection; ``on_disconnect`` is not called."""
        transport, self._transport = self._transport, None
        self._protocol = None
        if transport is not None:
            transport.close()
        self._fail_pending()

    # -- commands -------------------------------------------------------

    def _send(self, name: str, **fields: Any) -> None:
        if self._transport is None:
            raise FlicdConnectionError("Not connected to flicd")
        self._transport.write(encode_command(name, **fields))

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _future(self) -> asyncio.Future[Any]:
        return asyncio.get_running_loop().create_future()

    async def get_info(self) -> ServerInfo:
        future = self._future()
        self._send("CmdGetInfo")
        self._info_waiters.append(future)
        return await future

    async def get_button_info(self, bd_addr: str) -> ButtonInfo:
        future = self._future()
        self._send("CmdGetButtonInfo", bd_addr=bd_addr)
        self._button_info_waiters.append(future)
        return await future

    async def ping(self, timeout: float) -> None:
        ping_id = self._new_id()
        future = self._future()
        self._send("CmdPing", ping_id=ping_id)
        self._ping_waiters[ping_id] = future
        try:
            await asyncio.wait_for(future, timeout)
        finally:
            self._ping_waiters.pop(ping_id, None)

    def open_channel(self, bd_addr: str, listener: ChannelListener) -> int:
        conn_id = self._new_id()
        self._send(
            "CmdCreateConnectionChannel",
            conn_id=conn_id,
            bd_addr=bd_addr,
            latency_mode=LatencyMode.NormalLatency,
            auto_disconnect_time=AUTO_DISCONNECT_TIME,
        )
        self._channels[conn_id] = listener
        return conn_id

    def close_channel(self, conn_id: int) -> None:
        self._channels.pop(conn_id, None)
        self._send("CmdRemoveConnectionChannel", conn_id=conn_id)

    def add_battery_listener(
        self, bd_addr: str, callback: Callable[[int | None, datetime], None]
    ) -> int:
        listener_id = self._new_id()
        self._send(
            "CmdCreateBatteryStatusListener", listener_id=listener_id, bd_addr=bd_addr
        )
        self._battery[listener_id] = callback
        return listener_id

    async def run_scan_wizard(
        self, on_progress: Callable[[str], None] | None = None
    ) -> ScanResult:
        if self._wizard_id is not None:
            raise RuntimeError("A scan wizard is already running")
        wizard_id = self._new_id()
        future = self._future()
        self._send("CmdCreateScanWizard", scan_wizard_id=wizard_id)
        self._wizard_id = wizard_id
        self._wizard_future = future
        self._wizard_progress = on_progress
        self._wizard_found = {}
        try:
            return await future
        finally:
            self._wizard_id = None
            self._wizard_future = None
            self._wizard_progress = None

    async def cancel_scan_wizard(self) -> None:
        if self._wizard_id is not None:
            self._send("CmdCancelScanWizard", scan_wizard_id=self._wizard_id)

    async def delete_button(self, bd_addr: str) -> None:
        future = self._future()
        self._send("CmdDeleteButton", bd_addr=bd_addr)
        self._delete_waiters.setdefault(bd_addr, []).append(future)
        await future

    # -- receive path -----------------------------------------------------

    def _handle_payload(self, payload: bytes) -> None:
        try:
            decoded = decode_event(payload)
            if decoded is None:
                _LOGGER.debug("Ignoring unknown flicd opcode %s", payload[:1].hex())
                return
            self._dispatch(*decoded)
        except (struct.error, ValueError) as err:  # incl. UnicodeDecodeError
            _LOGGER.debug("Dropping malformed flicd packet %s: %r", payload.hex(), err)
        except Exception:
            _LOGGER.exception("Error handling flicd packet %s", payload.hex())

    def _dispatch(self, name: str, f: dict[str, Any]) -> None:
        if name == "EvtGetInfoResponse":
            if waiter := self._pop_waiter(self._info_waiters):
                waiter.set_result(
                    ServerInfo(
                        f["bluetooth_controller_state"],
                        f["my_bd_addr"],
                        f["my_bd_addr_type"],
                        f["max_pending_connections"],
                        f["max_concurrently_connected_buttons"],
                        f["bd_addr_of_verified_buttons"],
                    )
                )
        elif name == "EvtGetButtonInfoResponse":
            if waiter := self._pop_waiter(self._button_info_waiters):
                waiter.set_result(
                    ButtonInfo(
                        f["bd_addr"],
                        f["uuid"],
                        f["color"],
                        f["serial_number"],
                        f["flic_version"],
                        f["firmware_version"],
                    )
                )
        elif name == "EvtPingResponse":
            waiter = self._ping_waiters.get(f["ping_id"])
            if waiter is not None and not waiter.done():
                waiter.set_result(None)
        elif name == "EvtButtonDeleted":
            for waiter in self._delete_waiters.pop(f["bd_addr"], []):
                if not waiter.done():
                    waiter.set_result(None)
            self._server_event(name, f)
        elif name in _SERVER_EVENTS:
            self._server_event(name, f)
        elif name.startswith("EvtScanWizard"):
            self._dispatch_wizard(name, f)
        elif name == "EvtBatteryStatus":
            callback = self._battery.get(f["listener_id"])
            if callback is None:
                _LOGGER.debug("Unknown battery listener %s", f["listener_id"])
                return
            percentage = f["battery_percentage"]
            callback(
                None if percentage < 0 else percentage,
                datetime.fromtimestamp(f["timestamp"], tz=UTC),
            )
        elif "conn_id" in f:
            self._dispatch_channel(name, f)

    def _dispatch_channel(self, name: str, f: dict[str, Any]) -> None:
        listener = self._channels.get(f["conn_id"])
        if listener is None:
            _LOGGER.debug("Unknown conn_id %s for %s", f["conn_id"], name)
            return
        if name == "EvtButtonUpOrDown":
            listener.on_up_down(f["click_type"], f["was_queued"], f["time_diff"])
        elif name == "EvtButtonSingleOrDoubleClickOrHold":
            listener.on_click(f["click_type"], f["was_queued"], f["time_diff"])
        elif name == "EvtCreateConnectionChannelResponse":
            listener.on_status(f["connection_status"], DisconnectReason.Unspecified)
        elif name == "EvtConnectionStatusChanged":
            listener.on_status(f["connection_status"], f["disconnect_reason"])
        elif name == "EvtConnectionChannelRemoved":
            self._channels.pop(f["conn_id"], None)
            listener.on_removed(f["removed_reason"])

    def _dispatch_wizard(self, name: str, f: dict[str, Any]) -> None:
        if f["scan_wizard_id"] != self._wizard_id:
            _LOGGER.debug("Unknown scan_wizard_id %s", f["scan_wizard_id"])
            return
        if name == "EvtScanWizardFoundPublicButton":
            self._wizard_found = {"bd_addr": f["bd_addr"], "name": f["name"]}
        if name in _SCAN_PROGRESS and self._wizard_progress is not None:
            self._wizard_progress(_SCAN_PROGRESS[name])
        if name == "EvtScanWizardCompleted":
            if self._wizard_future is not None and not self._wizard_future.done():
                self._wizard_future.set_result(
                    ScanResult(
                        f["result"],
                        self._wizard_found.get("bd_addr"),
                        self._wizard_found.get("name"),
                    )
                )

    def _server_event(self, name: str, fields: dict[str, Any]) -> None:
        if self.on_server_event is not None:
            self.on_server_event(name, fields)

    @staticmethod
    def _pop_waiter(waiters: deque[asyncio.Future[Any]]) -> asyncio.Future[Any] | None:
        while waiters:
            waiter = waiters.popleft()
            if not waiter.done():
                return waiter
        return None

    # -- connection loss --------------------------------------------------

    def _connection_lost(self, protocol: _Protocol) -> None:
        if protocol is not self._protocol:
            return  # closed by us, or a stale connection
        self._transport = None
        self._protocol = None
        self._fail_pending()
        if self.on_disconnect is not None:
            self.on_disconnect()

    def _fail_pending(self) -> None:
        error = FlicdConnectionError("Connection to flicd lost")
        futures: list[asyncio.Future[Any]] = [
            *self._info_waiters,
            *self._button_info_waiters,
            *self._ping_waiters.values(),
            *(w for ws in self._delete_waiters.values() for w in ws),
        ]
        if self._wizard_future is not None:
            futures.append(self._wizard_future)
        self._info_waiters.clear()
        self._button_info_waiters.clear()
        self._delete_waiters.clear()
        for future in futures:
            if not future.done():
                future.set_exception(error)
        self._channels.clear()
        self._battery.clear()
