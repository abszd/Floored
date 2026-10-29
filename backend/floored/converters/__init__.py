"""Converter selection. See ``base.py`` for the output contract."""

from __future__ import annotations

from .base import ConversionResult, Converter, Room

__all__ = ["ConversionResult", "Converter", "Room", "get_converter", "available_converters"]


def available_converters() -> list[str]:
    from . import external

    names = ["legacy"]
    if external.is_available():
        names.insert(0, "floorplan_converter")
    return names


def get_converter(name: str | None = None) -> Converter:
    from ..config import get_settings
    from . import external
    from .legacy import LegacyConverter

    name = name or get_settings().converter
    if name in ("auto", "floorplan_converter", "external"):
        if external.is_available():
            return external.ExternalConverter()
        if name != "auto":
            raise RuntimeError("floorplan_converter is not installed (pip install -e converter/)")
    return LegacyConverter()
