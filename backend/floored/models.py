"""Persistent records.

Geometry is stored as GeoJSON in JSON columns: the data set is small (a few
thousand buildings at most) and this keeps SQLite as the only dependency.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Building(Base):
    __tablename__ = "buildings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String(300))
    address: Mapped[str | None] = mapped_column(String(500))
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    # GeoJSON Polygon/MultiPolygon of the building outline, if known.
    footprint: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    osm_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # Other names the scraper has seen for this building ("MU", "Memorial Union").
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    floors: Mapped[list[Floor]] = relationship(
        back_populates="building", cascade="all, delete-orphan", order_by="Floor.level"
    )


class Floor(Base):
    __tablename__ = "floors"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    building_id: Mapped[str] = mapped_column(ForeignKey("buildings.id", ondelete="CASCADE"), index=True)
    # Numeric level for ordering (-1 basement, 0 ground, 1 first, ...).
    level: Mapped[float] = mapped_column(Float, default=1)
    name: Mapped[str] = mapped_column(String(200))
    # pending -> processing -> placed | needs_review | error
    status: Mapped[str] = mapped_column(String(32), default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(String(2000))
    image_path: Mapped[str | None] = mapped_column(String(500))
    image_width: Mapped[int | None] = mapped_column(Integer)
    image_height: Mapped[int | None] = mapped_column(Integer)
    converter: Mapped[str | None] = mapped_column(String(64))
    # Converter output, in image pixels (see converters.base.ConversionResult).
    conversion: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Where the image sits on the map (see placement.transform.Placement).
    placement: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # How well the plan outline matched the building footprint (IoU, 0..1).
    fit_score: Mapped[float | None] = mapped_column(Float)
    # Rooms in lon/lat, regenerated whenever conversion or placement changes.
    geojson: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    building: Mapped[Building] = relationship(back_populates="floors")


class Source(Base):
    """A website the scraper crawls for floor plans."""

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String(200))
    seed_urls: Mapped[list[str]] = mapped_column(JSON)
    allowed_domains: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Appended to building names when geocoding, e.g. "Madison, WI".
    location_hint: Mapped[str | None] = mapped_column(String(300))
    max_pages: Mapped[int] = mapped_column(Integer, default=200)
    max_depth: Mapped[int] = mapped_column(Integer, default=3)
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    candidates: Mapped[list[Candidate]] = relationship(back_populates="source", cascade="all, delete-orphan")


class Candidate(Base):
    """A floor plan image the scraper found, waiting to be matched and processed."""

    __tablename__ = "candidates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    asset_url: Mapped[str] = mapped_column(String(2000), index=True)
    # PDFs yield one candidate per page.
    page_index: Mapped[int] = mapped_column(Integer, default=0)
    page_url: Mapped[str | None] = mapped_column(String(2000))
    page_title: Mapped[str | None] = mapped_column(String(500))
    context: Mapped[str | None] = mapped_column(Text)
    image_path: Mapped[str | None] = mapped_column(String(500))
    plan_score: Mapped[float] = mapped_column(Float, default=0)
    building_guess: Mapped[str | None] = mapped_column(String(300))
    address_guess: Mapped[str | None] = mapped_column(String(500))
    level_guess: Mapped[float | None] = mapped_column(Float)
    level_label: Mapped[str | None] = mapped_column(String(100))
    building_id: Mapped[str | None] = mapped_column(ForeignKey("buildings.id", ondelete="SET NULL"))
    match_confidence: Mapped[float] = mapped_column(Float, default=0)
    match_reason: Mapped[str | None] = mapped_column(String(500))
    # new -> matched | unmatched -> approved -> processed | rejected | error
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    floor_id: Mapped[str | None] = mapped_column(ForeignKey("floors.id", ondelete="SET NULL"))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    source: Mapped[Source | None] = relationship(back_populates="candidates")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    kind: Mapped[str] = mapped_column(String(64))
    # queued -> running -> done | failed
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
