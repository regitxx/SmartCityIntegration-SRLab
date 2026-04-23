"""Session store — SQLite (WAL) + msgspec.

Design notes:
- One row per session; state blob is msgspec-encoded SessionSlots.
- PII scrubber at ingress removes phone numbers and obvious ID patterns before
  the state blob is written. The original message text never enters the blob.
- `meta.forget_me` wipes the row.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

import aiosqlite

from smcity.settings import get_settings
from smcity.slots import SessionSlots

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_PHONE = re.compile(r"(?:\+852[\s-]?|\b)\d{4}[\s-]?\d{4}\b")
_HKID = re.compile(r"\b[A-Z]{1,2}\d{6}\(?[A-Z0-9]\)?\b")

# Session IDs are exposed on the WebSocket URL path; constrain to an opaque
# ASCII token so a stray path segment or UTF-8 gremlin can't reach SQLite.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# 0o600 — owner read/write only. Matches what gpg / ssh expect for secret files.
_OWNER_ONLY = stat.S_IRUSR | stat.S_IWUSR


def is_valid_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID_RE.fullmatch(session_id))


def redact_pii(text: str) -> str:
    if not get_settings().pii_redact_at_ingress:
        return text
    return _HKID.sub("[HKID]", _PHONE.sub("[PHONE]", text))


class InvalidSessionIdError(ValueError):
    """Raised when a caller passes a session_id that fails the regex guard."""


def _assert_session_id(session_id: str) -> None:
    if not is_valid_session_id(session_id):
        raise InvalidSessionIdError(f"invalid session_id (expected {_SESSION_ID_RE.pattern})")


def _tighten_perms(path: Path) -> None:
    """Best-effort `chmod 600` so the session DB + its WAL/SHM shards aren't
    world-readable. No-op on platforms where chmod semantics don't apply."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        target = path.with_name(path.name + suffix)
        if not target.exists():
            continue
        try:
            target.chmod(_OWNER_ONLY)
        except OSError:
            # Windows / networked FS may reject; fall through silently.
            return


class SessionStore:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)

    async def _init(self, db: aiosqlite.Connection) -> None:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute(_SCHEMA)
        await db.commit()
        _tighten_perms(self._path)

    async def load(self, session_id: str) -> SessionSlots:
        _assert_session_id(session_id)
        async with aiosqlite.connect(self._path) as db:
            await self._init(db)
            async with db.execute(
                "SELECT state_json FROM sessions WHERE session_id = ?", (session_id,)
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return SessionSlots(session_id=session_id)
        return SessionSlots.model_validate_json(row[0])

    async def save(self, slots: SessionSlots) -> None:
        _assert_session_id(slots.session_id)
        slots.touch()
        payload = slots.model_dump_json()
        async with aiosqlite.connect(self._path) as db:
            await self._init(db)
            await db.execute(
                "INSERT INTO sessions(session_id, state_json, updated_at) "
                "VALUES(?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "state_json=excluded.state_json, updated_at=excluded.updated_at",
                (slots.session_id, payload, slots.updated_at.isoformat()),
            )
            await db.commit()

    async def forget(self, session_id: str) -> None:
        _assert_session_id(session_id)
        async with aiosqlite.connect(self._path) as db:
            await self._init(db)
            await db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            await db.commit()
