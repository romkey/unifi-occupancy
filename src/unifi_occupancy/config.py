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
class DiscoveryConfig:
    """Home Assistant MQTT autodiscovery settings."""

    enabled: bool = True
    prefix: str = "homeassistant"
    node_id: str = "unifi_occupancy"


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


def parse_report_config(text: str, *, default_timezone: str = "UTC") -> ReportConfig:
    """Parse the contents of ``config.yaml``."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config.yaml is not valid YAML: {exc}") from exc

    root = _yaml_mapping(document, "config.yaml")
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


def load_report_config(path: Path, env: Mapping[str, str] | None = None) -> ReportConfig | None:
    """Read ``config.yaml``, or ``None`` when there is no such file.

    No file means no sheets. That is how this stays switched off for anyone who
    upgrades without asking for it, and why the daily sheets are opt-in by
    creating the file rather than by setting yet another variable.
    """
    env = os.environ if env is None else env
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    return parse_report_config(text, default_timezone=_get(env, "TZ") or "UTC")


@dataclass(frozen=True)
class Config:
    protect: ProtectConfig
    mqtt: MqttConfig
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    log_level: str = "INFO"
    #: How long a smart detection stays "on" after the last event that reported it.
    detection_hold_seconds: float = 30.0
    #: Where to look for the YAML file describing groups, periods and sheets.
    config_file: Path = DEFAULT_CONFIG_FILE
    #: Filled in from :func:`load_report_config`; ``None`` disables the sheets.
    reports: ReportConfig | None = None


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

    return Config(
        protect=protect,
        mqtt=mqtt,
        discovery=discovery,
        health=health,
        log_level=(_get(env, "LOG_LEVEL", "INFO") or "INFO").upper(),
        detection_hold_seconds=_get_positive_float(env, "DETECTION_HOLD_SECONDS", 30.0),
        config_file=Path(_get(env, "CONFIG_FILE") or DEFAULT_CONFIG_FILE),
    )
