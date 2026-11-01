# Danfoss Link for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)

Custom integration for the **Danfoss Link™ CC** – the central controller for
Danfoss Link / Living Connect radiator and floor heating thermostats. It talks
to the CC the same way the official *Danfoss Link* Android app does, through
the Danfoss (Trifork MDG) cloud relay.

> **Unofficial.** This project is not affiliated with, endorsed by or supported
> by Danfoss or Trifork. The protocol was reverse engineered for
> interoperability. Use at your own risk.

## Features

| Platform | Entities |
|---|---|
| `climate` | one per room: current and target temperature, `auto` (follow schedule) / `heat` (manual) |
| `number` | per room: comfort (at home), eco (away) and manual temperature |
| `select` | house mode: schedule / at home / pause / vacation |
| `sensor` | outdoor temperature, system state, unread alarms |
| `switch` | on/off (relay) devices |

Each room is its own device, so it can be assigned to an area.

### Weekly schedule

The room schedule is shown in the `schedule` attribute of the climate entity
(comfort periods per weekday, the rest of the day is eco) and can be changed
with the `danfoss_link.set_schedule` action:

```yaml
action: danfoss_link.set_schedule
target:
  entity_id: climate.bathroom
data:
  days: [monday, tuesday, wednesday, thursday, friday]  # optional, default all days
  periods: ["06:00-07:30", "18:00-22:00"]              # 15 minute steps, [] = eco all day
```

Rooms in the CC's **Living** zone share one schedule (like in the app):
changing one changes all of them.

## Installation

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/mazmar/ha-danfoss-link`, type **Integration**.
2. Search for **Danfoss Link**, **Download**, restart Home Assistant.

### Manual

Copy `custom_components/danfoss_link` to `<config>/custom_components/` and restart Home Assistant.

## Configuration

1. On the Danfoss Link CC start pairing of a new app – the display shows a pairing code.
2. In Home Assistant: **Settings → Devices & services → Add integration → Danfoss Link**.
3. Enter the code (it is valid only for a short time) and a name under which
   Home Assistant will appear in the CC's list of paired apps.

To remove access, delete the Home Assistant entry from the list of paired apps on the CC.

The integration polls the CC every 60 seconds; schedules every 15 minutes.

## How it works

```
HA ──TLS 1.2──▶ Trifork MDG cloud (mdg-danfoss.hosted.trifork.com:443)    control: place_call(CC, "cc_json")
HA ──TCP─────▶ MDG router ── routing handshake ── TLS 1.2 end-to-end with the CC
                 └─ u16-length framed JSON: {"id", "data": {"type": "JsonWebRequest", "data": {method, uri, content}}}
                    → the CC forwards it to its internal REST API (/api/rooms, /api/modes, …)
```

* The app never talks to the CC over the LAN, and neither does this integration.
  An internet connection and the Danfoss cloud are required.
* All MDG keys are on **brainpoolP320r1**. This curve has no TLS group id, and
  OpenSSL ≥ 1.1 refuses such servers in TLS 1.2 (`WRONG_CURVE`). That's why
  the integration ships a small TLS 1.2 client (`mdg/tls12.py`,
  ECDHE-ECDSA-AES256-GCM-SHA384) and pure Python curve arithmetic (`mdg/ec.py`).
* The cloud certificate is verified against the pinned Trifork MDG CA. The CC
  certificate is pinned to the identity learned during pairing.
* Pairing: everything except the last 5 digits of the code selects the
  pairing channel in the cloud. The last 5 digits are the password of a PACE
  key exchange (`mdg/pace.py`), which also binds both TLS keys.

## Security notes

The pairing creates a private key that is stored in Home Assistant's config
entry (`.storage/core.config_entries`). Anyone with that key can control the
heating through the cloud until the pairing is removed on the CC. Treat Home
Assistant backups accordingly.

## Development

```bash
python3 -m pytest tests                       # offline tests (EC, X.509, protobuf, PACE vs simulated CC, schedule)
python3 tools/danfoss_cli.py probe -v         # connect to the cloud only
python3 tools/danfoss_cli.py pair <CODE> -v   # pair; writes danfoss_identity.json (git-ignored, contains a private key!)
python3 tools/danfoss_cli.py status
python3 tools/danfoss_cli.py raw GET /api/rooms/1
```

### Releasing

Releases are built by GitHub Actions (`.github/workflows/release.yml`):

```bash
git tag v0.2.0
git push origin v0.2.0
```

The workflow runs the tests, writes `0.2.0` into `manifest.json`, builds
`danfoss_link.zip` and publishes a GitHub release. HACS offers it as an
update. Tags with a dash (e.g. `v0.3.0-beta.1`) become pre-releases, which
HACS shows only when *Show beta versions* is enabled for the repository.
`validate.yml` runs hassfest, the HACS validation and the tests on every push.

The integration icon is shipped in `custom_components/danfoss_link/brand/`
(supported since Home Assistant 2026.3).

To get debug logs in Home Assistant:

```yaml
logger:
  logs:
    custom_components.danfoss_link: debug
```

Tested with the Danfoss Link CC protocol as used by app version 1.3.1.
