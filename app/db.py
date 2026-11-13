"""DB モデル(MariaDB想定。テスト時は SQLite でも動く)。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import settings

LongText = Text().with_variant(mysql.MEDIUMTEXT(), "mysql").with_variant(mysql.MEDIUMTEXT(), "mariadb")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Host(Base):
    __tablename__ = "hosts"

    id: Mapped[int] = mapped_column(primary_key=True)
    vmid: Mapped[int] = mapped_column(Integer, unique=True)
    name: Mapped[str] = mapped_column(String(100))
    ip: Mapped[str] = mapped_column(String(64))
    port: Mapped[int] = mapped_column(Integer, default=22)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    os_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    pkg_manager: Mapped[str | None] = mapped_column(String(20), nullable=True)
    has_docker: Mapped[bool] = mapped_column(Boolean, default=False)
    reboot_required: Mapped[bool] = mapped_column(Boolean, default=False)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_scan_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    packages: Mapped[list["Package"]] = relationship(back_populates="host", cascade="all, delete-orphan")
    services: Mapped[list["Service"]] = relationship(back_populates="host", cascade="all, delete-orphan")
    containers: Mapped[list["Container"]] = relationship(back_populates="host", cascade="all, delete-orphan")


class Package(Base):
    __tablename__ = "packages"

    id: Mapped[int] = mapped_column(primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    installed: Mapped[str | None] = mapped_column(String(200), nullable=True)
    candidate: Mapped[str | None] = mapped_column(String(200), nullable=True)
    suite: Mapped[str | None] = mapped_column(String(200), nullable=True)
    upgradable: Mapped[bool] = mapped_column(Boolean, default=False)
    security: Mapped[bool] = mapped_column(Boolean, default=False)
    group: Mapped[str | None] = mapped_column("grp", String(100), nullable=True, index=True)

    host: Mapped[Host] = relationship(back_populates="packages")


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), index=True)
    unit: Mapped[str] = mapped_column(String(150))
    state: Mapped[str] = mapped_column(String(40))
    group: Mapped[str | None] = mapped_column("grp", String(100), nullable=True)

    host: Mapped[Host] = relationship(back_populates="services")


class Container(Base):
    __tablename__ = "containers"

    id: Mapped[int] = mapped_column(primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    image: Mapped[str] = mapped_column(String(400))
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    image_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    latest_image_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    update_available: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    compose_project: Mapped[str | None] = mapped_column(String(200), nullable=True)
    compose_service: Mapped[str | None] = mapped_column(String(200), nullable=True)
    compose_dir: Mapped[str | None] = mapped_column(String(500), nullable=True)
    compose_files: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    host: Mapped[Host] = relationship(back_populates="containers")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))       # scan / pkg / full / docker
    title: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued/running/success/partial/failed
    requested_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    log: Mapped[str | None] = mapped_column(LongText, nullable=True)


_engine_kwargs = {"pool_pre_ping": True}
if settings.database_url.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    _engine_kwargs["pool_recycle"] = 3600

engine = create_engine(settings.database_url, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
