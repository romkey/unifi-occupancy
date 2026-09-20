"""Console entry point."""

from __future__ import annotations

import asyncio
import logging
import sys

from .app import run
from .config import ConfigError, load_config


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(config.log_level)
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("shutting down")
        return 0
    except Exception as exc:  # noqa: BLE001 - top level guard
        logging.getLogger(__name__).error("fatal: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
