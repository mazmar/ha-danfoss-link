"""Tiny protobuf (proto2) codec for the MDG messages.

The schemas were recovered from the protoc-gen-lua modules shipped in the
Danfoss Link app (mdg_control_pb, mdg_routing_pb, mdg_pairing_pb). Only the
wire types actually used by MDG are supported.
"""

from __future__ import annotations

import struct
from typing import Any

# Field kinds
UINT = "uint"  # uint32 / uint64 / enum / bool (varint)
BOOL = "bool"
FIXED32 = "fixed32"
BYTES = "bytes"
STRING = "string"

# A schema is {field_name: (number, kind, repeated, sub_schema_or_None)}
Schema = dict[str, tuple[int, str, bool, "Schema | None"]]


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def encode(schema: Schema, msg: dict[str, Any]) -> bytes:
    """Serialize ``msg`` according to ``schema``; fields are emitted in number order."""
    out = bytearray()
    for name, (num, kind, repeated, sub) in sorted(schema.items(), key=lambda i: i[1][0]):
        if name not in msg or msg[name] is None:
            continue
        values = msg[name] if repeated else [msg[name]]
        for value in values:
            if sub is not None:
                data = encode(sub, value)
                out += _varint(num << 3 | 2) + _varint(len(data)) + data
            elif kind in (UINT, BOOL):
                out += _varint(num << 3 | 0) + _varint(int(value))
            elif kind == FIXED32:
                out += _varint(num << 3 | 5) + struct.pack("<I", value)
            elif kind in (BYTES, STRING):
                data = value.encode() if isinstance(value, str) else bytes(value)
                out += _varint(num << 3 | 2) + _varint(len(data)) + data
            else:  # pragma: no cover - schema error
                raise ValueError(kind)
    return bytes(out)


def decode(schema: Schema, buf: bytes) -> dict[str, Any]:
    """Parse ``buf``; unknown fields are skipped, repeated fields become lists."""
    by_num = {num: (name, kind, rep, sub) for name, (num, kind, rep, sub) in schema.items()}
    msg: dict[str, Any] = {n: [] for n, (_, _, rep, _) in schema.items() if rep}
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        num, wire = key >> 3, key & 7
        if wire == 0:
            raw, pos = _read_varint(buf, pos)
        elif wire == 1:
            raw = buf[pos : pos + 8]
            pos += 8
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            raw = buf[pos : pos + length]
            pos += length
        elif wire == 5:
            raw = struct.unpack_from("<I", buf, pos)[0]
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        if num not in by_num:
            continue
        name, kind, rep, sub = by_num[num]
        if sub is not None:
            value: Any = decode(sub, raw)
        elif kind == STRING:
            value = raw.decode("utf-8", "replace")
        elif kind == BOOL:
            value = bool(raw)
        else:
            value = raw
        if rep:
            msg[name].append(value)
        else:
            msg[name] = value
    return msg


# --- mdg.control -----------------------------------------------------------

ENDPOINT: Schema = {"host": (1, BYTES, False, None), "port": (2, UINT, False, None)}
ROUTING_INFO: Schema = {
    "peer_name": (3, STRING, False, None),
    "router": (4, BYTES, True, ENDPOINT),
    "token": (5, BYTES, False, None),
}
PROPERTY: Schema = {"key": (1, STRING, False, None), "value": (2, STRING, False, None)}

CONTROL = {
    # tag: (name, schema)
    1: ("handshake", {
        "magic": (1, FIXED32, False, None),
        "version_major": (2, UINT, False, None),
        "version_minor": (3, UINT, False, None),
        "property": (4, BYTES, True, PROPERTY),
    }),
    2: ("bye", {"reason": (1, UINT, False, None), "reason_text": (2, STRING, False, None)}),
    3: ("reconnect", {
        "new_host": (1, BYTES, False, ENDPOINT),
        "suggested_delay": (2, UINT, False, None),
    }),
    4: ("ping", {"req_id": (1, UINT, False, None), "perceived_qos": (2, UINT, False, None)}),
    5: ("ping_reply", {"req_id": (1, UINT, False, None)}),
    10: ("place_call", {
        "req_id": (1, UINT, False, None),
        "peer_name": (2, STRING, False, None),
        "protocol": (3, STRING, False, None),
        "ticket": (4, STRING, False, None),
    }),
    11: ("place_call_reply", {
        "req_id": (1, UINT, False, None),
        "result": (2, UINT, False, None),
        "routing": (3, BYTES, False, ROUTING_INFO),
        "result_text": (4, STRING, False, None),
    }),
    12: ("incoming_call", {
        "req_id": (1, UINT, False, None),
        "routing": (2, BYTES, False, ROUTING_INFO),
        "protocol": (3, STRING, False, None),
        "ticket": (4, STRING, False, None),
    }),
    13: ("incoming_call_reply", {
        "req_id": (1, UINT, False, None),
        "will_accept_call": (2, BOOL, False, None),
    }),
    20: ("set_peer_whitelist", {
        "req_id": (1, UINT, False, None),
        "peer_name": (2, STRING, True, None),
    }),
    21: ("set_peer_whitelist_reply", {"req_id": (1, UINT, False, None)}),
    30: ("allocate_pairing_channel", {"req_id": (1, UINT, False, None)}),
    31: ("allocate_pairing_channel_reply", {
        "req_id": (1, UINT, False, None),
        "result": (2, UINT, False, None),
        "pair_id": (3, STRING, False, None),
        "end_of_validity": (4, UINT, False, None),
    }),
    32: ("place_pairing_call", {
        "req_id": (1, UINT, False, None),
        "pair_id": (2, STRING, False, None),
    }),
    33: ("place_pairing_call_reply", {
        "req_id": (1, UINT, False, None),
        "result": (2, UINT, False, None),
        "routing": (3, BYTES, False, ROUTING_INFO),
        "result_text": (4, STRING, False, None),
    }),
}
CONTROL_TAGS = {name: tag for tag, (name, _) in CONTROL.items()}

PLACE_CALL_RESULTS = {0: "OK", 1: "PEER_NOT_AVAILABLE", 2: "SERVER_ERROR"}

# --- mdg.routing -----------------------------------------------------------

ROUTING = {
    0: ("handshake", {
        "magic": (1, FIXED32, False, None),
        "version_major": (2, UINT, False, None),
        "version_minor": (3, UINT, False, None),
        "token": (4, BYTES, False, None),
        "supported_protocols": (5, STRING, True, None),
    }),
    1: ("pleasehold", {}),
    2: ("goahead", {"selected_protocol": (1, STRING, False, None)}),
    3: ("routingerror", {"error": (1, UINT, False, None)}),
}
ROUTING_TAGS = {name: tag for tag, (name, _) in ROUTING.items()}

ROUTING_ERRORS = {
    0: "NO_ERROR",
    1: "SERVER_ERROR",
    2: "SYNTAX_ERROR",
    3: "INVALID_TOKEN",
    4: "PEER_TIMEOUT",
    5: "UNSUPPORTED_VERSION",
}

# --- pairing (PACE) --------------------------------------------------------

PAIRING = {
    1: ("pace_client_hello", {
        "magic": (1, FIXED32, False, None),
        "version_major": (2, UINT, False, None),
        "version_minor": (3, UINT, False, None),
    }),
    2: ("pace_server_hello", {
        "version_major": (1, UINT, False, None),
        "version_minor": (2, UINT, False, None),
        "salt": (3, BYTES, False, None),
        "iv": (4, BYTES, False, None),
        "z": (5, BYTES, False, None),
    }),
    3: ("pace_xy", {"xy": (1, BYTES, False, None)}),
    4: ("pace_pk", {"pk": (1, BYTES, False, None)}),
    5: ("pace_proof", {"token": (1, BYTES, False, None)}),
    6: ("contact_info", {"name": (1, STRING, False, None)}),
}
PAIRING_TAGS = {name: tag for tag, (name, _) in PAIRING.items()}


def encode_tagged(table: dict, name: str, msg: dict[str, Any]) -> bytes:
    """MDG convention: one type-code byte followed by the serialized message."""
    tags = {n: t for t, (n, _) in table.items()}
    tag = tags[name]
    return bytes([tag]) + encode(table[tag][1], msg)


def decode_tagged(table: dict, data: bytes) -> tuple[str, dict[str, Any]]:
    if not data:
        raise ValueError("empty MDG message")
    tag = data[0]
    if tag not in table:
        raise ValueError(f"unknown MDG type-code {tag}")
    name, schema = table[tag]
    return name, decode(schema, data[1:])
