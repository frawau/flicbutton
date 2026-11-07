# flicbutton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A self-healing, UI-configured Home Assistant custom integration (`flicbutton`) for Flic buttons served by `flicd`, publishable in the HA Marketplace main catalog.

**Architecture:** A bundled asyncio flicd protocol layer (`flicd/packets.py` codec + `flicd/client.py` client) sits under a per-config-entry `FlicHub`. The hub owns reconnect/keepalive, button bookkeeping, click filtering and the legacy `flic_click` bus event. HA platforms (event, binary_sensor, sensor), device triggers, config/options flows and diagnostics subscribe to the hub. No threads and no PyPI runtime dependency.

**Tech Stack:** Python 3.14, Home Assistant 2026.11, `pytest-homeassistant-custom-component`, ruff, black, GitHub Actions (hassfest + HACS/Marketplace validation).

**Spec:** `docs/superpowers/specs/2026-11-07-flicbutton-design.md`. Read it before starting any task.

## Global Constraints

- Domain `flicbutton`; display name `Flic button (flicd)`; must not override or import core `flic`.
- Repo `github.com/frawau/flicbutton`, MIT license, `codeowners: ["@frawau"]`.
- Minimum HA `2026.11.0` (`hacs.json` → `"homeassistant": "2026.11.0"`).
- `manifest.json`: `domain`, `name`, `version` (SemVer, start `0.1.0`, equal to the release tag), `documentation`, `issue_tracker`, `codeowners`, `config_flow: true`, `iot_class: "local_push"`, `integration_type: "hub"`, `requirements: []`, `dependencies: []`.
- Only `custom_components/flicbutton/` under `custom_components/`.
- No threads, no blocking I/O in the event loop, no `pyflic`.
- Metric units, 24 h time. Every datetime is timezone-aware UTC (`datetime.fromtimestamp(ts, tz=UTC)`). A naive datetime is a bug.
- `flic_click` payload exactly `{"button_name": "flic_<mac without colons, lowercase>", "button_address": "<aa:bb:cc:dd:ee:ff lowercase>", "click_type": "single"|"double"|"hold", "queued_time": <int seconds>}`.
- Defaults: port `5551`, queue timeout `3` s, ignored click types `[]`, auto-pair `False`.
- Reconnect backoff starts at `1` s, doubles, capped at `60` s. Ping every `30` s; `10` s answer timeout.
- Run `black` on every modified Python file before each commit; `ruff check` must pass.
- Do not use Flic's trademarked logo anywhere.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Fragmented or coalesced TCP reads** (a packet split across reads, including a 1-byte read inside the length header, or several packets in one read). Every packet must decode exactly once. Upstream `aioflic.py` breaks here, so do not copy its `data_received`. Test: Task 2 `test_frame_reader_split_header_and_coalesced`.
2. **Half-open TCP** (the Pi loses power and no FIN is sent). The hub must go unavailable within ping interval + timeout (≤ 40 s) and then reconnect. Test: Task 4 `test_ping_timeout_marks_unavailable_and_reconnects`.
3. **Stale queued clicks replayed after reconnect.** Clicks with `was_queued` and `time_diff > timeout` must be dropped from every output (bus, event entity, binary sensor). Test: Task 4 `test_queued_click_beyond_timeout_dropped_everywhere`.
4. **flicd is up but its Bluetooth controller is detached.** The hub is connected but buttons are unavailable, and pairing reports "Bluetooth unavailable" instead of hanging. Tests: Task 4 `test_controller_detached_makes_buttons_unavailable`, Task 8 `test_pair_bluetooth_unavailable`.
5. **Unknown opcodes and packets longer than documented** (the protocol says to ignore extra bytes). These must not crash the reader or kill the connection. Test: Task 2 `test_decode_unknown_opcode_and_extra_bytes`.

## File Structure

```
custom_components/flicbutton/
  __init__.py          setup/unload entry, runtime_data, device bookkeeping, device removal
  manifest.json
  const.py             DOMAIN, defaults, timing constants, event names, click-type strings
  config_flow.py       ConfigFlow (user step) + OptionsFlow (menu: settings, pair)
  hub.py               FlicHub: connection lifecycle, filtering, flic_click, subscriptions
  entity.py            FlicButtonEntity / FlicServerEntity base classes (device_info, availability)
  event.py             button event entity
  binary_sensor.py     button pressed + server connectivity
  sensor.py            button battery + server controller state
  device_trigger.py    device triggers single/double/hold
  diagnostics.py
  icons.json
  translations/en.json
  brand/icon.png, brand/icon@2x.png
  flicd/__init__.py    re-exports
  flicd/packets.py     codec, enums, framing (CC0-derived from upstream aioflic.py)
  flicd/client.py      FlicdClient (asyncio)
tests/
  conftest.py          enable_custom_integrations, fake_flicd fixture
  fake_flicd.py        in-process fake flicd TCP server
  test_packets.py  test_client.py  test_hub.py  test_config_flow.py
  test_init.py  test_entities.py  test_device_trigger.py  test_diagnostics.py  test_metadata.py
brand-src/icon.svg
hacs.json  README.md  LICENSE  pyproject.toml  requirements_test.txt
.github/workflows/validate.yml  .github/workflows/tests.yml
```

---

### Task 1: Repo scaffold and metadata

**Files:**
- Create: `pyproject.toml`, `requirements_test.txt`, `.gitignore`, `hacs.json`, `LICENSE`, `custom_components/flicbutton/{__init__.py,manifest.json,const.py}`, `tests/__init__.py`, `tests/conftest.py`, `tests/test_metadata.py`

**Interfaces:**
- Produces in `const.py`: `DOMAIN = "flicbutton"`, `DEFAULT_PORT = 5551`, `CONF_QUEUE_TIMEOUT = "queue_timeout"`, `DEFAULT_QUEUE_TIMEOUT = 3`, `CONF_IGNORED_CLICK_TYPES = "ignored_click_types"`, `CONF_AUTO_PAIR = "auto_pair"`, `CLICK_SINGLE/CLICK_DOUBLE/CLICK_HOLD = "single"/"double"/"hold"`, `CLICK_TYPES = [single, double, hold]`, `EVENT_FLIC_CLICK = "flic_click"`, `BACKOFF_INITIAL = 1.0`, `BACKOFF_MAX = 60.0`, `PING_INTERVAL = 30.0`, `PING_TIMEOUT = 10.0`, `CONNECT_TIMEOUT = 10.0`, `def legacy_button_name(bd_addr: str) -> str`.

- [ ] **Step 1:** Create a venv and install test deps. `requirements_test.txt` pins the newest `pytest-homeassistant-custom-component` whose `homeassistant` pin is ≥ 2026.11.0 (check PyPI; if none exists yet, pin the newest one and note it in the commit message), plus `ruff`. Run `python3.14 -m venv .venv && .venv/bin/pip install -r requirements_test.txt`. Expected: installs cleanly.
- [ ] **Step 2: Write failing tests** in `tests/test_metadata.py`:
  - `test_manifest_required_keys`: loads manifest.json; asserts every key in Global Constraints is present with the exact values given there; `version == "0.1.0"`.
  - `test_hacs_json`: `{"name": "Flic button (flicd)", "homeassistant": "2026.11.0"}` exactly; no other keys.
  - `test_legacy_button_name`: `legacy_button_name("80:E4:DA:71:2B:3C") == "flic_80e4da712b3c"`.
- [ ] **Step 3:** Run `.venv/bin/pytest tests/test_metadata.py -v`. Expected: FAIL (files missing).
- [ ] **Step 4:** Create the files. `pyproject.toml` holds `[tool.pytest.ini_options] asyncio_mode = "auto"`, `testpaths = ["tests"]`, plus ruff config (target py314). `conftest.py` has an autouse fixture `auto_enable_custom_integrations(enable_custom_integrations)`. `__init__.py` is a docstring only for now. `.gitignore` covers `.venv`, `__pycache__`, `.pytest_cache`, `.coverage`.
- [ ] **Step 5:** Run the tests. Expected: PASS. Run `black . && .venv/bin/ruff check .`. Expected: clean.
- [ ] **Step 6:** Commit `chore: scaffold flicbutton repo and metadata`.

---

### Task 2: flicd packet codec

**Files:**
- Create: `custom_components/flicbutton/flicd/__init__.py`, `custom_components/flicbutton/flicd/packets.py`
- Test: `tests/test_packets.py`

**Interfaces:**
- Produces (`flicd/packets.py`):
  - Enums (`IntEnum`, values = protocol order): `CreateConnectionChannelError`, `ConnectionStatus`, `DisconnectReason`, `RemovedReason`, `ClickType` (ButtonDown=0, ButtonUp, ButtonClick, ButtonSingleClick, ButtonDoubleClick, ButtonHold), `BdAddrType`, `LatencyMode` (NormalLatency=0, LowLatency, HighLatency), `BluetoothControllerState` (Detached=0, Resetting, Attached), `ScanWizardResult` (WizardSuccess=0, WizardCancelledByUser, WizardFailedTimeout, WizardButtonIsPrivate, WizardBluetoothUnavailable, WizardInternetBackendError, WizardInvalidData, WizardButtonBelongsToOtherPartner, WizardButtonAlreadyConnectedToOtherDevice).
  - `def encode_command(name: str, **fields) -> bytes` returns a framed packet (2-byte LE length + opcode + body).
  - `def decode_command(payload: bytes) -> tuple[str, dict[str, Any]]` (used by the fake server).
  - `def encode_event(name: str, **fields) -> bytes` returns a framed packet. `EvtGetInfoResponse` takes `bd_addr_of_verified_buttons: list[str]` and appends the address tail.
  - `def decode_event(payload: bytes) -> tuple[str, dict[str, Any]] | None` returns `None` for an unknown opcode; extra trailing bytes are ignored. bd_addr fields come out as lowercase `aa:bb:...`; enum fields come out as enum members; pascal strings are decoded UTF-8, with `""` mapped to `None` for color/serial; uuid is a 32-char hex string, or `None` if all zero.
  - `class FrameReader: def feed(self, data: bytes) -> list[bytes]` returns the complete payloads (without the length prefix).
- The opcode tables and struct formats are copied from upstream `clientlib/python/aioflic.py` (`_EVENTS`, `_COMMANDS`). The file header credits it: "Derived from fliclib-linux-hci aioflic.py, CC0 1.0".

- [ ] **Step 1: Write failing tests** in `tests/test_packets.py`:
  - `test_encode_cmd_ping`: `encode_command("CmdPing", ping_id=7) == bytes([5, 0, 7, 7, 0, 0, 0])`.
  - `test_encode_cmd_create_connection_channel_bdaddr_little_endian`: for `bd_addr="08:09:0a:0b:0c:0d"`, bytes 7..12 of the output (after the length, opcode and 4-byte conn_id) are `0d 0c 0b 0a 09 08`.
  - `test_decode_button_event`: payload `bytes([7]) + struct.pack("<IBBI", 3, 4, 1, 5)` decodes to `("EvtButtonSingleOrDoubleClickOrHold", {"conn_id": 3, "click_type": ClickType.ButtonDoubleClick, "was_queued": True, "time_diff": 5})`.
  - `test_get_info_roundtrip_with_buttons`: encode, then decode `EvtGetInfoResponse` with two addresses; `bd_addr_of_verified_buttons` round-trips.
  - `test_battery_status_decode`: `EvtBatteryStatus` decodes `battery_percentage == -1` and `timestamp == 1762500000`.
  - `test_decode_unknown_opcode_and_extra_bytes`: `decode_event(bytes([250, 1, 2])) is None`; a ping response with 3 extra trailing bytes still decodes `ping_id`.
  - `test_frame_reader_split_header_and_coalesced`: three framed events concatenated, fed as `[b[:1], b[1:2], b[2:9], b[9:]]`, yield exactly three payloads in order.
  - `test_command_roundtrip_all`: every command name round-trips through encode → `FrameReader` → `decode_command`.
- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_packets.py -v`. Expected: FAIL (ImportError).
- [ ] **Step 3:** Implement `packets.py`, using the upstream struct formats.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** black + ruff, then commit `feat: flicd protocol codec`.

---

### Task 3: Fake flicd server and async client

**Files:**
- Create: `tests/fake_flicd.py`, `custom_components/flicbutton/flicd/client.py`
- Modify: `tests/conftest.py` (add the `fake_flicd` async fixture: starts on `127.0.0.1:0`, yields, stops)
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: Task 2 codec.
- Produces (`flicd/client.py`):
  - `class FlicdConnectionError(Exception)`
  - `@dataclass(frozen=True) class ServerInfo: controller_state: BluetoothControllerState; bd_addr: str; bd_addr_type: BdAddrType; max_pending_connections: int; max_concurrent_buttons: int; verified_buttons: list[str]`
  - `@dataclass(frozen=True) class ButtonInfo: bd_addr: str; uuid: str | None; color: str | None; serial_number: str | None; flic_version: int; firmware_version: int`
  - `@dataclass(frozen=True) class ScanResult: result: ScanWizardResult; bd_addr: str | None; name: str | None`
  - `class ChannelListener(Protocol)` with `on_up_down(click_type, was_queued: bool, time_diff: int)`, `on_click(click_type, was_queued: bool, time_diff: int)` (fed by `EvtButtonSingleOrDoubleClickOrHold` only), `on_status(status: ConnectionStatus, reason: DisconnectReason)`, `on_removed(reason: RemovedReason)`.
  - `class FlicdClient`:
    - `__init__(self, host: str, port: int)`
    - `async def connect(self, timeout: float = CONNECT_TIMEOUT) -> None`; raises `FlicdConnectionError` on refuse or timeout.
    - `async def close(self) -> None`
    - `connected: bool` (property)
    - `on_disconnect: Callable[[], None] | None`, called once when the transport is lost (not on `close()`).
    - `on_server_event: Callable[[str, dict], None] | None` for `EvtNewVerifiedButton`, `EvtButtonDeleted`, `EvtBluetoothControllerStateChange`, `EvtNoSpaceForNewConnection`, `EvtGotSpaceForNewConnection`.
    - `async def get_info(self) -> ServerInfo`
    - `async def get_button_info(self, bd_addr: str) -> ButtonInfo` (responses resolve FIFO)
    - `async def ping(self, timeout: float) -> None`; raises `TimeoutError`.
    - `def open_channel(self, bd_addr: str, listener: ChannelListener) -> int` sends `CmdCreateConnectionChannel` with `LatencyMode.NormalLatency` and `auto_disconnect_time=511`.
    - `def close_channel(self, conn_id: int) -> None`
    - `def add_battery_listener(self, bd_addr: str, callback: Callable[[int | None, datetime], None]) -> int`. `-1` maps to `None`; the timestamp becomes an aware UTC datetime.
    - `async def run_scan_wizard(self, on_progress: Callable[[str], None] | None = None) -> ScanResult`. Progress strings: `"found_private"`, `"found_public"`, `"connected"`.
    - `async def cancel_scan_wizard(self) -> None`
    - `async def delete_button(self, bd_addr: str) -> None` resolves on `EvtButtonDeleted` for that address.
    - All pending futures fail with `FlicdConnectionError` when the connection drops.
- Produces (`tests/fake_flicd.py`): `class FakeFlicd` with `async start() -> None`, `port: int`, `async stop()`, `buttons: dict[str, dict]` (bd_addr → button-info fields, used for GetInfo/GetButtonInfo), `controller_state`, `answer_pings: bool`, `commands: list[tuple[str, dict]]`, `async send(name: str, **fields)` (to all clients), `async drop_clients()` (close sockets), `scan_wizard_script: list[tuple[str, dict]]` (events replayed on `CmdCreateScanWizard`, with `scan_wizard_id` filled in), `async wait_for_command(name: str, timeout=2.0) -> dict`. It answers `CmdDeleteButton` with `EvtButtonDeleted(deleted_by_this_client=True)` and removes the button.

- [ ] **Step 1: Write failing tests** in `tests/test_client.py` (all use `fake_flicd`):
  - `test_connect_refused_raises`: port with nothing listening → `FlicdConnectionError`.
  - `test_get_info_lists_verified_buttons`: `fake.buttons = {"80:e4:da:71:2b:3c": {...}}` → `info.verified_buttons == ["80:e4:da:71:2b:3c"]`, `info.controller_state is BluetoothControllerState.Attached`.
  - `test_button_info_fifo`: two concurrent `get_button_info` calls get their own addresses back.
  - `test_ping_timeout`: `fake.answer_pings = False` → `ping(0.2)` raises `TimeoutError`.
  - `test_channel_click_dispatch`: open a channel and send `EvtButtonSingleOrDoubleClickOrHold(conn_id, ButtonHold, False, 0)` → the listener's `on_click` gets `(ClickType.ButtonHold, False, 0)`; `EvtButtonUpOrDown` → `on_up_down`; other button opcodes are ignored.
  - `test_battery_listener_utc`: `EvtBatteryStatus(listener_id, 87, 1762500000)` → callback `(87, datetime(2025, 11, 7, 7, 20, tzinfo=UTC))`; `-1` → `None`.
  - `test_scan_wizard_success`: script `[("EvtScanWizardFoundPublicButton", {"bd_addr": A, "name": "F0"}), ("EvtScanWizardButtonConnected", {}), ("EvtScanWizardCompleted", {"result": WizardSuccess})]` → `ScanResult(WizardSuccess, A, "F0")`; progress `["found_public", "connected"]`.
  - `test_delete_button`: resolves; `fake.commands` contains `CmdDeleteButton` for A.
  - `test_drop_calls_on_disconnect_and_fails_pending`: pending `get_info` with the fake paused, then `drop_clients()` → future raises `FlicdConnectionError`; `on_disconnect` called once; `connected is False`.
- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_client.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement `FakeFlicd` (asyncio server using the Task 2 codec) and `FlicdClient` (`loop.create_connection` + an `asyncio.Protocol` subclass that uses `FrameReader`). Unknown `conn_id`/`listener_id`/`scan_wizard_id` values are logged at debug level and ignored, never raised (upstream raises `KeyError`).
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** black + ruff, then commit `feat: asyncio flicd client with fake server`.

---

### Task 4: FlicHub: lifecycle, reconnect, filtering, flic_click

**Files:**
- Create: `custom_components/flicbutton/hub.py`
- Test: `tests/test_hub.py`

**Interfaces:**
- Consumes: Task 3 `FlicdClient`, `ServerInfo`, `ButtonInfo`, `ScanResult`; Task 1 constants.
- Produces (`hub.py`):
  - `@dataclass class HubOptions: queue_timeout: int; ignored_click_types: list[str]; auto_pair: bool`, plus `@classmethod from_entry_options(cls, options: Mapping) -> HubOptions` with defaults applied.
  - `class HubEventKind(StrEnum)`: `AVAILABILITY`, `BUTTON_ADDED`, `BUTTON_REMOVED`, `CLICK`, `PRESSED`, `BATTERY`, `CONTROLLER_STATE`.
  - `@dataclass(frozen=True) class HubEvent: kind: HubEventKind; bd_addr: str | None = None; value: Any = None`. CLICK value: `(click_type_str, queued_time_int)`. PRESSED: `bool`. BATTERY: `int | None`. CONTROLLER_STATE: `BluetoothControllerState`.
  - `class FlicHub`:
    - `__init__(self, hass: HomeAssistant, entry_id: str, host: str, port: int, options: HubOptions)`
    - `async def async_start(self) -> None`: first connect + sync; raises `FlicdConnectionError` (the caller turns it into `ConfigEntryNotReady`).
    - `async def async_stop(self) -> None`
    - `connected: bool`; `available: bool` (connected and controller `Attached`); `server_info: ServerInfo | None`; `buttons: dict[str, ButtonInfo]`; `battery: dict[str, int | None]`.
    - `@callback def async_subscribe(self, cb: Callable[[HubEvent], None]) -> Callable[[], None]`
    - `async def async_pair(self, on_progress: Callable[[str], None] | None = None) -> ScanResult`; raises `PairingInProgress` if already running (exception defined in hub.py) and `FlicdConnectionError` if not connected.
    - `async def async_delete_button(self, bd_addr: str) -> None`
- Behaviour:
  - Sync (after every connect): `get_info` → for each verified button: `get_button_info`, `open_channel`, `add_battery_listener`. Emit `BUTTON_ADDED` for addresses not seen before in this hub's lifetime, then `AVAILABILITY`.
  - `EvtNewVerifiedButton` → same per-button setup + `BUTTON_ADDED`. `EvtButtonDeleted` → drop from `buttons` + `BUTTON_REMOVED`.
  - Reconnect: on `on_disconnect` or a ping failure, close the client, emit `AVAILABILITY`, and schedule reconnect via `homeassistant.helpers.event.async_call_later` (backoff 1, 2, 4 … capped at 60, reset after a successful sync). Keepalive via `async_track_time_interval` every `PING_INTERVAL` calling `ping(PING_TIMEOUT)`. Timers must go through HA helpers so tests can drive them with `async_fire_time_changed`.
  - Filtering, applied once in the channel listener before anything is emitted: if `was_queued and time_diff > queue_timeout`, drop (log a warning, same wording as legacy) for both up/down and clicks. Click types map SingleClick→single, DoubleClick→double, Hold→hold. If the mapped type is in `ignored_click_types`, drop the click (not up/down).
  - A surviving click fires `EVENT_FLIC_CLICK` with the exact legacy payload, then emits `CLICK`. Up/down emits `PRESSED` (`True` on ButtonDown).
  - `auto_pair=True`: after each sync, start a scan wizard loop that restarts on completion and stops on disconnect or `async_stop`.

- [ ] **Step 1: Write failing tests** in `tests/test_hub.py` (use `hass`, `fake_flicd`, `async_fire_time_changed`, `async_capture_events(hass, "flic_click")`):
  - `test_start_syncs_buttons`: two fake buttons → `hub.buttons` has both; two `BUTTON_ADDED` events; `hub.available is True`.
  - `test_start_unreachable_raises`: raises `FlicdConnectionError`.
  - `test_click_fires_legacy_event`: hold on A → exactly one `flic_click` with data `{"button_name": "flic_80e4da712b3c", "button_address": "80:e4:da:71:2b:3c", "click_type": "hold", "queued_time": 0}` and one `CLICK` event `("hold", 0)`.
  - `test_ignored_click_type_dropped`: `ignored_click_types=["double"]` → a double emits nothing; a single still fires; up/down still emits `PRESSED`.
  - `test_queued_click_beyond_timeout_dropped_everywhere`: `queue_timeout=3`, click and up/down with `was_queued=True, time_diff=4` → no `flic_click`, no `CLICK`, no `PRESSED`; `time_diff=3` → delivered.
  - `test_drop_marks_unavailable_and_reconnects`: `fake.drop_clients()` → `available is False`, AVAILABILITY emitted; fire time +1 s → reconnected, channels re-created (`CmdCreateConnectionChannel` seen again), `available is True`, no duplicate `BUTTON_ADDED`.
  - `test_backoff_doubles_and_caps`: with the fake stopped, the reconnect attempt delays are 1, 2, 4, 8, 16, 32, 60, 60 (assert via patched `async_call_later` delay args).
  - `test_ping_timeout_marks_unavailable_and_reconnects`: `answer_pings=False`, fire +30 s then +10 s → unavailable; `answer_pings=True`, fire +1 s → available.
  - `test_controller_detached_makes_buttons_unavailable`: send `EvtBluetoothControllerStateChange(Detached)` → `connected is True`, `available is False`, CONTROLLER_STATE emitted; `Attached` → available.
  - `test_new_verified_button_added` / `test_button_deleted_by_flicd_removed`.
  - `test_battery_updates`: `EvtBatteryStatus` → `hub.battery[A] == 87`, BATTERY event.
  - `test_pair_in_progress_rejected`: second concurrent `async_pair` raises `PairingInProgress`.
  - `test_auto_pair_restarts_wizard`: `auto_pair=True` → after a wizard completes with timeout, `CmdCreateScanWizard` is sent again.
- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_hub.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement `hub.py`.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** black + ruff, then commit `feat: FlicHub with reconnect, keepalive and filtering`.

---

### Task 5: Config flow (user step) and entry setup with devices

**Files:**
- Create: `custom_components/flicbutton/config_flow.py` (ConfigFlow only in this task), `custom_components/flicbutton/translations/en.json`
- Modify: `custom_components/flicbutton/__init__.py`
- Test: `tests/test_config_flow.py`, `tests/test_init.py`

**Interfaces:**
- Consumes: `FlicHub`, `HubOptions`, `FlicdClient`, `FlicdConnectionError`.
- Produces:
  - `type FlicConfigEntry = ConfigEntry[FlicHub]` in `__init__.py`; `entry.runtime_data` is the hub.
  - `PLATFORMS = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR]`
  - Device identifiers: button `(DOMAIN, bd_addr)`; server `(DOMAIN, f"server_{entry.unique_id}")`. Button devices use `via_device=(DOMAIN, f"server_{unique_id}")`, `manufacturer="Shortcut Labs"`, `model=f"Flic {flic_version}"`, `serial_number`, `sw_version=str(firmware_version)`, `name=f"Flic {bd_addr}"`. The color goes in `hw_version`, or in the model as `"Flic 1 (black)"` if `hw_version` is wrong for it (implementer's choice; keep it consistent).
  - `@callback def async_claim_button_device(hass, entry: FlicConfigEntry, info: ButtonInfo) -> dr.DeviceEntry`: creates or updates the device. If the device already belongs to another `flicbutton` entry, move the device and its entities (`er.async_update_entity(..., config_entry_id=entry.entry_id)`) to this entry, and remove the old entry from the device.
  - `async def async_remove_config_entry_device(hass, entry, device) -> bool`: returns False for the server device and when the hub is not connected (log a warning). Otherwise awaits `hub.async_delete_button(bd_addr)` and returns True.
  - On `BUTTON_REMOVED`, the device drops this entry (which deletes it if no entry remains).
  - Options changes reload the entry (`OptionsFlowWithReload` in Task 8).
- ConfigFlow: step `user` with schema `host` (str, required), `port` (int, default 5551). On submit: connect + `get_info` with a 10 s timeout. Unique id = `info.bd_addr` lowercased (the server's Bluetooth address). Abort `already_configured` (and update host/port on the existing entry). Error `cannot_connect`. Title `f"flicd {host}"`. Entry data `{host, port}`; options start empty.

- [ ] **Step 1: Write failing tests:**
  - `tests/test_config_flow.py`: `test_user_flow_creates_entry` (`data == {"host": "127.0.0.1", "port": fake.port}`, `unique_id` = the fake's bd_addr); `test_user_flow_cannot_connect` (error `{"base": "cannot_connect"}`, then a retry succeeds); `test_user_flow_already_configured_updates_host`.
  - `tests/test_init.py`: `test_setup_creates_server_and_button_devices` (one server device, two button devices with `via_device_id` = server, model `"Flic 1"`); `test_setup_not_ready_when_unreachable` (`entry.state is ConfigEntryState.SETUP_RETRY`); `test_unload`; `test_remove_device_unpairs` (the device-registry remove path → `CmdDeleteButton` sent, device gone); `test_remove_device_refused_when_offline`; `test_remove_server_device_refused`; `test_button_deleted_by_flicd_removes_device`; `test_button_moves_between_entries` (two fakes, A paired on fake1, then fake1 deletes it and fake2 announces it as new → the device has only entry2, and its entities belong to entry2 with unchanged entity_ids).
- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_config_flow.py tests/test_init.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement. `translations/en.json` contains `config.step.user` (title "Connect to flicd", field labels, a description saying flicd must be started with `-s 0.0.0.0` to accept remote clients), `config.error.cannot_connect`, `config.abort.already_configured`.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** black + ruff, then commit `feat: config flow and entry setup with device registry`.

---

### Task 6: Entities (event, binary_sensor, sensor)

**Files:**
- Create: `custom_components/flicbutton/{entity.py,event.py,binary_sensor.py,sensor.py,icons.json}`
- Modify: `translations/en.json` (entity names, event type state names)
- Test: `tests/test_entities.py`

**Interfaces:**
- Consumes: `FlicConfigEntry`, `HubEvent`/`HubEventKind`, device identifiers from Task 5.
- Produces:
  - `entity.py`: `class FlicButtonEntity(Entity)` (`_attr_has_entity_name = True`, `should_poll = False`, `device_info` from the button identifier, `available = hub.available and bd_addr in hub.buttons`, subscribes in `async_added_to_hass` and unsubscribes on removal, with an overridable `_handle_hub_event(event)`). `class FlicServerEntity(Entity)` for server-device entities, `available = True` always, so connectivity can report off.
  - Unique ids: `f"{bd_addr}_event"`, `f"{bd_addr}_pressed"`, `f"{bd_addr}_battery"`, `f"{unique_id}_connected"`, `f"{unique_id}_controller_state"`.
  - Each platform's `async_setup_entry` adds entities for the current `hub.buttons` and subscribes to `BUTTON_ADDED` to add more later.
- Entities:
  - `event`: translation key `click`. `event_types` = `CLICK_TYPES` minus the ignored ones. `_trigger_event(click_type, {"queued_time": n})` on `CLICK`.
  - `binary_sensor` button: translation key `pressed`, no device class, state from `PRESSED`.
  - `binary_sensor` server: `BinarySensorDeviceClass.CONNECTIVITY`, `EntityCategory.DIAGNOSTIC`, `is_on = hub.connected`.
  - `sensor` battery: `SensorDeviceClass.BATTERY`, `PERCENTAGE`, `SensorStateClass.MEASUREMENT`, `EntityCategory.DIAGNOSTIC`; `None` when unknown.
  - `sensor` controller state: `SensorDeviceClass.ENUM`, options `["detached", "resetting", "attached"]`, `EntityCategory.DIAGNOSTIC`, `None` while disconnected.

- [ ] **Step 1: Write failing tests** in `tests/test_entities.py` (entities are looked up through the entity registry by unique id; never hardcode entity_ids):
  - `test_entities_created_per_button`: per button, one each of event, pressed binary_sensor and battery sensor; server connectivity + controller state.
  - `test_event_entity_fires`: hold → state attributes `event_type == "hold"`, `queued_time == 0`.
  - `test_event_types_exclude_ignored`: options `ignored_click_types=["hold"]` → `event_types == ["single", "double"]`.
  - `test_pressed_binary_sensor`: ButtonDown → `on`, ButtonUp → `off`.
  - `test_battery_sensor`: 87 → `"87"`; -1 → `unknown`.
  - `test_unavailable_on_drop_and_back`: `drop_clients()` → button entities `unavailable`, connectivity `off`; after reconnect → available, connectivity `on`.
  - `test_new_button_entities_added_at_runtime`: `EvtNewVerifiedButton` → three new entities appear.
- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_entities.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement the platforms and `icons.json` (mdi icons: `mdi:gesture-tap-button` for the event, `mdi:circle-slice-8` for pressed, `mdi:bluetooth` for controller state).
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** black + ruff, then commit `feat: event, binary_sensor and sensor entities`.

---

### Task 7: Device triggers

**Files:**
- Create: `custom_components/flicbutton/device_trigger.py`
- Modify: `translations/en.json` (`device_automation.trigger_type.single|double|hold`)
- Test: `tests/test_device_trigger.py`

**Interfaces:**
- Consumes: device identifiers (Task 5); `EVENT_FLIC_CLICK` payload (Task 4).
- Produces: `TRIGGER_SCHEMA`, `async_get_triggers`, `async_attach_trigger`.
- Decision: the spec says triggers are "built on the event entity". Its intent is that triggers see exactly the post-filter clicks. `flic_click` is emitted after the same single filter, so the triggers attach an `event` trigger on `flic_click` with `event_data={"button_address": bd_addr, "click_type": type}`. This is equivalent, and it keeps working while the event entity is disabled. `async_get_triggers` offers only the non-ignored click types.

- [ ] **Step 1: Write failing tests:** `test_get_triggers_lists_three_types` (and two types when `hold` is ignored); `test_trigger_fires_automation` (an automation with a device trigger `double` → a double on that button runs the action; a double on another button doesn't); `test_server_device_has_no_triggers`.
- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_device_trigger.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement using `homeassistant.components.homeassistant.triggers.event`.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** black + ruff, then commit `feat: device triggers`.

---

### Task 8: Options flow: settings and on-demand pairing

**Files:**
- Modify: `custom_components/flicbutton/config_flow.py`, `translations/en.json`
- Test: `tests/test_config_flow.py`

**Interfaces:**
- Consumes: `FlicHub.async_pair`, `PairingInProgress`, `ScanResult`, `ScanWizardResult`.
- Produces: `FlicOptionsFlow(OptionsFlowWithReload)` with steps:
  - `init`: a menu with `settings` and `pair`.
  - `settings`: `queue_timeout` (int 0–60, unit s), `ignored_click_types` (multi-select of `CLICK_TYPES`, translated), `auto_pair` (bool). Saves the options, and the entry reloads.
  - `pair`: aborts `not_connected` if `not hub.connected`, aborts `pair_in_progress` on `PairingInProgress`, and aborts `auto_pair_enabled` if auto-pair is on (the wizards would collide). Otherwise it shows `async_show_progress(progress_action="pairing", progress_task=...)`, then `async_show_progress_done(next_step_id="pair_result")`.
  - `pair_result`: aborts with a reason mapped from `ScanWizardResult`: Success → `pair_success` (placeholder `bd_addr`), FailedTimeout → `pair_timeout`, ButtonIsPrivate → `pair_private`, BluetoothUnavailable → `pair_bluetooth_unavailable`, InternetBackendError → `pair_backend_error`, InvalidData → `pair_invalid_data`, BelongsToOtherPartner → `pair_other_partner`, AlreadyConnectedToOtherDevice → `pair_connected_elsewhere`, CancelledByUser → `pair_cancelled`. A connection loss during pairing → `not_connected`. Closing the progress dialog early calls `cancel_scan_wizard` (`async_remove` of the flow).
  - The progress description tells the user: "Press and hold the Flic button for 7 seconds until it blinks. Pairing needs internet access on the flicd host."

- [ ] **Step 1: Write failing tests:** `test_options_settings_saved` (the options equal the submitted values; the entry reloads); `test_pair_success` (scripted wizard → abort `pair_success`, the new button's device exists afterwards); `test_pair_timeout`; `test_pair_private`; `test_pair_bluetooth_unavailable` (controller Detached → the scripted fake completes with `WizardBluetoothUnavailable` → abort `pair_bluetooth_unavailable`); `test_pair_not_connected`; `test_pair_refused_with_auto_pair`.
- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_config_flow.py -v`. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement, and add every abort reason, step title and field label to `translations/en.json`.
- [ ] **Step 4:** Run the tests. Expected: PASS. Then `.venv/bin/pytest --cov=custom_components.flicbutton --cov-report=term-missing`: `config_flow.py` must be at 100 % and the total at ≥ 90 %. Add tests until both are met.
- [ ] **Step 5:** black + ruff, then commit `feat: options flow with on-demand pairing`.

---

### Task 9: Diagnostics

**Files:**
- Create: `custom_components/flicbutton/diagnostics.py`
- Test: `tests/test_diagnostics.py`

**Interfaces:**
- Produces: `async_get_config_entry_diagnostics(hass, entry) -> dict` returning `{"entry": async_redact_data(entry.as_dict(), {"host"}), "connected": bool, "available": bool, "server_info": asdict(...) | None, "buttons": [asdict(ButtonInfo) + {"battery": ...}]}`. Enums are serialised by name.

- [ ] **Step 1:** Write a failing `test_diagnostics` (host redacted, two buttons listed, `controller_state == "Attached"`). Use `get_diagnostics_for_config_entry` from `pytest_homeassistant_custom_component.components.diagnostics`.
- [ ] **Step 2:** Run it. Expected: FAIL. **Step 3:** Implement. **Step 4:** Run it. Expected: PASS.
- [ ] **Step 5:** black + ruff, then commit `feat: diagnostics`.

---

### Task 10: Brand icon (owner approval gate)

**Files:**
- Create: `brand-src/icon.svg`, `custom_components/flicbutton/brand/icon.png` (256×256), `custom_components/flicbutton/brand/icon@2x.png` (512×512)
- Modify: `tests/test_metadata.py`

- [ ] **Step 1:** Write a failing `test_brand_icons` (Pillow, already pulled in by HA): both files exist, are PNG, are exactly 256×256 and 512×512, and have RGBA with a transparent background corner.
- [ ] **Step 2:** Design `brand-src/icon.svg`: an original, generic flat round push-button seen at a slight angle, with a single tap ring. No Flic wordmark, no Flic logo shapes. Render with `inkscape brand-src/icon.svg -w 256 -h 256 -o custom_components/flicbutton/brand/icon.png` (and 512 for @2x).
- [ ] **Step 3:** Run the test. Expected: PASS.
- [ ] **Step 4: STOP.** Show the owner both PNGs and wait for approval. Iterate on the SVG until approved.
- [ ] **Step 5:** Commit `feat: brand icon`.

---

### Task 11: Docs, CI and release readiness

**Files:**
- Create: `README.md`, `.github/workflows/validate.yml`, `.github/workflows/tests.yml`, `info.md` only if Marketplace validation demands it

- [ ] **Step 1:** README sections: what it is; requirements (flicd from 50ButtonsEach/fliclib-linux-hci, started with `-s 0.0.0.0` when remote, a dedicated Bluetooth adapter, internet access for pairing); install via Marketplace; setup; pairing; multiple servers for range (one button per server, and re-pairing moves the device); entities; device triggers; the `flic_click` event (exact payload table); migrating from core `flic` (remove the YAML, keep the automations); troubleshooting; disclaimer "not affiliated with Shortcut Labs / Flic".
- [ ] **Step 2:** `validate.yml`: on push/PR/schedule, run `home-assistant/actions/hassfest@master` and the HACS/Marketplace validation action (`hacs/action@main` with `category: integration`). **Before writing it, check the official HA 2026.11 developer docs or release notes for a Marketplace-specific validation action and use that if one exists.** `tests.yml`: Python 3.14, install `requirements_test.txt`, `black --check .`, `ruff check .`, `pytest --cov --cov-fail-under=90`.
- [ ] **Step 3:** Run locally: `black --check . && .venv/bin/ruff check . && .venv/bin/pytest --cov=custom_components.flicbutton --cov-fail-under=90`. Expected: all pass.
- [ ] **Step 4:** Check the Marketplace requirements against the official 2026.11 docs (spec §5 used community sources). Fix any gaps (e.g. extra `manifest.json` keys, `hacs.json` keys, repo topics) and update `test_metadata.py` to match.
- [ ] **Step 5:** Commit `docs: README and CI workflows`.
- [ ] **Step 6: STOP before anything outward-facing.** Creating the GitHub repo `frawau/flicbutton`, pushing, tagging `v0.1.0`, publishing a release and submitting to the Marketplace default catalog all need explicit owner confirmation at the time.
- [ ] **Step 7 (owner hardware check, before submitting):** the owner installs from the repo as a custom repository against their real Pi Zero flicd, reboots the Pi, and confirms the buttons come back without restarting HA.
