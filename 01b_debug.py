import duckdb
con = duckdb.connect('./db/polybot.duckdb')
# See all column values from a few rows
rows = con.execute("SELECT * FROM trades LIMIT 20").fetchall()
desc = con.execute("DESCRIBE trades").fetchall()
for d in desc: print(d)
for r in rows: print(r)
con.close()