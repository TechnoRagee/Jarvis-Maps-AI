import sqlite3
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_FILE = "jarvis_maps.db"

print(f"Database file: {DB_FILE}")
print(f"File exists: {os.path.exists(DB_FILE)}")
if os.path.exists(DB_FILE):
    print(f"File size: {os.path.getsize(DB_FILE)} bytes")
print()

if os.path.exists(DB_FILE):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()

        # List all tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = cursor.fetchall()
        print(f"=== TABLES ({len(tables)}) ===")
        for t in tables:
            print(f"  - {t[0]}")
        print()

        # For each table, show schema and data
        for t in tables:
            table_name = t[0]
            if table_name == "sqlite_sequence":
                continue
            # Sanitize table identifier
            safe_table = table_name.replace('"', '""')
            print(f"--- {table_name} ---")

            # Schema
            cursor.execute(f'PRAGMA table_info("{safe_table}")')
            columns = cursor.fetchall()
            print("  Columns:")
            for col in columns:
                print(f"    {col[1]} ({col[2]}){' PRIMARY KEY' if col[5] else ''}{' NOT NULL' if col[3] else ''}")

            # Row count
            cursor.execute(f'SELECT COUNT(*) FROM "{safe_table}"')
            count = cursor.fetchone()[0]
            print(f"  Row count: {count}")

            # Sample data (first 10 rows)
            if count > 0:
                cursor.execute(f'SELECT * FROM "{safe_table}" LIMIT 10')
                rows = cursor.fetchall()
                col_names = [c[1] for c in columns]
                print("  Sample data (up to 10 rows):")
                for row in rows:
                    print(f"    {dict(zip(col_names, row))}")
            print()

# Also check if students.db exists
if os.path.exists("students.db"):
    print("=== students.db ===")
    with sqlite3.connect("students.db") as conn2:
        c2 = conn2.cursor()
        c2.execute("SELECT name FROM sqlite_master WHERE type='table'")
        for t in c2.fetchall():
            table_name = t[0]
            if table_name == "sqlite_sequence":
                continue
            safe_table = table_name.replace('"', '""')
            print(f"  Table: {table_name}")
            c2.execute(f'SELECT COUNT(*) FROM "{safe_table}"')
            print(f"  Row count: {c2.fetchone()[0]}")
            c2.execute(f'PRAGMA table_info("{safe_table}")')
            for col in c2.fetchall():
                print(f"    {col[1]} ({col[2]})")
