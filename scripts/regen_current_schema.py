#!/usr/bin/env python3
"""
regen_current_schema.py — Regenerate/verify src/db/current_schema.sql.

current_schema.sql is documentation only: it records the schema a freshly-migrated database
actually has, and has no runtime effect. src/db/initial_schema.sql is frozen and is never
touched by this tool.

Method (both modes):
  * Migrate a THROWAWAY database, never the live one. alembic.ini's URL is the bare relative
    `sqlite:///inventory.db`, so a plain `alembic upgrade head` would hit the real database;
    this sets sqlalchemy.url to a temp path instead (the same idiom tests/db/test_migrations.py
    uses) and runs `command.upgrade(cfg, "head")`.
  * Dump the DDL sqlite itself stored, straight out of sqlite_master. NOT the sqlite3 CLI's
    `.schema`, which decorates rebuilt tables with a version-dependent `IF NOT EXISTS` that is
    CLI presentation rather than stored DDL and would make the committed file host-dependent.

Usage:
    python scripts/regen_current_schema.py --write     # rewrite current_schema.sql
    python scripts/regen_current_schema.py --verify    # per-object equivalence check, no writes

--verify compares the committed file against a fresh migrated database per object, by both
normalised SQL text (needed for differences PRAGMA cannot see, e.g. CHECK-clause whitespace or
a quoted DEFAULT) and the table-shape PRAGMA triple (table_info / foreign_key_list /
index_list). It exits non-zero on any difference.
"""

import argparse
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PROJECT_ROOT / "src" / "db" / "current_schema.sql"

# Statement header: kind + object name, tolerating the double-quoting that migration-generated
# DDL carries (CREATE TABLE "inventory") and an optional IF NOT EXISTS.
_HEADER_RE = re.compile(
    r'\s*CREATE\s+(?:UNIQUE\s+)?(TABLE|INDEX|VIEW|TRIGGER)\s+'
    r'(?:IF\s+NOT\s+EXISTS\s+)?"?([A-Za-z_][A-Za-z0-9_]*)"?',
    re.IGNORECASE,
)


def _migrate_throwaway(db_path: Path) -> None:
    """Run every migration up to head against a throwaway database."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")


def _stored_objects(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """[(name, sql)] for every sqlite_master object carrying stored DDL, in creation order.

    Rows with a NULL sql (implicit indexes backing PRIMARY KEY / UNIQUE) carry no DDL and are
    excluded; they are still covered by the index_list leg of the PRAGMA comparison.
    """
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid"
    ).fetchall()
    return [(name, sql.strip()) for name, sql in rows]


def _render(objects: list[tuple[str, str]]) -> str:
    """The file body: one statement per object, terminated with ';' and an LF."""
    return "".join(f"{sql};\n" for _, sql in objects)


def _parse_file(text: str) -> list[tuple[str, str]]:
    """[(name, sql)] parsed back out of a rendered schema file, preserving file order."""
    parsed = []
    for chunk in text.split(";\n"):
        if not chunk.strip():
            continue
        match = _HEADER_RE.match(chunk)
        if match is None:
            raise SystemExit(f"Unparseable statement in {SCHEMA_PATH.name}: {chunk[:60]!r}")
        parsed.append((match.group(2), chunk.strip()))
    return parsed


def _normalise(sql: str) -> str:
    """Whitespace-insensitive form of a DDL statement, for text comparison."""
    return re.sub(r"\s+", " ", sql).strip()


def _table_shape(conn: sqlite3.Connection, table: str) -> tuple:
    """(table_info, foreign_key_list, index_list) as comparable tuple lists for one table.

    Same triple as tests/db/test_migrations.py's _table_shape, so the two checks agree.
    """
    ti = [tuple(r) for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()]
    fk = [
        (r[2], r[3], r[4], r[6])
        for r in conn.execute(f"PRAGMA foreign_key_list('{table}')").fetchall()
    ]
    idx = [(r[1], r[2], r[3]) for r in conn.execute(f"PRAGMA index_list('{table}')").fetchall()]
    return ti, sorted(fk), sorted(idx)


def _doc_db(objects: list[tuple[str, str]]) -> sqlite3.Connection:
    """Build an in-memory database from the schema file's own DDL.

    The explicit `CREATE TABLE sqlite_sequence` is skipped: the name is reserved, and sqlite
    re-creates that table by itself for the AUTOINCREMENT column that caused it. Foreign-key
    targets are not validated at CREATE time, so the statements stand alone for introspection.
    """
    conn = sqlite3.connect(":memory:")
    ddl = "".join(f"{sql};\n" for name, sql in objects if name != "sqlite_sequence")
    conn.executescript(ddl)
    return conn


def _migrated_objects(db_path: Path) -> list[tuple[str, str]]:
    _migrate_throwaway(db_path)
    conn = sqlite3.connect(db_path)
    try:
        return _stored_objects(conn)
    finally:
        conn.close()


def do_write() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "regen.db"
        objects = _migrated_objects(db_path)
    with open(SCHEMA_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_render(objects))
    print(f"Wrote {SCHEMA_PATH.relative_to(PROJECT_ROOT)}: {len(objects)} objects")
    for name, _ in objects:
        print(f"  {name}")
    return 0


def do_verify() -> int:
    failures = []

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "verify.db"
        migrated_objects = _migrated_objects(db_path)
        file_objects = _parse_file(SCHEMA_PATH.read_text(encoding="utf-8"))

        migrated_names = {n for n, _ in migrated_objects}
        file_names = {n for n, _ in file_objects}
        if migrated_names != file_names:
            failures.append(
                f"object set differs: only in migrated DB {sorted(migrated_names - file_names)}, "
                f"only in file {sorted(file_names - migrated_names)}"
            )
        print(f"object set: {len(migrated_names)} objects "
              f"{'MATCH' if migrated_names == file_names else 'DIFFER'}")

        file_sql = dict(file_objects)
        migrated = sqlite3.connect(db_path)
        doc = _doc_db(file_objects)
        try:
            for name, sql in migrated_objects:
                if name not in file_sql:
                    continue
                text_ok = _normalise(sql) == _normalise(file_sql[name])
                if not text_ok:
                    failures.append(f"{name}: stored DDL text differs from the file")

                shape_ok: bool | None = None
                if _is_table(migrated, name) and not name.startswith("sqlite_"):
                    shape_ok = _table_shape(migrated, name) == _table_shape(doc, name)
                    if not shape_ok:
                        failures.append(f"{name}: PRAGMA table shape differs from the file")

                shape_note = "" if shape_ok is None else f" + PRAGMA {_ok(shape_ok)}"
                print(f"  {name}: text {_ok(text_ok)}{shape_note}")
        finally:
            migrated.close()
            doc.close()

    if failures:
        print("\nFAIL — current_schema.sql does not match a freshly-migrated database:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nPASS — current_schema.sql matches a freshly-migrated database per object "
          "(normalised text + PRAGMA table shape).")
    return 0


def _ok(passed: bool) -> str:
    return "OK" if passed else "MISMATCH"


def _is_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? AND type = 'table'", (name,)
    ).fetchone()
    return row is not None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate current_schema.sql")
    mode.add_argument(
        "--verify", action="store_true", help="check the committed file, write nothing"
    )
    args = parser.parse_args()
    return do_write() if args.write else do_verify()


if __name__ == "__main__":
    sys.exit(main())
