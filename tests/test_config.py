from __future__ import annotations

import pytest

from unifi_occupancy.config import ConfigError, load_config

MINIMAL = {
    "UNIFI_HOST": "protect.example",
    "UNIFI_API_KEY": "secret",
    "MQTT_HOST": "broker.example",
}


def test_minimal_environment_loads_with_defaults() -> None:
    config = load_config(MINIMAL)

    assert config.protect.host == "protect.example"
    assert config.protect.api_key == "secret"
    assert config.protect.port == 443
    assert config.protect.verify_ssl is False
    assert config.mqtt.port == 1883
    assert config.mqtt.topic_prefix == "unifi-occupancy"
    assert config.discovery.enabled is True
    assert config.discovery.prefix == "homeassistant"
    assert config.log_level == "INFO"


@pytest.mark.parametrize("missing", ["UNIFI_HOST", "UNIFI_API_KEY", "MQTT_HOST"])
def test_required_variables(missing: str) -> None:
    env = {key: value for key, value in MINIMAL.items() if key != missing}

    with pytest.raises(ConfigError, match=missing):
        load_config(env)


def test_blank_value_counts_as_missing() -> None:
    with pytest.raises(ConfigError, match="UNIFI_API_KEY"):
        load_config({**MINIMAL, "UNIFI_API_KEY": "   "})


def test_tls_changes_the_default_mqtt_port() -> None:
    assert load_config({**MINIMAL, "MQTT_TLS": "true"}).mqtt.port == 8883
    assert load_config({**MINIMAL, "MQTT_TLS": "true", "MQTT_PORT": "1884"}).mqtt.port == 1884


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("true", True), ("YES", True), ("on", True), ("0", False), ("off", False)],
)
def test_boolean_parsing(value: str, expected: bool) -> None:
    assert load_config({**MINIMAL, "UNIFI_VERIFY_SSL": value}).protect.verify_ssl is expected


def test_boolean_rejects_nonsense() -> None:
    with pytest.raises(ConfigError, match="boolean"):
        load_config({**MINIMAL, "UNIFI_VERIFY_SSL": "maybe"})


def test_poll_interval_must_be_positive() -> None:
    with pytest.raises(ConfigError, match="greater than zero"):
        load_config({**MINIMAL, "UNIFI_POLL_INTERVAL": "0"})


def test_qos_is_validated() -> None:
    with pytest.raises(ConfigError, match="MQTT_QOS"):
        load_config({**MINIMAL, "MQTT_QOS": "3"})


def test_trailing_slashes_are_trimmed() -> None:
    config = load_config(
        {**MINIMAL, "MQTT_TOPIC_PREFIX": "sensors/unifi/", "HA_DISCOVERY_PREFIX": "ha/"}
    )

    assert config.mqtt.topic_prefix == "sensors/unifi"
    assert config.discovery.prefix == "ha"
    assert config.mqtt.availability_topic == "sensors/unifi/status"


def test_urls_are_built_from_host_and_port() -> None:
    config = load_config({**MINIMAL, "UNIFI_PORT": "8443"})

    assert config.protect.base_url == "https://protect.example:8443/proxy/protect/integration/v1"
    assert config.protect.websocket_url.startswith("wss://protect.example:8443/")
    assert config.protect.websocket_url.endswith("/subscribe/events")
