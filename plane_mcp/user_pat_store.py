"""Per-user Plane PAT store for the WorkOS OAuth path.

Maps a verified Google/WorkOS identity (email) to that user's own Plane API
token, so the OAuth path can call Plane *as the user* (Plane RBAC then filters
by workspace membership). Tokens are encrypted at rest with Fernet; only the
ciphertext is written to SQLite.

The store is a single SQLite file on a mounted volume (so it's covered by the
daily EBS snapshot). The container runs replicas=1, so a single writer is fine.
Blocking SQLite calls are run via ``asyncio.to_thread`` to stay async-friendly.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from fastmcp.utilities.logging import get_logger

logger = get_logger(__name__)

DEFAULT_DB_PATH = "/data/user_pat.db"


class UserPatStore:
    """Encrypted email -> Plane PAT store backed by SQLite."""

    def __init__(self, *, db_path: str, enc_key: str) -> None:
        if not enc_key:
            raise ValueError("MCP_PAT_ENC_KEY is not set (generate with Fernet.generate_key())")
        self._db_path = db_path
        self._fernet = Fernet(enc_key.encode() if isinstance(enc_key, str) else enc_key)
        self._init_db()

    @staticmethod
    def _norm(email: str) -> str:
        return (email or "").strip().lower()

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None -> autocommit; each op opens its own short-lived conn.
        conn = sqlite3.connect(self._db_path, timeout=10, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        directory = os.path.dirname(self._db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_pat (
                    email         TEXT PRIMARY KEY,
                    pat_encrypted BLOB NOT NULL,
                    updated_at    TEXT NOT NULL
                )
                """
            )
        logger.info("UserPatStore ready at %s", self._db_path)

    # --- sync core (run in a thread) -------------------------------------

    def _get_sync(self, email: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT pat_encrypted FROM user_pat WHERE email = ?", (self._norm(email),)
            ).fetchone()
        if not row:
            return None
        try:
            return self._fernet.decrypt(row[0]).decode()
        except InvalidToken:
            # Wrong/rotated encryption key — treat as not registered, never crash auth.
            logger.error("UserPatStore: failed to decrypt PAT for a user (key rotated?)")
            return None

    def _set_sync(self, email: str, pat: str) -> None:
        token = self._fernet.encrypt(pat.encode())
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO user_pat (email, pat_encrypted, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET pat_encrypted = excluded.pat_encrypted,
                                                 updated_at = excluded.updated_at
                """,
                (self._norm(email), token, now),
            )

    def _delete_sync(self, email: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM user_pat WHERE email = ?", (self._norm(email),))

    def _has_sync(self, email: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM user_pat WHERE email = ?", (self._norm(email),)
            ).fetchone()
        return row is not None

    # --- async API -------------------------------------------------------

    async def get_pat(self, email: str) -> str | None:
        return await asyncio.to_thread(self._get_sync, email)

    async def set_pat(self, email: str, pat: str) -> None:
        await asyncio.to_thread(self._set_sync, email, pat)

    async def delete_pat(self, email: str) -> None:
        await asyncio.to_thread(self._delete_sync, email)

    async def has_pat(self, email: str) -> bool:
        return await asyncio.to_thread(self._has_sync, email)


_store: UserPatStore | None = None


def get_user_pat_store() -> UserPatStore:
    """Return the process-wide store, building it from env on first use.

    Env:
      MCP_PAT_ENC_KEY   Fernet key (required)
      MCP_PAT_DB_PATH   SQLite path (default /data/user_pat.db)
    """
    global _store
    if _store is None:
        _store = UserPatStore(
            db_path=os.getenv("MCP_PAT_DB_PATH", DEFAULT_DB_PATH),
            enc_key=os.getenv("MCP_PAT_ENC_KEY", ""),
        )
    return _store
