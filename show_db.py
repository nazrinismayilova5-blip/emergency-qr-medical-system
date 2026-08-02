import sqlite3
from pathlib import Path

db_path = Path("data/emergency.db")

if not db_path.exists():
    print("Database not found at", db_path)
    exit(1)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("PRAGMA table_info(people)")
columns = [col["name"] for col in cur.fetchall()]

if "in_hospital" not in columns:
    print("Column 'in_hospital' does NOT exist. Adding it...")
    cur.execute("ALTER TABLE people ADD COLUMN in_hospital INTEGER NOT NULL DEFAULT 1;")
    conn.commit()
    print("Column 'in_hospital' added successfully.")
else:
    print("Column 'in_hospital' already exists.")

print("\nTABLE STRUCTURE:\n")
cur.execute("PRAGMA table_info(people)")
for col in cur.fetchall():
    print(f"- {col['name']} ({col['type']})")

print("\nDATA:\n")
rows = cur.execute("SELECT * FROM people").fetchall()

for row in rows:
    print(dict(row))

conn.close()
