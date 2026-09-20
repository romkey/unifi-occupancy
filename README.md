# unifi-occupancy

[![CI](https://img.shields.io/github/actions/workflow/status/romkey/unifi-occupancy/ci.yml?branch=main&label=CI&logo=githubactions&logoColor=white)](https://github.com/romkey/unifi-occupancy/actions/workflows/ci.yml)
[![Lint](https://img.shields.io/badge/lint-ruff%20%7C%20black%20%7C%20mypy-261230?logo=ruff&logoColor=white)](https://github.com/romkey/unifi-occupancy/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![UniFi Protect](https://img.shields.io/badge/UniFi-Protect-0559C9?logo=ubiquiti&logoColor=white)](https://www.ui.com/camera-security)
[![MQTT](https://img.shields.io/badge/MQTT-3.1.1%20%7C%205.0-660066?logo=mqtt&logoColor=white)](https://mqtt.org/)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-autodiscovery-41BDF5?logo=homeassistant&logoColor=white)](https://www.home-assistant.io/integrations/mqtt/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](./Dockerfile)
[![License: MIT](https://img.shields.io/badge/license-MIT-0A7E8C.svg)](./LICENSE)

Publish UniFi Protect detection state to MQTT — **without giving anything access to camera
video**.

The point is occupancy, not surveillance. Home Assistant (or anything else on your broker)
gets to know that *something moved at the front door* or *a smoke alarm is sounding in the
garage*, and gets nothing that could be turned back into a picture. No RTSP URLs, no
snapshot endpoints, no controller credentials leave this process — just booleans.

## What it publishes

For every camera the API returns, four detections:

| Detection | Source | HA device class |
| --- | --- | --- |
| `motion` | Protect motion detection | `motion` |
| `person` | smart detection, `person` | `occupancy` |
| `siren` | smart audio detection, `alrmSiren` | `sound` |
| `smoke` | smart audio detection, `alrmSmoke` | `smoke` |

…and, for each of those, whether the detection is actually **enabled** on that camera. A
camera that supports person detection but has it switched off reports `person_enabled=OFF`,
so you can tell "nobody is there" apart from "nobody is looking".

### Topics

```
unifi-occupancy/status                        online | offline   (last will)
unifi-occupancy/<camera>/motion               ON | OFF
unifi-occupancy/<camera>/motion_enabled       ON | OFF
unifi-occupancy/<camera>/person               ON | OFF
unifi-occupancy/<camera>/person_enabled       ON | OFF
unifi-occupancy/<camera>/siren                ON | OFF
unifi-occupancy/<camera>/siren_enabled        ON | OFF
unifi-occupancy/<camera>/smoke                ON | OFF
unifi-occupancy/<camera>/smoke_enabled        ON | OFF
unifi-occupancy/<camera>/attributes           {"camera_name": "Front Door", ...}
```

`<camera>` is the camera's Protect name, lowercased with non-alphanumerics collapsed to
underscores: *Front Door* → `front_door`.

### Home Assistant

Autodiscovery is on by default. Each camera arrives as one HA **device** carrying eight
binary sensors — four detections, plus four diagnostic "…Detection Enabled" sensors. Nothing
needs to be added to `configuration.yaml`. Set `HA_DISCOVERY_ENABLED=false` if you would
rather wire the topics up by hand.

## How it works

Two sources, combined:

- **Polling** (`GET /cameras`, every `UNIFI_POLL_INTERVAL` seconds) gives the camera
  inventory, which detections are enabled, and Protect's own motion flag.
- **The event websocket** gives smart detections the moment they fire.

Protect is reliable about telling you a detection *started* and much less reliable about
telling you it stopped, so a detection latches ON and decays after
`DETECTION_HOLD_SECONDS` unless another event refreshes it. A detection that is disabled on
the camera never reports ON, whatever arrives on the event stream.

Values are only published when they change, so a quiet house is a quiet broker. Cameras
removed from Protect have their discovery entries retracted.

## Requirements

- A UniFi OS console (UDM, UNVR, Cloud Key) running Protect
- A **UniFi Protect API key**: UniFi OS → *Settings* → *Admins & Users* → your user →
  *Create API Key*. This uses the read-only `/proxy/protect/integration/v1` API — no
  username or password is needed, and the key is never published anywhere.
- An MQTT broker

## Running it

```bash
cp .env.example .env
$EDITOR .env          # at minimum: UNIFI_HOST, UNIFI_API_KEY, MQTT_HOST
docker compose up -d
```

The production container runs as an unprivileged user with a read-only root filesystem and
all capabilities dropped.

### Without Docker

```bash
make install
UNIFI_HOST=... UNIFI_API_KEY=... MQTT_HOST=... make run
```

## Configuration

Everything is environment variables; see [.env.example](./.env.example).

| Variable | Default | Meaning |
| --- | --- | --- |
| `UNIFI_HOST` | *required* | UniFi OS console hostname or IP |
| `UNIFI_API_KEY` | *required* | Protect API key |
| `UNIFI_PORT` | `443` | Console HTTPS port |
| `UNIFI_VERIFY_SSL` | `false` | Verify the console certificate (UniFi self-signs by default) |
| `UNIFI_POLL_INTERVAL` | `5` | Seconds between camera polls |
| `UNIFI_REQUEST_TIMEOUT` | `10` | HTTP timeout, seconds |
| `UNIFI_USE_WEBSOCKET` | `true` | Subscribe to the live event stream |
| `MQTT_HOST` | *required* | Broker hostname or IP |
| `MQTT_PORT` | `1883`, or `8883` with TLS | Broker port |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | unset | Broker credentials |
| `MQTT_CLIENT_ID` | `unifi-occupancy` | Client id |
| `MQTT_TOPIC_PREFIX` | `unifi-occupancy` | Prefix for every topic |
| `MQTT_TLS` | `false` | Connect over TLS |
| `MQTT_KEEPALIVE` | `60` | Keepalive, seconds |
| `MQTT_RETAIN` | `true` | Retain published state |
| `MQTT_QOS` | `0` | QoS for published state |
| `HA_DISCOVERY_ENABLED` | `true` | Publish Home Assistant discovery configs |
| `HA_DISCOVERY_PREFIX` | `homeassistant` | HA discovery topic prefix |
| `HA_NODE_ID` | `unifi_occupancy` | Node id used in discovery topics and unique ids |
| `DETECTION_HOLD_SECONDS` | `30` | How long a detection stays ON after its last event |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

## Development

Four compose files, one per job:

```bash
docker compose -f docker-compose.dev.yml  up          # app + throwaway mosquitto
docker compose -f docker-compose.test.yml run --rm test
docker compose -f docker-compose.lint.yml run --rm lint
docker compose up -d                                  # production
```

Or locally:

```bash
make install
make test     # pytest + coverage
make lint     # ruff, black --check, mypy --strict
make fmt
```

Watch what it's doing against the dev broker:

```bash
mosquitto_sub -h localhost -t 'unifi-occupancy/#' -v
```

### Layout

| Module | Responsibility |
| --- | --- |
| `config.py` | Environment → typed config, with validation |
| `models.py` | Parsing Protect payloads into the narrow set of facts we expose |
| `state.py` | Combining polls and events into per-camera detection state |
| `protect.py` | Read-only Protect HTTP + websocket client |
| `discovery.py` | Home Assistant discovery payloads |
| `publisher.py` | Topic layout, change detection, retraction |
| `app.py` | The poll and event loops |

The Protect and MQTT transports sit behind thin seams, so the test suite runs with no
controller and no broker.

## Privacy notes

- Only `/meta/info` and `/cameras` are read, plus the event subscription. Video, snapshot
  and stream endpoints are never called.
- Published attributes are limited to camera id, name, model, connection state, last motion
  time and which detections the hardware supports.
- The API key exists only in the process environment. It is not logged and not published.
- Nothing subscribes to MQTT; this process only writes.

## License

[MIT](./LICENSE)
