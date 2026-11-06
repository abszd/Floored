"""Persistent records.

Geometry is stored as GeoJSON in JSON columns: the data set is small (a few
thousand buildings at most) and this keeps SQLite as the only dependency.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
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
    # Set when the level was guessed because the plan's own one was missing or taken.
    level_note: Mapped[str | None] = mapped_column(String(300))
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
    # Earlier versions of the plan for undo, newest last (see pipeline.snapshot).
    history: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    # What the converter and the automatic fit produced, before any hand edits:
    # {conversion, placement, fit_iou, converter, image_path, image_size, at}.
    # Kept to learn from the fixes people make (see floored.feedback).
    machine: Mapped[dict[str, Any] | None] = mapped_column(JSON)
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
    drafts: Mapped[list[Draft]] = relationship(back_populates="source", cascade="all, delete-orphan")


class Draft(Base):
    """A building that found plans seem to belong to, before the user says where it is.

    Plans from one source with the same building name (or the same address,
    or the same PDF) share a draft, so a building is located once rather
    than once per floor. Confirming a draft creates or picks the real
    building and turns its plans into floors.
    """

    __tablename__ = "drafts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    # What plans are grouped by: the normalised name, address or file.
    key: Mapped[str] = mapped_column(String(600), index=True)
    name: Mapped[str] = mapped_column(String(300))
    address: Mapped[str | None] = mapped_column(String(500))
    # pending -> done (the geocoder has had its say)
    match_state: Mapped[str] = mapped_column(String(16), default="pending")
    # Where we think it is: {confidence, reason, building_id | place, chosen}.
    suggestion: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # The real building, once the user (or a confident match) confirmed it.
    building_id: Mapped[str | None] = mapped_column(ForeignKey("buildings.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    source: Mapped[Source | None] = relationship(back_populates="drafts")
    building: Mapped[Building | None] = relationship()
    candidates: Mapped[list[Candidate]] = relationship(back_populates="draft")


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
    # Why the level was changed when its building was confirmed (another plan had it).
    level_note: Mapped[str | None] = mapped_column(String(300))
    building_id: Mapped[str | None] = mapped_column(ForeignKey("buildings.id", ondelete="SET NULL"))
    match_confidence: Mapped[float] = mapped_column(Float, default=0)
    match_reason: Mapped[str | None] = mapped_column(String(500))
    # new -> matched | unmatched -> approved -> processed | rejected | error
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    floor_id: Mapped[str | None] = mapped_column(ForeignKey("floors.id", ondelete="SET NULL"))
    error: Mapped[str | None] = mapped_column(Text)
    draft_id: Mapped[str | None] = mapped_column(ForeignKey("drafts.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    source: Mapped[Source | None] = relationship(back_populates="candidates")
    draft: Mapped[Draft | None] = relationship(back_populates="candidates")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    kind: Mapped[str] = mapped_column(String(64))
    # queued -> running -> done | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    # Live numbers for the activity panel (pages read, plans found, ...).
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    stop_requested: Mapped[bool | None] = mapped_column(Boolean, default=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Correction(Base):
    """One fix a person made to what the machine produced, for improving the converter and matching."""

    __tablename__ = "corrections"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    floor_id: Mapped[str | None] = mapped_column(ForeignKey("floors.id", ondelete="CASCADE"), index=True)
    building_id: Mapped[str | None] = mapped_column(String(32), index=True)
    # crop, split_floor, merge_rooms, split_room, add_room, delete_room, delete_lines, set_kind,
    # rename, move, autofit, line_up, set_level, rename_floor, copy_floor, undo, reconvert, match
    op: Mapped[str] = mapped_column(String(32))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
