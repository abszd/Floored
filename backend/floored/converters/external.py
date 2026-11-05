"""Adapter for the standalone ``floorplan_converter`` package in ``converter/``."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from .base import ConversionResult


def _import_package() -> Any:
    try:
        import floorplan_converter  # type: ignore[import-not-found]

        return floorplan_converter
    except ImportError:
        # Running from a checkout without `pip install -e converter/`.
        repo_converter = Path(__file__).resolve().parents[3] / "converter"
        if repo_converter.is_dir() and str(repo_converter) not in sys.path:
            sys.path.insert(0, str(repo_converter))
        import floorplan_converter  # type: ignore[import-not-found]

        return floorplan_converter


log = logging.getLogger(__name__)


def read_floor(image: bytes) -> dict[str, Any] | None:
    """Which floor a plan shows, read from its own text by the converter, if it can.

    ``{"level", "label", "confidence", "text", "source", "building"?}`` or
    ``None`` (nothing printed, or a converter without the floor reader).
    """
    try:
        fn = getattr(_import_package(), "read_floor", None)
    except Exception:
        return None
    if fn is None:
        return None
    try:
        return fn(image)
    except Exception as exc:  # a bad page shouldn't stop a crawl
        log.warning("reading the floor from a plan failed: %s", exc)
        return None


def is_available() -> bool:
    try:
        _import_package()
        return True
    except Exception:
        return False


class ExternalConverter:
    name = "floorplan_converter"

    def __init__(self, **options: Any) -> None:
        self._pkg = _import_package()
        self._options = options

    def convert(self, image: bytes) -> ConversionResult:
        result = self._pkg.convert(image, debug=True, **self._options)
        data = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        version = getattr(self._pkg, "__version__", None)
        if version:
            data.setdefault("meta", {})["converter_version"] = version
        return ConversionResult.from_dict(data)
