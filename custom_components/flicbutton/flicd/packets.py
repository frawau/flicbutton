"""Binary packet codec for the flicd protocol.

Derived from fliclib-linux-hci aioflic.py, CC0 1.0.

Wire format: little-endian, each packet is a 2-byte length (excluding itself),
a 1-byte opcode and the body. Trailing extra bytes in a body are ignored.
Bluetooth addresses are sent byte-reversed. This module is pure and has no
Home Assistant or asyncio dependency.
"""

from __future__ import annotations

import struct
from enum import IntEnum
from typing import Any


class CreateConnectionChannelError(IntEnum):
    NoError = 0
    MaxPendingConnectionsReached = 1


class ConnectionStatus(IntEnum):
    Disconnected = 0
    Connected = 1
    Ready = 2


class DisconnectReason(IntEnum):
    Unspecified = 0
    ConnectionEstablishmentFailed = 1
    TimedOut = 2
    BondingKeysMismatch = 3


class RemovedReason(IntEnum):
    RemovedByThisClient = 0
    ForceDisconnectedByThisClient = 1
    ForceDisconnectedByOtherClient = 2
    ButtonIsPrivate = 3
    VerifyTimeout = 4
    InternetBackendError = 5
    InvalidData = 6
    CouldntLoadDevice = 7
    DeletedByThisClient = 8
    DeletedByOtherClient = 9
    ButtonBelongsToOtherPartner = 10
    DeletedFromButton = 11


class ClickType(IntEnum):
    ButtonDown = 0
    ButtonUp = 1
    ButtonClick = 2
    ButtonSingleClick = 3
    ButtonDoubleClick = 4
    ButtonHold = 5


class BdAddrType(IntEnum):
    PublicBdAddrType = 0
    RandomBdAddrType = 1


class LatencyMode(IntEnum):
    NormalLatency = 0
    LowLatency = 1
    HighLatency = 2


class BluetoothControllerState(IntEnum):
    Detached = 0
    Resetting = 1
    Attached = 2


class ScanWizardResult(IntEnum):
    WizardSuccess = 0
    WizardCancelledByUser = 1
    WizardFailedTimeout = 2
    WizardButtonIsPrivate = 3
    WizardBluetoothUnavailable = 4
    WizardInternetBackendError = 5
    WizardInvalidData = 6
    WizardButtonBelongsToOtherPartner = 7
    WizardButtonAlreadyConnectedToOtherDevice = 8


# (name, struct format, space separated field names); opcode = list index.
_EVENTS: list[tuple[str, str, str]] = [
    (
        "EvtAdvertisementPacket",
        "<I6s17pb????",
        "scan_id bd_addr name rssi is_private already_verified "
        "already_connected_to_this_device already_connected_to_other_device",
    ),
    ("EvtCreateConnectionChannelResponse", "<IBB", "conn_id error connection_status"),
    (
        "EvtConnectionStatusChanged",
        "<IBB",
        "conn_id connection_status disconnect_reason",
    ),
    ("EvtConnectionChannelRemoved", "<IB", "conn_id removed_reason"),
    ("EvtButtonUpOrDown", "<IBBI", "conn_id click_type was_queued time_diff"),
    ("EvtButtonClickOrHold", "<IBBI", "conn_id click_type was_queued time_diff"),
    (
        "EvtButtonSingleOrDoubleClick",
        "<IBBI",
        "conn_id click_type was_queued time_diff",
    ),
    (
        "EvtButtonSingleOrDoubleClickOrHold",
        "<IBBI",
        "conn_id click_type was_queued time_diff",
    ),
    ("EvtNewVerifiedButton", "<6s", "bd_addr"),
    (
        "EvtGetInfoResponse",
        "<B6sBBhBBH",
        "bluetooth_controller_state my_bd_addr my_bd_addr_type "
        "max_pending_connections max_concurrently_connected_buttons "
        "current_pending_connections currently_no_space_for_new_connection "
        "nb_verified_buttons",
    ),
    ("EvtNoSpaceForNewConnection", "<B", "max_concurrently_connected_buttons"),
    ("EvtGotSpaceForNewConnection", "<B", "max_concurrently_connected_buttons"),
    ("EvtBluetoothControllerStateChange", "<B", "state"),
    ("EvtPingResponse", "<I", "ping_id"),
    (
        "EvtGetButtonInfoResponse",
        "<6s16s17p17pBI",
        "bd_addr uuid color serial_number flic_version firmware_version",
    ),
    ("EvtScanWizardFoundPrivateButton", "<I", "scan_wizard_id"),
    ("EvtScanWizardFoundPublicButton", "<I6s17p", "scan_wizard_id bd_addr name"),
    ("EvtScanWizardButtonConnected", "<I", "scan_wizard_id"),
    ("EvtScanWizardCompleted", "<IB", "scan_wizard_id result"),
    ("EvtButtonDeleted", "<6s?", "bd_addr deleted_by_this_client"),
    ("EvtBatteryStatus", "<Ibq", "listener_id battery_percentage timestamp"),
]

_COMMANDS: list[tuple[str, str, str]] = [
    ("CmdGetInfo", "", ""),
    ("CmdCreateScanner", "<I", "scan_id"),
    ("CmdRemoveScanner", "<I", "scan_id"),
    (
        "CmdCreateConnectionChannel",
        "<I6sBh",
        "conn_id bd_addr latency_mode auto_disconnect_time",
    ),
    ("CmdRemoveConnectionChannel", "<I", "conn_id"),
    ("CmdForceDisconnect", "<6s", "bd_addr"),
    ("CmdChangeModeParameters", "<IBh", "conn_id latency_mode auto_disconnect_time"),
    ("CmdPing", "<I", "ping_id"),
    ("CmdGetButtonInfo", "<6s", "bd_addr"),
    ("CmdCreateScanWizard", "<I", "scan_wizard_id"),
    ("CmdCancelScanWizard", "<I", "scan_wizard_id"),
    ("CmdDeleteButton", "<6s", "bd_addr"),
    ("CmdCreateBatteryStatusListener", "<I6s", "listener_id bd_addr"),
    ("CmdRemoveBatteryStatusListener", "<I", "listener_id"),
]

_EVENT_STRUCTS = [struct.Struct(fmt) for _, fmt, _ in _EVENTS]
_EVENT_FIELDS = [tuple(fields.split()) for _, _, fields in _EVENTS]
_EVENT_OPCODES = {name: i for i, (name, _, _) in enumerate(_EVENTS)}
_COMMAND_STRUCTS = [struct.Struct(fmt) for _, fmt, _ in _COMMANDS]
_COMMAND_FIELDS = [tuple(fields.split()) for _, _, fields in _COMMANDS]
_COMMAND_OPCODES = {name: i for i, (name, _, _) in enumerate(_COMMANDS)}

# Field name -> enum used to decode it.
_ENUM_FIELDS: dict[str, type[IntEnum]] = {
    "error": CreateConnectionChannelError,
    "connection_status": ConnectionStatus,
    "disconnect_reason": DisconnectReason,
    "removed_reason": RemovedReason,
    "click_type": ClickType,
    "bluetooth_controller_state": BluetoothControllerState,
    "my_bd_addr_type": BdAddrType,
    "state": BluetoothControllerState,
    "result": ScanWizardResult,
    "latency_mode": LatencyMode,
}

_BD_ADDR_FIELDS = ("bd_addr", "my_bd_addr")
_UUID_NONE = "0" * 32


def _addr_to_bytes(addr: str) -> bytes:
    return bytes.fromhex("".join(reversed(addr.split(":"))))


def _addr_to_str(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in reversed(raw))


def _frame(opcode: int, body: bytes) -> bytes:
    return struct.pack("<HB", len(body) + 1, opcode) + body


def _pack(
    structs: list[struct.Struct],
    field_names: list[tuple[str, ...]],
    opcode: int,
    fields: dict[str, Any],
) -> bytes:
    values = []
    for key in field_names[opcode]:
        value = fields[key]
        if key in _BD_ADDR_FIELDS:
            value = _addr_to_bytes(value)
        elif key == "uuid":
            value = bytes.fromhex(value or _UUID_NONE)
        elif key in ("name", "color", "serial_number"):
            value = (value or "").encode("utf-8")
        values.append(value)
    return structs[opcode].pack(*values)


def _unpack(
    structs: list[struct.Struct],
    field_names: list[tuple[str, ...]],
    opcode: int,
    payload: bytes,
) -> dict[str, Any]:
    struct_ = structs[opcode]
    values = struct_.unpack(payload[1 : 1 + struct_.size])
    items = dict(zip(field_names[opcode], values, strict=True))
    for key in _BD_ADDR_FIELDS:
        if key in items:
            items[key] = _addr_to_str(items[key])
    for key, enum in _ENUM_FIELDS.items():
        if key in items:
            items[key] = enum(items[key])
    return items


def encode_command(name: str, /, **fields: Any) -> bytes:
    """Return a framed command packet."""
    opcode = _COMMAND_OPCODES[name]
    return _frame(opcode, _pack(_COMMAND_STRUCTS, _COMMAND_FIELDS, opcode, fields))


def decode_command(payload: bytes) -> tuple[str, dict[str, Any]]:
    """Decode an unframed command payload (used by the test fake server)."""
    opcode = payload[0]
    items = _unpack(_COMMAND_STRUCTS, _COMMAND_FIELDS, opcode, payload)
    return _COMMANDS[opcode][0], items


def encode_event(name: str, /, **fields: Any) -> bytes:
    """Return a framed event packet (used by the test fake server).

    ``EvtGetInfoResponse`` takes ``bd_addr_of_verified_buttons`` and appends the
    addresses after the fixed part; ``nb_verified_buttons`` defaults to its length.
    """
    opcode = _EVENT_OPCODES[name]
    extra = b""
    if name == "EvtGetInfoResponse":
        addrs = list(fields.pop("bd_addr_of_verified_buttons", []))
        fields.setdefault("nb_verified_buttons", len(addrs))
        extra = b"".join(_addr_to_bytes(a) for a in addrs)
    body = _pack(_EVENT_STRUCTS, _EVENT_FIELDS, opcode, fields) + extra
    return _frame(opcode, body)


def decode_event(payload: bytes) -> tuple[str, dict[str, Any]] | None:
    """Decode an unframed event payload; None for an unknown opcode."""
    if not payload:
        return None
    opcode = payload[0]
    if opcode >= len(_EVENTS):
        return None
    items = _unpack(_EVENT_STRUCTS, _EVENT_FIELDS, opcode, payload)
    name = _EVENTS[opcode][0]

    for key in ("name", "color", "serial_number"):
        if key in items:
            items[key] = items[key].decode("utf-8")
    if name == "EvtGetButtonInfoResponse":
        items["uuid"] = items["uuid"].hex()
        if items["uuid"] == _UUID_NONE:
            items["uuid"] = None
        items["color"] = items["color"] or None
        items["serial_number"] = items["serial_number"] or None
    elif name == "EvtGetInfoResponse":
        pos = 1 + _EVENT_STRUCTS[opcode].size
        count = items["nb_verified_buttons"]
        items["bd_addr_of_verified_buttons"] = [
            _addr_to_str(payload[pos + 6 * i : pos + 6 * i + 6]) for i in range(count)
        ]
    return name, items


class FrameReader:
    """Reassemble length-prefixed packets from an arbitrary byte stream."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        """Add bytes; return complete payloads (without the length prefix)."""
        self._buffer += data
        payloads = []
        while len(self._buffer) >= 2:
            length = self._buffer[0] | (self._buffer[1] << 8)
            if len(self._buffer) < 2 + length:
                break
            payloads.append(bytes(self._buffer[2 : 2 + length]))
            del self._buffer[: 2 + length]
        return payloads
