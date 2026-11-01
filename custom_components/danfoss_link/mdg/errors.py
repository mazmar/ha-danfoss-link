"""MDG exceptions."""


class MdgError(Exception):
    """Base class for MDG errors."""


class MdgConnectionError(MdgError):
    """Connection to the cloud, router or peer failed."""


class MdgSecurityError(MdgError):
    """A certificate did not match what we expect."""


class MdgPeerUnavailable(MdgError):
    """The cloud says the CC is not connected."""


class MdgPairingError(MdgError):
    """Pairing failed (usually a wrong or expired code)."""
