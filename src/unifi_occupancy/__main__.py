"""Console entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from .app import run
from .config import (
    DEFAULT_CONFIG_FILE,
    ConfigError,
    ReportConfig,
    load_config,
    load_report_config,
)
from .report import SheetWriter
from .store import EventStore

logger = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="unifi-occupancy",
        description="Publish UniFi Protect detection state to MQTT and write occupancy sheets.",
    )
    parser.add_argument(
        "--rebuild-sheets",
        action="store_true",
        help="re-render every daily sheet from recorded history, then exit",
    )
    return parser.parse_args(argv)


def rebuild_sheets() -> int:
    """Re-render the sheets without needing a controller or a broker.

    Deliberately does not go through :func:`load_config`: regenerating pages
    from history has nothing to do with Protect or MQTT, and should not demand
    their credentials.
    """
    path = Path(os.environ.get("CONFIG_FILE") or DEFAULT_CONFIG_FILE)
    try:
        reports: ReportConfig | None = load_report_config(path)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if reports is None:
        print(f"no config file at {path}; nothing to rebuild", file=sys.stderr)
        return 2

    with EventStore(reports.database) as store:
        days = SheetWriter(store, reports).rebuild()
    logger.info("rebuilt %d day(s) into %s", days, reports.output_dir)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    if args.rebuild_sheets:
        configure_logging((os.environ.get("LOG_LEVEL") or "INFO").upper())
        return rebuild_sheets()

    try:
        config = load_config()
        config = replace(config, reports=load_report_config(config.config_file))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(config.log_level)
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        logger.info("shutting down")
        return 0
    except Exception as exc:  # noqa: BLE001 - top level guard
        logger.error("fatal: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
