"""Create database roles and apply migrations on Neon Postgres.

Uses NEON_MIGRATION_URL (the owner role from the Neon console). Creates:
  kb_views  (NOLOGIN)  owns the api.* views; subject to row-level security
  kb_ingest (LOGIN)    trusted backend + ingestion role
  kb_query  (LOGIN)    read-only role for the SQL Agent; can only read api.* views

    python scripts/setup_db.py            # first run, or after changing the schema
    python scripts/setup_db.py --rotate   # issue new kb_ingest/kb_query passwords

Whenever passwords are generated, NEON_INGEST_URL and NEON_QUERY_URL are written to .env.
Passwords are generated automatically when .env does not yet hold working URLs.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ROOT, enter_root, update_env, url_with_credentials

MIGRATIONS = ROOT / "backend" / "migrations"


def role_exists(cur: psycopg.Cursor, name: str) -> bool:  # type: ignore[type-arg]
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    return cur.fetchone() is not None


def ensure_roles(conn: psycopg.Connection, rotate: bool) -> dict[str, str]:  # type: ignore[type-arg]
    passwords: dict[str, str] = {}
    with conn.cursor() as cur:
        if not role_exists(cur, "kb_views"):
            cur.execute("CREATE ROLE kb_views NOLOGIN")
        for role in ("kb_ingest", "kb_query"):
            password = secrets.token_urlsafe(24)
            if not role_exists(cur, role):
                cur.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
                passwords[role] = password
            elif rotate:
                cur.execute(
                    sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
                passwords[role] = password
        # The migration role must be a member of kb_views to hand it view ownership.
        cur.execute("GRANT kb_views TO CURRENT_USER")
        cur.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO kb_ingest, kb_query").format(
                sql.Identifier(conn.info.dbname)
            )
        )
    return passwords


def apply_migrations(conn: psycopg.Connection) -> list[str]:  # type: ignore[type-arg]
    applied: list[str] = []
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS public.kb_migrations "
            "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        cur.execute("REVOKE ALL ON public.kb_migrations FROM PUBLIC")
        cur.execute("SELECT version FROM public.kb_migrations")
        done = {row[0] for row in cur.fetchall()}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name in done:
            continue
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))  # type: ignore[call-overload]
            cur.execute("INSERT INTO public.kb_migrations (version) VALUES (%s)", (path.name,))
        applied.append(path.name)
    return applied


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="issue new kb_ingest/kb_query passwords and write their URLs to .env",
    )
    args = parser.parse_args()
    enter_root()

    from app.core.config import Settings

    settings = Settings()
    owner_url = settings.neon_migration_url.get_secret_value()
    configured = all(
        url and "password@host" not in url
        for url in (
            settings.neon_ingest_url.get_secret_value(),
            settings.neon_query_url.get_secret_value(),
        )
    )
    if not owner_url or "password@host" in owner_url:
        print("Set NEON_MIGRATION_URL in .env to your Neon owner connection string first.")
        return 1

    with psycopg.connect(owner_url, autocommit=True) as conn:
        passwords = ensure_roles(conn, rotate=args.rotate or not configured)
        applied = apply_migrations(conn)

    print(f"Migrations applied: {', '.join(applied) or 'none (already up to date)'}")
    names = {"kb_ingest": "NEON_INGEST_URL", "kb_query": "NEON_QUERY_URL"}
    if passwords:
        update_env(
            {
                names[role]: url_with_credentials(owner_url, role, password)
                for role, password in passwords.items()
            }
        )
        print(f"Wrote {', '.join(names[r] for r in passwords)} to .env")
    else:
        print("Role passwords unchanged (use --rotate to issue new ones).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
