# unifi-occupancy

[![Lint](https://img.shields.io/github/actions/workflow/status/romkey/unifi-occupancy/lint.yml?branch=main&label=lint&logo=githubactions&logoColor=white)](https://github.com/romkey/unifi-occupancy/actions/workflows/lint.yml)
[![Tests](https://img.shields.io/github/actions/workflow/status/romkey/unifi-occupancy/test.yml?branch=main&label=tests&logo=githubactions&logoColor=white)](https://github.com/romkey/unifi-occupancy/actions/workflows/test.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![UniFi Protect](https://img.shields.io/badge/UniFi-Protect-0559C9?logo=ubiquiti&logoColor=white)](https://www.ui.com/camera-security)
[![MQTT](https://img.shields.io/badge/MQTT-3.1.1%20%7C%205.0-660066?logo=mqtt&logoColor=white)](https://mqtt.org/)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-autodiscovery-41BDF5?logo=homeassistant&logoColor=white)](https://www.home-assistant.io/integrations/mqtt/)
[![Docker image](https://img.shields.io/github/actions/workflow/status/romkey/unifi-occupancy/docker-publish.yml?label=docker%20image&logo=githubactions&logoColor=white)](https://github.com/romkey/unifi-occupancy/actions/workflows/docker-publish.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-0A7E8C.svg)](./LICENSE)

Publish UniFi Protect detection state to MQTT — **without giving anything access to camera
video**.

The point is occupancy, not surveillance. Home Assistant (or anything else on your broker)
gets to know that *something moved at the front door* or *a smoke alarm is sounding in the
garage*, and gets nothing that could be turned back into a picture. No RTSP URLs, no
snapshot endpoints, no controller credentials leave this process — just booleans.

It can also keep those booleans. Give it a `config.yaml` and it writes a **daily occupancy
sheet** — one HTML page per day, a timeline per camera and per group, so you can see at a
glance how busy an area was and when. Still just booleans; still no pictures.

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

## Daily occupancy sheets

Opt-in, and the switch is a file: create `config.yaml` and the service starts recording
detections and writing pages. Without it nothing is written to disk and the service behaves
exactly as it did before.

```bash
mkdir -p config
cp config.example.yaml config/config.yaml
$EDITOR config/config.yaml
docker compose up -d
```

Point a web server at the output directory and you get:

```
/var/www/html/index.html        every day so far, with per-group event counts
/var/www/html/2026-11-01.html   one day
/var/www/html/2026-10-31.html   …
```

### What a day looks like

One section per **group**, and one per camera that belongs to no group. Within a group, a
row that adds every camera up, then a row each. Within a row, one pair of strips per
detection:

- the **upper strip** is when the detection was actually on — a bar from the moment it
  fired to the moment it lapsed, so you can see a four-second trip apart from someone
  standing on the porch for ten minutes;
- the **lower strip** is **density** — events per bucket, shaded darkest at the busiest
  bucket anywhere on that page, so rows can be compared against each other and not just
  against themselves.

Behind both, the day is shaded into the **periods** you named — morning, afternoon, evening,
night, or whatever divisions suit the place. Motion and person always get strips; siren and
smoke are recorded too, and appear on a day they actually fired.

Hovering a bar gives its exact times.

### When it writes

The current day's sheet is rewritten every `refresh_minutes` (by default on the hour), and
the last write of the day lands on local **midnight** — which both finalises the day that
just ended and starts the new one. `index.html` is rewritten each time.

Every page is self-contained: no JavaScript, no fonts, no CDN, light and dark. Any web
server can serve the directory, and the files will still open in ten years.

### Configuration

Groups and times of day do not fit comfortably in environment variables, so they live in
YAML — `/config/config.yaml`, or wherever `CONFIG_FILE` points. See
[config.example.yaml](./config.example.yaml); the short version:

```yaml
timezone: America/Los_Angeles

reports:
  output_dir: /var/www/html
  database: /var/lib/unifi-occupancy/occupancy.sqlite3
  bucket_minutes: 10
  refresh_minutes: 60
  retention_days: 0      # 0 keeps everything

periods:
  - { name: Night,     start: "00:00" }
  - { name: Morning,   start: "06:00" }
  - { name: Afternoon, start: "12:00" }
  - { name: Evening,   start: "18:00" }

groups:
  - name: Front of house
    cameras: [Front Door, Driveway, Porch]
  - name: Back yard
    cameras: [Back Yard, Garage]
```

| Key | Default | Meaning |
| --- | --- | --- |
| `timezone` | `$TZ`, else `UTC` | IANA zone. Days, midnights and periods are local to it |
| `reports.enabled` | `true` | Keep the file but stop writing sheets |
| `reports.output_dir` | `/var/www/html` | Where the HTML goes |
| `reports.database` | `/var/lib/unifi-occupancy/occupancy.sqlite3` | The detection history |
| `reports.title` | `Occupancy` | Heading on every page |
| `reports.refresh_minutes` | `60` | How often the current day is rewritten |
| `reports.bucket_minutes` | `10` | Width of one bucket on the density strip |
| `reports.retention_days` | `0` | Days of event detail to keep; `0` keeps everything |
| `periods[].name` / `.start` | morning/afternoon/evening/night | Shading bands; each runs until the next starts, the last wraps past midnight |
| `groups[].name` / `.cameras` | none | Cameras covering one area. Name, slug or Protect id |

Any camera not named in a group gets a section of its own, so you can group the cameras
worth grouping and leave the rest alone. A camera that is removed from Protect keeps its
place on the days it was there.

Quote the times. Unquoted ones happen to parse correctly too — YAML reads `18:00` as
1080, which is already minutes past midnight — but a bare `6` means 00:06, not 06:00.

### Storage

Detections go into SQLite, one row per detection with a start and an end. A busy household
is on the order of a megabyte a month. `retention_days` prunes the per-event detail;
sheets and their counts are kept regardless, so pruning never makes an old page lie.

To re-render every page — after editing groups or periods, say:

```bash
docker compose exec unifi-occupancy python -m unifi_occupancy --rebuild-sheets
```

That reads the history and writes the pages; it needs neither the controller nor the
broker.

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

The history behind the sheets is derived from exactly what was published, not from a second
reading of the event stream, so a sheet can never disagree with what Home Assistant was told
at the time. An event left open by a crash is closed at `DETECTION_HOLD_SECONDS` after it
started — where the detection would in fact have decayed — rather than being allowed to
stretch across the whole outage.

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

mkdir -p config       # optional: only if you want the daily sheets
cp config.example.yaml config/config.yaml
$EDITOR config/config.yaml

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

Protect and MQTT are environment variables; see [.env.example](./.env.example). Camera
groups and the daily sheets are YAML; see [above](#configuration).

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
| `CONFIG_FILE` | `/config/config.yaml` | Groups, periods and sheets. No file, no sheets |
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
| `config.py` | Environment and YAML → typed config, with validation |
| `models.py` | Parsing Protect payloads into the narrow set of facts we expose |
| `state.py` | Combining polls and events into per-camera detection state |
| `protect.py` | Read-only Protect HTTP + websocket client |
| `discovery.py` | Home Assistant discovery payloads |
| `publisher.py` | Topic layout, change detection, retraction |
| `recorder.py` | Published state → detection edges worth keeping |
| `store.py` | The SQLite detection history and per-day summaries |
| `report.py` | Grouping, the timeline arithmetic, the refresh schedule |
| `render.py` | View models → self-contained HTML |
| `app.py` | The poll, event and sheet loops |

The Protect and MQTT transports sit behind thin seams, so the test suite runs with no
controller and no broker.

## Privacy notes

- Only `/meta/info` and `/cameras` are read, plus the event subscription. Video, snapshot
  and stream endpoints are never called.
- Published attributes are limited to camera id, name, model, connection state, last motion
  time and which detections the hardware supports.
- The API key exists only in the process environment. It is not logged and not published.
- Nothing subscribes to MQTT; this process only writes.
- The occupancy sheets record camera names, detection kinds and timestamps. Nothing else is
  stored, and nothing in `/var/www/html` is served by this process — it only writes the
  files. They are as public as you make them.

## License

[MIT](./LICENSE)
