"""Tests for the per-user Plane PAT store (encryption, normalization, lifecycle).

Async methods are driven with ``asyncio.run`` to avoid a pytest-asyncio dependency
(the suite is otherwise synchronous).
"""

import asyncio
import sqlite3

import pytest
from cryptography.fernet import Fernet

from plane_mcp.user_pat_store import UserPatStore


def _store(tmp_path, key: str | None = None) -> UserPatStore:
    return UserPatStore(db_path=str(tmp_path / "pat.db"), enc_key=key or Fernet.generate_key().decode())


def test_round_trip_and_normalization(tmp_path):
    s = _store(tmp_path)
    asyncio.run(s.set_pat("Ryo@Slogin.io", "plane_api_x"))
    assert asyncio.run(s.get_pat("ryo@slogin.io")) == "plane_api_x"  # case-normalized
    assert asyncio.run(s.has_pat("  RYO@slogin.io  ")) is True  # trimmed + lowered
    assert asyncio.run(s.get_pat("nobody@slogin.io")) is None


def test_update_overwrites(tmp_path):
    s = _store(tmp_path)
    asyncio.run(s.set_pat("a@slogin.io", "old"))
    asyncio.run(s.set_pat("a@slogin.io", "new"))
    assert asyncio.run(s.get_pat("a@slogin.io")) == "new"


def test_delete(tmp_path):
    s = _store(tmp_path)
    asyncio.run(s.set_pat("a@slogin.io", "x"))
    asyncio.run(s.delete_pat("a@slogin.io"))
    assert asyncio.run(s.has_pat("a@slogin.io")) is False


def test_stored_value_is_ciphertext(tmp_path):
    db = str(tmp_path / "pat.db")
    s = UserPatStore(db_path=db, enc_key=Fernet.generate_key().decode())
    asyncio.run(s.set_pat("a@slogin.io", "plane_api_secret"))
    raw = sqlite3.connect(db).execute("SELECT pat_encrypted FROM user_pat").fetchone()[0]
    assert b"plane_api_secret" not in raw  # encrypted at rest, never plaintext


def test_wrong_key_returns_none_not_crash(tmp_path):
    db = str(tmp_path / "pat.db")
    asyncio.run(UserPatStore(db_path=db, enc_key=Fernet.generate_key().decode()).set_pat("a@slogin.io", "x"))
    other = UserPatStore(db_path=db, enc_key=Fernet.generate_key().decode())  # different key
    assert asyncio.run(other.get_pat("a@slogin.io")) is None  # undecryptable -> None, no exception


def test_missing_key_raises(tmp_path):
    with pytest.raises(ValueError):
        UserPatStore(db_path=str(tmp_path / "x.db"), enc_key="")
