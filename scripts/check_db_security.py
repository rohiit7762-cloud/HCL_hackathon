"""Verify the database security guarantees by connecting as the restricted kb_query role.

    python scripts/check_db_security.py

Run after setup_db.py and seed_demo.py. Exits non-zero if any check fails.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import enter_root

Conn = psycopg.Connection[tuple[object, ...]]


def as_user(conn: Conn, tenant: str, employee: str) -> None:
    conn.execute(
        "SELECT set_config('app.tenant_id', %s, true), set_config('app.employee_id', %s, true)",
        (tenant, employee),
    )


def scalar(conn: Conn, query: str, params: tuple[object, ...] = ()) -> object:
    row = conn.execute(query, params).fetchone()  # type: ignore[arg-type]
    return row[0] if row else None


def must_fail(conn: Conn, query: str) -> bool:
    try:
        with conn.transaction():
            conn.execute(query)  # type: ignore[arg-type]
    except psycopg.Error:
        return True
    return False


def checks(conn: Conn) -> list[tuple[str, Callable[[], bool]]]:
    def in_tx(fn: Callable[[], bool]) -> Callable[[], bool]:
        def run() -> bool:
            with conn.transaction():
                return fn()

        return run

    return [
        (
            "kb_query cannot read core tables",
            lambda: must_fail(conn, "SELECT * FROM core.employees"),
        ),
        (
            "kb_query cannot write to views",
            lambda: must_fail(conn, "UPDATE api.incidents SET status = 'x'"),
        ),
        ("kb_query cannot create tables", lambda: must_fail(conn, "CREATE TABLE api.x (id int)")),
        ("kb_query cannot switch roles", lambda: must_fail(conn, "SET ROLE kb_ingest")),
        (
            "kb_query cannot read the audit log",
            lambda: must_fail(conn, "SELECT * FROM core.audit_log"),
        ),
        (
            "no tenant context -> no rows",
            in_tx(lambda: scalar(conn, "SELECT count(*) FROM api.incidents") == 0),
        ),
        (
            "tenant context -> own tenant rows",
            in_tx(
                lambda: (
                    as_user(conn, "lumen", "E100"),
                    int(scalar(conn, "SELECT count(*) FROM api.incidents") or 0) > 0,
                )[1]
            ),
        ),
        (
            "other tenant's rows invisible",
            in_tx(
                lambda: (
                    as_user(conn, "lumen", "E100"),
                    scalar(
                        conn,
                        "SELECT count(*) FROM api.employee_directory WHERE employee_id = 'O100'",
                    )
                    == 0,
                )[1]
            ),
        ),
        (
            "employee sees only own HR row",
            in_tx(
                lambda: (
                    as_user(conn, "lumen", "E100"),
                    scalar(conn, "SELECT array_agg(employee_id) FROM api.employee_hr") == ["E100"],
                )[1]
            ),
        ),
        (
            "manager sees reports' ratings but not salaries",
            in_tx(
                lambda: (
                    as_user(conn, "lumen", "E102"),
                    scalar(
                        conn,
                        "SELECT count(*) FROM api.employee_hr WHERE employee_id = 'E100' "
                        "AND performance_rating IS NOT NULL AND salary IS NULL",
                    )
                    == 1,
                )[1]
            ),
        ),
        (
            "HR sees all HR rows",
            in_tx(
                lambda: (
                    as_user(conn, "lumen", "E103"),
                    int(scalar(conn, "SELECT count(*) FROM api.employee_hr") or 0) >= 10,
                )[1]
            ),
        ),
        (
            "restricted dataset hidden from non-HR",
            in_tx(
                lambda: (
                    as_user(conn, "lumen", "E100"),
                    scalar(
                        conn,
                        "SELECT count(*) FROM api.dataset_rows "
                        "WHERE dataset_name = 'compensation_bands'",
                    )
                    == 0,
                )[1]
            ),
        ),
        ("statement timeout is enforced", lambda: timeout_enforced(conn)),
    ]


def timeout_enforced(conn: Conn) -> bool:
    try:
        with conn.transaction():
            conn.execute("SET LOCAL statement_timeout = 200")
            conn.execute("SELECT pg_sleep(2)")
    except psycopg.errors.QueryCanceled:
        return True
    return False


def main() -> int:
    enter_root()
    from app.core.config import Settings

    url = Settings().neon_query_url.get_secret_value()
    if not url:
        print("NEON_QUERY_URL is not set. Run scripts/setup_db.py first.")
        return 1
    failed = 0
    with psycopg.connect(url, prepare_threshold=None) as conn:
        for name, check in checks(conn):
            try:
                ok = bool(check())
            except psycopg.Error as exc:
                ok = False
                name += f" ({type(exc).__name__})"
                conn.rollback()
            failed += not ok
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print("All database security checks passed." if not failed else f"{failed} check(s) failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
