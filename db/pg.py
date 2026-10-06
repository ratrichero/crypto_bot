"""Wrapper mong psycopg v3. Doc DATABASE_URL tu env."""
import os

import psycopg
import psycopg.rows


def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("Chua dat DATABASE_URL")
    return url


def conn():
    return psycopg.connect(dsn(), row_factory=psycopg.rows.dict_row)


def query(sql, params=None):
    """SELECT -> list[dict]."""
    with conn() as c, c.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


def execute(sql, params=None):
    """INSERT/UPDATE/DDL. Tra ve so dong anh huong."""
    with conn() as c, c.cursor() as cur:
        cur.execute(sql, params or ())
        n = cur.rowcount
        c.commit()
        return n


def executemany(sql, rows):
    with conn() as c, c.cursor() as cur:
        cur.executemany(sql, rows)
        c.commit()
