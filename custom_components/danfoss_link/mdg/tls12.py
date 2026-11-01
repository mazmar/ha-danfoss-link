"""Minimal TLS 1.2 client for MDG (ECDHE-ECDSA-AES256-GCM-SHA384 only).

Why not the ``ssl`` module: MDG servers and CCs authenticate with ECDSA keys
on brainpoolP320r1. That curve has no TLS group id, and OpenSSL >= 1.1 aborts
TLS 1.2 handshakes with ``WRONG_CURVE`` when the server's signing key is on a
curve that is not in the client's supported_groups list. The original app uses
OpenSSL 1.0.2 which did not enforce this. So we speak TLS ourselves: ECDHE on
x25519/P-256/P-384 via ``cryptography``, ECDSA on brainpoolP320r1 via
:mod:`.ec`, AES-256-GCM records and the SHA-384 PRF.

The peer certificate is handed to ``verify_peer`` during the handshake, before
any application data is exchanged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import hashlib
import hmac
import os
import struct

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec as cec, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .ec import BRAINPOOL_P320R1, PrivateKey, ecdsa_verify
from .errors import MdgConnectionError, MdgSecurityError
from .x509 import Certificate, parse_certificate, pem_to_der

TLS12 = b"\x03\x03"
CT_CCS, CT_ALERT, CT_HANDSHAKE, CT_APPDATA = 20, 21, 22, 23
HT_CLIENT_HELLO, HT_SERVER_HELLO, HT_CERTIFICATE = 1, 2, 11
HT_SERVER_KEY_EXCHANGE, HT_CERTIFICATE_REQUEST, HT_SERVER_HELLO_DONE = 12, 13, 14
HT_CERTIFICATE_VERIFY, HT_CLIENT_KEY_EXCHANGE, HT_FINISHED = 15, 16, 20

SUITE_ECDHE_ECDSA_AES256_GCM_SHA384 = 0xC02C

# (TLS group id, cryptography factory)
GROUPS = {
    29: "x25519",
    23: cec.SECP256R1,
    24: cec.SECP384R1,
}
# TLS 1.2 SignatureAndHashAlgorithm for ECDSA: hash id -> hashlib name
ECDSA_HASHES = {6: "sha512", 5: "sha384", 4: "sha256", 3: "sha224", 2: "sha1"}

MAX_FRAGMENT = 16384


def _p_hash(secret: bytes, seed: bytes, length: int) -> bytes:
    out, a = b"", seed
    while len(out) < length:
        a = hmac.new(secret, a, hashlib.sha384).digest()
        out += hmac.new(secret, a + seed, hashlib.sha384).digest()
    return out[:length]


def prf(secret: bytes, label: bytes, seed: bytes, length: int) -> bytes:
    return _p_hash(secret, label + seed, length)


def _u8(data: bytes) -> bytes:
    return bytes([len(data)]) + data


def _u16(data: bytes) -> bytes:
    return struct.pack(">H", len(data)) + data


def _u24(data: bytes) -> bytes:
    return struct.pack(">I", len(data))[1:] + data


def _ecdsa_der(r: int, s: int) -> bytes:
    from .x509 import der_int, seq

    return seq(der_int(r), der_int(s))


def _parse_ecdsa_der(sig: bytes) -> tuple[int, int]:
    from .x509 import _children, _read

    _, body, _, _ = _read(sig, 0)
    r_t, s_t = _children(body)[:2]
    return int.from_bytes(r_t[1], "big"), int.from_bytes(s_t[1], "big")


class _Buf:
    def __init__(self, data: bytes) -> None:
        self.data, self.pos = data, 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise MdgConnectionError("truncated TLS message")
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return struct.unpack(">H", self.take(2))[0]

    def u24(self) -> int:
        return int.from_bytes(self.take(3), "big")

    def vec8(self) -> bytes:
        return self.take(self.u8())

    def vec16(self) -> bytes:
        return self.take(self.u16())

    def vec24(self) -> bytes:
        return self.take(self.u24())


class TlsStream:
    """TLS 1.2 client over an asyncio stream; offers readexactly/write/drain."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        private_key_pem: str,
        certificate_pem: str,
        verify_peer: Callable[[list[Certificate]], None],
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._key = _load_key(private_key_pem)
        self._cert_der = pem_to_der(certificate_pem)
        self._verify_peer = verify_peer
        self._transcript = bytearray()
        self._hs_buffer = b""
        self._write_key: AESGCM | None = None
        self._read_key: AESGCM | None = None
        self._write_iv = self._read_iv = b""
        self._write_seq = self._read_seq = 0
        self._plain = bytearray()
        self.peer_certificates: list[Certificate] = []
        self.closed = False

    # --- records -----------------------------------------------------------

    async def _read_record(self) -> tuple[int, bytes]:
        try:
            header = await self._reader.readexactly(5)
            ctype, _version, length = header[0], header[1:3], struct.unpack(">H", header[3:])[0]
            body = await self._reader.readexactly(length)
        except asyncio.IncompleteReadError as err:
            self.closed = True
            raise ConnectionResetError("TLS peer closed the connection") from err
        if self._read_key is not None:
            nonce = self._read_iv + body[:8]
            aad = struct.pack(">Q", self._read_seq) + bytes([ctype]) + TLS12
            aad += struct.pack(">H", length - 8 - 16)
            try:
                body = self._read_key.decrypt(nonce, body[8:], aad)
            except Exception as err:  # InvalidTag
                raise MdgSecurityError("TLS record authentication failed") from err
            self._read_seq += 1
        if ctype == CT_ALERT:
            level, desc = body[0], body[1]
            self.closed = True
            if desc == 0:  # close_notify
                raise ConnectionResetError("TLS close_notify")
            raise MdgConnectionError(f"TLS alert from peer: level {level} description {desc}")
        return ctype, body

    def _write_record(self, ctype: int, data: bytes) -> None:
        for i in range(0, max(len(data), 1), MAX_FRAGMENT):
            chunk = data[i : i + MAX_FRAGMENT]
            if self._write_key is not None:
                explicit = struct.pack(">Q", self._write_seq)
                aad = explicit + bytes([ctype]) + TLS12 + struct.pack(">H", len(chunk))
                chunk = explicit + self._write_key.encrypt(self._write_iv + explicit, chunk, aad)
                self._write_seq += 1
            self._writer.write(bytes([ctype]) + TLS12 + _u16(chunk))

    def _send_handshake(self, htype: int, body: bytes) -> None:
        msg = bytes([htype]) + _u24(body)
        self._transcript += msg
        self._write_record(CT_HANDSHAKE, msg)

    async def _next_handshake(self) -> tuple[int, bytes]:
        while len(self._hs_buffer) < 4 or len(self._hs_buffer) < 4 + int.from_bytes(
            self._hs_buffer[1:4], "big"
        ):
            ctype, body = await self._read_record()
            if ctype != CT_HANDSHAKE:
                raise MdgConnectionError(f"unexpected TLS record type {ctype} during handshake")
            self._hs_buffer += body
        length = int.from_bytes(self._hs_buffer[1:4], "big")
        msg, self._hs_buffer = self._hs_buffer[: 4 + length], self._hs_buffer[4 + length :]
        self._transcript += msg
        return msg[0], msg[4:]

    # --- handshake ---------------------------------------------------------

    async def handshake(self) -> None:
        client_random = os.urandom(32)
        groups = b"".join(struct.pack(">H", g) for g in GROUPS)
        sigalgs = b"".join(bytes([h, 3]) for h in ECDSA_HASHES)
        extensions = (
            struct.pack(">H", 10) + _u16(_u16(groups))
            + struct.pack(">H", 11) + _u16(_u8(b"\x00"))
            + struct.pack(">H", 13) + _u16(_u16(sigalgs))
            + struct.pack(">H", 0xFF01) + _u16(b"\x00")
        )
        hello = (
            TLS12 + client_random + _u8(b"")
            + _u16(struct.pack(">H", SUITE_ECDHE_ECDSA_AES256_GCM_SHA384))
            + _u8(b"\x00") + _u16(extensions)
        )
        self._send_handshake(HT_CLIENT_HELLO, hello)
        await self._writer.drain()

        htype, body = await self._next_handshake()
        if htype != HT_SERVER_HELLO:
            raise MdgConnectionError(f"expected ServerHello, got {htype}")
        b = _Buf(body)
        if b.take(2) != TLS12:
            raise MdgConnectionError("server did not choose TLS 1.2")
        server_random = b.take(32)
        b.vec8()
        if b.u16() != SUITE_ECDHE_ECDSA_AES256_GCM_SHA384:
            raise MdgConnectionError("server chose an unsupported cipher suite")

        htype, body = await self._next_handshake()
        if htype != HT_CERTIFICATE:
            raise MdgConnectionError(f"expected Certificate, got {htype}")
        b = _Buf(_Buf(body).vec24())
        certs = []
        while b.pos < len(b.data):
            certs.append(parse_certificate(b.vec24()))
        if not certs:
            raise MdgSecurityError("peer sent no certificate")
        self.peer_certificates = certs
        self._verify_peer(certs)
        if not certs[0].curve_ok:
            raise MdgSecurityError("peer key is not on brainpoolP320r1")

        htype, body = await self._next_handshake()
        if htype != HT_SERVER_KEY_EXCHANGE:
            raise MdgConnectionError(f"expected ServerKeyExchange, got {htype}")
        b = _Buf(body)
        if b.u8() != 3:
            raise MdgConnectionError("server did not use a named curve for ECDHE")
        group = b.u16()
        server_point = b.vec8()
        params = body[: b.pos]
        hash_id, sig_id = b.u8(), b.u8()
        signature = b.vec16()
        if sig_id != 3 or hash_id not in ECDSA_HASHES:
            raise MdgSecurityError("unsupported ServerKeyExchange signature")
        digest = hashlib.new(ECDSA_HASHES[hash_id], client_random + server_random + params).digest()
        if not ecdsa_verify(
            BRAINPOOL_P320R1, certs[0].public_point, digest, *_parse_ecdsa_der(signature)
        ):
            raise MdgSecurityError("ServerKeyExchange signature is invalid")
        own_point, premaster = _ecdhe(group, server_point)

        cert_request: bytes | None = None
        while True:
            htype, body = await self._next_handshake()
            if htype == HT_CERTIFICATE_REQUEST:
                cert_request = body
            elif htype == HT_SERVER_HELLO_DONE:
                break
            else:
                raise MdgConnectionError(f"unexpected handshake message {htype}")

        if cert_request is not None:
            self._send_handshake(HT_CERTIFICATE, _u24(_u24(self._cert_der)))
        self._send_handshake(HT_CLIENT_KEY_EXCHANGE, _u8(own_point))
        if cert_request is not None:
            b = _Buf(cert_request)
            b.vec8()  # certificate types
            offered = b.vec16()
            pairs = {(offered[i], offered[i + 1]) for i in range(0, len(offered), 2)}
            hash_id = next((h for h in (4, 5, 6, 2) if (h, 3) in pairs), 4)
            digest = hashlib.new(ECDSA_HASHES[hash_id], bytes(self._transcript)).digest()
            sig = _ecdsa_der(*self._key.sign_digest(digest, getattr(hashlib, ECDSA_HASHES[hash_id])))
            self._send_handshake(HT_CERTIFICATE_VERIFY, bytes([hash_id, 3]) + _u16(sig))

        master = prf(premaster, b"master secret", client_random + server_random, 48)
        keys = prf(master, b"key expansion", server_random + client_random, 72)
        self._write_record(CT_CCS, b"\x01")
        self._write_key, self._write_iv = AESGCM(keys[0:32]), keys[64:68]
        verify = prf(master, b"client finished", hashlib.sha384(self._transcript).digest(), 12)
        self._send_handshake(HT_FINISHED, verify)
        await self._writer.drain()

        expected = prf(master, b"server finished", hashlib.sha384(self._transcript).digest(), 12)
        ctype, body = await self._read_record()
        if ctype != CT_CCS:
            raise MdgConnectionError(f"expected ChangeCipherSpec, got record {ctype}")
        self._read_key, self._read_iv = AESGCM(keys[32:64]), keys[68:72]
        htype, body = await self._next_handshake()
        if htype != HT_FINISHED or not hmac.compare_digest(body, expected):
            raise MdgSecurityError("TLS Finished verification failed")

    # --- stream API --------------------------------------------------------

    async def readexactly(self, n: int) -> bytes:
        while len(self._plain) < n:
            ctype, body = await self._read_record()
            if ctype == CT_APPDATA:
                self._plain += body
            elif ctype == CT_HANDSHAKE:
                continue  # e.g. HelloRequest; renegotiation is not supported
        out = bytes(self._plain[:n])
        del self._plain[:n]
        return out

    def write(self, data: bytes) -> None:
        self._write_record(CT_APPDATA, data)

    async def drain(self) -> None:
        await self._writer.drain()

    def close(self) -> None:
        if not self.closed and self._write_key is not None:
            try:
                self._write_record(CT_ALERT, b"\x01\x00")
            except Exception:  # noqa: BLE001
                pass
        self.closed = True
        self._writer.close()

    async def wait_closed(self) -> None:
        await self._writer.wait_closed()


def _load_key(pem: str) -> PrivateKey:
    from .x509 import _children, _read

    _, body, _, _ = _read(pem_to_der(pem), 0)
    return PrivateKey(BRAINPOOL_P320R1, int.from_bytes(_children(body)[1][1], "big"))


def _ecdhe(group: int, server_point: bytes) -> tuple[bytes, bytes]:
    """Return (our public point, premaster secret) for the server's ECDHE share."""
    curve = GROUPS.get(group)
    if curve is None:
        raise MdgConnectionError(f"server chose unsupported ECDHE group {group}")
    if curve == "x25519":
        priv = x25519.X25519PrivateKey.generate()
        shared = priv.exchange(x25519.X25519PublicKey.from_public_bytes(server_point))
        own = priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return own, shared
    priv = cec.generate_private_key(curve())
    peer = cec.EllipticCurvePublicKey.from_encoded_point(curve(), server_point)
    shared = priv.exchange(cec.ECDH(), peer)
    own = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return own, shared
