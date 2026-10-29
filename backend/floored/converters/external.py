"""Adapter for the standalone ``floorplan_converter`` package in ``converter/``."""

from __future__ import annotations

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
