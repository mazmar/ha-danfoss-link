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


class MdgInvalidPairingCode(MdgPairingError):
    """The text is not a pairing code."""


class MdgLanPairingCode(MdgPairingError):
    """Codes starting with 0000 are for LAN pairing, which the app does without the cloud."""


class MdgPairingCodeExpired(MdgPairingError):
    """No CC is waiting for this code (expired or mistyped)."""


class MdgWrongPairingCode(MdgPairingError):
    """A CC answered, but the password part of the code did not match."""


class MdgPairingTimeout(MdgPairingError):
    """The cloud or the CC stopped answering during pairing."""
