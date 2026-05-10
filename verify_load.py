import duckdb

DB_PATH  = "./db/polybot.duckdb"
CSV_PATH = "./processed/trades.csv"

print(f"Verifying {CSV_PATH} → {DB_PATH}\n")

con = duckdb.connect(DB_PATH, read_only=True)

# ── row counts ────────────────────────────────────────────────
print("Counting CSV rows (this may take a moment for large files)...")
csv_rows = con.execute(f"""
    SELECT COUNT(*) FROM read_csv_auto('{CSV_PATH}', header=true)
""").fetchone()[0]

db_rows = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

print(f"  CSV rows : {csv_rows:>15,}")
print(f"  DB rows  : {db_rows:>15,}")
print(f"  Match    : {'YES' if csv_rows == db_rows else 'NO — MISMATCH'}")

# ── column match ──────────────────────────────────────────────
csv_cols = set(con.execute(
    f"DESCRIBE SELECT * FROM read_csv_auto('{CSV_PATH}', header=true)"
).df()["column_name"])

db_cols = set(con.execute("DESCRIBE trades").df()["column_name"])

missing_in_db  = csv_cols - db_cols
extra_in_db    = db_cols  - csv_cols

print(f"\nColumns in CSV : {sorted(csv_cols)}")
print(f"Columns in DB  : {sorted(db_cols)}")
if missing_in_db:
    print(f"  MISSING in DB : {missing_in_db}")
if extra_in_db:
    print(f"  EXTRA in DB   : {extra_in_db}")
if not missing_in_db and not extra_in_db:
    print("  Column match  : YES")

# ── first / last row spot-check ───────────────────────────────
# uses the first column as a stable sort key
first_col = sorted(csv_cols)[0]

csv_first = con.execute(f"""
    SELECT * FROM read_csv_auto('{CSV_PATH}', header=true)
    ORDER BY "{first_col}" ASC LIMIT 1
""").fetchone()

csv_last = con.execute(f"""
    SELECT * FROM read_csv_auto('{CSV_PATH}', header=true)
    ORDER BY "{first_col}" DESC LIMIT 1
""").fetchone()

db_first = con.execute(f'SELECT * FROM trades ORDER BY "{first_col}" ASC  LIMIT 1').fetchone()
db_last  = con.execute(f'SELECT * FROM trades ORDER BY "{first_col}" DESC LIMIT 1').fetchone()

print(f"\nFirst row match : {'YES' if csv_first == db_first else 'NO'}")
print(f"  CSV : {csv_first}")
print(f"  DB  : {db_first}")

print(f"\nLast row match  : {'YES' if csv_last == db_last else 'NO'}")
print(f"  CSV : {csv_last}")
print(f"  DB  : {db_last}")

# ── summary ───────────────────────────────────────────────────
print("\n─────────────────────────────────────")
ok = (csv_rows == db_rows and not missing_in_db and not extra_in_db
      and csv_first == db_first and csv_last == db_last)
print(f"Overall: {'PASS — data matches' if ok else 'FAIL — see issues above'}")

con.close()
