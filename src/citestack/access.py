"""Credential-bound tenants and transactional quotas; no caller-selected tenant IDs."""

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from citestack.config import Settings


class TenantDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    key_hashes: list[str] = Field(min_length=1, max_length=4)
    index_path: Path | None = None
    rate_per_minute: int = Field(default=30, ge=1, le=10_000)
    burst: int = Field(default=5, ge=1, le=1000)
    daily_limit: int = Field(default=1000, ge=1, le=10_000_000)

    @model_validator(mode="after")
    def valid_hashes(self):
        for digest in self.key_hashes:
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("Keys must be SHA-256 digests of high-entropy API keys")
        return self


@dataclass(frozen=True)
class Tenant:
    id: str
    index_path: Path | None
    rate_per_minute: int
    burst: int
    daily_limit: int


class TenantRegistry:
    def __init__(self, settings: Settings):
        self.by_hash: dict[str, Tenant] = {}
        self.tenants: dict[str, Tenant] = {}
        self.anonymous = (
            settings.environment == "development"
            and not settings.api_key
            and not settings.previous_api_key
        )
        if settings.tenant_config:
            self.anonymous = False
            try:
                with settings.tenant_config.open("rb") as source:
                    raw = source.read(262_145)
                if len(raw) > 262_144:
                    raise ValueError("Registry too large")
                payload = json.loads(raw)
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"tenants"}
                    or not isinstance(payload["tenants"], list)
                    or not 1 <= len(payload["tenants"]) <= 128
                ):
                    raise ValueError("Invalid registry")
                definitions = [
                    TenantDefinition.model_validate(value) for value in payload["tenants"]
                ]
            except (OSError, ValueError, TypeError, KeyError):
                raise ValueError("Cannot load a valid tenant registry") from None
            paths: set[Path] = set()
            for definition in definitions:
                path = definition.index_path
                if path:
                    if not path.is_absolute():
                        path = settings.tenant_config.parent / path
                    path = path.resolve()
                    if path in paths:
                        raise ValueError("Tenants must have distinct index snapshots")
                    paths.add(path)
                if definition.id in self.tenants:
                    raise ValueError("Duplicate tenant ID")
                tenant = Tenant(
                    definition.id,
                    path,
                    definition.rate_per_minute,
                    definition.burst,
                    definition.daily_limit,
                )
                self.tenants[tenant.id] = tenant
                for digest in definition.key_hashes:
                    if digest in self.by_hash:
                        raise ValueError("An API key cannot belong to multiple entries")
                    self.by_hash[digest] = tenant
        else:
            tenant = Tenant(
                "default",
                settings.index_path.resolve(),
                settings.rate_limit_per_minute,
                settings.rate_limit_burst,
                settings.daily_request_limit,
            )
            self.tenants[tenant.id] = tenant
            for key in (settings.api_key, settings.previous_api_key):
                if key:
                    self.by_hash[hashlib.sha256(key.get_secret_value().encode()).hexdigest()] = (
                        tenant
                    )

    def authenticate(self, credential: bytes | None) -> Tenant | None:
        if self.anonymous:
            return self.tenants["default"]
        if credential is None or len(credential) > 512:
            return None
        return self.by_hash.get(hashlib.sha256(credential).hexdigest())


class QuotaExceeded(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after


class QuotaUnavailable(Exception):
    pass


class QuotaStore:
    """Atomic admission shared across processes on one host; survives restarts."""

    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = path, clock
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=1)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS quota (
                tenant TEXT PRIMARY KEY, tokens REAL NOT NULL, updated REAL NOT NULL,
                day INTEGER NOT NULL, used INTEGER NOT NULL)""")

    def admit(self, tenant: Tenant):
        now = self.clock()
        day = int(now // 86400)
        try:
            with closing(sqlite3.connect(self.path, timeout=0.2)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT tokens, updated, day, used FROM quota WHERE tenant=?", (tenant.id,)
                ).fetchone()
                if row is None:
                    tokens, updated, quota_day, used = float(tenant.burst), now, day, 0
                else:
                    tokens, updated, quota_day, used = row
                    tokens = min(
                        tenant.burst, tokens + max(0, now - updated) * tenant.rate_per_minute / 60
                    )
                    if day > quota_day:
                        quota_day, used = day, 0
                if used >= tenant.daily_limit:
                    raise QuotaExceeded(max(1, int((quota_day + 1) * 86400 - now)))
                if tokens < 1:
                    raise QuotaExceeded(max(1, int((1 - tokens) * 60 / tenant.rate_per_minute) + 1))
                db.execute(
                    "INSERT OR REPLACE INTO quota VALUES (?, ?, ?, ?, ?)",
                    (tenant.id, tokens - 1, max(now, updated), quota_day, used + 1),
                )
        except sqlite3.Error:
            raise QuotaUnavailable from None
