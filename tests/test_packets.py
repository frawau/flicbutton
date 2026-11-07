"""Tests for the flicd packet codec."""

import struct

from custom_components.flicbutton.flicd.packets import (
    _COMMANDS,
    ClickType,
    FrameReader,
    decode_command,
    decode_event,
    encode_command,
    encode_event,
)

_SAMPLE_FIELDS = {
    "scan_id": 9,
    "scan_wizard_id": 10,
    "conn_id": 11,
    "listener_id": 12,
    "ping_id": 13,
    "bd_addr": "08:09:0a:0b:0c:0d",
    "latency_mode": 1,
    "auto_disconnect_time": 511,
}


def test_encode_cmd_ping():
    assert encode_command("CmdPing", ping_id=7) == bytes([5, 0, 7, 7, 0, 0, 0])


def test_encode_cmd_create_connection_channel_bdaddr_little_endian():
    data = encode_command(
        "CmdCreateConnectionChannel",
        conn_id=1,
        bd_addr="08:09:0a:0b:0c:0d",
        latency_mode=0,
        auto_disconnect_time=511,
    )
    assert data[7:13] == bytes([0x0D, 0x0C, 0x0B, 0x0A, 0x09, 0x08])


def test_decode_button_event():
    payload = bytes([7]) + struct.pack("<IBBI", 3, 4, 1, 5)
    assert decode_event(payload) == (
        "EvtButtonSingleOrDoubleClickOrHold",
        {
            "conn_id": 3,
            "click_type": ClickType.ButtonDoubleClick,
            "was_queued": True,
            "time_diff": 5,
        },
    )


def test_get_info_roundtrip_with_buttons():
    addrs = ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]
    framed = encode_event(
        "EvtGetInfoResponse",
        bluetooth_controller_state=2,
        my_bd_addr="01:02:03:04:05:06",
        my_bd_addr_type=0,
        max_pending_connections=3,
        max_concurrently_connected_buttons=20,
        current_pending_connections=0,
        currently_no_space_for_new_connection=False,
        nb_verified_buttons=2,
        bd_addr_of_verified_buttons=addrs,
    )
    (payload,) = FrameReader().feed(framed)
    name, fields = decode_event(payload)
    assert name == "EvtGetInfoResponse"
    assert fields["bd_addr_of_verified_buttons"] == addrs
    assert fields["my_bd_addr"] == "01:02:03:04:05:06"
    assert fields["nb_verified_buttons"] == 2


def test_battery_status_decode():
    payload = bytes([20]) + struct.pack("<Ibq", 4, -1, 1762500000)
    name, fields = decode_event(payload)
    assert name == "EvtBatteryStatus"
    assert fields["battery_percentage"] == -1
    assert fields["timestamp"] == 1762500000


def test_decode_unknown_opcode_and_extra_bytes():
    assert decode_event(bytes([250, 1, 2])) is None
    payload = bytes([13]) + struct.pack("<I", 42) + b"\x01\x02\x03"
    assert decode_event(payload) == ("EvtPingResponse", {"ping_id": 42})


def test_get_button_info_strings_and_uuid():
    payload = bytes([14]) + struct.pack(
        "<6s16s17p17pBI",
        bytes([6, 5, 4, 3, 2, 1]),
        bytes(16),
        b"",
        b"ABC",
        2,
        7,
    )
    name, fields = decode_event(payload)
    assert name == "EvtGetButtonInfoResponse"
    assert fields["bd_addr"] == "01:02:03:04:05:06"
    assert fields["uuid"] is None
    assert fields["color"] is None
    assert fields["serial_number"] == "ABC"


def test_frame_reader_split_header_and_coalesced():
    events = [
        encode_event("EvtPingResponse", ping_id=1),
        encode_event("EvtPingResponse", ping_id=2),
        encode_event("EvtPingResponse", ping_id=3),
    ]
    b = b"".join(events)
    reader = FrameReader()
    out = []
    for chunk in (b[:1], b[1:2], b[2:9], b[9:]):
        out.extend(reader.feed(chunk))
    assert [decode_event(p)[1]["ping_id"] for p in out] == [1, 2, 3]
    assert len(out) == 3


def test_command_roundtrip_all():
    for name, _fmt, field_names in _COMMANDS:
        fields = {f: _SAMPLE_FIELDS[f] for f in field_names.split()}
        (payload,) = FrameReader().feed(encode_command(name, **fields))
        assert decode_command(payload) == (name, fields)
