"""Our MDG identity (key + self-signed certificate) and TLS contexts."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable

from .ec import PrivateKey
from .errors import MdgSecurityError
from .x509 import (
    Certificate,
    generate_certificate,
    load_private_key,
    parse_certificate,
    pem_to_der,
    private_key_pem,
)

# Trifork "MDG CA" shipped with the app as luafiles/trusted-CAs.pem.
MDG_CA_PEM = """-----BEGIN CERTIFICATE-----
MIIDBTCCApqgAwIBAgIJAKCiuBkN8rDiMAoGCCqGSM49BAMCMEUxCzAJBgNVBAYT
AkRLMQ8wDQYDVQQHDAZBYXJodXMxFDASBgNVBAoMC1RyaWZvcmsgQS9TMQ8wDQYD
VQQDDAZNREcgQ0EwIBcNMTQwOTI2MDg0MDIxWhgPMjEyNDA0MDIwODQwMjFaMEUx
CzAJBgNVBAYTAkRLMQ8wDQYDVQQHDAZBYXJodXMxFDASBgNVBAoMC1RyaWZvcmsg
QS9TMQ8wDQYDVQQDDAZNREcgQ0EwggF1MIIBHQYHKoZIzj0CATCCARACAQEwNAYH
KoZIzj0BAQIpANNeRyA2vE+34Tx4XtIB4GX5j8+m9vQN70+Suex4k+wo/NQSsfGz
LicwVAQoPuMLVo+6sPiDzOvUbT87uKKnNRP163naZhkOsIX/qfSS83WpfYYOtAQo
UgiDlJ39vELTrRmGQGiKb+E/QTSVVLSazDHczYhFOYFvXrSsj7HxpgRRBEO9fpr7
U9i4Uom8xI7lv+byATfRCgh+tueHHioQpZnHEK+NDTniBhEU/dBVRewcyKtAkyR/
dydeB0P/7RFxguqpx3h3qqxqx9NSRdFpLo7hAikA015HIDa8T7fhPHhe0gHgZfmP
z6W2jxKjLUgux+6GWOmGkVVbRMWTEQIBAQNSAAS2uIReR64UUHwRAcz92jY5TV9k
pbwiZ77iPmsK8610vXodt/7voo2PefGEo0gVumrHtMJSHj9sNtjIlGpKrCJYzMRG
8zYX3LUYkifjg3nPp6NTMFEwHQYDVR0OBBYEFLo53LF721iyXzuNAfqlfHaTa/7b
MB8GA1UdIwQYMBaAFLo53LF721iyXzuNAfqlfHaTa/7bMA8GA1UdEwQIMAYBAf8C
AQAwCgYIKoZIzj0EAwIDWQAwVgIpAKG+nzzpNZ8x/Dh9wMB0cylhytSAEM89XhTz
Nw43JPTEsZwmIe1oqhsCKQCuONpI+E3SnY66ePTAxva3e+zxOUbQ5SI9V2n/BdP3
JaebqfPytyR9
-----END CERTIFICATE-----
"""
MDG_CA = parse_certificate(pem_to_der(MDG_CA_PEM))

@dataclass(frozen=True)
class Identity:
    """Key pair + certificate this client presents to the cloud and the CC."""

    private_key_pem: str
    certificate_pem: str

    @classmethod
    def generate(cls) -> Identity:
        key = PrivateKey.generate()
        return cls(private_key_pem(key), generate_certificate(key))

    @property
    def certificate(self) -> Certificate:
        return parse_certificate(pem_to_der(self.certificate_pem))

    @property
    def peer_id(self) -> str:
        return self.certificate.peer_id

    def validate(self) -> None:
        """Raise ValueError unless key and certificate belong together (brainpoolP320r1)."""
        cert = self.certificate
        if not cert.curve_ok:
            raise ValueError("certificate key is not on brainpoolP320r1")
        if load_private_key(self.private_key_pem).public_bytes() != cert.public_key:
            raise ValueError("private key does not belong to the certificate")


def verify_cloud_chain(certs: list[Certificate]) -> None:
    """Check that the server chain ends in the pinned Trifork MDG CA."""
    current = certs[0]
    for _ in range(len(certs) + 1):
        if current.verify_signed_by(MDG_CA):
            return
        issuer = next(
            (c for c in certs[1:] if c.subject == current.issuer and current.verify_signed_by(c)),
            None,
        )
        if issuer is None:
            break
        current = issuer
    raise MdgSecurityError("cloud server certificate is not signed by the MDG CA")


def pin_peer(expected_peer_id: str | None) -> Callable[[list[Certificate]], None]:
    """Verifier for the CC: its key must match the peer id stored at pairing."""

    def verify(certs: list[Certificate]) -> None:
        if expected_peer_id is not None and certs[0].peer_id != expected_peer_id:
            raise MdgSecurityError(
                f"peer identity mismatch: expected {expected_peer_id}, got {certs[0].peer_id}"
            )

    return verify
