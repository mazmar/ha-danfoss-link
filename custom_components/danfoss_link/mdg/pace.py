"""MDG remote pairing (PACE with generic mapping on brainpoolP320r1).

Recovered from pairing_procedure / pace / ec Lua modules and the native
LuaCrypto 0.3.1 build in libluavm-java.so. Quirks reproduced on purpose:

* the AES IV is always 16 zero bytes (LuaCrypto gets an lbn userdata as IV,
  ``lua_tolstring`` returns NULL), the ``iv`` field is ignored;
* the PBKDF2 output is cut to 16 bytes and zero padded to an AES-256 key;
* the proof token is the *hex* HMAC-SHA256 string.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import pb
from .connection import ControlConnection, PeerConnection, open_routed_connection
from .ec import BRAINPOOL_P320R1, PrivateKey, decode_point, encode_point, point_add, scalar_mult
from .errors import MdgPairingError
from .identity import Identity
from .x509 import peer_id_from_ski

_LOGGER = logging.getLogger(__name__)

PACE_MAGIC = 0xF09D8CA7
PASSWORD_LEN = 5
PAIRING_TIMEOUT = 30
CURVE = BRAINPOOL_P320R1


@dataclass
class Pairing:
    peer_id: str
    name: str
    certificate_der: bytes
    pairing_time: int


def split_code(code: str) -> tuple[str, str]:
    """OTP -> (pair_id sent to the cloud, 5 digit PACE password)."""
    code = "".join(code.split())
    if len(code) <= PASSWORD_LEN:
        raise MdgPairingError("pairing code is too short")
    return code[:-PASSWORD_LEN], code[-PASSWORD_LEN:]


def _pi(password: str, server_pub_pem: bytes, client_pub_pem: bytes) -> bytes:
    out = b""
    for item in (password.encode(), server_pub_pem, client_pub_pem):
        out += len(item).to_bytes(2, "big") + item
    return out + b"\xff"


def _decrypt_nonce(pi: bytes, salt: bytes, z: bytes) -> int:
    key = hashlib.pbkdf2_hmac("sha256", pi, salt, 1024, 32)[:16].ljust(32, b"\0")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(b"\0" * 16)).decryptor()
    plain = decryptor.update(z) + decryptor.finalize()
    return int.from_bytes(plain[: CURVE.size], "big")


def _random_scalar() -> int:
    return PrivateKey.generate(CURVE).d


def _send(conn: PeerConnection, name: str, msg: dict) -> asyncio.Future:
    return conn.send(pb.encode_tagged(pb.PAIRING, name, msg))


async def _recv(conn: PeerConnection, expected: str) -> dict:
    name, msg = pb.decode_tagged(pb.PAIRING, await conn.recv())
    if name != expected:
        raise MdgPairingError(f"expected {expected}, got {name}")
    return msg


def _point(data: bytes):
    try:
        return decode_point(CURVE, data)
    except ValueError as err:
        raise MdgPairingError(f"invalid point from peer: {err}") from err


async def _run_pace(conn: PeerConnection, identity: Identity, password: str) -> bool:
    await _send(
        conn, "pace_client_hello", {"magic": PACE_MAGIC, "version_major": 1, "version_minor": 0}
    )
    hello = await _recv(conn, "pace_server_hello")
    if (hello.get("version_major"), hello.get("version_minor")) != (1, 0):
        raise MdgPairingError(f"unsupported PACE version {hello}")

    pi = _pi(password, conn.peer_cert.public_key_pem(), identity.certificate.public_key_pem())
    s = await asyncio.get_running_loop().run_in_executor(
        None, _decrypt_nonce, pi, hello["salt"], hello["z"]
    )

    # Generic mapping: H = r * P_peer, Gm = s*G + H
    r = _random_scalar()
    await _send(conn, "pace_xy", {"xy": encode_point(CURVE, scalar_mult(CURVE, r, CURVE.g))})
    peer_xy = _point((await _recv(conn, "pace_xy"))["xy"])
    h = scalar_mult(CURVE, r, peer_xy)
    gm = point_add(CURVE, scalar_mult(CURVE, s, CURVE.g), h)

    x = _random_scalar()
    own_pk = encode_point(CURVE, scalar_mult(CURVE, x, gm))
    await _send(conn, "pace_pk", {"pk": own_pk})
    peer_pk_bytes = (await _recv(conn, "pace_pk"))["pk"]
    peer_pk = _point(peer_pk_bytes)
    shared = scalar_mult(CURVE, x, peer_pk)
    if shared is None:
        raise MdgPairingError("PACE resulted in the point at infinity")

    k_mac = hashlib.sha256(encode_point(CURVE, shared)).digest()
    token = hmac.new(k_mac, peer_pk_bytes, hashlib.sha256).hexdigest().encode()
    await _send(conn, "pace_proof", {"token": token})
    peer_token = (await _recv(conn, "pace_proof"))["token"]
    expected = hmac.new(k_mac, own_pk, hashlib.sha256).hexdigest().encode()
    return hmac.compare_digest(peer_token, expected)


async def pair(control: ControlConnection, identity: Identity, code: str, name: str) -> Pairing:
    """Pair with a CC using the code shown on its display."""
    pair_id, password = split_code(code)
    routing = await control.place_pairing_call(pair_id)
    conn = await open_routed_connection(identity, routing, expected_peer_id=None)
    try:
        async with asyncio.timeout(PAIRING_TIMEOUT):
            if not await _run_pace(conn, identity, password):
                raise MdgPairingError("invalid pairing code")
            await _send(conn, "contact_info", {"name": name})
            contact = await _recv(conn, "contact_info")
    except (asyncio.IncompleteReadError, ConnectionError) as err:
        # The CC closes the socket when the proof does not match.
        raise MdgPairingError("invalid pairing code") from err
    finally:
        await conn.close()

    cert = conn.peer_cert
    peer_id = peer_id_from_ski(cert.ski) if cert.ski else cert.peer_id
    _LOGGER.info("Paired with Danfoss Link CC %s (%s)", contact.get("name"), peer_id)
    return Pairing(
        peer_id=peer_id,
        name=contact.get("name") or "Danfoss Link",
        certificate_der=cert.der,
        pairing_time=int(time.time()),
    )
