"""``--rebuild-sheets``: re-rendering the pages from history alone."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from unifi_occupancy.__main__ import rebuild_sheets
from unifi_occupancy.models import trigger_kind
from unifi_occupancy.store import EventStore

CONFIG = """
timezone: UTC
reports:
  output_dir: {out}
  database: {db}
triggers:
  - name: Fire or smoke
    prompt: Is there a fire?
  - name: Gate left open
    prompt: Is the gate open?
"""


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    out = tmp_path / "www"
    database = tmp_path / "history.sqlite3"
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG.format(out=out, db=database), encoding="utf-8")
    monkeypatch.setenv("CONFIG_FILE", str(path))
    with EventStore(database) as store:
        now = datetime.now(tz=UTC)
        store.record_camera("cam-1", "Front Door", now)
        store.start_event("cam-1", "Front Door", trigger_kind("fire_or_smoke"), now)
        store.end_event("cam-1", trigger_kind("fire_or_smoke"), now)
    return out


def _today(out: Path) -> str:
    day = datetime.now(tz=UTC).date().isoformat()
    return (out / f"{day}.html").read_text(encoding="utf-8")


def test_a_rebuild_names_the_trigger_lanes_from_the_config_file(configured: Path) -> None:
    """Without the triggers, every lane would be spelled out of its slug."""
    assert rebuild_sheets() == 0

    assert "<b>Fire or smoke</b>" in _today(configured)


def test_a_rebuild_honours_the_environment_the_service_runs_with(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``record`` decides whether a lane is drawn, so the default has to reach here."""
    assert rebuild_sheets() == 0
    # Recording: the gate has never fired and still gets its lane, the way
    # motion does on a quiet day.
    assert "<b>Gate left open</b>" in _today(configured)

    monkeypatch.setenv("VISION_RECORD_TRIGGERS", "false")
    assert rebuild_sheets() == 0

    page = _today(configured)
    # Not recording: nothing can ever arrive in that lane, so it is not drawn.
    assert "<b>Gate left open</b>" not in page
    # The fire trigger keeps the day it was recorded on, name and all.
    assert "<b>Fire or smoke</b>" in page


def test_a_rebuild_without_a_config_file_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "absent.yaml"))

    assert rebuild_sheets() == 2
