"""One-off: import Parent Customer .csv into postage.db (project root)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import importer  # noqa: E402


def main() -> None:
    csv_path = ROOT / "projectfiles" / "data" / "Parent Customer .csv"
    if not csv_path.is_file():
        print(f"Missing file: {csv_path}", file=sys.stderr)
        sys.exit(1)
    db.init_db()
    r = importer.import_customers_csv(str(csv_path), db.DB_PATH)
    print("Import:", r)
    conn = db.get_connection()
    try:
        n = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        with_parent = conn.execute(
            "SELECT COUNT(*) FROM customers WHERE parent_number IS NOT NULL"
        ).fetchone()[0]
        parents = conn.execute(
            "SELECT COUNT(DISTINCT parent_number) FROM customers WHERE parent_number IS NOT NULL"
        ).fetchone()[0]
        standalone = conn.execute(
            "SELECT COUNT(*) FROM customers WHERE parent_number IS NULL AND parent_name IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    print(f"customers={n}, with_parent={with_parent}, distinct_parents={parents}, standalone={standalone}")
    if r.get("warnings"):
        for w in r["warnings"][:20]:
            print("warning:", w)
        if len(r["warnings"]) > 20:
            print(f"... and {len(r['warnings']) - 20} more warnings")


if __name__ == "__main__":
    main()
