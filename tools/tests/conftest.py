"""
Import tests run against a real, throw-away Postgres database with both
migrations applied. Set TEST_PG_ADMIN_URL to a superuser connection
(default: postgres@127.0.0.1). Tests are skipped if it can't be reached.
"""
import os
import pathlib
import sys

import psycopg
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ADMIN_URL = os.getenv("TEST_PG_ADMIN_URL", "postgresql://postgres:pw12345@127.0.0.1:5432/postgres")
MIGRATIONS = pathlib.Path(__file__).resolve().parents[2] / "supabase" / "migrations"
DB_NAME = "eim_import_test"


@pytest.fixture(scope="session")
def db_url():
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no test Postgres available: {e}")
    admin.execute(f"drop database if exists {DB_NAME}")
    admin.execute(f"create database {DB_NAME}")
    for role in ("anon", "authenticated"):
        try:
            admin.execute(f"create role {role} nologin")
        except psycopg.errors.DuplicateObject:
            pass
    base, _, _ = ADMIN_URL.rpartition("/")
    url = f"{base}/{DB_NAME}"
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("create schema if not exists auth")
        c.execute("create table auth.users (id uuid primary key)")
        c.execute("create schema if not exists extensions")
        for f in sorted(MIGRATIONS.glob("*.sql")):
            c.execute(f.read_text())
    yield url
    admin.execute(f"drop database if exists {DB_NAME} with (force)")


@pytest.fixture
def conn(db_url):
    c = psycopg.connect(db_url)
    yield c
    c.rollback()
    c.close()
