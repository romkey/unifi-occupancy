"""Configuration.

The MQTT bridge is configured entirely from the process environment, so the
container can be set up through ``docker compose``/``--env-file`` alone.

The daily occupancy sheets need structure the environment is a poor fit for --
camera groups, named times of day -- so those come from a YAML file, by default
``/config/config.yaml``. The absence of that file is itself meaningful: it means
no sheets.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from .models import slugify

DEFAULT_MQTT_PORT = 1883
DEFAULT_MQTT_TLS_PORT = 8883


class ConfigError(ValueError):
    """Raised when the environment does not describe a usable configuration."""


def _get(env: Mapping[str, str], key: str, default: str | None = None) -> str | None:
    value = env.get(key)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _require(env: Mapping[str, str], key: str) -> str:
    value = _get(env, key)
    if value is None:
        raise ConfigError(f"{key} is required")
    return value


def _get_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = _get(env, key)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be a boolean value, got {raw!r}")


def _get_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - message assertion only
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _get_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc


def _get_positive_float(env: Mapping[str, str], key: str, default: float) -> float:
    value = _get_float(env, key, default)
    if value <= 0:
        raise ConfigError(f"{key} must be greater than zero, got {value}")
    return value


@dataclass(frozen=True)
class ProtectConfig:
    """How to reach the UniFi Protect integration API."""

    host: str
    api_key: str
    port: int = 443
    verify_ssl: bool = False
    poll_interval: float = 5.0
    request_timeout: float = 10.0
    use_websocket: bool = True

    @property
    def base_url(self) -> str:
        return f"https://{self.host}:{self.port}/proxy/protect/integration/v1"

    @property
    def websocket_url(self) -> str:
        return f"wss://{self.host}:{self.port}/proxy/protect/integration/v1/subscribe/events"


@dataclass(frozen=True)
class MqttConfig:
    """How to reach the MQTT broker and what to publish under."""

    host: str
    port: int = DEFAULT_MQTT_PORT
    username: str | None = None
    password: str | None = None
    client_id: str = "unifi-occupancy"
    topic_prefix: str = "unifi-occupancy"
    tls: bool = False
    keepalive: int = 60
    retain: bool = True
    qos: int = 0

    @property
    def availability_topic(self) -> str:
        return f"{self.topic_prefix}/status"


#: The default question put to the vision model. Overridable in full with
#: ``CAMERA_HEALTH_PROMPT``; see .env.example.
DEFAULT_HEALTH_PROMPT = (
    "You are inspecting a single still frame from a fixed security camera to decide "
    "whether the camera itself is still producing a usable picture. You are not "
    "judging whether anything interesting is happening in the scene.\n"
    "\n"
    "Answer bad if the view is obstructed or blocked (a wall, a vehicle, foliage, a "
    "cover, tape, a hand), out of focus, blurred or smeared, dirty, fogged by "
    "condensation or spiderweb, washed out by glare or an infrared reflection off a "
    "nearby surface, almost entirely black or entirely white, visibly corrupted "
    "(banding, tearing, a green or magenta cast), or aimed somewhere useless such as "
    "straight at the sky, the ground or a ceiling.\n"
    "\n"
    "Answer good if the frame shows a usable view of whatever it is pointed at, even "
    "if it is dull, empty, grainy, rainy, snowy or lit only by infrared at night. "
    "Night-time darkness on its own is not a fault; an image with no discernible "
    "structure at all is.\n"
    "\n"
    'Reply with JSON and nothing else: {"health": "good"} or {"health": "bad", '
    '"explanation": "<at most 15 words>"}.'
)


@dataclass(frozen=True)
class HealthConfig:
    """The optional vision check on each camera's own picture.

    Off unless ``CAMERA_HEALTH_ENABLED`` is set, because switching it on means
    this process starts reading snapshots and sending them to a model -- which
    is the one thing the rest of the service is built not to do.
    """

    enabled: bool = False
    #: Any OpenAI-compatible ``/chat/completions`` endpoint.
    base_url: str = "https://api.openai.com/v1"
    #: Optional: local servers (Ollama, llama.cpp, vLLM) usually need no key.
    api_key: str | None = None
    model: str = "gpt-4o-mini"
    prompt: str = DEFAULT_HEALTH_PROMPT
    #: How often every camera is checked.
    interval_minutes: float = 60.0
    #: Seconds between one camera's check and the next, so a sweep is a trickle
    #: rather than a burst of simultaneous uploads.
    stagger_seconds: float = 5.0
    request_timeout: float = 60.0
    #: Ask Protect for the full-resolution snapshot rather than a small one.
    high_quality_snapshot: bool = False
    #: Explanations are meant to fit in a notification, not to be an essay.
    max_explanation_chars: int = 200

    @property
    def interval_seconds(self) -> float:
        return self.interval_minutes * 60.0

    @property
    def completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"


@dataclass(frozen=True)
class VisionConfig:
    """Where a vision model lives, for anything that wants to ask one.

    The camera health check came first and brought its own ``CAMERA_HEALTH_*``
    settings with it. Custom triggers ask the same kind of question of the same
    kind of endpoint, so rather than a second set of credentials these default
    to the health check's -- ``VISION_*`` overrides them when the two want
    different models, and a trigger can override them again for itself.
    """

    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None
    model: str = "gpt-4o-mini"
    request_timeout: float = 60.0
    #: Seconds between one camera's check and the next within a sweep.
    stagger_seconds: float = 5.0
    high_quality_snapshot: bool = False
    max_explanation_chars: int = 200
    #: Whether triggers that can share one call do.
    batch: bool = True


#: Appended to every trigger prompt unless the trigger switches it off. The
#: operator writes what to look for; this is what makes the answer readable.
TRIGGER_ANSWER_FORMAT = (
    "Answer only about what is visible in this frame, and answer no unless you can "
    "actually see it.\n"
    "\n"
    'Reply with JSON and nothing else: {"match": true, "explanation": "<at most 15 '
    'words>"} if it is present, or {"match": false} if it is not.'
)

DEFAULT_TRIGGER_INTERVAL_MINUTES = 15.0

#: What to write under ``cameras:`` to mean every camera there is.
ALL_CAMERAS: frozenset[str] = frozenset({"all", "*", "any", "every", "everything"})


@dataclass(frozen=True)
class TriggerConfig:
    """One thing to watch the cameras for, in the operator's own words.

    A name, a question, the cameras it applies to and how often to ask. The
    answer becomes a boolean on ``<prefix>/<camera>/trigger/<name>``, so a
    trigger is for something a frame can settle -- "is there a fire in this
    doorway", "is the gate open", "is there a parcel on the step" -- not for
    anything needing memory of what came before.
    """

    name: str
    #: The question itself, as written. :attr:`question` is what is sent.
    prompt: str
    #: Camera names, slugs or Protect ids; empty means every camera.
    cameras: tuple[str, ...] = ()
    interval_minutes: float = DEFAULT_TRIGGER_INTERVAL_MINUTES
    enabled: bool = True
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None
    model: str = "gpt-4o-mini"
    request_timeout: float = 60.0
    stagger_seconds: float = 5.0
    high_quality_snapshot: bool = False
    max_explanation_chars: int = 200
    #: Whether a matching frame is published alongside the boolean.
    #:
    #: Off by default, and deliberately per-trigger only: there is no env var
    #: and no inherited default for this one, because it is the only thing the
    #: service publishes that is not a boolean or a sentence, and nobody should
    #: be able to switch it on for every trigger at once by setting one
    #: variable. A published frame is the single most sensitive thing here --
    #: it is likely to show a face -- so saying yes has to be a decision taken
    #: once per question you ask. See :mod:`unifi_occupancy.topics` for what it
    #: costs on the broker.
    publish_snapshot: bool = False
    #: Home Assistant ``device_class`` for the binary sensor, e.g. ``problem``,
    #: ``smoke``, ``safety``. ``None`` leaves the entity classless.
    device_class: str | None = None
    #: Whether to append :data:`TRIGGER_ANSWER_FORMAT` to the prompt.
    append_answer_format: bool = True
    #: Whether this trigger may share a vision call with others like it.
    #:
    #: On by default. Triggers that match on :attr:`batch_key` -- same
    #: interval, same endpoint, same model, same snapshot quality -- are asked
    #: together about one frame, which sends the image once instead of once per
    #: trigger. Set it false for a trigger whose prompt is delicate enough that
    #: it wants the model's whole attention, and it is asked on its own.
    batch: bool = True

    @property
    def key(self) -> str:
        """The trigger's slug: its topic segment and its entity id."""
        return slugify(self.name)

    @property
    def interval_seconds(self) -> float:
        return self.interval_minutes * 60.0

    @property
    def all_cameras(self) -> bool:
        return not self.cameras

    @property
    def question(self) -> str:
        """The prompt as it goes to the model."""
        if not self.append_answer_format:
            return self.prompt
        return f"{self.prompt.rstrip()}\n\n{TRIGGER_ANSWER_FORMAT}"

    @property
    def completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    @property
    def batchable(self) -> bool:
        """Whether this trigger may be asked alongside others.

        ``append_answer_format: false`` means the operator is writing the whole
        prompt themselves, including how the answer should come back. A batched
        call imposes its own answer format, so honouring both is impossible and
        the explicit instruction wins: that trigger is always asked alone.
        """
        return self.batch and self.append_answer_format

    @property
    def batch_key(self) -> tuple[object, ...]:
        """What has to match for two triggers to share a call.

        The interval, because a shared call is a shared schedule. The endpoint,
        key and model, because the call goes to exactly one of them. The
        snapshot quality, because there is one frame. Everything else --
        device class, explanation length, whether the frame is published -- is
        applied per trigger after the answer comes back, so it does not
        constrain the grouping.
        """
        return (
            self.interval_minutes,
            self.base_url,
            self.api_key,
            self.model,
            self.request_timeout,
            self.high_quality_snapshot,
        )

    def selects(self, camera_id: str, camera_name: str) -> bool:
        """Whether this trigger watches the named camera.

        Matched the way camera groups are: by Protect id, by name, or by slug,
        punctuation and case ignored -- so ``Front Door``, ``front_door`` and
        the id all mean the same camera.
        """
        if self.all_cameras:
            return True
        aliases = {camera_id, slugify(camera_id), slugify(camera_name)}
        return any({spec, slugify(spec)} & aliases for spec in self.cameras)


@dataclass(frozen=True)
class DiscoveryConfig:
    """Home Assistant MQTT autodiscovery settings."""

    enabled: bool = True
    prefix: str = "homeassistant"
    node_id: str = "unifi_occupancy"


#: Arbitrary, and outside the ranges Prometheus' own exporters have claimed.
DEFAULT_METRICS_PORT = 9833

#: A token shorter than this is not a secret, it is a speed bump.
MIN_METRICS_TOKEN_CHARS = 16


@dataclass(frozen=True)
class MetricsConfig:
    """The Prometheus endpoint: off unless asked for, and never unauthenticated.

    Switching this on opens a listening socket on a process whose whole job so
    far has been to make outbound connections, so it is opt-in, and a token is
    required rather than optional: an exporter that answers anyone who asks
    hands out a camera-by-camera map of a building's movement.
    """

    enabled: bool = False
    #: Binds every interface by default so a published container port works.
    host: str = "0.0.0.0"
    port: int = DEFAULT_METRICS_PORT
    #: Compared with ``hmac.compare_digest`` against the bearer token.
    token: str | None = None
    path: str = "/metrics"


MINUTES_PER_DAY = 24 * 60

DEFAULT_CONFIG_FILE = Path("/config/config.yaml")


@dataclass(frozen=True)
class Period:
    """A named stretch of the day -- morning, afternoon, evening, night."""

    name: str
    #: Minutes after local midnight at which the period begins.
    start_minute: int


DEFAULT_PERIODS: tuple[Period, ...] = (
    Period("Night", 0),
    Period("Morning", 6 * 60),
    Period("Afternoon", 12 * 60),
    Period("Evening", 18 * 60),
)


@dataclass(frozen=True)
class PeriodBand:
    """One contiguous stretch of a single day covered by a period.

    When the earliest period starts after midnight the small hours belong to
    whichever period wraps around from the night before, so a single period can
    produce two bands.
    """

    name: str
    start_minute: int
    end_minute: int

    @property
    def minutes(self) -> int:
        return self.end_minute - self.start_minute


def period_bands(periods: Sequence[Period]) -> tuple[PeriodBand, ...]:
    """Expand period start times into the bands that tile one day."""
    if not periods:
        return ()
    ordered = sorted(periods, key=lambda period: period.start_minute)
    bands: list[PeriodBand] = []
    if ordered[0].start_minute > 0:
        bands.append(PeriodBand(ordered[-1].name, 0, ordered[0].start_minute))
    for index, period in enumerate(ordered):
        end = ordered[index + 1].start_minute if index + 1 < len(ordered) else MINUTES_PER_DAY
        if end > period.start_minute:
            bands.append(PeriodBand(period.name, period.start_minute, end))
    return tuple(bands)


@dataclass(frozen=True)
class CameraGroup:
    """Cameras covering one area, reported together."""

    name: str
    #: Camera names, slugs or Protect ids; matched case- and punctuation-insensitively.
    members: tuple[str, ...]

    @property
    def key(self) -> str:
        return slugify(self.name)


@dataclass(frozen=True)
class ReportConfig:
    """The daily occupancy sheets: what to write, where, and how to shade it."""

    enabled: bool = True
    output_dir: Path = Path("/var/www/html")
    database: Path = Path("/var/lib/unifi-occupancy/occupancy.sqlite3")
    title: str = "Occupancy"
    #: IANA zone name. Days, midnights and periods are all local to it.
    timezone: str = "UTC"
    #: Width of one bucket on the density strips.
    bucket_minutes: int = 10
    #: How often the current day's sheet is rewritten.
    refresh_minutes: int = 60
    #: Days of history to keep; 0 keeps everything.
    retention_days: int = 0
    periods: tuple[Period, ...] = DEFAULT_PERIODS
    groups: tuple[CameraGroup, ...] = ()

    @property
    def tz(self) -> tzinfo:
        return ZoneInfo(self.timezone)

    @property
    def bands(self) -> tuple[PeriodBand, ...]:
        return period_bands(self.periods)


def _yaml_mapping(value: object, where: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _yaml_str(raw: Mapping[str, Any], key: str, default: str, where: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.{key} must be a non-empty string")
    return value.strip()


def _yaml_bool(raw: Mapping[str, Any], key: str, default: bool, where: str) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key} must be true or false")
    return value


def _yaml_int(
    raw: Mapping[str, Any], key: str, default: int, where: str, *, minimum: int, maximum: int
) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}.{key} must be a whole number")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{where}.{key} must be between {minimum} and {maximum}, got {value}")
    return value


def _yaml_optional_str(
    raw: Mapping[str, Any], key: str, default: str | None, where: str
) -> str | None:
    value = raw.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.{key} must be a non-empty string")
    return value.strip()


def _yaml_float(
    raw: Mapping[str, Any], key: str, default: float, where: str, *, minimum: float
) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{where}.{key} must be a number")
    if value < minimum:
        raise ConfigError(f"{where}.{key} must be at least {minimum}, got {value}")
    return float(value)


def _yaml_path(raw: Mapping[str, Any], key: str, default: Path, where: str) -> Path:
    value = raw.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.{key} must be a path")
    return Path(value.strip())


def _time_of_day(value: object, where: str) -> int:
    """Minutes after midnight from ``"06:00"``.

    PyYAML reads an unquoted ``18:00`` as the integer 1080 -- which is already
    minutes after midnight -- so quoted and unquoted times both land here
    correctly. A bare number is therefore minutes, not hours.
    """
    if isinstance(value, bool):
        raise ConfigError(f'{where} must be a time of day like "06:00"')
    if isinstance(value, int):
        minutes = value
    elif isinstance(value, str):
        parts = value.strip().split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ConfigError(f'{where} must be a time of day like "06:00", got {value!r}')
        if int(parts[1]) > 59:
            raise ConfigError(f"{where} has more than 59 minutes: {value!r}")
        minutes = int(parts[0]) * 60 + int(parts[1])
    else:
        raise ConfigError(f'{where} must be a time of day like "06:00"')
    if not 0 <= minutes < MINUTES_PER_DAY:
        raise ConfigError(f"{where} must fall between 00:00 and 23:59, got {value!r}")
    return minutes


def _parse_periods(raw: object) -> tuple[Period, ...]:
    if raw is None:
        return DEFAULT_PERIODS
    if not isinstance(raw, list) or not raw:
        raise ConfigError("periods must be a non-empty list")
    periods: list[Period] = []
    for index, item in enumerate(raw):
        where = f"periods[{index}]"
        entry = _yaml_mapping(item, where)
        if "start" not in entry:
            raise ConfigError(f"{where} needs a start time")
        periods.append(
            Period(
                name=_yaml_str(entry, "name", "", where),
                start_minute=_time_of_day(entry["start"], f"{where}.start"),
            )
        )
    return tuple(periods)


def _parse_groups(raw: object) -> tuple[CameraGroup, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("groups must be a list")
    groups: list[CameraGroup] = []
    for index, item in enumerate(raw):
        where = f"groups[{index}]"
        entry = _yaml_mapping(item, where)
        members = entry.get("cameras")
        if isinstance(members, str):
            members = [members]
        if not isinstance(members, list) or not members:
            raise ConfigError(f"{where}.cameras must be a non-empty list of camera names")
        groups.append(
            CameraGroup(
                name=_yaml_str(entry, "name", "", where),
                members=tuple(str(member).strip() for member in members if str(member).strip()),
            )
        )
    keys = [group.key for group in groups]
    duplicate = next((key for key in keys if keys.count(key) > 1), None)
    if duplicate is not None:
        raise ConfigError(f"two groups resolve to the same name: {duplicate!r}")
    return tuple(groups)


def _parse_triggers(raw: object, defaults: VisionConfig) -> tuple[TriggerConfig, ...]:
    """Parse the ``triggers:`` list. Absent or empty means no triggers."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("triggers must be a list")
    triggers: list[TriggerConfig] = []
    for index, item in enumerate(raw):
        where = f"triggers[{index}]"
        entry = _yaml_mapping(item, where)
        name = _yaml_str(entry, "name", "", where)
        if not slugify(name) or slugify(name) == "camera":
            raise ConfigError(f"{where}.name must contain a letter or a digit")
        if "prompt" not in entry:
            raise ConfigError(f"{where} needs a prompt saying what to look for")
        base_url = _yaml_str(entry, "api_base_url", defaults.base_url, where)
        if not base_url.startswith(("http://", "https://")):
            raise ConfigError(f"{where}.api_base_url must be an http(s) URL, got {base_url!r}")
        triggers.append(
            TriggerConfig(
                name=name,
                prompt=_yaml_str(entry, "prompt", "", where),
                cameras=_parse_trigger_cameras(entry.get("cameras"), where),
                interval_minutes=_yaml_float(
                    entry,
                    "interval_minutes",
                    DEFAULT_TRIGGER_INTERVAL_MINUTES,
                    where,
                    minimum=0.1,
                ),
                enabled=_yaml_bool(entry, "enabled", True, where),
                base_url=base_url,
                api_key=_yaml_optional_str(entry, "api_key", defaults.api_key, where),
                model=_yaml_str(entry, "model", defaults.model, where),
                request_timeout=_yaml_float(
                    entry, "request_timeout", defaults.request_timeout, where, minimum=1.0
                ),
                stagger_seconds=_yaml_float(
                    entry, "stagger_seconds", defaults.stagger_seconds, where, minimum=0.0
                ),
                high_quality_snapshot=_yaml_bool(
                    entry, "high_quality_snapshot", defaults.high_quality_snapshot, where
                ),
                max_explanation_chars=_yaml_int(
                    entry,
                    "max_explanation_chars",
                    defaults.max_explanation_chars,
                    where,
                    minimum=20,
                    maximum=2_000,
                ),
                publish_snapshot=_yaml_bool(entry, "publish_snapshot", False, where),
                device_class=_yaml_optional_str(entry, "device_class", None, where),
                append_answer_format=_yaml_bool(entry, "append_answer_format", True, where),
                batch=_yaml_bool(entry, "batch", defaults.batch, where),
            )
        )
    keys = [trigger.key for trigger in triggers]
    duplicate = next((key for key in keys if keys.count(key) > 1), None)
    if duplicate is not None:
        raise ConfigError(f"two triggers resolve to the same name: {duplicate!r}")
    return tuple(triggers)


def _parse_trigger_cameras(raw: object, where: str) -> tuple[str, ...]:
    """``cameras:`` -- a list, one name, or ALL (which is also the default)."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f'{where}.cameras must be a list of camera names, or "ALL"')
    members = [str(member).strip() for member in raw if str(member).strip()]
    if any(member.lower() in ALL_CAMERAS for member in members):
        return ()
    if not members:
        raise ConfigError(f'{where}.cameras must be a list of camera names, or "ALL"')
    return tuple(members)


def _document(text: str) -> Mapping[str, Any]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config.yaml is not valid YAML: {exc}") from exc
    return _yaml_mapping(document, "config.yaml")


def parse_report_config(text: str, *, default_timezone: str = "UTC") -> ReportConfig:
    """Parse the ``reports:`` half of ``config.yaml``."""
    return _report_config(_document(text), default_timezone)


def _report_config(root: Mapping[str, Any], default_timezone: str) -> ReportConfig:
    reports = _yaml_mapping(root.get("reports"), "reports")

    timezone = _yaml_str(root, "timezone", default_timezone, "config.yaml")
    try:
        ZoneInfo(timezone)
    except (KeyError, ValueError, OSError) as exc:
        raise ConfigError(f"timezone {timezone!r} is not a known IANA zone name") from exc

    return ReportConfig(
        enabled=_yaml_bool(reports, "enabled", True, "reports"),
        output_dir=_yaml_path(reports, "output_dir", Path("/var/www/html"), "reports"),
        database=_yaml_path(
            reports,
            "database",
            Path("/var/lib/unifi-occupancy/occupancy.sqlite3"),
            "reports",
        ),
        title=_yaml_str(reports, "title", "Occupancy", "reports"),
        timezone=timezone,
        bucket_minutes=_yaml_int(
            reports, "bucket_minutes", 10, "reports", minimum=1, maximum=MINUTES_PER_DAY
        ),
        refresh_minutes=_yaml_int(
            reports, "refresh_minutes", 60, "reports", minimum=1, maximum=MINUTES_PER_DAY
        ),
        retention_days=_yaml_int(
            reports, "retention_days", 0, "reports", minimum=0, maximum=36_500
        ),
        periods=_parse_periods(root.get("periods")),
        groups=_parse_groups(root.get("groups")),
    )


@dataclass(frozen=True)
class FileConfig:
    """Everything ``config.yaml`` says: the sheets, and the custom triggers.

    ``reports`` is ``None`` only when there is no file at all, which is how the
    sheets stay switched off for anyone who upgrades without asking for them.
    """

    reports: ReportConfig | None = None
    triggers: tuple[TriggerConfig, ...] = ()


def parse_file_config(
    text: str,
    *,
    default_timezone: str = "UTC",
    vision: VisionConfig | None = None,
) -> FileConfig:
    """Parse the whole of ``config.yaml``."""
    root = _document(text)
    return FileConfig(
        reports=_report_config(root, default_timezone),
        triggers=_parse_triggers(root.get("triggers"), vision or VisionConfig()),
    )


def load_file_config(
    path: Path,
    env: Mapping[str, str] | None = None,
    vision: VisionConfig | None = None,
) -> FileConfig:
    """Read ``config.yaml``, or an empty :class:`FileConfig` if there is none."""
    env = os.environ if env is None else env
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return FileConfig()
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    return parse_file_config(text, default_timezone=_get(env, "TZ") or "UTC", vision=vision)


def load_report_config(path: Path, env: Mapping[str, str] | None = None) -> ReportConfig | None:
    """Read the ``reports:`` half of ``config.yaml``, or ``None`` if there is no file.

    No file means no sheets. That is how this stays switched off for anyone who
    upgrades without asking for it, and why the daily sheets are opt-in by
    creating the file rather than by setting yet another variable.
    """
    return load_file_config(path, env).reports


@dataclass(frozen=True)
class Config:
    protect: ProtectConfig
    mqtt: MqttConfig
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    #: Default endpoint for anything that asks a vision model a question.
    vision: VisionConfig = field(default_factory=VisionConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    log_level: str = "INFO"
    #: How long a smart detection stays "on" after the last event that reported it.
    detection_hold_seconds: float = 30.0
    #: Where to look for the YAML file describing groups, periods and sheets.
    config_file: Path = DEFAULT_CONFIG_FILE
    #: Filled in from :func:`load_report_config`; ``None`` disables the sheets.
    reports: ReportConfig | None = None
    #: Filled in from :func:`load_file_config`; empty means nothing to watch for.
    triggers: tuple[TriggerConfig, ...] = ()

    @property
    def active_triggers(self) -> tuple[TriggerConfig, ...]:
        """The triggers that are actually switched on."""
        return tuple(trigger for trigger in self.triggers if trigger.enabled)

    @property
    def trigger_groups(self) -> tuple[tuple[TriggerConfig, ...], ...]:
        """The active triggers, gathered into the calls they will share."""
        return group_triggers(self.active_triggers)


def group_triggers(
    triggers: Sequence[TriggerConfig],
) -> tuple[tuple[TriggerConfig, ...], ...]:
    """Gather triggers into the vision calls they can share.

    Triggers agreeing on :attr:`TriggerConfig.batch_key` are asked together
    about one frame: one snapshot, one request, one answer covering all of
    them. Since a vision request is mostly the image, three triggers sharing a
    call cost about what one costs rather than three.

    A trigger that opted out, or whose prompt format rules batching out, is a
    group of one and is asked exactly as it was before. So is a trigger whose
    interval, endpoint, model or snapshot quality nothing else shares.

    Order is preserved: groups come out in the order their first trigger was
    written in ``config.yaml``, and members in the order they were written.
    An automation's behaviour should not depend on dictionary ordering, and a
    log line listing a group should read the way the file does.
    """
    groups: dict[tuple[object, ...], list[TriggerConfig]] = {}
    singles: list[tuple[TriggerConfig, ...]] = []
    order: list[tuple[object, ...] | int] = []
    for trigger in triggers:
        if not trigger.batchable:
            order.append(len(singles))
            singles.append((trigger,))
            continue
        key = trigger.batch_key
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(trigger)
    out: list[tuple[TriggerConfig, ...]] = []
    for item in order:
        out.append(singles[item] if isinstance(item, int) else tuple(groups[item]))
    return tuple(out)


def _health_config(env: Mapping[str, str]) -> HealthConfig:
    """The camera health check, which is off unless explicitly switched on."""
    enabled = _get_bool(env, "CAMERA_HEALTH_ENABLED", False)
    base_url = _get(env, "CAMERA_HEALTH_API_BASE_URL", "https://api.openai.com/v1") or ""
    model = _get(env, "CAMERA_HEALTH_MODEL", "gpt-4o-mini") or ""
    prompt = _get(env, "CAMERA_HEALTH_PROMPT", DEFAULT_HEALTH_PROMPT) or ""

    # A blank model or prompt falls back to the default, as everywhere else
    # here, so only the endpoint can actually be wrong -- and only when the
    # feature is on, so half-finished settings never stop the service starting.
    if enabled and not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"CAMERA_HEALTH_API_BASE_URL must be an http(s) URL, got {base_url!r}")

    return HealthConfig(
        enabled=enabled,
        base_url=base_url,
        api_key=_get(env, "CAMERA_HEALTH_API_KEY"),
        model=model,
        prompt=prompt,
        interval_minutes=_get_positive_float(env, "CAMERA_HEALTH_INTERVAL_MINUTES", 60.0),
        stagger_seconds=_get_float(env, "CAMERA_HEALTH_STAGGER_SECONDS", 5.0),
        request_timeout=_get_positive_float(env, "CAMERA_HEALTH_REQUEST_TIMEOUT", 60.0),
        high_quality_snapshot=_get_bool(env, "CAMERA_HEALTH_HIGH_QUALITY_SNAPSHOT", False),
        max_explanation_chars=_get_int(env, "CAMERA_HEALTH_MAX_EXPLANATION_CHARS", 200),
    )


def _vision_config(env: Mapping[str, str], health: HealthConfig) -> VisionConfig:
    """The default vision endpoint: ``VISION_*``, else the health check's own."""
    # Not validated here: these are only defaults, and a half-finished setting
    # left behind by someone trying the health check out must not stop the
    # service from starting. A trigger that actually uses the endpoint checks
    # it as it is parsed, where the feature is unambiguously on.
    base_url = _get(env, "VISION_API_BASE_URL", health.base_url) or health.base_url
    return VisionConfig(
        base_url=base_url,
        api_key=_get(env, "VISION_API_KEY", health.api_key),
        model=_get(env, "VISION_MODEL", health.model) or health.model,
        request_timeout=_get_positive_float(env, "VISION_REQUEST_TIMEOUT", health.request_timeout),
        stagger_seconds=_get_float(env, "VISION_STAGGER_SECONDS", health.stagger_seconds),
        high_quality_snapshot=_get_bool(
            env, "VISION_HIGH_QUALITY_SNAPSHOT", health.high_quality_snapshot
        ),
        max_explanation_chars=_get_int(
            env, "VISION_MAX_EXPLANATION_CHARS", health.max_explanation_chars
        ),
        batch=_get_bool(env, "VISION_BATCH_TRIGGERS", True),
    )


def _metrics_config(env: Mapping[str, str]) -> MetricsConfig:
    """The Prometheus endpoint, which has to be asked for and has to have a token."""
    enabled = _get_bool(env, "METRICS_ENABLED", False)
    token = _get(env, "METRICS_TOKEN")
    path = _get(env, "METRICS_PATH", "/metrics") or "/metrics"
    port = _get_int(env, "METRICS_PORT", DEFAULT_METRICS_PORT)

    if not path.startswith("/"):
        path = f"/{path}"
    path = path.rstrip("/") or "/metrics"

    # Only checked when the endpoint is actually on, so a half-written .env
    # never stops the bridge itself from starting.
    if enabled:
        if token is None:
            raise ConfigError(
                "METRICS_TOKEN is required when METRICS_ENABLED is set; "
                "generate one with `openssl rand -hex 32`"
            )
        if len(token) < MIN_METRICS_TOKEN_CHARS:
            raise ConfigError(
                f"METRICS_TOKEN must be at least {MIN_METRICS_TOKEN_CHARS} characters"
            )
        if not 1 <= port <= 65535:
            raise ConfigError(f"METRICS_PORT must be a port number, got {port}")

    return MetricsConfig(
        enabled=enabled,
        host=_get(env, "METRICS_HOST", "0.0.0.0") or "0.0.0.0",
        port=port,
        token=token,
        path=path,
    )


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a :class:`Config` from ``env`` (defaults to ``os.environ``)."""
    env = os.environ if env is None else env

    protect = ProtectConfig(
        host=_require(env, "UNIFI_HOST"),
        api_key=_require(env, "UNIFI_API_KEY"),
        port=_get_int(env, "UNIFI_PORT", 443),
        verify_ssl=_get_bool(env, "UNIFI_VERIFY_SSL", False),
        poll_interval=_get_positive_float(env, "UNIFI_POLL_INTERVAL", 5.0),
        request_timeout=_get_positive_float(env, "UNIFI_REQUEST_TIMEOUT", 10.0),
        use_websocket=_get_bool(env, "UNIFI_USE_WEBSOCKET", True),
    )

    tls = _get_bool(env, "MQTT_TLS", False)
    mqtt = MqttConfig(
        host=_require(env, "MQTT_HOST"),
        port=_get_int(env, "MQTT_PORT", DEFAULT_MQTT_TLS_PORT if tls else DEFAULT_MQTT_PORT),
        username=_get(env, "MQTT_USERNAME"),
        password=_get(env, "MQTT_PASSWORD"),
        client_id=_get(env, "MQTT_CLIENT_ID", "unifi-occupancy") or "unifi-occupancy",
        topic_prefix=(_get(env, "MQTT_TOPIC_PREFIX", "unifi-occupancy") or "").rstrip("/")
        or "unifi-occupancy",
        tls=tls,
        keepalive=_get_int(env, "MQTT_KEEPALIVE", 60),
        retain=_get_bool(env, "MQTT_RETAIN", True),
        qos=_get_int(env, "MQTT_QOS", 0),
    )
    if mqtt.qos not in (0, 1, 2):
        raise ConfigError(f"MQTT_QOS must be 0, 1 or 2, got {mqtt.qos}")

    discovery = DiscoveryConfig(
        enabled=_get_bool(env, "HA_DISCOVERY_ENABLED", True),
        prefix=(_get(env, "HA_DISCOVERY_PREFIX", "homeassistant") or "").rstrip("/")
        or "homeassistant",
        node_id=_get(env, "HA_NODE_ID", "unifi_occupancy") or "unifi_occupancy",
    )

    health = _health_config(env)
    vision = _vision_config(env, health)
    metrics = _metrics_config(env)

    return Config(
        protect=protect,
        mqtt=mqtt,
        discovery=discovery,
        health=health,
        vision=vision,
        metrics=metrics,
        log_level=(_get(env, "LOG_LEVEL", "INFO") or "INFO").upper(),
        detection_hold_seconds=_get_positive_float(env, "DETECTION_HOLD_SECONDS", 30.0),
        config_file=Path(_get(env, "CONFIG_FILE") or DEFAULT_CONFIG_FILE),
    )
