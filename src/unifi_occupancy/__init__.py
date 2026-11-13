"""Publish UniFi Protect detection state to MQTT without exposing camera video."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("unifi-occupancy")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
