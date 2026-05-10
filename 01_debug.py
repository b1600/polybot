import duckdb
con = duckdb.connect('./db/polybot.duckdb')

for t in ['trades', 'market_outcomes', 'window_trades', 'calibration']:
    n = con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    print(f'{t}: {n:,}')

print(con.execute('SELECT * FROM market_outcomes LIMIT 3').fetchall())
print(con.execute('SELECT * FROM window_trades LIMIT 3').fetchall())

# Key: do window_start values actually overlap?
print(con.execute('SELECT DISTINCT window_start FROM market_outcomes LIMIT 5').fetchall())
print(con.execute('SELECT DISTINCT window_start FROM window_trades LIMIT 5').fetchall())

# How many rows survive the JOIN?
print(con.execute('''
    SELECT COUNT(*) FROM window_trades wt
    JOIN market_outcomes o ON wt.window_start = o.window_start
''').fetchone())

# What are the actual nonusdc_side and winning_side values?
print(con.execute('SELECT DISTINCT nonusdc_side FROM window_trades').fetchall())
print(con.execute('SELECT DISTINCT winning_side FROM market_outcomes').fetchall())
con.close()