"""The event history behind the daily sheets.

Everything else in this service is stateless -- it reports what is happening
now and forgets. A sheet showing how busy an area was yesterday needs the
opposite, so detections are written here as rows with a start and an end.

SQLite, synchronously, on purpose: a handful of writes a minute and one
day-sized read an hour is nowhere near enough work to justify a connection pool
or a thread, and keeping it synchronous means the schema and the queries are the
whole story.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY,
    camera_id    TEXT    NOT NULL,
    camera_name  TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    started_at   REAL    NOT NULL,
    ended_at     REAL
);
CREATE INDEX IF NOT EXISTS events_started_at ON events (started_at);
CREATE INDEX IF NOT EXISTS events_open ON events (camera_id, kind) WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS day_summaries (
    day        TEXT    NOT NULL,
    group_key  TEXT    NOT NULL,
    group_name TEXT    NOT NULL,
    kind       TEXT    NOT NULL,
    events     INTEGER NOT NULL,
    seconds    REAL    NOT NULL,
    PRIMARY KEY (day, group_key, kind)
);
"""


@dataclass(frozen=True)
class StoredEvent:
    """One detection, from the moment it fired to the moment it lapsed."""

    camera_id: str
    camera_name: str
    kind: str
    started_at: datetime
    #: ``None`` only for a detection that is still live.
    ended_at: datetime | None

    def duration(self, until: datetime) -> float:
        """Seconds this event covers, treating a live event as running to ``until``."""
        end = self.ended_at or until
        return max(0.0, (end - self.started_at).total_seconds())


@dataclass(frozen=True)
class Summary:
    """Event count and total duration for one group, day and detection kind."""

    group_key: str
    group_name: str
    kind: str
    events: int
    seconds: float


def _epoch(moment: datetime) -> float:
    return moment.timestamp()


def _moment(value: float | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(value, tz=UTC)


class EventStore:
    """The detection log, plus the per-day summaries the index page is built from."""

    def __init__(self, path: Path) -> None:
        self._path = path
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def __enter__(self) -> EventStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    # -- writing ----------------------------------------------------------

    def start_event(self, camera_id: str, camera_name: str, kind: str, at: datetime) -> None:
        """Open an event, unless one of the same kind is already open."""
        open_row = self._db.execute(
            "SELECT id FROM events WHERE camera_id = ? AND kind = ? AND ended_at IS NULL",
            (camera_id, kind),
        ).fetchone()
        if open_row is not None:
            return
        self._db.execute(
            "INSERT INTO events (camera_id, camera_name, kind, started_at) VALUES (?, ?, ?, ?)",
            (camera_id, camera_name, kind, _epoch(at)),
        )

    def end_event(self, camera_id: str, kind: str, at: datetime) -> None:
        """Close the open event of this kind, if there is one."""
        self._db.execute(
            """
            UPDATE events SET ended_at = ?
             WHERE camera_id = ? AND kind = ? AND ended_at IS NULL AND started_at <= ?
            """,
            (_epoch(at), camera_id, kind, _epoch(at)),
        )

    def close_open_events(self, hold_seconds: float) -> int:
        """Close events left open by a crash or a restart.

        An event the process never saw end would otherwise stretch across the
        whole outage; the detection would in fact have decayed ``hold_seconds``
        after its last sighting, so that is where it is cut.
        """
        cursor = self._db.execute(
            "UPDATE events SET ended_at = started_at + ? WHERE ended_at IS NULL",
            (hold_seconds,),
        )
        closed = cursor.rowcount or 0
        if closed:
            logger.info("closed %d event(s) left open by a previous run", closed)
        return closed

    def save_summaries(self, day: str, summaries: Iterable[Summary]) -> None:
        """Replace the stored summary for ``day``.

        The index is built from these rows rather than from the events, so that
        it still reports honest numbers for days whose events have been pruned.
        """
        rows = list(summaries)
        self._db.execute("BEGIN")
        try:
            self._db.execute("DELETE FROM day_summaries WHERE day = ?", (day,))
            self._db.executemany(
                """
                INSERT INTO day_summaries (day, group_key, group_name, kind, events, seconds)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (day, row.group_key, row.group_name, row.kind, row.events, row.seconds)
                    for row in rows
                ],
            )
        except sqlite3.Error:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def prune(self, before: datetime) -> int:
        """Drop events that started before ``before``; summaries are kept."""
        cursor = self._db.execute("DELETE FROM events WHERE started_at < ?", (_epoch(before),))
        return cursor.rowcount or 0

    # -- reading ----------------------------------------------------------

    def events_between(self, start: datetime, end: datetime) -> list[StoredEvent]:
        """Every event overlapping ``[start, end)``, oldest first.

        An event that began before the window still belongs to it if it was
        still running when the window opened -- a detection that started at
        23:58 is part of the next morning too.
        """
        rows = self._db.execute(
            """
            SELECT camera_id, camera_name, kind, started_at, ended_at
              FROM events
             WHERE started_at < ? AND (ended_at IS NULL OR ended_at > ?)
             ORDER BY started_at
            """,
            (_epoch(end), _epoch(start)),
        ).fetchall()
        return [
            StoredEvent(
                camera_id=str(row["camera_id"]),
                camera_name=str(row["camera_name"]),
                kind=str(row["kind"]),
                started_at=datetime.fromtimestamp(row["started_at"], tz=UTC),
                ended_at=_moment(row["ended_at"]),
            )
            for row in rows
        ]

    def span(self) -> tuple[datetime, datetime] | None:
        """Oldest and newest event start, or ``None`` when there are no events."""
        row = self._db.execute(
            "SELECT MIN(started_at) AS lo, MAX(started_at) AS hi FROM events"
        ).fetchone()
        if row is None or row["lo"] is None:
            return None
        return (
            datetime.fromtimestamp(row["lo"], tz=UTC),
            datetime.fromtimestamp(row["hi"], tz=UTC),
        )

    def summarised_days(self) -> list[str]:
        """Days that have a stored summary, newest first."""
        rows = self._db.execute(
            "SELECT DISTINCT day FROM day_summaries ORDER BY day DESC"
        ).fetchall()
        return [str(row["day"]) for row in rows]

    def summaries(self, days: Sequence[str] | None = None) -> dict[str, list[Summary]]:
        """Stored summaries keyed by day."""
        if days is None:
            rows: Iterator[sqlite3.Row] = iter(
                self._db.execute(
                    "SELECT * FROM day_summaries ORDER BY day DESC, group_name, kind"
                ).fetchall()
            )
        else:
            if not days:
                return {}
            placeholders = ",".join("?" for _ in days)
            rows = iter(
                self._db.execute(
                    f"SELECT * FROM day_summaries WHERE day IN ({placeholders})"
                    " ORDER BY day DESC, group_name, kind",
                    tuple(days),
                ).fetchall()
            )
        collected: dict[str, list[Summary]] = {}
        for row in rows:
            collected.setdefault(str(row["day"]), []).append(
                Summary(
                    group_key=str(row["group_key"]),
                    group_name=str(row["group_name"]),
                    kind=str(row["kind"]),
                    events=int(row["events"]),
                    seconds=float(row["seconds"]),
                )
            )
        return collected
