"""Center configuration from environment variables (12-factor)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env("JL_DATABASE_URL", "sqlite+aiosqlite:///./center.db"))
    junctions_file: str = field(default_factory=lambda: _env("JL_JUNCTIONS", "configs/junctions.local.json"))
    jwt_secret: str = field(default_factory=lambda: _env("JL_JWT_SECRET", secrets.token_hex(32)))
    jwt_hours: int = field(default_factory=lambda: int(_env("JL_JWT_HOURS", "8")))
    admin_user: str = field(default_factory=lambda: _env("JL_ADMIN_USER", "operator"))
    admin_password: str = field(default_factory=lambda: _env("JL_ADMIN_PASSWORD", "operator123"))
    viewer_user: str = field(default_factory=lambda: _env("JL_VIEWER_USER", "viewer"))
    viewer_password: str = field(default_factory=lambda: _env("JL_VIEWER_PASSWORD", "viewer123"))
    snmp_community: str = field(default_factory=lambda: _env("JL_SNMP_COMMUNITY", "public"))
    snmp_write_community: str = field(default_factory=lambda: _env("JL_SNMP_WRITE_COMMUNITY", "private"))
    poll_interval: float = field(default_factory=lambda: float(_env("JL_POLL_INTERVAL", "1.0")))
    jlp_port: int = field(default_factory=lambda: int(_env("JL_JLP_PORT", "15050")))
    trap_port: int = field(default_factory=lambda: int(_env("JL_TRAP_PORT", "1162")))
    cors_origins: list[str] = field(
        default_factory=lambda: _env("JL_CORS", "http://localhost:5173").split(","))
    static_dir: str = field(default_factory=lambda: _env("JL_STATIC_DIR", "web/dist"))
