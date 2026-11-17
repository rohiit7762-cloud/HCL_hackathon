import sys

sys.path.insert(0, "backend")

import psycopg

from app.core.config import get_settings

settings = get_settings()
with psycopg.connect(settings.neon_query_url.get_secret_value(), connect_timeout=8) as conn:
    row = conn.execute(
        "SELECT current_database(), current_user, current_setting('server_version')"
    ).fetchone()
    print(f"Neon connection successful; server version {row[2]}.")
