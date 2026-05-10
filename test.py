import csv

with open('/Volumes/ExtremeSSD/CodeSSD/polybot/goldsky/orderFilled.csv') as f:
    reader = csv.reader(f)
    for i, row in enumerate(reader):
        print(row)
        if i >= 10:
            break