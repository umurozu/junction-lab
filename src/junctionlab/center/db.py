"""Relational schema (SQLite for dev, PostgreSQL in Docker / production)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="viewer")  # viewer | operator


class Junction(Base):
    __tablename__ = "junctions"
    id: Mapped[int] = mapped_column(primary_key=True)          # == device id
    name: Mapped[str] = mapped_column(String(128))
    preset: Mapped[str] = mapped_column(String(32))
    lat: Mapped[float]
    lon: Mapped[float]
    host: Mapped[str] = mapped_column(String(128))
    snmp_port: Mapped[int] = mapped_column(Integer, default=161)
    jlp_port: Mapped[int] = mapped_column(Integer, default=6000)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    junction_id: Mapped[int] = mapped_column(ForeignKey("junctions.id"))
    kind: Mapped[str] = mapped_column(String(32))        # alarm | alarm_cleared | mode | comm | command
    severity: Mapped[str] = mapped_column(String(16))    # info | warning | critical
    source: Mapped[str] = mapped_column(String(16))      # snmp-trap | snmp-poll | jlp | api
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)

    __table_args__ = (Index("ix_events_junction_ts", "junction_id", "ts"), Index("ix_events_ts", "ts"))


class Count(Base):
    """Detector volume per phase per reporting interval (from JLP COUNTS frames)."""
    __tablename__ = "counts"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    junction_id: Mapped[int] = mapped_column(ForeignKey("junctions.id"))
    phase: Mapped[int]
    volume: Mapped[int]

    __table_args__ = (Index("ix_counts_junction_ts", "junction_id", "ts"),)


class Database:
    def __init__(self, url: str) -> None:
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.session: async_sessionmaker[AsyncSession] = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()
