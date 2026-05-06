"""Per-user Jira profile storage. Plain SQLite — kept locally, not shared."""
import sqlite3
from contextlib import contextmanager
from typing import Optional, TypedDict

from src import config


class Profile(TypedDict, total=False):
    base_url: str
    email: str
    api_token: str
    default_project: Optional[str]
    default_board: Optional[int]


_FIELDS = ("base_url", "email", "api_token", "default_project", "default_board")
_REQUIRED = ("base_url", "email", "api_token")


@contextmanager
def _conn():
    c = sqlite3.connect(config.USER_DB)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY,
                base_url TEXT,
                email TEXT,
                api_token TEXT,
                default_project TEXT,
                default_board INTEGER
            )
        """)


def get(user_id: str) -> Optional[Profile]:
    with _conn() as c:
        row = c.execute(
            f"SELECT {','.join(_FIELDS)} FROM user_profiles WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return {k: v for k, v in zip(_FIELDS, row) if v is not None}  # type: ignore


def save_field(user_id: str, field: str, value) -> None:
    if field not in _FIELDS:
        raise ValueError(f"unknown field: {field}")
    with _conn() as c:
        c.execute(
            f"INSERT INTO user_profiles (user_id, {field}) VALUES (?, ?) "
            f"ON CONFLICT(user_id) DO UPDATE SET {field}=excluded.{field}",
            (user_id, value),
        )


def delete(user_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM user_profiles WHERE user_id=?", (user_id,))


def is_complete(profile: Optional[Profile]) -> bool:
    if not profile:
        return False
    return all(profile.get(f) for f in _REQUIRED)
