"""Apply pending SQL files in migrations/ to the database in DATABASE_URL.

Usage:  python migrate.py

Each file runs in its own transaction together with a row in
schema_migrations recording that it ran, so a failed migration leaves the
database untouched and a successful one is never applied twice.
"""

from pathlib import Path

import database

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def main():
    with database.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename   TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;
            """
        )
        cur.execute("SELECT filename FROM schema_migrations;")
        applied = {row["filename"] for row in cur.fetchall()}

    pending = [p for p in sorted(MIGRATIONS_DIR.glob("*.sql")) if p.name not in applied]
    if not pending:
        print("Database is up to date.")
        return

    for path in pending:
        print(f"Applying {path.name} ...")
        with database.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s);", (path.name,))
    print(f"Applied {len(pending)} migration(s).")


if __name__ == "__main__":
    main()
