"""Pure Python elliptic curve arithmetic for brainpoolP320r1.

MDG uses brainpoolP320r1 for every key (cloud CA, CC, app). The
``cryptography`` package does not support this curve, so the little EC math
we need (key generation, ECDSA, ECDH) is implemented here. Performance is not
an issue: a handful of scalar multiplications per connection.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import secrets


@dataclass(frozen=True)
class Curve:
    """Short Weierstrass curve y^2 = x^3 + ax + b over GF(p)."""

    name: str
    p: int
    a: int
    b: int
    gx: int
    gy: int
    n: int
    h: int = 1

    @property
    def size(self) -> int:
        """Field element size in bytes."""
        return (self.p.bit_length() + 7) // 8

    @property
    def g(self) -> tuple[int, int]:
        return (self.gx, self.gy)


BRAINPOOL_P320R1 = Curve(
    name="brainpoolP320r1",
    p=0xD35E472036BC4FB7E13C785ED201E065F98FCFA6F6F40DEF4F92B9EC7893EC28FCD412B1F1B32E27,
    a=0x3EE30B568FBAB0F883CCEBD46D3F3BB8A2A73513F5EB79DA66190EB085FFA9F492F375A97D860EB4,
    b=0x520883949DFDBC42D3AD198640688A6FE13F41349554B49ACC31DCCD884539816F5EB4AC8FB1F1A6,
    gx=0x43BD7E9AFB53D8B85289BCC48EE5BFE6F20137D10A087EB6E7871E2A10A599C710AF8D0D39E20611,
    gy=0x14FDD05545EC1CC8AB4093247F77275E0743FFED117182EAA9C77877AAAC6AC7D35245D1692E8EE1,
    n=0xD35E472036BC4FB7E13C785ED201E065F98FCFA5B68F12A32D482EC7EE8658E98691555B44C59311,
)

Point = tuple[int, int] | None  # None = point at infinity


def is_on_curve(curve: Curve, pt: Point) -> bool:
    if pt is None:
        return True
    x, y = pt
    return (y * y - (x * x * x + curve.a * x + curve.b)) % curve.p == 0


def point_add(curve: Curve, p1: Point, p2: Point) -> Point:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    p = curve.p
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % p == 0:
            return None
        lam = (3 * x1 * x1 + curve.a) * pow(2 * y1, -1, p) % p
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, p) % p
    x3 = (lam * lam - x1 - x2) % p
    return (x3, (lam * (x1 - x3) - y1) % p)


def point_neg(curve: Curve, pt: Point) -> Point:
    if pt is None:
        return None
    return (pt[0], (-pt[1]) % curve.p)


def scalar_mult(curve: Curve, k: int, pt: Point) -> Point:
    """Montgomery ladder (constant sequence of operations per bit)."""
    r0: Point = None
    r1: Point = pt
    for i in reversed(range(max(k.bit_length(), curve.n.bit_length()))):
        if (k >> i) & 1:
            r0, r1 = point_add(curve, r0, r1), point_add(curve, r1, r1)
        else:
            r0, r1 = point_add(curve, r0, r0), point_add(curve, r0, r1)
    return r0


def encode_point(curve: Curve, pt: Point) -> bytes:
    """Uncompressed SEC1 encoding 04||X||Y."""
    if pt is None:
        return b"\x00"
    size = curve.size
    return b"\x04" + pt[0].to_bytes(size, "big") + pt[1].to_bytes(size, "big")


def decode_point(curve: Curve, data: bytes) -> Point:
    size = curve.size
    if len(data) != 1 + 2 * size or data[0] != 4:
        raise ValueError("only uncompressed points are supported")
    pt = (int.from_bytes(data[1 : 1 + size], "big"), int.from_bytes(data[1 + size :], "big"))
    if not is_on_curve(curve, pt):
        raise ValueError("point is not on the curve")
    return pt


@dataclass(frozen=True)
class PrivateKey:
    curve: Curve
    d: int

    @classmethod
    def generate(cls, curve: Curve = BRAINPOOL_P320R1) -> PrivateKey:
        return cls(curve, secrets.randbelow(curve.n - 1) + 1)

    @property
    def public_point(self) -> tuple[int, int]:
        pt = scalar_mult(self.curve, self.d, self.curve.g)
        assert pt is not None
        return pt

    def public_bytes(self) -> bytes:
        return encode_point(self.curve, self.public_point)

    def ecdh(self, peer: Point) -> bytes:
        """Raw ECDH shared secret (x coordinate, fixed length)."""
        shared = scalar_mult(self.curve, self.d, peer)
        if shared is None:
            raise ValueError("ECDH resulted in the point at infinity")
        return shared[0].to_bytes(self.curve.size, "big")

    def sign_digest(self, digest: bytes, hashfunc=hashlib.sha256) -> tuple[int, int]:
        """ECDSA with deterministic nonces (RFC 6979)."""
        n = self.curve.n
        e = _bits2int(digest, n)
        for k in _rfc6979_nonces(self.d, digest, n, hashfunc):
            pt = scalar_mult(self.curve, k, self.curve.g)
            assert pt is not None
            r = pt[0] % n
            if r == 0:
                continue
            s = pow(k, -1, n) * (e + r * self.d) % n
            if s != 0:
                return r, s
        raise AssertionError("unreachable")


def ecdsa_verify(curve: Curve, pub: Point, digest: bytes, r: int, s: int) -> bool:
    n = curve.n
    if pub is None or not (0 < r < n and 0 < s < n):
        return False
    w = pow(s, -1, n)
    e = _bits2int(digest, n)
    pt = point_add(
        curve,
        scalar_mult(curve, e * w % n, curve.g),
        scalar_mult(curve, r * w % n, pub),
    )
    return pt is not None and pt[0] % n == r


def _bits2int(data: bytes, n: int) -> int:
    value = int.from_bytes(data, "big")
    excess = len(data) * 8 - n.bit_length()
    return value >> excess if excess > 0 else value


def _rfc6979_nonces(d: int, digest: bytes, n: int, hashfunc):
    qlen = (n.bit_length() + 7) // 8
    x = d.to_bytes(qlen, "big")
    h1 = (_bits2int(digest, n) % n).to_bytes(qlen, "big")
    hlen = hashfunc().digest_size
    v = b"\x01" * hlen
    k = b"\x00" * hlen
    k = hmac.new(k, v + b"\x00" + x + h1, hashfunc).digest()
    v = hmac.new(k, v, hashfunc).digest()
    k = hmac.new(k, v + b"\x01" + x + h1, hashfunc).digest()
    v = hmac.new(k, v, hashfunc).digest()
    while True:
        t = b""
        while len(t) < qlen:
            v = hmac.new(k, v, hashfunc).digest()
            t += v
        cand = _bits2int(t, n)
        if 1 <= cand < n:
            yield cand
        k = hmac.new(k, v + b"\x00", hashfunc).digest()
        v = hmac.new(k, v, hashfunc).digest()
