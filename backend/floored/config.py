"""Runtime settings, read from environment variables (prefix ``FLOORED_``)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"FLOORED_{name}", default)


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")).resolve())
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", ""))
    # Which converter turns an image into rooms: "auto" uses the external
    # converter package when it can be imported and falls back to "legacy".
    converter: str = field(default_factory=lambda: _env("CONVERTER", "auto"))
    nominatim_url: str = field(default_factory=lambda: _env("NOMINATIM_URL", "https://nominatim.openstreetmap.org"))
    overpass_url: str = field(default_factory=lambda: _env("OVERPASS_URL", "https://overpass-api.de/api/interpreter"))
    # Nominatim and most sites require an identifying User-Agent with contact info.
    user_agent: str = field(default_factory=lambda: _env("USER_AGENT", "FlooredBot/0.1 (+https://github.com/abszd/Floored)"))
    # Candidates whose building match is at least this confident are processed
    # without waiting for review.
    auto_approve_confidence: float = field(default_factory=lambda: float(_env("AUTO_APPROVE", "0.85")))
    crawl_delay_s: float = field(default_factory=lambda: float(_env("CRAWL_DELAY", "1.0")))
    workers: int = field(default_factory=lambda: int(_env("WORKERS", "2")))
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(o.strip() for o in _env("CORS_ORIGINS", "*").split(",") if o.strip())
    )

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'floored.db'}"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.media_dir.mkdir(parents=True, exist_ok=True)
        _settings.cache_dir.mkdir(parents=True, exist_ok=True)
    return _settings


def override_settings(settings: Settings) -> None:
    """Replace the process-wide settings (used by tests and the CLI)."""
    global _settings
    _settings = settings
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
