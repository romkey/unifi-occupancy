"""Building the sheets: group cameras, do the arithmetic, write the files.

One day is one file, ``YYYY-MM-DD.html``, rewritten every
``reports.refresh_minutes`` while that day is the current one. ``index.html``
lists every day there has ever been a sheet for, with per-group event counts.

Each refresh renders *today and yesterday*. That is what makes the midnight
rollover fall out for free: the first refresh after local midnight finalises the
day that just ended and starts the new one, and nothing has to remember whether
it already did so.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path

from .config import MINUTES_PER_DAY, ReportConfig
from .models import DETECTION_KEYS, Camera, slugify
from .render import (
    ALWAYS_RENDERED,
    DaySheet,
    IndexCell,
    IndexEntry,
    Row,
    Section,
    Segment,
    Track,
    day_filename,
    render_day,
    render_index,
)
from .store import EventStore, StoredEvent, Summary

logger = logging.getLogger(__name__)

INDEX_FILENAME = "index.html"

#: A camera that has gone away keeps its place on the sheet; it is still part of
#: the history. This is the roster entry used for it.
_Roster = dict[str, str]


def day_window(day: date, zone: tzinfo) -> tuple[datetime, datetime]:
    """The half-open UTC interval covering one local day.

    Built from local midnight to local midnight rather than by adding 24 hours,
    so the day a clock change falls on is 23 or 25 hours long, as it really was.
    """
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def next_refresh(now: datetime, reports: ReportConfig) -> datetime:
    """The next wall-clock boundary at which the sheets should be rewritten.

    Aligned to local time so that an hourly refresh lands on the hour, and so
    that the day's final refresh lands on local midnight.
    """
    zone = reports.tz
    step = reports.refresh_minutes
    local = now.astimezone(zone)
    midnight = datetime.combine(local.date(), time.min, tzinfo=zone)
    elapsed = (local - midnight).total_seconds() / 60.0
    slot = int(elapsed // step) + 1
    if slot * step >= MINUTES_PER_DAY:
        candidate = datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=zone)
    else:
        candidate = midnight + timedelta(minutes=slot * step)
    while candidate <= now:
        candidate += timedelta(minutes=step)
    return candidate


def _aliases(camera_id: str, camera_name: str) -> frozenset[str]:
    return frozenset({camera_id, slugify(camera_id), slugify(camera_name)})


def _merge(intervals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """Collapse overlapping intervals, so a group is not counted twice over."""
    ordered = sorted((start, end) for start, end in intervals if end > start)
    merged: list[tuple[float, float]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


@dataclass(frozen=True)
class _Window:
    """One day, as the numbers needed to place things on a timeline."""

    day: date
    start: datetime
    end: datetime
    buckets: int

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    def offset(self, moment: datetime) -> float:
        """Seconds from the start of the day, clamped to it."""
        return min(self.seconds, max(0.0, (moment - self.start).total_seconds()))

    def fraction(self, moment: datetime) -> float:
        return self.offset(moment) / self.seconds

    def bucket_of(self, moment: datetime) -> int:
        return min(self.buckets - 1, max(0, int(self.fraction(moment) * self.buckets)))


class SheetWriter:
    """Renders and writes the daily sheets and the index."""

    def __init__(
        self,
        store: EventStore,
        reports: ReportConfig,
        cameras: Callable[[], Sequence[Camera]] = lambda: (),
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._store = store
        self._reports = reports
        self._cameras = cameras
        self._clock = clock
        self._warned_unmatched: set[str] = set()

    # -- grouping ---------------------------------------------------------

    def _roster(self, events: Sequence[StoredEvent]) -> _Roster:
        """Camera id -> display name, from the live inventory and the history.

        Both are needed: a camera added an hour ago has no events yet, and a
        camera removed last week still has a day's worth.
        """
        roster: _Roster = {event.camera_id: event.camera_name for event in events}
        for camera in self._cameras():
            roster[camera.id] = camera.name
        return roster

    def _sections_for(self, roster: _Roster) -> list[tuple[str, str, list[str]]]:
        """``(key, name, camera ids)`` for every group, then every loose camera."""
        assigned: set[str] = set()
        sections: list[tuple[str, str, list[str]]] = []
        by_name = sorted(roster.items(), key=lambda item: item[1].lower())
        for group in self._reports.groups:
            # Listed order wins, so a group reads the way it was written down.
            members: list[str] = []
            for spec in group.members:
                tokens = {spec, slugify(spec)}
                matches = [
                    camera_id for camera_id, name in by_name if _aliases(camera_id, name) & tokens
                ]
                if not matches and spec not in self._warned_unmatched:
                    logger.warning("group %r lists %r, which matches no camera", group.name, spec)
                    self._warned_unmatched.add(spec)
                members.extend(camera_id for camera_id in matches if camera_id not in members)
            assigned.update(members)
            sections.append((group.key, group.name, members))

        loose = sorted(
            ((cid, name) for cid, name in roster.items() if cid not in assigned),
            key=lambda item: item[1].lower(),
        )
        sections.extend((slugify(name) or slugify(cid), name, [cid]) for cid, name in loose)
        return sections

    # -- arithmetic -------------------------------------------------------

    def _track(
        self, kind: str, camera_ids: Sequence[str], events: Sequence[StoredEvent], window: _Window
    ) -> Track:
        wanted = set(camera_ids)
        mine = [event for event in events if event.kind == kind and event.camera_id in wanted]
        live_until = min(self._clock(), window.end)
        # Worked in seconds from the start of the day, not in fractions of it:
        # a total built out of fractions is a total that is slightly wrong.
        spans = _merge(
            (window.offset(event.started_at), window.offset(event.ended_at or live_until))
            for event in mine
        )
        buckets = [0] * window.buckets
        started_today = 0
        for event in mine:
            if window.start <= event.started_at < window.end:
                buckets[window.bucket_of(event.started_at)] += 1
                started_today += 1
        segments = tuple(
            Segment(
                start / window.seconds,
                end / window.seconds,
                self._segment_title(start, end, window),
            )
            for start, end in spans
        )
        return Track(
            kind=kind,
            segments=segments,
            buckets=tuple(buckets),
            events=started_today,
            seconds=sum(end - start for start, end in spans),
        )

    def _segment_title(self, start: float, end: float, window: _Window) -> str:
        """Hover text for one segment; ``start`` and ``end`` are seconds into the day."""
        zone = self._reports.tz
        began = (window.start + timedelta(seconds=start)).astimezone(zone)
        ended = (window.start + timedelta(seconds=end)).astimezone(zone)
        return f"{began:%H:%M:%S} \u2013 {ended:%H:%M:%S} ({round(end - start)}s)"

    def _build(self, day: date) -> tuple[tuple[Section, ...], int, list[Summary]]:
        window_start, window_end = day_window(day, self._reports.tz)
        window = _Window(
            day=day,
            start=window_start,
            end=window_end,
            buckets=max(
                1,
                round(
                    (window_end - window_start).total_seconds()
                    / (self._reports.bucket_minutes * 60)
                ),
            ),
        )
        events = self._store.events_between(window_start, window_end)
        roster = self._roster(events)

        sections: list[Section] = []
        summaries: list[Summary] = []
        peak = 0

        for key, name, camera_ids in self._sections_for(roster):
            members = set(camera_ids)
            fired = {event.kind for event in events if event.camera_id in members}
            kinds = tuple(
                kind for kind in DETECTION_KEYS if kind in ALWAYS_RENDERED or kind in fired
            )
            rows: list[Row] = []
            total_tracks = {kind: self._track(kind, camera_ids, events, window) for kind in kinds}
            if len(camera_ids) > 1:
                rows.append(
                    Row(
                        label="All cameras",
                        tracks=tuple(total_tracks[kind] for kind in kinds),
                        aggregate=True,
                        note=f"{len(camera_ids)} cameras",
                    )
                )
            for camera_id in camera_ids:
                rows.append(
                    Row(
                        label=roster[camera_id] if len(camera_ids) > 1 else "",
                        tracks=tuple(
                            self._track(kind, [camera_id], events, window) for kind in kinds
                        ),
                    )
                )
            if not camera_ids:
                rows.append(Row(label="", tracks=tuple(total_tracks[kind] for kind in kinds)))

            for row in rows:
                for track in row.tracks:
                    peak = max(peak, max(track.buckets, default=0))

            sections.append(
                Section(
                    key=key,
                    name=name,
                    rows=tuple(rows),
                    totals={kind: total_tracks[kind].events for kind in kinds},
                )
            )
            summaries.extend(
                Summary(
                    group_key=key,
                    group_name=name,
                    kind=kind,
                    events=total_tracks[kind].events,
                    seconds=total_tracks[kind].seconds,
                )
                for kind in kinds
            )

        return tuple(sections), peak, summaries

    # -- writing ----------------------------------------------------------

    def _write(self, filename: str, html: str) -> Path:
        """Write a page, via a temporary file, so nothing serves a half-page."""
        directory = self._reports.output_dir
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / filename
        scratch = directory / f".{filename}.tmp"
        scratch.write_text(html, encoding="utf-8")
        scratch.replace(target)
        return target

    def write_day(self, day: date, *, save: bool = True) -> Path:
        sections, peak, summaries = self._build(day)
        today = self._clock().astimezone(self._reports.tz).date()
        sheet = DaySheet(
            day=day,
            sections=sections,
            generated_at=self._clock().astimezone(self._reports.tz),
            peak_bucket=peak,
            previous_day=day - timedelta(days=1),
            next_day=day + timedelta(days=1) if day < today else None,
        )
        if save:
            self._store.save_summaries(day.isoformat(), summaries)
        return self._write(day_filename(day), render_day(sheet, self._reports))

    def write_index(self) -> Path:
        stored = self._store.summaries()
        ordered_keys: list[str] = [group.key for group in self._reports.groups]
        names: dict[str, str] = {group.key: group.name for group in self._reports.groups}
        extra: dict[str, str] = {}
        for rows in stored.values():
            for row in rows:
                if row.group_key not in names:
                    extra[row.group_key] = row.group_name
        for key in sorted(extra, key=lambda item: extra[item].lower()):
            ordered_keys.append(key)
            names[key] = extra[key]

        entries: list[IndexEntry] = []
        for day_text in sorted(stored, reverse=True):
            cells: dict[str, IndexCell] = {}
            for row in stored[day_text]:
                cell = cells.get(row.group_key)
                counts = dict(cell.counts) if cell else {}
                counts[row.kind] = row.events
                cells[row.group_key] = IndexCell(
                    group_key=row.group_key,
                    group_name=row.group_name,
                    counts=counts,
                )
            entries.append(
                IndexEntry(
                    day=date.fromisoformat(day_text),
                    href=day_filename(date.fromisoformat(day_text)),
                    cells=cells,
                )
            )

        columns = [(key, names[key]) for key in ordered_keys]
        html = render_index(
            entries, columns, self._reports, self._clock().astimezone(self._reports.tz)
        )
        return self._write(INDEX_FILENAME, html)

    def refresh(self, now: datetime | None = None) -> None:
        """Rewrite today's sheet, finalise yesterday's, and refresh the index."""
        moment = now or self._clock()
        today = moment.astimezone(self._reports.tz).date()
        yesterday = today - timedelta(days=1)

        self.write_day(today)
        if self._has_history(yesterday):
            self.write_day(yesterday)
        self.write_index()
        self._prune(today)

    def _has_history(self, day: date) -> bool:
        if day.isoformat() in self._store.summarised_days():
            return True
        start, end = day_window(day, self._reports.tz)
        return bool(self._store.events_between(start, end))

    def _prune(self, today: date) -> None:
        if not self._reports.retention_days:
            return
        cutoff, _ = day_window(
            today - timedelta(days=self._reports.retention_days), self._reports.tz
        )
        dropped = self._store.prune(cutoff)
        if dropped:
            logger.info("pruned %d event(s) older than %s", dropped, cutoff.date())

    def rebuild(self) -> int:
        """Re-render every day the store knows about. Used by ``--rebuild``."""
        zone = self._reports.tz
        days: set[date] = {date.fromisoformat(day) for day in self._store.summarised_days()}
        span = self._store.span()
        if span is not None:
            cursor = span[0].astimezone(zone).date()
            last = span[1].astimezone(zone).date()
            while cursor <= last:
                days.add(cursor)
                cursor += timedelta(days=1)
        for day in sorted(days):
            self.write_day(day)
        self.write_index()
        return len(days)

    async def run_forever(self) -> None:
        """Refresh on every boundary until cancelled.

        Runs on the event loop rather than in a thread: a day's worth of events
        is a few hundred rows, and keeping it here means the one SQLite
        connection is only ever touched from one place.
        """
        while True:
            try:
                self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - sheets must not stop the bridge
                logger.warning("could not write occupancy sheets: %s", exc)
            wake = next_refresh(self._clock(), self._reports)
            await asyncio.sleep(max(1.0, (wake - self._clock()).total_seconds()))
