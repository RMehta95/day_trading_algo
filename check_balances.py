import alpaca_trade_api as tradeapi
import csv
import pandas as pd
from config import *  # link to other file

api = tradeapi.REST(
#    base_url=base_url,
    key_id=LIVE_API_KEY,
    secret_key=LIVE_SECRET_KEY
)

api2 = tradeapi.REST(
#    base_url=base_url,
    key_id=API_KEY,
    secret_key=SECRET_KEY
)


# Get our account information.
account = api.get_account()


# print(account.trading_blocked)
# print(account.shorting_enabled)

# Check if our account is restricted from trading.
if account.trading_blocked:
    print('Account is currently restricted from trading.')

# Check how much money we can use to open new positions.
print('Buying power: ${}'.format(account.buying_power))
print('Account cash: ${}'.format(account.cash))
print('Equity / portfolio value: ${}'.format(account.equity))
print('Long market value:  ${}'.format(account.long_market_value))
print('Short market value:  ${}'.format(account.short_market_value))
print('Day trade count: {}'.format(account.daytrade_count))

# Trade history  (doc: https://alpaca.markets/docs/api-documentation/api-v2/orders/)
closed_orders = api.list_orders(
    status='all', #open, closed, all
    limit=500, # max
    nested=True  # show nested multi-leg orders
)

print(closed_orders)

# Get only the closed orders for a particular stock
# closed_aa_orders = [o for o in closed_orders if o.symbol == 'AA']
# print(closed_aa_orders)

# Write to .CSV
csvFile = open('historical_orders.csv', 'w')

# Solution from StackExchange
with open('historical_orders.csv', 'w', newline='') as csvfile:
    writer = csv.DictWriter(csvfile, fieldnames=closed_orders[0].__dict__['_raw'].keys())
    writer.writeheader()
    for order in closed_orders:
        writer.writerow(order.__dict__['_raw'])

# Paper history  (doc: https://alpaca.markets/docs/api-documentation/api-v2/orders/)
closed_orders2 = api2.list_orders(
    status='all', #open, closed, all
    limit=500, # max
    nested=True  # show nested multi-leg orders
)

# Write to .CSV
csvFile2 = open('historical_orders_paper.csv', 'w')

# Solution from StackExchange
with open('historical_orders_paper.csv', 'w', newline='') as csvfile:
    writer2 = csv.DictWriter(csvfile, fieldnames=closed_orders2[0].__dict__['_raw'].keys())
    writer2.writeheader()
    for order in closed_orders2:
        writer2.writerow(order.__dict__['_raw'])
