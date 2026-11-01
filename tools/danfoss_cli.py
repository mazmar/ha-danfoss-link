"""Command line test client for the Danfoss Link MDG implementation (no HA needed).

    python3 tools/danfoss_cli.py probe            # TLS + handshake to the cloud only
    python3 tools/danfoss_cli.py pair <CODE>      # pair, stores identity in danfoss_identity.json
    python3 tools/danfoss_cli.py status           # system info, rooms, modes
    python3 tools/danfoss_cli.py raw GET /api/rooms
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "custom_components" / "danfoss_link"))

from mdg.cc_json import CcJsonClient  # noqa: E402
from mdg.connection import ControlConnection  # noqa: E402
from mdg.identity import Identity  # noqa: E402
from mdg.pace import pair  # noqa: E402

STORE = pathlib.Path("danfoss_identity.json")
NAME = "Home Assistant (CLI)"


def load() -> tuple[Identity, str]:
    data = json.loads(STORE.read_text())
    return Identity(data["private_key"], data["certificate"]), data["peer_id"]


async def main(argv: list[str]) -> None:
    cmd = argv[0] if argv else "probe"
    if cmd == "probe":
        ident = Identity.generate()
        ctrl = ControlConnection(ident, NAME)
        await ctrl.connect([])
        print("connected, our peer id:", ident.peer_id)
        print("ping reply:", await ctrl.request("ping", {}))
        await ctrl.close()
    elif cmd == "pair":
        ident = Identity.generate()
        ctrl = ControlConnection(ident, NAME)
        await ctrl.connect([])
        try:
            result = await pair(ctrl, ident, argv[1], NAME)
            await ctrl.request("set_peer_whitelist", {"peer_name": [result.peer_id]})
        finally:
            await ctrl.close()
        STORE.write_text(json.dumps({
            "peer_id": result.peer_id,
            "private_key": ident.private_key_pem,
            "certificate": ident.certificate_pem,
        }))
        print(f"paired with {result.name} ({result.peer_id}), saved to {STORE}")
    elif cmd in ("status", "raw"):
        ident, peer_id = load()
        client = CcJsonClient(ident, peer_id, NAME)
        try:
            if cmd == "raw":
                body = json.loads(argv[3]) if len(argv) > 3 else None
                print(json.dumps(await client.request(argv[1], argv[2], body), indent=2))
            else:
                for uri in ("/api/system/apiversions", "/api/system/info", "/api/system/status",
                            "/api/modes", "/api/rooms"):
                    status, content = await client.request("GET", uri)
                    print(f"== {uri} [{status}]\n{json.dumps(content, indent=2)}")
        finally:
            await client.close()
    else:
        print(__doc__)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG if "-v" in sys.argv else logging.INFO)
    asyncio.run(main([a for a in sys.argv[1:] if a != "-v"]))
