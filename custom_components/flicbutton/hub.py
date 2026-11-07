"""Connection hub: one flicd connection per config entry, kept alive forever."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from functools import partial
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import (
    BACKOFF_INITIAL,
    BACKOFF_MAX,
    CLICK_DOUBLE,
    CLICK_HOLD,
    CLICK_SINGLE,
    CONF_AUTO_PAIR,
    CONF_IGNORED_CLICK_TYPES,
    CONF_QUEUE_TIMEOUT,
    CONNECT_TIMEOUT,
    DEFAULT_QUEUE_TIMEOUT,
    EVENT_FLIC_CLICK,
    PING_INTERVAL,
    PING_TIMEOUT,
    legacy_button_name,
)
from .flicd import (
    BluetoothControllerState,
    ClickType,
    ConnectionStatus,
    DisconnectReason,
    RemovedReason,
    ScanWizardResult,
)
from .flicd.client import (
    ButtonInfo,
    FlicdClient,
    FlicdConnectionError,
    ScanResult,
    ServerInfo,
)

_LOGGER = logging.getLogger(__name__)

_CLICK_TYPES = {
    ClickType.ButtonSingleClick: CLICK_SINGLE,
    ClickType.ButtonDoubleClick: CLICK_DOUBLE,
    ClickType.ButtonHold: CLICK_HOLD,
}
# Wizard results flicd returns at once; restarting right away would spin.
_AUTO_PAIR_PAUSE_RESULTS = frozenset(
    {
        ScanWizardResult.WizardBluetoothUnavailable,
        ScanWizardResult.WizardInternetBackendError,
    }
)


class PairingInProgress(Exception):
    """A scan wizard is already running on this hub."""


@dataclass
class HubOptions:
    queue_timeout: int
    ignored_click_types: list[str]
    auto_pair: bool

    @classmethod
    def from_entry_options(cls, options: Mapping[str, Any]) -> HubOptions:
        return cls(
            queue_timeout=int(options.get(CONF_QUEUE_TIMEOUT, DEFAULT_QUEUE_TIMEOUT)),
            ignored_click_types=list(options.get(CONF_IGNORED_CLICK_TYPES, [])),
            auto_pair=bool(options.get(CONF_AUTO_PAIR, False)),
        )


class HubEventKind(StrEnum):
    AVAILABILITY = "availability"
    BUTTON_ADDED = "button_added"  # value: True on a fresh pairing, False on sync
    BUTTON_REMOVED = "button_removed"
    CLICK = "click"
    PRESSED = "pressed"
    BATTERY = "battery"
    CONTROLLER_STATE = "controller_state"


@dataclass(frozen=True)
class HubEvent:
    kind: HubEventKind
    bd_addr: str | None = None
    value: Any = None


class _ChannelListener:
    """Per-button channel listener; filters once, before anything is emitted."""

    def __init__(self, hub: FlicHub, bd_addr: str) -> None:
        self._hub = hub
        self._bd_addr = bd_addr

    def _stale(self, click_type: ClickType, was_queued: bool, time_diff: int) -> bool:
        if not was_queued or time_diff <= self._hub.options.queue_timeout:
            return False
        unit = "second" if time_diff == 1 else "seconds"
        _LOGGER.warning(
            "Queued %s dropped for %s. Time in queue was %s",
            click_type.name,
            self._bd_addr,
            f"{time_diff:d} {unit}",
        )
        return True

    def on_up_down(
        self, click_type: ClickType, was_queued: bool, time_diff: int
    ) -> None:
        if self._stale(click_type, was_queued, time_diff):
            return
        self._hub._async_emit(
            HubEvent(
                HubEventKind.PRESSED,
                self._bd_addr,
                click_type is ClickType.ButtonDown,
            )
        )

    def on_click(self, click_type: ClickType, was_queued: bool, time_diff: int) -> None:
        if self._stale(click_type, was_queued, time_diff):
            return
        hass_click_type = _CLICK_TYPES.get(click_type)
        if hass_click_type is None:
            return
        if hass_click_type in self._hub.options.ignored_click_types:
            return
        self._hub.hass.bus.async_fire(
            EVENT_FLIC_CLICK,
            {
                "button_name": legacy_button_name(self._bd_addr),
                "button_address": self._bd_addr,
                "click_type": hass_click_type,
                "queued_time": time_diff,
            },
        )
        self._hub._async_emit(
            HubEvent(HubEventKind.CLICK, self._bd_addr, (hass_click_type, time_diff))
        )

    def on_status(self, status: ConnectionStatus, reason: DisconnectReason) -> None:
        _LOGGER.debug("Button %s channel %s (%s)", self._bd_addr, status.name, reason)

    def on_removed(self, reason: RemovedReason) -> None:
        _LOGGER.debug("Button %s channel removed: %s", self._bd_addr, reason.name)


class FlicHub:
    """Owns the flicd connection of one config entry. Loop-only."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        host: str,
        port: int,
        options: HubOptions,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.host = host
        self.port = port
        self.options = options
        self.server_info: ServerInfo | None = None
        self.buttons: dict[str, ButtonInfo] = {}
        self.battery: dict[str, int | None] = {}

        self._client: FlicdClient | None = None
        self._synced = False
        self._controller_state: BluetoothControllerState | None = None
        self._subscribers: list[Callable[[HubEvent], None]] = []
        self._stopping = False
        self._backoff = BACKOFF_INITIAL
        self._pairing = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._unsub_reconnect: CALLBACK_TYPE | None = None
        self._unsub_keepalive: CALLBACK_TYPE | None = None
        self._ping_task: asyncio.Task[None] | None = None
        self._auto_pair_task: asyncio.Task[None] | None = None
        # Per-connection state, cleared on every disconnect.
        self._channels: dict[str, int] = {}
        self._setup_started: set[str] = set()

    # -- public API ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._synced and self._client is not None and self._client.connected

    @property
    def available(self) -> bool:
        return (
            self.connected
            and self._controller_state is BluetoothControllerState.Attached
        )

    @callback
    def async_subscribe(self, cb: Callable[[HubEvent], None]) -> Callable[[], None]:
        self._subscribers.append(cb)

        @callback
        def _unsubscribe() -> None:
            if cb in self._subscribers:
                self._subscribers.remove(cb)

        return _unsubscribe

    async def async_start(self) -> None:
        """Connect and sync; raises FlicdConnectionError on failure."""
        self._stopping = False
        await self._async_connect()

    async def async_stop(self) -> None:
        self._stopping = True
        if self._unsub_reconnect is not None:
            self._unsub_reconnect()
            self._unsub_reconnect = None
        client = self._detach_client()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if client is not None:
            await client.close()

    async def async_pair(
        self, on_progress: Callable[[str], None] | None = None
    ) -> ScanResult:
        if self._pairing:
            raise PairingInProgress
        client = self._client
        if client is None or not self.connected:
            raise FlicdConnectionError("Not connected to flicd")
        return await self._async_run_wizard(client, on_progress)

    async def async_delete_button(self, bd_addr: str) -> None:
        client = self._client
        if client is None or not self.connected:
            raise FlicdConnectionError("Not connected to flicd")
        await client.delete_button(bd_addr.lower())

    # -- events ------------------------------------------------------------

    @callback
    def _async_emit(self, event: HubEvent) -> None:
        for cb in list(self._subscribers):
            try:
                cb(event)
            except Exception:
                _LOGGER.exception("Error in flicbutton subscriber for %s", event)

    def _track(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        task = self.hass.async_create_background_task(
            coro, f"flicbutton {self.entry_id} {name}"
        )
        if not task.done():
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return task

    # -- connection lifecycle ------------------------------------------------

    async def _async_connect(self) -> None:
        client = FlicdClient(self.host, self.port)
        await client.connect(CONNECT_TIMEOUT)
        client.on_server_event = self._on_server_event
        self._client = client
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                await self._async_sync(client)
            if not client.connected:
                raise FlicdConnectionError("Connection to flicd lost during sync")
        except BaseException as err:  # incl. cancellation: never leak the socket
            self._detach_client()
            await client.close()
            if isinstance(err, TimeoutError):
                raise FlicdConnectionError("flicd did not answer the sync") from err
            raise
        client.on_disconnect = self._on_disconnect
        self._synced = True
        self._backoff = BACKOFF_INITIAL
        self._unsub_keepalive = async_track_time_interval(
            self.hass, self._async_keepalive, timedelta(seconds=PING_INTERVAL)
        )
        _LOGGER.info("Connected to flicd at %s:%s", self.host, self.port)
        self._async_emit(HubEvent(HubEventKind.AVAILABILITY))
        if self.options.auto_pair:
            self._auto_pair_task = self._track(
                self._async_auto_pair(client), "auto pair"
            )

    async def _async_sync(self, client: FlicdClient) -> None:
        info = await client.get_info()
        self.server_info = info
        self._controller_state = info.controller_state
        verified = set(info.verified_buttons)
        for bd_addr in [a for a in self.buttons if a not in verified]:
            self._async_drop_button(bd_addr)
        for bd_addr in info.verified_buttons:
            await self._async_setup_button(client, bd_addr)

    async def _async_setup_button(
        self, client: FlicdClient, bd_addr: str, fresh_pairing: bool = False
    ) -> None:
        if bd_addr in self._setup_started:
            return
        self._setup_started.add(bd_addr)
        info = await client.get_button_info(bd_addr)
        if client is not self._client:
            return
        is_new = bd_addr not in self.buttons
        self.buttons[bd_addr] = info
        self.battery.setdefault(bd_addr, None)
        self._channels[bd_addr] = client.open_channel(
            bd_addr, _ChannelListener(self, bd_addr)
        )
        client.add_battery_listener(bd_addr, partial(self._on_battery, bd_addr))
        if is_new:
            self._async_emit(
                HubEvent(HubEventKind.BUTTON_ADDED, bd_addr, fresh_pairing)
            )

    @callback
    def _async_drop_button(self, bd_addr: str) -> None:
        self._channels.pop(bd_addr, None)
        self._setup_started.discard(bd_addr)
        self.battery.pop(bd_addr, None)
        if self.buttons.pop(bd_addr, None) is not None:
            self._async_emit(HubEvent(HubEventKind.BUTTON_REMOVED, bd_addr))

    def _detach_client(self) -> FlicdClient | None:
        """Forget the current client and all per-connection state."""
        client, self._client = self._client, None
        self._synced = False
        if client is not None:
            client.on_disconnect = None
            client.on_server_event = None
        if self._unsub_keepalive is not None:
            self._unsub_keepalive()
            self._unsub_keepalive = None
        if self._auto_pair_task is not None:
            self._auto_pair_task.cancel()
            self._auto_pair_task = None
        self._channels.clear()
        self._setup_started.clear()
        return client

    @callback
    def _async_connection_lost(self, reason: str) -> None:
        if self._stopping or self._client is None:
            return
        _LOGGER.warning(
            "Lost connection to flicd at %s:%s: %s", self.host, self.port, reason
        )
        self._detach_client()
        self._async_emit(HubEvent(HubEventKind.AVAILABILITY))
        self._schedule_reconnect()

    @callback
    def _schedule_reconnect(self) -> None:
        if self._stopping:
            return
        delay = self._backoff
        self._backoff = min(self._backoff * 2, BACKOFF_MAX)
        self._unsub_reconnect = async_call_later(self.hass, delay, self._on_reconnect)

    @callback
    def _on_reconnect(self, _now: datetime) -> None:
        self._unsub_reconnect = None
        self._track(self._async_reconnect(), "reconnect")

    async def _async_reconnect(self) -> None:
        if self._stopping:
            return
        try:
            await self._async_connect()
        except FlicdConnectionError as err:
            _LOGGER.debug("Reconnect to flicd failed: %s", err)
            self._schedule_reconnect()

    @callback
    def _on_disconnect(self) -> None:
        self._async_connection_lost("connection closed")

    @callback
    def _async_keepalive(self, _now: datetime) -> None:
        if self._client is None or (self._ping_task and not self._ping_task.done()):
            return
        self._ping_task = self._track(self._async_ping(self._client), "ping")

    async def _async_ping(self, client: FlicdClient) -> None:
        try:
            await client.ping(PING_TIMEOUT)  # module global: patched in tests
        except (TimeoutError, FlicdConnectionError):
            if client is not self._client:
                return
            self._async_connection_lost("no answer to ping")
            await client.close()

    # -- server events -----------------------------------------------------

    @callback
    def _on_server_event(self, name: str, fields: dict[str, Any]) -> None:
        client = self._client
        if client is None:
            return
        if name == "EvtNewVerifiedButton":
            self._track(
                self._async_setup_new_button(client, fields["bd_addr"]), "add button"
            )
        elif name == "EvtButtonDeleted":
            self._async_drop_button(fields["bd_addr"])
        elif name == "EvtBluetoothControllerStateChange":
            self._controller_state = fields["state"]
            self._async_emit(
                HubEvent(HubEventKind.CONTROLLER_STATE, None, fields["state"])
            )
            self._async_emit(HubEvent(HubEventKind.AVAILABILITY))
        else:
            _LOGGER.debug("flicd event %s %s", name, fields)

    async def _async_setup_new_button(self, client: FlicdClient, bd_addr: str) -> None:
        try:
            await self._async_setup_button(client, bd_addr, fresh_pairing=True)
        except FlicdConnectionError:
            pass  # the reconnect sync picks it up

    @callback
    def _on_battery(self, bd_addr: str, level: int | None, _ts: datetime) -> None:
        if bd_addr not in self.buttons:
            return
        self.battery[bd_addr] = level
        self._async_emit(HubEvent(HubEventKind.BATTERY, bd_addr, level))

    # -- pairing -------------------------------------------------------------

    async def _async_run_wizard(
        self, client: FlicdClient, on_progress: Callable[[str], None] | None
    ) -> ScanResult:
        self._pairing = True
        try:
            wizard = self._track(client.run_scan_wizard(on_progress), "scan wizard")
            try:
                return await asyncio.shield(wizard)
            except asyncio.CancelledError:
                # The wizard task is still registered, so flicd gets the cancel.
                if client.connected:
                    await client.cancel_scan_wizard()
                wizard.cancel()
                await asyncio.wait([wizard])
                raise
        finally:
            self._pairing = False

    async def _async_auto_pair(self, client: FlicdClient) -> None:
        while client is self._client:
            try:
                result = await self._async_run_wizard(client, None)
            except FlicdConnectionError:
                return
            _LOGGER.debug("Auto-pair wizard finished: %s", result)
            if result.result in _AUTO_PAIR_PAUSE_RESULTS:
                await asyncio.sleep(BACKOFF_MAX)
