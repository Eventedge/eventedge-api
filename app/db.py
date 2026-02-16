"""Database connection helper — reads from env vars, same Postgres as the bot."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg2
from psycopg2.extensions import connection as Conn


def _db_config() -> dict:
    return {
        "host": os.getenv("PGHOST", "127.0.0.1"),
        "port": int(os.getenv("PGPORT", "5432")),
        "user": os.getenv("PGUSER", "eventedge_bot"),
        "password": os.getenv("PGPASSWORD", ""),
        "dbname": os.getenv("PGDATABASE", "eventedge"),
    }


@contextmanager
def get_conn() -> Iterator[Conn]:
    conn = psycopg2.connect(**_db_config())
    try:
        yield conn
    finally:
        conn.close()
