from __future__ import annotations

from pathlib import Path

import pytest

from unifi_occupancy.config import (
    DEFAULT_CONFIG_FILE,
    DEFAULT_PERIODS,
    ConfigError,
    Period,
    load_config,
    load_report_config,
    parse_report_config,
    period_bands,
)

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


# ---------------------------------------------------------------------------
# config.yaml: groups, periods and the daily sheets
# ---------------------------------------------------------------------------

FULL_YAML = """
timezone: America/New_York

reports:
  enabled: true
  output_dir: /srv/occupancy
  database: /data/history.sqlite3
  title: Who went past
  bucket_minutes: 15
  refresh_minutes: 30
  retention_days: 90

periods:
  - name: Night
    start: "00:00"
  - name: Morning
    start: "06:30"
  - name: Afternoon
    start: 12:00
  - name: Evening
    start: "18:00"

groups:
  - name: Front of house
    cameras:
      - Front Door
      - Driveway
  - name: Back yard
    cameras: Back Yard
"""


def test_a_full_config_file_parses() -> None:
    reports = parse_report_config(FULL_YAML)

    assert reports.enabled is True
    assert reports.timezone == "America/New_York"
    assert reports.output_dir == Path("/srv/occupancy")
    assert reports.database == Path("/data/history.sqlite3")
    assert reports.title == "Who went past"
    assert reports.bucket_minutes == 15
    assert reports.refresh_minutes == 30
    assert reports.retention_days == 90
    assert [period.name for period in reports.periods] == [
        "Night",
        "Morning",
        "Afternoon",
        "Evening",
    ]
    assert [group.name for group in reports.groups] == ["Front of house", "Back yard"]
    assert reports.groups[0].members == ("Front Door", "Driveway")
    assert reports.groups[0].key == "front_of_house"


def test_an_empty_config_file_still_means_sheets() -> None:
    """The file's existence is the switch; its contents only tune things."""
    reports = parse_report_config("")

    assert reports.enabled is True
    assert reports.groups == ()
    assert reports.periods == DEFAULT_PERIODS
    assert reports.output_dir == Path("/var/www/html")


def test_a_missing_file_means_no_sheets(tmp_path: Path) -> None:
    assert load_report_config(tmp_path / "absent.yaml") is None


def test_the_file_is_read_when_it_is_there(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(FULL_YAML)

    reports = load_report_config(path)

    assert reports is not None
    assert reports.title == "Who went past"


def test_the_timezone_falls_back_to_the_environment(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("reports:\n  title: Occupancy\n")

    assert load_report_config(path, {"TZ": "Europe/Berlin"}).timezone == "Europe/Berlin"  # type: ignore[union-attr]
    assert load_report_config(path, {}).timezone == "UTC"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("written", "minutes"),
    [('"06:00"', 360), ('"00:00"', 0), ('"23:59"', 1439), ("18:00", 1080), ('"6:05"', 365)],
)
def test_times_of_day_parse_quoted_or_not(written: str, minutes: int) -> None:
    """PyYAML reads an unquoted ``18:00`` as 1080 -- already minutes past midnight."""
    reports = parse_report_config(f"periods:\n  - name: X\n    start: {written}\n")

    assert reports.periods[0].start_minute == minutes


@pytest.mark.parametrize(
    "body",
    [
        "timezone: Mars/Olympus\n",
        "periods: []\n",
        "periods:\n  - name: X\n",
        'periods:\n  - start: "06:00"\n',
        'periods:\n  - name: X\n    start: "25:00"\n',
        'periods:\n  - name: X\n    start: "06:99"\n',
        "periods:\n  - name: X\n    start: true\n",
        "groups:\n  - name: X\n",
        "groups:\n  - name: X\n    cameras: []\n",
        "groups:\n  - cameras: [A]\n",
        "groups:\n  - name: Front\n    cameras: [A]\n  - name: FRONT\n    cameras: [B]\n",
        "reports:\n  enabled: yes please\n",
        "reports:\n  bucket_minutes: 0\n",
        "reports:\n  bucket_minutes: huge\n",
        "reports:\n  retention_days: -1\n",
        'reports:\n  title: ""\n',
        "reports: 3\n",
        "- not\n- a\n- mapping\n",
    ],
)
def test_a_bad_config_file_is_rejected_with_an_explanation(body: str) -> None:
    with pytest.raises(ConfigError):
        parse_report_config(body)


def test_invalid_yaml_is_rejected() -> None:
    with pytest.raises(ConfigError, match="not valid YAML"):
        parse_report_config("groups: [unclosed\n")


def test_periods_tile_the_whole_day() -> None:
    bands = period_bands(DEFAULT_PERIODS)

    assert [(band.name, band.start_minute, band.end_minute) for band in bands] == [
        ("Night", 0, 360),
        ("Morning", 360, 720),
        ("Afternoon", 720, 1080),
        ("Evening", 1080, 1440),
    ]
    assert sum(band.minutes for band in bands) == 1440


def test_a_period_starting_after_midnight_wraps_around() -> None:
    """Night running 22:00-06:00 has to cover both ends of the day."""
    bands = period_bands((Period("Day", 6 * 60), Period("Night", 22 * 60)))

    assert [(band.name, band.start_minute, band.end_minute) for band in bands] == [
        ("Night", 0, 360),
        ("Day", 360, 1320),
        ("Night", 1320, 1440),
    ]
    assert sum(band.minutes for band in bands) == 1440


def test_periods_sharing_a_start_do_not_produce_empty_bands() -> None:
    bands = period_bands((Period("A", 0), Period("B", 0), Period("C", 720)))

    assert all(band.minutes > 0 for band in bands)
    assert sum(band.minutes for band in bands) == 1440


def test_no_periods_means_no_shading() -> None:
    assert period_bands(()) == ()


def test_the_config_file_path_comes_from_the_environment() -> None:
    assert load_config(MINIMAL).config_file == DEFAULT_CONFIG_FILE
    assert load_config({**MINIMAL, "CONFIG_FILE": "/etc/occ.yaml"}).config_file == Path(
        "/etc/occ.yaml"
    )
