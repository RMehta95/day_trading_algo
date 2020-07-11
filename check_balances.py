import alpaca_trade_api as tradeapi
import csv
import pandas as pd
from config import *  # link to other file
from datetime import datetime

api = tradeapi.REST(
    base_url="https://api.alpaca.markets",
    key_id=LIVE_API_KEY,
    secret_key=LIVE_SECRET_KEY
)

api2 = tradeapi.REST(
    base_url="https://paper-api.alpaca.markets",
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

# print(closed_orders)

# Get only the closed orders for a particular stock
# closed_aa_orders = [o for o in closed_orders if o.symbol == 'AA']
# print(closed_aa_orders)

# Write to .CSV
t = datetime.today()
csvFileName = 'historical_orders/historical_orders_{year:04d}{month:02d}{day:02d}.csv'.format(year=t.year, month=t.month, day=t.day)

headers = ('submitted_at', 'filled_at', 'canceled_at', 'symbol', 'qty', 'filled_qty', 'filled_avg_price', 'order_type',
           'side', 'time_in_force', 'limit_price', 'stop_price', 'status')


# Solution from StackExchange
with open(csvFileName, 'w', newline='') as csvfile:
    writer = csv.DictWriter(csvfile, headers, extrasaction='ignore')  # fieldnames=closed_orders[0].__dict__['_raw'].keys())
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
csvFileName2 = 'historical_orders/historical_orders_paper_{year:04d}{month:02d}{day:02d}.csv'.format(year=t.year, month=t.month, day=t.day)

# Solution from StackExchange
with open(csvFileName2, 'w', newline='') as csvfile:
    writer2 = csv.DictWriter(csvfile, headers, extrasaction='ignore')  # fieldnames=closed_orders2[0].__dict__['_raw'].keys())
    writer2.writeheader()
    for order in closed_orders2:
        writer2.writerow(order.__dict__['_raw'])