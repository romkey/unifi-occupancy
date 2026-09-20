"""Environment-driven configuration.

Every knob the application has is read from the process environment so the
container can be configured entirely through ``docker compose``/``--env-file``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

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


@dataclass(frozen=True)
class DiscoveryConfig:
    """Home Assistant MQTT autodiscovery settings."""

    enabled: bool = True
    prefix: str = "homeassistant"
    node_id: str = "unifi_occupancy"


@dataclass(frozen=True)
class Config:
    protect: ProtectConfig
    mqtt: MqttConfig
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    log_level: str = "INFO"
    #: How long a smart detection stays "on" after the last event that reported it.
    detection_hold_seconds: float = 30.0


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

    return Config(
        protect=protect,
        mqtt=mqtt,
        discovery=discovery,
        log_level=(_get(env, "LOG_LEVEL", "INFO") or "INFO").upper(),
        detection_hold_seconds=_get_positive_float(env, "DETECTION_HOLD_SECONDS", 30.0),
    )
