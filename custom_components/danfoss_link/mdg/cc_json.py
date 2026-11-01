"""cc_json: JsonWebRequest/JsonWebResponse over an MDG peer connection.

Each frame (u16 BE length) carries one JSON object::

    -> {"id": N, "data": {"type": "JsonWebRequest",
                          "data": {"method", "uri", "headers": [...], "content"?}}}
    <- {"id": N, "cc_time": ms, "data": {"type": "JsonWebResponse",
                          "data": {"status", "statusDescription", "headers", "content"}}}

The CC proxies the request to its internal HTTP server; responses may arrive
out of order. The router drops the stream after 60 s of silence and the CC
handler after 200 s, so we send a cheap request when idle.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import time
from typing import Any

from .connection import ControlConnection, PeerConnection, open_routed_connection
from .errors import MdgConnectionError, MdgError
from .identity import Identity

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30
KEEPALIVE_INTERVAL = 40
HEADERS = ["Accept: application/json; charset=utf-8"]


class CcJsonClient:
    """Keeps a cc_json session to one CC alive and multiplexes requests."""

    def __init__(self, identity: Identity, peer_id: str, name: str) -> None:
        self._identity = identity
        self._peer_id = peer_id
        self._name = name
        self._control: ControlConnection | None = None
        self._peer: PeerConnection | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._connect_lock = asyncio.Lock()
        self._last_activity = 0.0

    async def _ensure_connected(self) -> PeerConnection:
        async with self._connect_lock:
            if self._peer is not None:
                return self._peer
            if self._control is None or not self._control.connected:
                if self._control is not None:
                    await self._control.close()
                self._control = ControlConnection(self._identity, self._name)
                await self._control.connect([self._peer_id])
            routing = await self._control.place_call(self._peer_id, "cc_json")
            self._peer = await open_routed_connection(self._identity, routing, self._peer_id)
            self._last_activity = time.monotonic()
            self._reader_task = asyncio.create_task(self._read_loop(self._peer))
            if self._keepalive_task is None or self._keepalive_task.done():
                self._keepalive_task = asyncio.create_task(self._keepalive())
            _LOGGER.debug("cc_json session to %s established", self._peer_id)
            return self._peer

    async def _read_loop(self, peer: PeerConnection) -> None:
        try:
            while True:
                raw = await peer.recv()
                self._last_activity = time.monotonic()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    # The CC pastes the raw HTTP body into the envelope; an
                    # empty body yields invalid JSON ("content":}).
                    msg = json.loads(raw.replace(b'"content":}', b'"content":null}'))
                fut = self._pending.get(msg.get("id"))
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except (asyncio.IncompleteReadError, OSError, ValueError, MdgError) as err:
            _LOGGER.debug("cc_json session closed: %r", err)
        finally:
            await self._drop(peer, MdgConnectionError("connection to the CC was lost"))

    async def _drop(self, peer: PeerConnection, err: Exception) -> None:
        if self._peer is peer:
            self._peer = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)
        await peer.close()

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(5)
            if self._peer is None:
                continue
            if time.monotonic() - self._last_activity >= KEEPALIVE_INTERVAL:
                with contextlib.suppress(MdgError, asyncio.TimeoutError):
                    await self.request("GET", "/api/system/apiversions")

    async def request(
        self, method: str, uri: str, content: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        """Implements api.Transport."""
        peer = await self._ensure_connected()
        req_id = next(self._ids)
        inner: dict[str, Any] = {"method": method, "uri": uri, "headers": HEADERS}
        if content is not None:
            inner["content"] = content
        envelope = {"id": req_id, "data": {"type": "JsonWebRequest", "data": inner}}
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            _LOGGER.debug("cc_json -> %s %s %s", method, uri, content)
            await peer.send(json.dumps(envelope, separators=(",", ":")).encode())
            self._last_activity = time.monotonic()
            reply = await asyncio.wait_for(fut, REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            # Like the app: a stuck request means the stream is dead.
            await self._drop(peer, MdgConnectionError("request timed out"))
            raise
        except OSError as err:
            await self._drop(peer, MdgConnectionError(str(err)))
            raise MdgConnectionError(str(err)) from err
        finally:
            self._pending.pop(req_id, None)
        data = reply.get("data", {}).get("data", {})
        _LOGGER.debug("cc_json <- %s %s: %s", data.get("status"), uri, data.get("content"))
        return int(data.get("status", 0)), data.get("content")

    async def close(self) -> None:
        for task in (self._keepalive_task, self._reader_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self._peer is not None:
            await self._peer.close()
            self._peer = None
        if self._control is not None:
            await self._control.close()
            self._control = None
