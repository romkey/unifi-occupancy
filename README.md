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

It can also watch for things Protect has no idea about. Give it a name and a question — *is
there a fire in this doorway?* — and it asks a vision model that question about the cameras
you name, on the interval you choose, and publishes the answer as one more boolean. Still no
pictures on the broker.

And it can tell you how it is doing: an optional, token-protected **Prometheus endpoint**
reports every poll, every API failure, every detection's duration and every background job
it runs.

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
unifi-occupancy/info                          {"service": "unifi-occupancy", ...}
unifi-occupancy/<camera>/motion               ON | OFF
unifi-occupancy/<camera>/motion_enabled       ON | OFF
unifi-occupancy/<camera>/person               ON | OFF
unifi-occupancy/<camera>/person_enabled       ON | OFF
unifi-occupancy/<camera>/siren                ON | OFF
unifi-occupancy/<camera>/siren_enabled        ON | OFF
unifi-occupancy/<camera>/smoke                ON | OFF
unifi-occupancy/<camera>/smoke_enabled        ON | OFF
unifi-occupancy/<camera>/attributes           {"camera_name": "Front Door", ...}
unifi-occupancy/<camera>/info                 {"mac": "...", "firmware": "...", ...}
unifi-occupancy/<camera>/health               {"health": "good"}   (opt-in)
unifi-occupancy/<camera>/trigger/<name>       ON | OFF             (opt-in)
unifi-occupancy/<camera>/trigger/<name>/attributes   {"state": "match", ...}
unifi-occupancy/<camera>/trigger/<name>/snapshot     <JPEG bytes>    (opt-in, unretained)
```

`<camera>` is the camera's Protect name, lowercased with non-alphanumerics collapsed to
underscores: *Front Door* → `front_door`.

### Metadata

Two retained JSON topics describe the setup rather than its state, so anything on the
broker can inventory a controller without asking Protect anything.

`unifi-occupancy/info` is this service: its version, the Protect application version it is
talking to, the console behind it, the MQTT layout it is publishing under, which optional
features are switched on, and how many cameras it can see.

```json
{
  "service": "unifi-occupancy",
  "version": "0.9.0",
  "protect": {
    "host": "192.168.1.1",
    "port": 443,
    "api": "integration/v1",
    "application_version": "6.3.52",
    "console": { "name": "Dream Machine", "type": "UDMPRO", "version": "6.3.52" }
  },
  "mqtt": { "topic_prefix": "unifi-occupancy", "retain": true, "qos": 0 },
  "features": {
    "websocket": true,
    "ha_discovery": true,
    "occupancy_sheets": true,
    "camera_health": false,
    "custom_triggers": true,
    "trigger_snapshots": false,
    "metrics": false
  },
  "detections": ["motion", "person", "siren", "smoke"],
  "triggers": [
    {
      "name": "Fire", "key": "fire", "cameras": "all",
      "interval_minutes": 5, "model": "qwen2.5-vl", "publish_snapshot": false
    }
  ],
  "cameras": 7
}
```

The console block is read from `/nvrs` and is an explicit allowlist — name, type, version,
firmware, hardware platform, timezone, uptime — not whatever Protect happened to return. A
controller that does not expose that endpoint simply has no `console` key.

`unifi-occupancy/<camera>/info` is one camera as a *device*: id, name, slug, model, Protect's
hardware string, MAC, firmware, whether it is connected, and for each detection whether the
hardware has it and whether it is switched on.

```json
{
  "camera_id": "661a...",
  "name": "Front Door",
  "slug": "front_door",
  "model": "camera",
  "hardware": "UVC G4 Doorbell Pro",
  "mac": "AABBCCDDEEFF",
  "firmware": "4.70.55",
  "connected": true,
  "detections": { "motion": { "supported": true, "enabled": true }, "...": {} }
}
```

This overlaps `attributes` deliberately: `attributes` is what Home Assistant shows on an
entity and moves whenever `last_motion` does, while `info` is the static description of the
hardware and carries the fields HA has no use for. Both are published only when they change.

### Home Assistant

Autodiscovery is on by default, and nothing needs to be added to `configuration.yaml`. Set
`HA_DISCOVERY_ENABLED=false` if you would rather wire the topics up by hand.

Each camera arrives as one HA **device** carrying nine entities: four detection binary
sensors, four diagnostic "…Detection Enabled" sensors, and a diagnostic **Camera Info**
sensor whose state is the hardware model and whose attributes are the whole `info` payload —
MAC, firmware, and which detections the hardware has. The identity fields also reach HA as
the device's own `model`, `sw_version` and MAC, so Settings → Devices shows them without
opening an entity.

Both the entity state and the device's model are Protect's `type` (`UVC G4 Doorbell Pro`),
not its `modelKey` — Protect answers `modelKey` with the bare word `camera` for every
camera. `model` is still published on `info` and `attributes` unchanged, with `hardware`
alongside it.

The service itself arrives as a device of its own, **UniFi Occupancy** — a bridge, in HA's
sense — carrying two diagnostic sensors: **Service Version**, whose attributes are the whole
`unifi-occupancy/info` payload (console, MQTT layout, which features are on), and **Protect
Version**. Those are what you want in front of you when something is off, and without this
device they would live only on a topic HA never reads.

## Is the camera still looking at anything?

Opt-in, off by default, and the one feature here that reads a picture.

A camera that has been nudged, spidered over, sprayed, rained into or slowly annexed by a
shrub keeps reporting detections — of the shrub. Nothing in the detection stream says the
view stopped being worth anything, which is exactly the failure you find out about when you
go looking for footage you do not have.

So with `CAMERA_HEALTH_ENABLED=true`, once an hour, this takes **one snapshot per camera**
and asks a vision model a single question: does this camera still see something usable? The
answer lands on its own topic:

```
unifi-occupancy/front_door/health   {"health": "good", "model": "gpt-4o-mini", "checked_at": "..."}
unifi-occupancy/driveway/health     {"health": "bad", "explanation": "lens is fogged over", ...}
unifi-occupancy/garage/health       {"health": "unknown", "explanation": "camera is not connected", ...}
```

Three verdicts, and the difference between the last two matters: `bad` is the model saying
the camera is in trouble, `unknown` is this service saying it could not find out — camera
offline, snapshot refused, endpoint down, model gibberish. An `unknown` never raises an
alarm and never hides one.

### What it asks

Any OpenAI-compatible `/chat/completions` endpoint will do: OpenAI, OpenRouter, or a model
running on your own hardware through Ollama, llama.cpp, vLLM or LM Studio. The API key is
optional, because local servers do not want one.

The prompt ships in the code and is overridable in full with `CAMERA_HEALTH_PROMPT` — see
[.env.example](./.env.example) for the text. It asks for `bad` on a view that is obstructed,
blocked, out of focus, smeared, dirty, fogged, glared out, black, white, corrupted or aimed
at a ceiling; and for `good` on anything usable, explicitly including night-time infrared,
rain, snow, grain and sheer boredom — a dark picture at 2am is a working camera, not a
broken one. Replies are read as JSON when they are JSON and as prose when they are not, so
small local models that cannot hold a format still get a verdict out.

### Home Assistant

With the check on, each camera's device gains two diagnostic entities: **Camera Health**, a
sensor reading `good` / `bad` / `unknown`, and **Camera Health Problem**, a `problem` binary
sensor that is on only for an explicit `bad`. Automate on the second and read the first.

### What it costs, and what it gives up

One image per camera per hour — 24 a day each, a few cents a day against a hosted
small-vision model, nothing at all against a local one. `CAMERA_HEALTH_INTERVAL_MINUTES`
turns that dial; `CAMERA_HEALTH_STAGGER_SECONDS` spaces the sweep out so a dozen cameras do
not all upload at once.

And it gives up the thing the rest of this service is built around: with the check on,
snapshots do leave the process, to whichever endpoint you configured. That is why it is off
unless you switch it on, why it needs an API key with snapshot permission to work at all,
and why the frames are held in memory for one request and never published, logged or
written to disk. What reaches your broker is still a word and, at worst, a short sentence.
If that trade is not one you want, do not set the variable and nothing changes.

## Watching for something of your own

Opt-in, configured in `config.yaml`, and the other feature that reads a picture.

Protect reports what Protect knows how to report: motion, a person, a siren, a smoke alarm.
It has nothing to say about the things that actually happen in a doorway — somebody setting
a fire on the step, a gate left open, a parcel nobody collected, a van across the drive.
Those are plain in a frame and absent from the API.

So a **trigger** is a name and a question. Every interval it takes one snapshot of each
camera it watches, puts your question to a vision model, and publishes the answer:

```yaml
triggers:
  - name: Fire
    prompt: >
      Is there an active fire in this frame -- open flames, burning material, or
      thick smoke rising from something burning? Do not answer yes for lights,
      reflections, sunsets, headlights or a lit cigarette.
    cameras: ALL
    interval_minutes: 5
    device_class: problem
```

```
unifi-occupancy/front_door/trigger/fire             ON
unifi-occupancy/front_door/trigger/fire/attributes  {"state": "match", "name": "Fire",
                                                     "match": true,
                                                     "explanation": "flames on the doormat",
                                                     "model": "qwen2.5-vl", "checked_at": "..."}
unifi-occupancy/front_door/trigger/fire/snapshot    <JPEG bytes>   (opt-in, unretained)
```

`ON` means the model could see what you described the last time it looked, `OFF` means it
looked and could not. A third answer, `unknown`, goes only to the attributes and **leaves
the boolean exactly as it was**: no snapshot, no endpoint or a reply nobody could read is
not the same as "no fire", and a trigger whose job is to notice a fire must never quietly
report all-clear because it could not look.

Each trigger takes:

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | *required* | What the trigger is called. Slugified for the topic and the entity id |
| `prompt` | *required* | The question itself, in your own words |
| `cameras` | `ALL` | A list of camera names, slugs or Protect ids — or `ALL` |
| `interval_minutes` | `15` | How often each of those cameras is checked |
| `enabled` | `true` | `false` keeps the trigger written down but stops it running |
| `model` | the health check's | Model to ask; must be able to see images |
| `api_base_url` | the health check's | OpenAI-compatible endpoint for this trigger |
| `api_key` | the health check's | Optional; local servers need none |
| `publish_snapshot` | `false` | Publish the frame that matched. Opt-in, per trigger — see below |
| `device_class` | unset | Home Assistant device class: `problem`, `smoke`, `safety`, … |
| `high_quality_snapshot` | `false` | Full-resolution snapshots for this trigger |
| `request_timeout` | `60` | Vision request timeout, seconds |
| `stagger_seconds` | `5` | Gap between one camera's check and the next |
| `max_explanation_chars` | `200` | Trim length for the explanation |
| `append_answer_format` | `true` | Append the JSON answer format to your prompt |
| `batch` | `true` | Let this trigger share one call with others like it — see below |

The endpoint, key and model default to whatever the camera health check uses, so a local
vision server configured once serves both; `VISION_API_BASE_URL`, `VISION_API_KEY` and
`VISION_MODEL` override them for triggers alone, and the keys above override them for one
trigger. The health check itself does not have to be switched on for triggers to work.

Answers are read as JSON when they are JSON and as prose when they are not, and the reading
is biased towards `OFF`: an ambiguous reply is never a match, because a trigger that fires
on ambiguity is worse than no trigger at all. A prose reply counts as a match only when it
*answers* the question — "yes", "affirmative", "confirmed" — never when it merely describes
the frame, because a description says nothing about which of your triggers it answers: "a
person is visible on the step" is not a fire. Those replies come out `unknown` and leave the
boolean where it was. Unless you set `append_answer_format: false`,
your prompt has a short instruction appended asking for
`{"match": true, "explanation": "…"}`, so you can write the question and nothing else.

A trigger renamed or deleted outright leaves its last retained topics on the broker, since
nothing is left to say they existed. Setting `enabled: false` instead retracts them
properly — the entity, the boolean and its attributes — and is the way to switch one off.

### Publishing the frame that matched

"There is a fire at the front door" is a great deal more use with the picture attached. Set
`publish_snapshot: true` on a trigger and the frame that matched goes to
`<prefix>/<camera>/trigger/<name>/snapshot` as raw JPEG bytes, alongside the boolean.

This is the only thing the service publishes that is not a boolean, a number or a sentence,
so it is **off by default and switched on one trigger at a time**. There is deliberately no
environment variable and no inherited default for it: every other endpoint setting can be
set once and inherited by every trigger, but nobody should be able to start publishing
pictures of their doorways by setting a single variable. Before you turn it on:

- A frame of a doorway is likely to show a face, and quite possibly a crime. Everyone who
  can read your broker can read it — usually a wider set of people than those who can open
  Protect.
- It is published **unretained**, so a subscriber who is listening gets it and nobody else
  does. It is not left sitting on the broker for whoever looks next month, and it does not
  outlive the match. Home Assistant holds the last one for as long as the entity exists.
- Only a `match` publishes a frame. `clear` and `unknown` publish nothing, so an endpoint
  that is down does not fill the broker with pictures.
- The frame is best-effort. It goes out just before the boolean so an automation finds the
  picture already there, but a frame the broker refuses — a message size limit, most likely —
  is logged and dropped, and the boolean and its attributes are published anyway. A fire with
  no picture is still a fire.
- Nothing is written to disk on any path, with this on or off.
- A high-quality snapshot can run to a megabyte or two. Brokers have message size limits
  (mosquitto's default `message_size_limit` is unlimited, but check yours) and some log
  every publish.
- `unifi-occupancy/info` reports `features.trigger_snapshots` and a `publish_snapshot` flag
  per trigger, `feature_enabled{feature="trigger_snapshots"}` says the same in Prometheus,
  and the service logs a line per trigger at startup. You should always be able to find out
  whether anything is publishing pictures without reading `config.yaml`.

Turning it back off retracts the camera entity on the next poll. The frames themselves were
never retained, so there is nothing to clear.

### Home Assistant

Each trigger gets two entities per camera it watches, and a third if it publishes frames —
all of them on that camera's existing device, next to its detections:

| Entity | What it is |
| --- | --- |
| `binary_sensor.<camera>_<trigger>` | The boolean. `ON` when the model could see it. Automate on this one |
| `sensor.<camera>_<trigger>_status` | The last answer as a word: `match`, `clear` or `unknown` — diagnostic |
| `camera.<camera>_<trigger>_snapshot` | The frame that matched. Only with `publish_snapshot: true` — diagnostic |

The binary sensor carries the explanation, the model and the check time as attributes. Give
it a `device_class` and it looks like what it is: `problem` for a fire, `safety`, `smoke`,
`door`, whatever fits. Automate on it exactly as you would on motion.

The status sensor is there because the boolean structurally cannot say `unknown` — an
unanswerable check leaves it alone, so `OFF` means both "no fire" and "no fire as far as I
know". Condition on the status sensor when the difference matters:

```yaml
automation:
  - alias: Fire at the front door
    trigger:
      - platform: state
        entity_id: binary_sensor.front_door_fire
        to: "on"
    condition:
      - condition: state
        entity_id: sensor.front_door_fire_status
        state: match
    action:
      - service: notify.mobile_app_phone
        data:
          title: Fire at the front door
          message: "{{ state_attr('binary_sensor.front_door_fire', 'explanation') }}"
          data:
            image: /api/camera_proxy/camera.front_door_fire_snapshot
```

The camera entity has no image until the first match, since its topic is unretained — Home
Assistant shows it as unknown until then, which is correct rather than a fault.

#### A blueprint, if you would rather not write that YAML

[`blueprints/automation/unifi_occupancy/trigger_notify.yaml`](blueprints/automation/unifi_occupancy/trigger_notify.yaml)
does the above from the UI: pick the triggers you care about, pick how you want to be told,
and it works out the rest. Copy it into `<config>/blueprints/automation/unifi_occupancy/`
and reload automations, or import it by URL from **Settings → Automations & Scenes →
Blueprints → Import**. It declares no `min_version`, and uses the long-standing
`trigger:`/`condition:`/`action:` syntax rather than the newer plural keys, so it imports on
an older Home Assistant as well as a current one.

It derives each trigger's status sensor and snapshot camera from the sensor that fired, so
you select one set of entities rather than pairing three up by hand. It then exposes
`trigger_name`, `camera_name`, `explanation`, `model`, `checked_at`, `snapshot_entity`,
`image_url` and a ready-made `title`/`message` to whatever action you chose — so the
notification above is three fields in a form:

```yaml
action: notify.mobile_app_your_phone
data:
  title: "{{ title }}"
  message: "{{ message }}"
  data:
    image: "{{ image_url }}"
```

Two options worth knowing. *Only when the model actually answered* adds the `match` check
from the previous section, so a stale `ON` left over from before an outage cannot notify you
again; it is on by default. *Quiet period* suppresses repeat matches for a while — rarely
needed, since a trigger only fires on the way from `OFF` to `ON`, but useful for something
flickering in and out of frame.

The derivation reads the entity ids as announced over discovery
(`binary_sensor.unifi_occupancy_front_door_trigger_fire` and its `_status` and `_snapshot`
companions). Rename those companions in Home Assistant and the blueprint stops finding
them, at which point it degrades — no picture, no `match` check — rather than failing. The
boolean you selected is unaffected either way.

### Triggers on the same interval share a call

Triggers agreeing on the things a single request fixes — the **interval**, the endpoint, the
API key, the model and `high_quality_snapshot` — are asked together about one frame. Three
such triggers on a camera make one snapshot and one request per cycle, not three. Nothing to
configure; it is what happens unless you say otherwise.

The saving is larger than the request count suggests. A vision request is almost entirely
the image, so sending one frame with three questions costs close to what one question costs,
rather than three times as much. It also spares your controller two snapshots per camera per
cycle.

This is also why a group runs as one scheduled job. Before, each trigger had its own loop
sleeping `interval - elapsed`, so two triggers nominally on five minutes started a moment
apart and drifted as the model's latency varied — there was no moment at which they wanted
the same frame. Sharing a cycle removes the question rather than answering it.

**What it costs in accuracy.** A combined prompt is not free. A model answering four
questions at once is not the same model answering one, and smaller local models feel this
more than hosted ones. Four specific failures, and what the prompt does about each:

| | |
| --- | --- |
| **Bleed** — your fire prompt's "do not answer yes for sunsets" leaking onto the parcel question | Each prompt goes inside its own `<question id="...">` block, and the preamble says instructions inside a question govern that question alone |
| **Priming** — asking about fire and smoke together making both likelier | The preamble says what one question asks about is not evidence for any other |
| **Narrative** — the model making the answers cohere into one story about the frame | The preamble says not to reconcile them; all can be true, all can be false |
| **Contagious guessing** — one unanswerable question dragging the rest down | The preamble asks for an explicit `null` on anything the frame cannot settle, per question |

Your prompts go in verbatim — nothing rewrites them. A trigger whose wording you have tuned
carefully, and which you would rather have the model's whole attention, gets `batch: false`
and is asked on its own exactly as before. `VISION_BATCH_TRIGGERS=false` does that for every
trigger at once. A trigger setting `append_answer_format: false` is never batched, since it
has taken over the answer format a batched call needs.

**Nothing fails together.** A key missing from the reply, or answered with something
unreadable, marks that one trigger `unknown` and leaves the rest alone. A reply that is not
an answer at all falls back to asking each trigger separately, on the same frame — one extra
round trip only when something is already wrong, rather than every trigger on that camera
going `unknown` at the same instant. Only the endpoint actually failing is shared, which
separate calls would not have survived either.

`unifi-occupancy/info` lists `trigger_groups`, and
`trigger_batch_calls_total{outcome}` counts batched calls and the fallbacks, so you can see
what is actually being combined.

### What it costs

One image per watched camera per interval, per *group* of triggers rather than per trigger.
A fire check on four doorways every five minutes is about 1,150 images a day — pennies on a
small hosted model, free on your own hardware, and the reason the default interval is
fifteen minutes rather than one. Add a second trigger on the same interval and those four
doorways still cost 1,150; give it a different interval and you have two sweeps. Pick the
interval for how fast the thing you are watching for matters, keep the camera list short,
and prefer a local model if you intend to run it at minutes.

The same trade as the health check applies: with a trigger configured, snapshots leave this
process for the endpoint you named. They are held in memory for one request and are never
published, logged or written to disk; what reaches your broker is a boolean and, at worst, a
short sentence. Write no triggers and no snapshot is ever taken.

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

On the current day, a green vertical line runs down through every camera in a section at
the time the sheet was last written: everything to the right of it is a day that has not
happened yet, not a quiet one. A finished day has no line.

Hovering a bar gives its exact times. Hovering anywhere else over a group's timelines
draws a **red rule** down through every camera in it, labelled with the moment under the
pointer — so a bar can be read off the lane it is drawn on instead of squinting at the
three-hourly axis below. The reading is in the reporting timezone, not the reader's, and it
is right on the two days a year the clocks change, when a day is 23 or 25 hours long.

The **count cards** at the top of a page are links: click one to jump to that group's
timelines, which is worth having once a page carries a dozen groups.

### When it writes

The current day's sheet is rewritten every `refresh_minutes` (by default on the hour), and
the last write of the day lands on local **midnight** — which both finalises the day that
just ended and starts the new one. `index.html` is rewritten each time.

### Light and dark

Pages follow the browser's own light/dark setting, and carry an **Auto / Light / Dark**
control for overriding it — useful on a wall tablet whose OS theme is not what you want the
sheet to be. The choice is remembered per browser and applies to every sheet.

That control and the hover rule are the only JavaScript in a sheet: a few dozen inline
lines, no library. With scripts disabled the control hides itself and the page still follows
the browser, the hover rule simply never appears, and everything else — the bars, the
density, the period shading, the card links — is plain HTML and works regardless.

Every page is otherwise self-contained: no fonts, no CDN, nothing fetched. Any web server
can serve the directory, and the files will still open in ten years.

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

**Every camera is listed, whether or not anything happened to it.** A door that was never
opened is a result — an empty lane says so, and a camera missing from the page does not. The
roster is kept in the database alongside the events, so this holds for `--rebuild-sheets`
too, which has no controller to ask.

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
broker. Cameras that have never fired are listed from the stored roster, so a rebuild looks
the same as a live refresh.

## Prometheus metrics

Set `METRICS_ENABLED=true` and a token, and the service serves a Prometheus endpoint on
port 9833:

```bash
METRICS_ENABLED=true
METRICS_TOKEN=$(openssl rand -hex 32)
```

```bash
curl -H "Authorization: Bearer $METRICS_TOKEN" http://nas.example:9833/metrics
```

It is off by default, and when it is on a token is **required** — `METRICS_ENABLED` without
`METRICS_TOKEN` is a configuration error and the service will not start. An open exporter
here is not a dull list of request counts: `unifi_occupancy_camera_detection_active` is a
live, camera-by-camera map of who is moving where in a building.

In Prometheus:

```yaml
scrape_configs:
  - job_name: unifi-occupancy
    static_configs:
      - targets: ["nas.example:9833"]
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/unifi-occupancy.token
```

The token may also be sent as `X-Metrics-Token:` instead of `Authorization: Bearer`, for
agents that find that easier. It is never read from a query string, so it cannot end up in
a proxy log or somebody's shell history. Requests without it get `401` whatever path they
ask for — including the real one — so moving the endpoint with `METRICS_PATH` actually
hides it.

With Docker, publish the port (it is not published by default):

```yaml
services:
  unifi-occupancy:
    ports:
      - "9833:9833"
```

### What is in it

Everything the process knows about itself, and nothing a scrape has to go and fetch:
answering `/metrics` touches no camera, no controller and no database. Every name below is
prefixed `unifi_occupancy_`; the table leaves that off to stay readable.

| Family | What it tells you |
| --- | --- |
| `build_info`, `start_time_seconds`, `uptime_seconds` | Version, Python, architecture, how long since the last restart |
| `feature_enabled{feature}`, `configured_seconds{setting}` | Which optional features are on, and the intervals they run at |
| `protect_info{...}` | The controller and console being read, as labels |
| `protect_up`, `protect_cameras_polled` | Whether the last poll worked, and what it returned |
| `protect_requests_total{endpoint,outcome}` | Every API call, split `success` / `error` / `auth_error` — an expired API key shows up here first |
| `protect_request_duration_seconds{endpoint}` | How long the controller takes to answer |
| `websocket_connected`, `websocket_connects_total`, `websocket_failures_total{cause}` | Whether the event stream is up, and how much it has been flapping |
| `event_frames_total{outcome}`, `events_total{kind,phase}` | Frames read, and the detections in them by kind and by start/end |
| `cameras`, `cameras_connected`, `camera_info{...}`, `camera_connected{camera}` | The inventory, and each camera's identity and link state |
| `camera_detection_active{camera,kind}` | What is firing right now |
| `camera_detection_enabled{camera,kind}`, `camera_detection_supported{camera,kind}` | What each camera can do, and what is switched on — a detection quietly turned off in Protect is visible here |
| `detections_total{camera,kind}`, `detection_seconds_total{camera,kind}` | How often each detection has fired, and how long it has spent on |
| `mqtt_publishes_total{outcome}`, `mqtt_published_bytes_total`, `mqtt_publishes_suppressed_total`, `mqtt_last_publish_timestamp_seconds` | What went to the broker, what was skipped as unchanged, and when |
| `job_runs_total{job,outcome}`, `job_duration_seconds{job}`, `job_in_progress{job}`, `job_last_success_timestamp_seconds{job}` | The background jobs — `poll`, `events`, `health_sweep`, `sheets` and one `trigger:<name>` per custom trigger — their outcomes, durations and last success |
| `health_checks_total{camera,verdict}`, `health_check_duration_seconds`, `camera_health{camera,status}`, `health_last_check_timestamp_seconds{camera}` | The optional vision check: verdicts per camera, how long each took, when each was last looked at |
| `trigger_checks_total{trigger,camera,answer}`, `trigger_check_duration_seconds{trigger}`, `trigger_state{trigger,camera,state}`, `trigger_last_check_timestamp_seconds{trigger,camera}` | Custom triggers: answers per trigger and camera, how long each check took, when each was last asked |
| `trigger_batch_calls_total{outcome}` | Calls that asked several triggers at once. `unreadable` is one that fell back to separate calls |
| `trigger_snapshots_total{trigger,camera}` | Matching frames published to the broker. Stays absent unless a trigger has `publish_snapshot` on |
| `recorded_edges_total{kind,edge}`, `recorder_failures_total{operation}`, `sheets_written_total` | The history database and the daily sheets |
| `http_requests_total{path,status}`, `http_request_duration_seconds` | This endpoint itself |
| `process_cpu_seconds_total`, `process_max_resident_bytes`, `asyncio_tasks` | What the process is costing |

Counters exist at zero before anything has happened, so a service that has been quiet all
day reads as quiet rather than as broken.

A few queries to start from:

```promql
# The API key stopped working.
rate(unifi_occupancy_protect_requests_total{outcome="auth_error"}[5m]) > 0

# The event stream is flapping.
rate(unifi_occupancy_websocket_failures_total[15m]) > 0

# Nothing has reached the broker in ten minutes.
time() - unifi_occupancy_mqtt_last_publish_timestamp_seconds > 600

# The sheets have not been written for two hours.
time() - unifi_occupancy_job_last_success_timestamp_seconds{job="sheets"} > 7200

# A camera the vision check thinks is blocked, smeared or pointed at a wall.
unifi_occupancy_camera_health{status="bad"} == 1

# Busiest cameras over a day.
topk(5, increase(unifi_occupancy_detection_seconds_total{kind="person"}[24h]))
```

### Cardinality

Per-camera series are the only ones that grow, and there is no label anywhere carrying an
event id, a Protect object id or a timestamp. A camera removed from the controller has its
series dropped on the next poll rather than frozen at its last value, and requests for an
unknown path are labelled `other` rather than with whatever URL a stranger tried — so
nobody can grow this process's memory one request at a time.

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
| `CAMERA_HEALTH_ENABLED` | `false` | Hourly vision check on each camera's own picture |
| `CAMERA_HEALTH_API_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint |
| `CAMERA_HEALTH_API_KEY` | unset | Optional; local servers need none |
| `CAMERA_HEALTH_MODEL` | `gpt-4o-mini` | Model to ask; must be able to see images |
| `CAMERA_HEALTH_PROMPT` | see [.env.example](./.env.example) | The question put to the model |
| `CAMERA_HEALTH_INTERVAL_MINUTES` | `60` | How often every camera is checked |
| `CAMERA_HEALTH_STAGGER_SECONDS` | `5` | Gap between one camera's check and the next |
| `CAMERA_HEALTH_REQUEST_TIMEOUT` | `60` | Vision request timeout, seconds |
| `CAMERA_HEALTH_HIGH_QUALITY_SNAPSHOT` | `false` | Full-resolution snapshots |
| `CAMERA_HEALTH_MAX_EXPLANATION_CHARS` | `200` | Trim length for a `bad` explanation |
| `VISION_API_BASE_URL` | `CAMERA_HEALTH_API_BASE_URL` | Default endpoint for custom triggers |
| `VISION_API_KEY` | `CAMERA_HEALTH_API_KEY` | Default key for custom triggers |
| `VISION_MODEL` | `CAMERA_HEALTH_MODEL` | Default model for custom triggers |
| `VISION_REQUEST_TIMEOUT` | `CAMERA_HEALTH_REQUEST_TIMEOUT` | Default trigger request timeout |
| `VISION_STAGGER_SECONDS` | `CAMERA_HEALTH_STAGGER_SECONDS` | Default gap within a trigger sweep |
| `VISION_HIGH_QUALITY_SNAPSHOT` | `CAMERA_HEALTH_HIGH_QUALITY_SNAPSHOT` | Default snapshot quality |
| `VISION_MAX_EXPLANATION_CHARS` | `CAMERA_HEALTH_MAX_EXPLANATION_CHARS` | Default explanation trim |
| `VISION_BATCH_TRIGGERS` | `true` | `false` asks every trigger on its own, never combined |
| `METRICS_ENABLED` | `false` | Serve the Prometheus endpoint |
| `METRICS_TOKEN` | unset | *Required* when the endpoint is on; at least 16 characters |
| `METRICS_HOST` | `0.0.0.0` | Address to bind |
| `METRICS_PORT` | `9833` | Port to bind |
| `METRICS_PATH` | `/metrics` | Path the endpoint answers on |
| `CONFIG_FILE` | `/config/config.yaml` | Groups, periods, sheets and triggers. No file, none of them |
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
| `info.py` | The `info` metadata payloads for the service and each camera |
| `vision.py` | One frame, one prompt, one short answer, over chat completions |
| `health.py` | The optional vision check: snapshot, ask, verdict, schedule |
| `triggers.py` | Custom triggers: your question, your cameras, your interval |
| `publisher.py` | Topic layout, change detection, retraction |
| `metrics.py` | The counters, gauges and histograms, and the exposition format |
| `exporter.py` | The token-protected `/metrics` HTTP endpoint |
| `recorder.py` | Published state → detection edges worth keeping |
| `store.py` | The SQLite detection history, camera roster and per-day summaries |
| `report.py` | Grouping, the timeline arithmetic, the refresh schedule |
| `render.py` | View models → self-contained HTML |
| `app.py` | The poll, event and sheet loops |
| `blueprints/` | Home Assistant blueprints, shipped for copying into your config |

The Protect and MQTT transports sit behind thin seams, so the test suite runs with no
controller and no broker.

## Privacy notes

- Only `/meta/info`, `/nvrs` and `/cameras` are read, plus the event subscription. Video and
  stream endpoints are never called.
- The snapshot endpoint is called only by the two vision features — the health check with
  `CAMERA_HEALTH_ENABLED=true`, and any trigger you write in `config.yaml`. One frame per
  camera per interval, sent to the endpoint you configured, held in memory for the length of
  that one request. Leave the variable unset and write no triggers, and no snapshot is ever
  taken.
- No frame is ever written to disk or logged, on any path or setting.
- One setting publishes a frame to MQTT: `publish_snapshot: true` on an individual trigger,
  which sends the frame that *matched* to `trigger/<name>/snapshot`, unretained. It is off
  by default, it cannot be switched on globally, and `features.trigger_snapshots` in the
  `info` topic tells you whether anything has it on. Everything else this service publishes
  is a boolean, a number or a short sentence. See
  [Publishing the frame that matched](#publishing-the-frame-that-matched).
- Published attributes are limited to camera id, name, model, hardware, connection state,
  last motion time and which detections the hardware supports.
- The `info` topics add the console's name, type, version and uptime, and each camera's MAC,
  firmware and hardware model. Both are allowlists; nothing is forwarded from Protect that
  is not named in `info.py`.
- The API key exists only in the process environment. It is not logged and not published.
  Nor is the vision endpoint's key, if you set one.
- Nothing subscribes to MQTT; this process only writes.
- The Prometheus endpoint is the only listening socket this service ever opens, it is off
  until you switch it on, and it answers nobody without the token. What it reports is the
  same class of fact as the MQTT topics — which cameras exist and what they are detecting —
  so treat the token like a key to that, and prefer binding `METRICS_HOST` to an address
  only your monitoring host can reach.
- The occupancy sheets record camera names, detection kinds and timestamps. Nothing else is
  stored, and nothing in `/var/www/html` is served by this process — it only writes the
  files. They are as public as you make them.

## License

[MIT](./LICENSE)
