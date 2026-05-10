import duckdb
import time
import threading

DB_PATH  = "./db/polybot.duckdb"
CSV_PATH = "./processed/trades.csv"

def fmt(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"

# ── live ticker ───────────────────────────────────────────────
_done = threading.Event()

def ticker(start):
    while not _done.is_set():
        print(f"\rLoading... {fmt(time.time() - start)}", end="", flush=True)
        time.sleep(0.5)

# ── connect ───────────────────────────────────────────────────
con = duckdb.connect(DB_PATH)

con.execute("""
    CREATE TABLE IF NOT EXISTS _checkpoint (
        csv_path VARCHAR PRIMARY KEY,
        rows_loaded BIGINT,
        completed_at TIMESTAMP
    )
""")

# migrate old 2-column schema to 3-column schema
cols = {row[1] for row in con.execute("PRAGMA table_info(_checkpoint)").fetchall()}
if "completed_at" not in cols:
    con.execute("ALTER TABLE _checkpoint ADD COLUMN completed_at TIMESTAMP")

# ── resumability check ────────────────────────────────────────
row = con.execute(
    "SELECT rows_loaded FROM _checkpoint WHERE csv_path = ?", [CSV_PATH]
).fetchone()

if row:
    print(f"Already loaded: {row[0]:,} rows from {CSV_PATH}")
    print("Delete the checkpoint to force a reload:")
    print(f"  DELETE FROM _checkpoint WHERE csv_path = '{CSV_PATH}';")
    print(f"  DROP TABLE IF EXISTS trades;")
    con.close()
    exit(0)

# ── create table schema from CSV header ───────────────────────
con.execute(f"""
    CREATE TABLE IF NOT EXISTS trades AS
    SELECT * FROM read_csv_auto('{CSV_PATH}', header=true)
    WHERE 1=0
""")

# ── single-pass bulk load (no OFFSET scanning) ────────────────
print(f"Loading {CSV_PATH} → {DB_PATH}")
t0 = time.time()

_done.clear()
tick = threading.Thread(target=ticker, args=(t0,), daemon=True)
tick.start()

try:
    con.execute(f"COPY trades FROM '{CSV_PATH}' (AUTO_DETECT true, HEADER true)")
finally:
    _done.set()
    tick.join()

# ── verify and save checkpoint ────────────────────────────────
total = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
elapsed = time.time() - t0

con.execute("""
    INSERT INTO _checkpoint (csv_path, rows_loaded, completed_at) VALUES (?, ?, now())
    ON CONFLICT (csv_path) DO UPDATE
        SET rows_loaded  = excluded.rows_loaded,
            completed_at = excluded.completed_at
""", [CSV_PATH, total])

print(f"\rDone — {total:,} rows in {fmt(elapsed)}  ({total/elapsed:,.0f} rows/sec)")

con.close()
