"""Offline tests of the MDG implementation (no Home Assistant needed).

Run: python3 -m pytest tests  (from the repository root)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import pathlib
import sys

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "custom_components" / "danfoss_link"))

from mdg import ec, pb, x509  # noqa: E402
from mdg.identity import MDG_CA, Identity  # noqa: E402
from mdg.pace import CURVE, _pi, _run_pace, split_code  # noqa: E402


def test_mdg_ca_is_self_signed():
    assert MDG_CA.curve_ok
    assert MDG_CA.verify_signed_by(MDG_CA)


def test_identity_certificate():
    ident = Identity.generate()
    cert = ident.certificate
    assert cert.verify_signed_by(cert)
    assert cert.ski == x509.subject_key_identifier(cert.public_key)
    assert len(ident.peer_id) == 59 and ident.peer_id == ident.peer_id.lower()


def test_ecdsa_roundtrip():
    key = ec.PrivateKey.generate()
    digest = hashlib.sha256(b"hello").digest()
    r, s = key.sign_digest(digest)
    assert ec.ecdsa_verify(ec.BRAINPOOL_P320R1, key.public_point, digest, r, s)
    assert not ec.ecdsa_verify(ec.BRAINPOOL_P320R1, key.public_point, b"\0" * 32, r, s)


def test_protobuf_roundtrip():
    msg = {
        "req_id": 7,
        "result": 0,
        "routing": {
            "peer_name": "aa:bb",
            "router": [{"host": b"1.2.3.4", "port": 443}],
            "token": b"\x01\x02",
        },
    }
    data = pb.encode_tagged(pb.CONTROL, "place_call_reply", msg)
    assert data[0] == 11
    name, decoded = pb.decode_tagged(pb.CONTROL, data)
    assert name == "place_call_reply"
    assert decoded["routing"]["router"][0]["port"] == 443
    assert decoded["routing"]["token"] == b"\x01\x02"


def test_handshake_magic_encoding():
    data = pb.encode_tagged(
        pb.ROUTING, "handshake",
        {"magic": 0xF09D8C95, "version_major": 1, "version_minor": 0, "token": b"t",
         "supported_protocols": ["tlsv1_2"]},
    )
    # type 0, field 1 fixed32 little endian
    assert data[:6] == b"\x00\x0d" + (0xF09D8C95).to_bytes(4, "little")


def test_split_code():
    assert split_code("123 456 789") == ("1234", "56789")
    with pytest.raises(Exception):
        split_code("12345")


class _FakeConn:
    """In-memory stand-in for PeerConnection."""

    def __init__(self, inbox: asyncio.Queue, outbox: asyncio.Queue, peer_cert):
        self._in, self._out, self.peer_cert = inbox, outbox, peer_cert

    async def send(self, payload: bytes) -> None:
        await self._out.put(payload)

    async def recv(self) -> bytes:
        return await self._in.get()


async def _fake_cc_server(conn: _FakeConn, own: Identity, client_cert, password: str):
    """Server side of PACE as implemented in pace.luac main.18 (+ common part)."""
    name, _ = pb.decode_tagged(pb.PAIRING, await conn.recv())
    assert name == "pace_client_hello"
    s = ec.PrivateKey.generate().d
    pi = _pi(password, own.certificate.public_key_pem(), client_cert.public_key_pem())
    salt = b"1234"
    key = hashlib.pbkdf2_hmac("sha256", pi, salt, 1024, 32)[:16].ljust(32, b"\0")
    plain = s.to_bytes(40, "big") + os.urandom(24)
    enc = Cipher(algorithms.AES(key), modes.CBC(b"\0" * 16)).encryptor()
    z = enc.update(plain) + enc.finalize()
    await conn.send(pb.encode_tagged(pb.PAIRING, "pace_server_hello", {
        "version_major": 1, "version_minor": 0, "salt": salt, "iv": os.urandom(8), "z": z}))
    r = ec.PrivateKey.generate().d
    await conn.send(pb.encode_tagged(pb.PAIRING, "pace_xy",
                                     {"xy": ec.encode_point(CURVE, ec.scalar_mult(CURVE, r, CURVE.g))}))
    _, m = pb.decode_tagged(pb.PAIRING, await conn.recv())
    h = ec.scalar_mult(CURVE, r, ec.decode_point(CURVE, m["xy"]))
    gm = ec.point_add(CURVE, ec.scalar_mult(CURVE, s, CURVE.g), h)
    x = ec.PrivateKey.generate().d
    own_pk = ec.encode_point(CURVE, ec.scalar_mult(CURVE, x, gm))
    await conn.send(pb.encode_tagged(pb.PAIRING, "pace_pk", {"pk": own_pk}))
    _, m = pb.decode_tagged(pb.PAIRING, await conn.recv())
    peer_pk = m["pk"]
    shared = ec.scalar_mult(CURVE, x, ec.decode_point(CURVE, peer_pk))
    kmac = hashlib.sha256(ec.encode_point(CURVE, shared)).digest()
    await conn.send(pb.encode_tagged(pb.PAIRING, "pace_proof", {
        "token": hmac.new(kmac, peer_pk, hashlib.sha256).hexdigest().encode()}))
    _, m = pb.decode_tagged(pb.PAIRING, await conn.recv())
    return m["token"] == hmac.new(kmac, own_pk, hashlib.sha256).hexdigest().encode()


@pytest.mark.parametrize(("client_pw", "server_pw", "ok"), [("12345", "12345", True), ("12345", "12346", False)])
def test_pace_against_fake_cc(client_pw, server_pw, ok):
    async def run():
        client_id, server_id = Identity.generate(), Identity.generate()
        a, b = asyncio.Queue(), asyncio.Queue()
        client = _FakeConn(a, b, server_id.certificate)
        server = _FakeConn(b, a, client_id.certificate)
        return await asyncio.gather(
            _run_pace(client, client_id, client_pw),
            _fake_cc_server(server, server_id, client_id.certificate, server_pw),
        )

    client_ok, server_ok = asyncio.run(run())
    assert client_ok is ok and server_ok is ok


def test_schedule_helpers():
    import importlib.util
    import types

    # load api.py as part of the package without running __init__ (needs HA)
    pkg_dir = pathlib.Path(__file__).parents[1] / "custom_components" / "danfoss_link"
    pkg = types.ModuleType("danfoss_link")
    pkg.__path__ = [str(pkg_dir)]
    sys.modules.setdefault("danfoss_link", pkg)
    spec = importlib.util.spec_from_file_location("danfoss_link.api", pkg_dir / "api.py")
    api = importlib.util.module_from_spec(spec)
    sys.modules["danfoss_link.api"] = api
    spec.loader.exec_module(api)
    Period, build_day, format_timespan = api.Period, api.build_day, api.format_timespan
    parse_periods, parse_timespan = api.parse_periods, api.parse_timespan

    assert parse_timespan("06:30:00") == 390
    assert parse_timespan("1.00:00:00") == 1440
    assert format_timespan(1440) == "1.00:00:00" and format_timespan(390) == "06:30:00"
    day = build_day([Period(360, 480, "AtHome"), Period(960, 1350, "AtHome")], "Away")
    assert [(d["startTimeFromMidnight"], d["state"]) for d in day] == [
        ("00:00:00", "Away"), ("06:00:00", "AtHome"), ("08:00:00", "Away"),
        ("16:00:00", "AtHome"), ("22:30:00", "Away"),
    ]
    assert day[-1]["endTimeFromMidnight"] == "1.00:00:00"
    assert [str(p) for p in parse_periods(day) if p.state == "AtHome"] == ["06:00-08:00", "16:00-22:30"]
