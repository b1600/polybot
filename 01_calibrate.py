import duckdb
import time
import threading

DB_PATH = "./db/polybot.duckdb"

# ── helpers ───────────────────────────────────────────────────
def fmt(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"

_done  = threading.Event()
_start = time.time()

def ticker(get_label, step_start):
    while not _done.is_set():
        step_elapsed  = fmt(time.time() - step_start)
        total_elapsed = fmt(time.time() - _start)
        print(f"\r{get_label()}  step {step_elapsed}  |  total {total_elapsed}",
              end="", flush=True)
        time.sleep(0.5)

def run(con, label, sql):
    """Run SQL with a live mm:ss ticker. Returns the cursor."""
    _done.clear()
    lbl = [label]
    t0  = time.time()
    t   = threading.Thread(target=ticker, args=(lambda: lbl[0], t0), daemon=True)
    t.start()
    try:
        result = con.execute(sql)
    finally:
        _done.set()
        t.join()
    print(f"\r{label}  {fmt(time.time() - t0)}  |  total {fmt(time.time() - _start)}")
    return result

# ── resumability helpers ──────────────────────────────────────
def setup_progress(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS _calibration_progress (
            step        INTEGER PRIMARY KEY,
            name        VARCHAR,
            row_count   BIGINT,
            completed_at TIMESTAMP DEFAULT now()
        )
    """)

def is_done(con, step):
    row = con.execute(
        "SELECT 1 FROM _calibration_progress WHERE step = ?", [step]
    ).fetchone()
    return row is not None

def mark_done(con, step, name, row_count):
    con.execute("""
        INSERT INTO _calibration_progress (step, name, row_count)
        VALUES (?, ?, ?)
        ON CONFLICT (step) DO UPDATE
            SET row_count    = excluded.row_count,
                completed_at = now()
    """, [step, name, row_count])

def skip(step, name, con, table):
    n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    total = fmt(time.time() - _start)
    print(f"  [skip] step {step} ({name}) already done — {n:,} rows | total {total}")
    return n

# ── connect ───────────────────────────────────────────────────
con = duckdb.connect(DB_PATH)
setup_progress(con)

print("=" * 60)
print(f"01_calibrate.py  |  db: {DB_PATH}")
print("=" * 60)

# Show which steps are already complete
done_steps = con.execute(
    "SELECT step, name, row_count, completed_at FROM _calibration_progress ORDER BY step"
).fetchdf()
if not done_steps.empty:
    print("\nResuming — completed steps:")
    print(done_steps.to_string(index=False))
else:
    print("\nStarting fresh.")

# ── Step 0: Explore raw data ──────────────────────────────────
print("\n" + "=" * 60)
print("STEP 0 — Data exploration (always runs)")
print("=" * 60)

print("\n--- Schema ---")
print(con.execute("DESCRIBE trades").fetchdf().to_string(index=False))

print("\n--- Sample rows ---")
print(con.execute("SELECT * FROM trades LIMIT 5").fetchdf().to_string(index=False))

print("\n--- nonusdc_side values ---")
print(con.execute("""
    SELECT nonusdc_side, COUNT(*) AS n
    FROM trades GROUP BY 1 ORDER BY n DESC LIMIT 10
""").fetchdf().to_string(index=False))

print("\n--- taker_direction values ---")
print(con.execute("""
    SELECT taker_direction, COUNT(*) AS n
    FROM trades GROUP BY 1 ORDER BY n DESC LIMIT 10
""").fetchdf().to_string(index=False))

print("\n--- price range ---")
print(con.execute("""
    SELECT MIN(price) AS min_price, MAX(price) AS max_price,
           ROUND(AVG(price), 4) AS avg_price, COUNT(*) AS total_rows
    FROM trades
""").fetchdf().to_string(index=False))

print("\n--- timestamp range ---")
print(con.execute("""
    SELECT MIN(timestamp) AS earliest, MAX(timestamp) AS latest
    FROM trades
""").fetchdf().to_string(index=False))

print("\n--- market_id sample ---")
print(con.execute("""
    SELECT DISTINCT market_id FROM trades
    WHERE market_id IS NOT NULL AND market_id != ''
    LIMIT 10
""").fetchdf().to_string(index=False))

# ── Step 1: Market outcomes ───────────────────────────────────
print("\n" + "=" * 60)
print("STEP 1 — Build market_outcomes")
print("=" * 60)

if is_done(con, 1):
    skip(1, "market_outcomes", con, "market_outcomes")
else:
    run(con, "Building market_outcomes...", """
        CREATE OR REPLACE TABLE market_outcomes AS
        WITH windowed AS (
            SELECT *,
                (EPOCH(TRY_CAST(timestamp AS TIMESTAMP))::BIGINT // 300) * 300
                    AS window_start
            FROM trades
            WHERE price IS NOT NULL
        ),
        resolution_trades AS (
            SELECT
                window_start,
                nonusdc_side,
                price,
                timestamp,
                ROW_NUMBER() OVER (
                    PARTITION BY window_start, nonusdc_side
                    ORDER BY TRY_CAST(timestamp AS TIMESTAMP) DESC
                ) AS rn
            FROM windowed
            WHERE CAST(price AS DOUBLE) >= 0.95
               OR CAST(price AS DOUBLE) <= 0.05
        )
        SELECT
            window_start,
            nonusdc_side                        AS winning_side,
            CAST(price AS DOUBLE)               AS final_price,
            TRY_CAST(timestamp AS TIMESTAMP)    AS resolved_at
        FROM resolution_trades
        WHERE rn = 1
          AND CAST(price AS DOUBLE) >= 0.95
    """)
    n = con.execute("SELECT COUNT(*) FROM market_outcomes").fetchone()[0]
    mark_done(con, 1, "market_outcomes", n)
    print(f"  {n:,} resolved markets")

print("\n--- Sample outcomes ---")
print(con.execute("""
    SELECT * FROM market_outcomes ORDER BY window_start DESC LIMIT 10
""").fetchdf().to_string(index=False))

# ── Step 2: Window trades ─────────────────────────────────────
print("\n" + "=" * 60)
print("STEP 2 — Build window_trades")
print("=" * 60)

if is_done(con, 2):
    skip(2, "window_trades", con, "window_trades")
else:
    run(con, "Building window_trades...", """
        CREATE OR REPLACE TABLE window_trades AS
        SELECT
            TRY_CAST(timestamp AS TIMESTAMP)                                AS ts,
            nonusdc_side,
            maker_direction,
            taker_direction,
            CAST(price AS DOUBLE)                                           AS price,
            CAST(usd_amount AS DOUBLE)                                      AS usd_amount,
            (EPOCH(TRY_CAST(timestamp AS TIMESTAMP))::BIGINT // 300) * 300 AS window_start,
            300 - (EPOCH(TRY_CAST(timestamp AS TIMESTAMP))::BIGINT % 300)  AS seconds_remaining
        FROM trades
        WHERE price IS NOT NULL
          AND CAST(price AS DOUBLE) BETWEEN 0.01 AND 0.99
          AND nonusdc_side IS NOT NULL
          AND nonusdc_side NOT IN ('', 'USDC')
    """)
    n = con.execute("SELECT COUNT(*) FROM window_trades").fetchone()[0]
    mark_done(con, 2, "window_trades", n)
    print(f"  {n:,} qualifying trades")

# ── Step 3: Calibration ───────────────────────────────────────
print("\n" + "=" * 60)
print("STEP 3 — Build calibration table")
print("=" * 60)

if is_done(con, 3):
    skip(3, "calibration", con, "calibration")
else:
    run(con, "Building calibration...", """
        CREATE OR REPLACE TABLE calibration AS
        SELECT
            ROUND(wt.price / 0.05) * 0.05                                   AS price_bucket,
            (wt.seconds_remaining // 30) * 30                               AS seconds_remaining_bucket,
            COUNT(*)                                                         AS trade_count,

            -- Did this token actually win?
            AVG(CASE WHEN wt.nonusdc_side = o.winning_side
                     THEN 1.0 ELSE 0.0 END)                                 AS actual_win_rate,
            STDDEV(CASE WHEN wt.nonusdc_side = o.winning_side
                        THEN 1.0 ELSE 0.0 END)                              AS win_rate_std,

            -- Did the taker back the winner?
            AVG(CASE
                WHEN wt.taker_direction = 'BUY'
                     AND wt.nonusdc_side = o.winning_side  THEN 1.0
                WHEN wt.taker_direction = 'SELL'
                     AND wt.nonusdc_side != o.winning_side THEN 1.0
                ELSE 0.0 END)                                               AS taker_win_rate,

            AVG(wt.usd_amount)                                              AS avg_trade_size_usd
        FROM window_trades wt
        JOIN market_outcomes o ON wt.window_start = o.window_start
        WHERE wt.seconds_remaining BETWEEN 10 AND 290
        GROUP BY 1, 2
        HAVING COUNT(*) >= 100
        ORDER BY 1, 2
    """)
    n = con.execute("SELECT COUNT(*) FROM calibration").fetchone()[0]
    mark_done(con, 3, "calibration", n)
    print(f"  {n:,} calibration buckets")

# ── Step 4: Results (always runs) ────────────────────────────
print("\n" + "=" * 60)
print("STEP 4 — Results (always runs)")
print("=" * 60)

print("\n--- Full calibration table ---")
print(con.execute("""
    SELECT
        price_bucket,
        seconds_remaining_bucket            AS secs_left,
        trade_count,
        ROUND(actual_win_rate, 4)           AS actual_win_rate,
        ROUND(actual_win_rate
              - price_bucket, 4)            AS edge_vs_market,
        ROUND(win_rate_std
              / SQRT(trade_count), 4)       AS std_error,
        ROUND(taker_win_rate, 4)            AS taker_win_rate,
        ROUND(avg_trade_size_usd, 2)        AS avg_usd
    FROM calibration
    ORDER BY price_bucket, seconds_remaining_bucket
""").fetchdf().to_string(index=False))

print("\n--- Significant mispricings (|edge| > 5%, std_error < 0.02) ---")
mispricings = con.execute("""
    SELECT
        price_bucket,
        seconds_remaining_bucket            AS secs_left,
        trade_count,
        ROUND(actual_win_rate, 4)           AS actual_win_rate,
        ROUND(actual_win_rate
              - price_bucket, 4)            AS edge_vs_market,
        ROUND(win_rate_std
              / SQRT(trade_count), 4)       AS std_error,
        ROUND((actual_win_rate - price_bucket)
              / NULLIF(win_rate_std / SQRT(trade_count), 0), 2) AS z_score
    FROM calibration
    WHERE ABS(actual_win_rate - price_bucket) > 0.05
      AND win_rate_std / SQRT(trade_count) < 0.02
    ORDER BY ABS(actual_win_rate - price_bucket) DESC
""").fetchdf()

if mispricings.empty:
    print("  None found — market appears well-calibrated.")
else:
    print(mispricings.to_string(index=False))

print("\n--- Calibration by price bucket ---")
print(con.execute("""
    SELECT
        price_bucket,
        SUM(trade_count)                    AS total_trades,
        ROUND(AVG(actual_win_rate), 4)      AS avg_actual_win_rate,
        ROUND(AVG(actual_win_rate)
              - price_bucket, 4)            AS avg_edge,
        ROUND(AVG(taker_win_rate), 4)       AS avg_taker_win_rate
    FROM calibration
    GROUP BY price_bucket
    ORDER BY price_bucket
""").fetchdf().to_string(index=False))

print("\n--- Best time windows (highest taker win rate) ---")
print(con.execute("""
    SELECT
        seconds_remaining_bucket            AS secs_left,
        SUM(trade_count)                    AS total_trades,
        ROUND(AVG(actual_win_rate), 4)      AS avg_actual_win_rate,
        ROUND(AVG(taker_win_rate), 4)       AS avg_taker_win_rate,
        ROUND(AVG(actual_win_rate
                  - price_bucket), 4)       AS avg_edge
    FROM calibration
    GROUP BY seconds_remaining_bucket
    ORDER BY avg_taker_win_rate DESC
    LIMIT 10
""").fetchdf().to_string(index=False))

# ── Done ──────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"Done  |  total {fmt(time.time() - _start)}")
print(f"Tables in {DB_PATH}:")
print("  market_outcomes          — resolved market results")
print("  window_trades            — qualifying in-window trades")
print("  calibration              — empirical P(win) by price + time")
print("  _calibration_progress    — step checkpoint (resumability)")
print("=" * 60)

con.close()
