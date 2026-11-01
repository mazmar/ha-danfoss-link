"""Minimal DER/X.509 support for MDG certificates on brainpoolP320r1.

* Builds the self-signed client certificate exactly like the app's native
  ``ecgroup:generate_certificate`` (explicit curve parameters, serial 1,
  subject/issuer ``C=DK, O=Trifork, CN=localhost``, SKI = SHA1(public point)).
* Parses certificates far enough to verify ECDSA-SHA256 signatures with the
  pure Python EC code, because OpenSSL 3 refuses to verify certificates with
  explicit EC parameters (X509_V_ERR_EC_KEY_EXPLICIT_PARAMS).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib

from .ec import BRAINPOOL_P320R1, Curve, Point, PrivateKey, decode_point, ecdsa_verify

OID_EC_PUBLIC_KEY = "1.2.840.10045.2.1"
OID_PRIME_FIELD = "1.2.840.10045.1.1"
OID_ECDSA_SHA256 = "1.2.840.10045.4.3.2"
OID_BRAINPOOL_P320R1 = "1.3.36.3.3.2.8.1.1.9"
OID_SKI = "2.5.29.14"
OID_AKI = "2.5.29.35"
OID_BASIC_CONSTRAINTS = "2.5.29.19"
OID_C = "2.5.4.6"
OID_O = "2.5.4.10"
OID_CN = "2.5.4.3"


# --- DER encoding ------------------------------------------------------------


def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _len(len(value)) + value


def seq(*items: bytes) -> bytes:
    return tlv(0x30, b"".join(items))


def der_int(value: int) -> bytes:
    body = value.to_bytes(max(1, (value.bit_length() + 8) // 8), "big")
    return tlv(0x02, body)


def der_oid(oid: str) -> bytes:
    parts = [int(p) for p in oid.split(".")]
    out = bytearray([40 * parts[0] + parts[1]])
    for part in parts[2:]:
        chunk = [part & 0x7F]
        part >>= 7
        while part:
            chunk.append(0x80 | (part & 0x7F))
            part >>= 7
        out += bytes(reversed(chunk))
    return tlv(0x06, bytes(out))


def der_octets(data: bytes) -> bytes:
    return tlv(0x04, data)


def der_bits(data: bytes) -> bytes:
    return tlv(0x03, b"\x00" + data)


def _utctime(value: datetime) -> bytes:
    if value.year >= 2050:
        return tlv(0x18, value.strftime("%Y%m%d%H%M%SZ").encode())
    return tlv(0x17, value.strftime("%y%m%d%H%M%SZ").encode())


def _name(*rdns: tuple[str, str]) -> bytes:
    # MBSTRING_ASC -> PrintableString for these simple values
    return seq(*(tlv(0x31, seq(der_oid(oid), tlv(0x13, value.encode()))) for oid, value in rdns))


def ec_parameters(curve: Curve) -> bytes:
    """SpecifiedECDomain (explicit parameters), as written by OpenSSL with asn1_flag 0."""
    size = curve.size
    return seq(
        der_int(1),
        seq(der_oid(OID_PRIME_FIELD), der_int(curve.p)),
        seq(der_octets(curve.a.to_bytes(size, "big")), der_octets(curve.b.to_bytes(size, "big"))),
        der_octets(b"\x04" + curve.gx.to_bytes(size, "big") + curve.gy.to_bytes(size, "big")),
        der_int(curve.n),
        der_int(curve.h),
    )


def subject_key_identifier(public_point_bytes: bytes) -> bytes:
    """OpenSSL "hash" SKI method: SHA1 over the subjectPublicKey bit string contents."""
    return hashlib.sha1(public_point_bytes).digest()


def peer_id_from_ski(ski: bytes) -> str:
    """MDG peer ids are the SKI in lowercase colon separated hex."""
    return ":".join(f"{b:02x}" for b in ski)


def _ecdsa_sig_der(r: int, s: int) -> bytes:
    return seq(der_int(r), der_int(s))


def _pem(label: str, der: bytes) -> str:
    b64 = base64.encodebytes(der).decode().replace("\n", "")
    lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"


def private_key_pem(key: PrivateKey) -> str:
    """RFC 5915 ECPrivateKey with explicit parameters ("BEGIN EC PRIVATE KEY")."""
    size = key.curve.size
    der = seq(
        der_int(1),
        der_octets(key.d.to_bytes(size, "big")),
        tlv(0xA0, ec_parameters(key.curve)),
        tlv(0xA1, der_bits(key.public_bytes())),
    )
    return _pem("EC PRIVATE KEY", der)


def load_private_key(pem: str) -> PrivateKey:
    """Load an EC key on brainpoolP320r1 from "EC PRIVATE KEY" (RFC 5915) or PKCS#8 PEM."""
    der = pem_to_der(pem)
    _, body, _, _ = _read(der, 0)
    items = _children(body)
    if len(items) >= 3 and items[1][0] == 0x30:  # PKCS#8: version, algorithm, key
        if not _curve_matches(_children(items[1][1])[1]):
            raise ValueError("private key is not on brainpoolP320r1")
        _, body, _, _ = _read(items[2][1], 0)
        items = _children(body)
    if items[0][1] != b"\x01" or items[1][0] != 0x04:
        raise ValueError("not an EC private key")
    d = int.from_bytes(items[1][1], "big")
    if not 0 < d < BRAINPOOL_P320R1.n:
        raise ValueError("invalid private key")
    return PrivateKey(BRAINPOOL_P320R1, d)


def generate_certificate(key: PrivateKey, now: datetime | None = None) -> str:
    """Self-signed certificate equivalent to the app's generate_certificate()."""
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    pub = key.public_bytes()
    ski = subject_key_identifier(pub)
    name = _name((OID_C, "DK"), (OID_O, "Trifork"), (OID_CN, "localhost"))
    sig_alg = seq(der_oid(OID_ECDSA_SHA256))
    spki = seq(seq(der_oid(OID_EC_PUBLIC_KEY), ec_parameters(key.curve)), der_bits(pub))
    extensions = seq(
        seq(der_oid(OID_SKI), der_octets(der_octets(ski))),
        seq(der_oid(OID_AKI), der_octets(seq(tlv(0x80, ski)))),
        seq(der_oid(OID_BASIC_CONSTRAINTS), tlv(0x01, b"\xff"), der_octets(seq())),
    )
    tbs = seq(
        tlv(0xA0, der_int(2)),
        der_int(1),
        sig_alg,
        name,
        seq(_utctime(now), _utctime(now + timedelta(seconds=2_000_000_000))),
        name,
        spki,
        tlv(0xA3, extensions),
    )
    r, s = key.sign_digest(hashlib.sha256(tbs).digest())
    return _pem("CERTIFICATE", seq(tbs, sig_alg, der_bits(_ecdsa_sig_der(r, s))))


# --- DER decoding ------------------------------------------------------------


def _read(data: bytes, pos: int) -> tuple[int, bytes, int, int]:
    """Return (tag, value, start_of_tlv, end_of_tlv)."""
    start = pos
    tag = data[pos]
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        nbytes = length & 0x7F
        length = int.from_bytes(data[pos : pos + nbytes], "big")
        pos += nbytes
    return tag, data[pos : pos + length], start, pos + length


def _children(data: bytes) -> list[tuple[int, bytes, bytes]]:
    """Children of a constructed value as (tag, value, full_tlv)."""
    out = []
    pos = 0
    while pos < len(data):
        tag, value, start, end = _read(data, pos)
        out.append((tag, value, data[start:end]))
        pos = end
    return out


def _decode_oid(value: bytes) -> str:
    parts = [value[0] // 40, value[0] % 40]
    acc = 0
    for byte in value[1:]:
        acc = (acc << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(acc)
            acc = 0
    return ".".join(str(p) for p in parts)


@dataclass
class Certificate:
    der: bytes
    tbs: bytes
    subject: bytes
    issuer: bytes
    spki: bytes  # SubjectPublicKeyInfo DER, verbatim
    public_key: bytes  # uncompressed point
    curve_ok: bool  # key is on brainpoolP320r1 (named or explicit)
    signature: tuple[int, int]
    signature_oid: str
    ski: bytes | None

    @property
    def public_point(self) -> Point:
        return decode_point(BRAINPOOL_P320R1, self.public_key)

    @property
    def peer_id(self) -> str:
        """MDG peer id of the certificate owner (from SHA1 of the key, not the extension)."""
        return peer_id_from_ski(subject_key_identifier(self.public_key))

    def public_key_pem(self) -> bytes:
        """Same bytes as OpenSSL's PEM_write_bio_PUBKEY (used as PACE input)."""
        return _pem("PUBLIC KEY", self.spki).encode()

    def verify_signed_by(self, issuer: Certificate) -> bool:
        if self.signature_oid != OID_ECDSA_SHA256 or not issuer.curve_ok:
            return False
        if self.issuer != issuer.subject:
            return False
        digest = hashlib.sha256(self.tbs).digest()
        return ecdsa_verify(BRAINPOOL_P320R1, issuer.public_point, digest, *self.signature)


def _curve_matches(params: tuple[int, bytes, bytes]) -> bool:
    tag, value, full = params
    if tag == 0x06:
        return _decode_oid(value) == OID_BRAINPOOL_P320R1
    if tag == 0x30:
        return full == ec_parameters(BRAINPOOL_P320R1) or _explicit_matches(value)
    return False


def _explicit_matches(value: bytes) -> bool:
    try:
        _, field, coeffs, base, order, *_ = _children(value)
        p = int.from_bytes(_children(field[1])[1][1], "big")
        a, b = (int.from_bytes(c[1], "big") for c in _children(coeffs[1])[:2])
        c = BRAINPOOL_P320R1
        g = decode_point(c, base[1])
        return (p, a, b, g, int.from_bytes(order[1], "big")) == (c.p, c.a, c.b, c.g, c.n)
    except (ValueError, IndexError):
        return False


def parse_certificate(der: bytes) -> Certificate:
    _, cert, _, _ = _read(der, 0)
    tbs_t, sig_alg_t, sig_t = _children(cert)[:3]
    tbs_items = _children(tbs_t[1])
    if tbs_items[0][0] == 0xA0:  # explicit version
        tbs_items = tbs_items[1:]
    _serial, _alg, issuer, _validity, subject, spki = tbs_items[:6]
    alg, key_bits = _children(spki[1])
    alg_items = _children(alg[1])
    curve_ok = _decode_oid(alg_items[0][1]) == OID_EC_PUBLIC_KEY and _curve_matches(alg_items[1])
    public_key = key_bits[1][1:]
    ski = None
    for tag, value, _full in tbs_items[6:]:
        if tag != 0xA3:
            continue
        for ext in _children(_children(value)[0][1]):
            parts = _children(ext[1])
            if _decode_oid(parts[0][1]) == OID_SKI:
                ski = _children(parts[-1][1])[0][1]
    sig_oid = _decode_oid(_children(sig_alg_t[1])[0][1])
    r_t, s_t = _children(_children(sig_t[1][1:])[0][1])[:2]
    return Certificate(
        der=der,
        tbs=tbs_t[2],
        subject=subject[2],
        issuer=issuer[2],
        spki=spki[2],
        public_key=public_key,
        curve_ok=curve_ok,
        signature=(int.from_bytes(r_t[1], "big"), int.from_bytes(s_t[1], "big")),
        signature_oid=sig_oid,
        ski=ski,
    )


def pem_to_der(pem: str) -> bytes:
    body = "".join(
        line for line in pem.strip().splitlines() if line and not line.startswith("-----")
    )
    return base64.b64decode(body)
