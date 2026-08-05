"""ORM models. Mirrors migrations/001_init.sql — the SQL file is authoritative."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import get_settings

DIM = get_settings().embedding_dim


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Profile(Base):
    __tablename__ = "profiles"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("users.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    base_resume_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(DIM), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    facts: Mapped[list[Fact]] = relationship(back_populates="profile")
    __table_args__ = (UniqueConstraint("user_id", "version"),)


class Fact(Base):
    """One verified claim. Nothing reaches a resume that isn't traceable to a row here."""

    __tablename__ = "facts"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("profiles.id"))
    type: Mapped[str] = mapped_column(Text, nullable=False)
    org: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[str | None] = mapped_column(Text)
    end_date: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    skills: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(DIM), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    profile: Mapped[Profile] = relationship(back_populates="facts")


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    ats_type: Mapped[str] = mapped_column(Text, nullable=False)
    board_token: Mapped[str] = mapped_column(Text, nullable=False)
    careers_url: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("ats_type", "board_token"),)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("companies.id"))
    ats_job_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    apply_url: Mapped[str] = mapped_column(Text, nullable=False)
    remote: Mapped[bool] = mapped_column(Boolean, default=False)
    canonical_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    posted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(DIM), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Match(Base):
    __tablename__ = "matches"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("jobs.id"))
    profile_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("profiles.id"))
    score: Mapped[float] = mapped_column(Float, nullable=False)
    similarity: Mapped[float | None] = mapped_column(Float)
    reasons_json: Mapped[list[str]] = mapped_column(JSONB, default=list)
    blockers_json: Mapped[list[str]] = mapped_column(JSONB, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("job_id", "profile_id"),)


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Application(Base):
    __tablename__ = "applications"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("users.id"))
    job_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("jobs.id"))
    state: Mapped[str] = mapped_column(Text, default="discovered")
    resume_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("artifacts.id")
    )
    cover_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("artifacts.id")
    )
    tailored_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    needs_input_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (UniqueConstraint("user_id", "job_id"),)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("applications.id")
    )
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, default="running")
    cost_cents: Mapped[float] = mapped_column(Float, default=0.0)


class RunEvent(Base):
    """Append-only. Never updated in place."""

    __tablename__ = "run_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("runs.id"))
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    level: Mapped[str] = mapped_column(Text, default="info")
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class FieldMapCache(Base):
    """One row per (ats_type, field_signature). Pay the LLM once, not per application."""

    __tablename__ = "field_map_cache"
    ats_type: Mapped[str] = mapped_column(Text, primary_key=True)
    signature: Mapped[str] = mapped_column(Text, primary_key=True)
    canonical: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Credential(Base):
    __tablename__ = "credentials"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("users.id"))
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    kms_key_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("user_id", "provider"),)
