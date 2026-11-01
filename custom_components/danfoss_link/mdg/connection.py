"""MDG cloud transport: control connection, routed peer connections.

Protocol summary (recovered from the Danfoss Link 1.3.1 Lua bytecode):

* Every message on every MDG socket is ``u16 big-endian length || payload``.
* Control: TCP+TLS1.2 to the Trifork cloud on port 443. Payload is
  ``type-code byte || protobuf``. Client sends handshake (magic 0xF09D8CA8)
  and set_peer_whitelist, then place_call / place_pairing_call.
* A call reply contains a router endpoint + token. The client opens a *plain*
  TCP connection to the router, sends a routing handshake (magic 0xF09D8C95,
  supported protocol "tlsv1_2") and waits for GoAhead. Then TLS 1.2 is run
  end-to-end with the CC over that stream; inside it framing is again
  ``u16 length || payload``.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import itertools
import logging
import random
import struct
import time
from typing import Any

from . import pb
from .errors import MdgConnectionError, MdgPeerUnavailable, MdgSecurityError
from .identity import Identity, pin_peer, verify_cloud_chain
from .tls12 import TlsStream
from .x509 import Certificate

_LOGGER = logging.getLogger(__name__)

CONTROL_HOST = "mdg-danfoss.hosted.trifork.com"
CONTROL_HOSTS_FALLBACK = ("77.66.11.90", "77.66.11.92", "5.179.92.180", "5.179.92.182")
CONTROL_PORT = 443
CONTROL_MAGIC = 0xF09D8CA8
ROUTING_MAGIC = 0xF09D8C95
MDG_GIT_SHA = "979b49e2a1ce8762bef4b79f1df9f74c4b2095d3"

CONNECT_TIMEOUT = 15
REQUEST_TIMEOUT = 10
ROUTING_TIMEOUT = 10
PING_INTERVAL = 108  # 0.9 * control_ping_interval (120 s)


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    header = await reader.readexactly(2)
    return await reader.readexactly(struct.unpack(">H", header)[0])


def write_frame(writer: asyncio.StreamWriter, payload: bytes) -> None:
    if len(payload) > 0xFFFF:
        raise ValueError("MDG frame too large")
    writer.write(struct.pack(">H", len(payload)) + payload)


@dataclass
class RoutingInfo:
    peer_name: str
    host: str
    port: int
    token: bytes

    @classmethod
    def from_pb(cls, msg: dict[str, Any]) -> RoutingInfo:
        if not msg.get("router"):
            raise MdgConnectionError("call reply contains no router")
        router = msg["router"][0]
        return cls(
            peer_name=msg.get("peer_name", ""),
            host=router["host"].decode(),
            port=int(router["port"]),
            token=msg["token"],
        )


class ControlConnection:
    """TLS connection to the MDG cloud used to place calls."""

    def __init__(self, identity: Identity, name: str) -> None:
        self._identity = identity
        self._name = name
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._req_ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._last_send = 0.0
        self._connect_count = 0
        self.closed = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self.closed.is_set()

    async def connect(self, whitelist: list[str]) -> None:
        hosts = [CONTROL_HOST, *random.sample(CONTROL_HOSTS_FALLBACK, len(CONTROL_HOSTS_FALLBACK))]
        last_err: Exception | None = None
        for host in hosts:
            try:
                tls = await asyncio.wait_for(self._open_tls(host), CONNECT_TIMEOUT)
            except MdgSecurityError:
                raise
            except (OSError, asyncio.TimeoutError, MdgConnectionError) as err:
                _LOGGER.debug("MDG control %s failed: %r", host, err)
                last_err = err
            else:
                self._reader = self._writer = tls
                break
        else:
            raise MdgConnectionError(f"cannot reach MDG cloud: {last_err!r}")

        self._connect_count += 1
        self.closed.clear()
        now = time.time()
        props = {
            "name": self._name,
            "mdg.connect.count": str(self._connect_count),
            "mdg.git.sha": MDG_GIT_SHA,
            "mdg.process.start.time": str(int(now)),
            "mdg.process.time.now": time.strftime("%d-%m-%Y_%X", time.localtime(now)),
        }
        self._send(
            "handshake",
            {
                "magic": CONTROL_MAGIC,
                "version_major": 1,
                "version_minor": 0,
                "property": [{"key": k, "value": v} for k, v in props.items()],
            },
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._ping_task = asyncio.create_task(self._ping_loop())
        await self.request("set_peer_whitelist", {"peer_name": whitelist})

    async def _open_tls(self, host: str) -> TlsStream:
        reader, writer = await asyncio.open_connection(host, CONTROL_PORT)
        tls = TlsStream(
            reader,
            writer,
            self._identity.private_key_pem,
            self._identity.certificate_pem,
            verify_cloud_chain,
        )
        try:
            await tls.handshake()
        except BaseException:
            writer.close()
            raise
        return tls

    def _send(self, name: str, msg: dict[str, Any]) -> None:
        if self._writer is None or self.closed.is_set():
            raise MdgConnectionError("control connection is closed")
        write_frame(self._writer, pb.encode_tagged(pb.CONTROL, name, msg))
        self._last_send = time.monotonic()

    async def request(self, name: str, msg: dict[str, Any], timeout: float = REQUEST_TIMEOUT):
        req_id = next(self._req_ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            self._send(name, {"req_id": req_id, **msg})
            await self._writer.drain()
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as err:
            raise MdgConnectionError(f"no reply to {name}") from err
        finally:
            self._pending.pop(req_id, None)

    async def _read_loop(self) -> None:
        try:
            while True:
                name, msg = pb.decode_tagged(pb.CONTROL, await read_frame(self._reader))
                _LOGGER.debug("MDG control <- %s %s", name, msg)
                if name.endswith("_reply") and "req_id" in msg:
                    fut = self._pending.get(msg["req_id"])
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                elif name == "incoming_call":
                    # We never accept calls.
                    self._send("incoming_call_reply", {"req_id": msg["req_id"], "will_accept_call": False})
                elif name == "bye":
                    _LOGGER.info("MDG cloud said bye: %s", msg)
                    break
                elif name == "reconnect":
                    _LOGGER.debug("MDG cloud asks to reconnect: %s", msg)
                    break
                # server handshake is ignored like in the app
        except (asyncio.IncompleteReadError, OSError, ValueError, MdgConnectionError) as err:
            _LOGGER.debug("MDG control connection lost: %s", err)
        finally:
            self._teardown()

    async def _ping_loop(self) -> None:
        while not self.closed.is_set():
            await asyncio.sleep(max(1.0, PING_INTERVAL - (time.monotonic() - self._last_send)))
            if time.monotonic() - self._last_send >= PING_INTERVAL:
                try:
                    await self.request("ping", {}, timeout=5)
                except MdgConnectionError:
                    _LOGGER.debug("MDG ping timed out")
                    self._teardown()

    def _teardown(self) -> None:
        self.closed.set()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MdgConnectionError("control connection closed"))
        if self._writer is not None:
            self._writer.close()

    async def close(self) -> None:
        for task in (self._ping_task, self._reader_task):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._teardown()

    async def place_call(self, peer_id: str, protocol: str = "cc_json") -> RoutingInfo:
        reply = await self.request("place_call", {"peer_name": peer_id, "protocol": protocol})
        result = reply.get("result", 2)
        if result == 1:
            raise MdgPeerUnavailable("Danfoss Link CC is not connected to the cloud")
        if result != 0:
            raise MdgConnectionError(
                f"place_call failed: {pb.PLACE_CALL_RESULTS.get(result, result)} "
                f"{reply.get('result_text', '')}"
            )
        return RoutingInfo.from_pb(reply["routing"])

    async def place_pairing_call(self, pair_id: str) -> RoutingInfo:
        reply = await self.request("place_pairing_call", {"pair_id": pair_id})
        result = reply.get("result", 2)
        if result != 0:
            raise MdgPeerUnavailable(
                f"pairing call failed: {pb.PLACE_CALL_RESULTS.get(result, result)} "
                f"{reply.get('result_text', '')}"
            )
        return RoutingInfo.from_pb(reply["routing"])


class PeerConnection:
    """End-to-end TLS stream to the CC through an MDG router."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer_cert: Certificate
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.peer_cert = peer_cert

    async def send(self, payload: bytes) -> None:
        write_frame(self.writer, payload)
        await self.writer.drain()

    async def recv(self) -> bytes:
        return await read_frame(self.reader)

    async def close(self) -> None:
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()


async def open_routed_connection(
    identity: Identity, routing: RoutingInfo, expected_peer_id: str | None
) -> PeerConnection:
    """Connect to the router, wait for GoAhead and run TLS (client) to the peer."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(routing.host, routing.port), CONNECT_TIMEOUT
        )
    except (OSError, asyncio.TimeoutError) as err:
        raise MdgConnectionError(f"cannot reach MDG router {routing.host}:{routing.port}") from err

    try:
        write_frame(
            writer,
            pb.encode_tagged(
                pb.ROUTING,
                "handshake",
                {
                    "magic": ROUTING_MAGIC,
                    "version_major": 1,
                    "version_minor": 0,
                    "token": routing.token,
                    "supported_protocols": ["tlsv1_2"],
                },
            ),
        )
        await writer.drain()
        while True:
            name, msg = pb.decode_tagged(
                pb.ROUTING, await asyncio.wait_for(read_frame(reader), ROUTING_TIMEOUT)
            )
            if name == "goahead":
                break
            if name == "pleasehold":
                continue
            if name == "routingerror":
                raise MdgConnectionError(
                    f"router error {pb.ROUTING_ERRORS.get(msg.get('error'), msg.get('error'))}"
                )
            raise MdgConnectionError(f"unexpected routing message {name}")

        # The app does not check the CC's identity; we pin the key from pairing.
        tls = TlsStream(
            reader,
            writer,
            identity.private_key_pem,
            identity.certificate_pem,
            pin_peer(expected_peer_id),
        )
        await asyncio.wait_for(tls.handshake(), CONNECT_TIMEOUT)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError, ValueError) as err:
        writer.close()
        raise MdgConnectionError(f"routed connection failed: {err!r}") from err
    except BaseException:
        writer.close()
        raise
    return PeerConnection(tls, tls, tls.peer_certificates[0])
