####
# DESCRIPTION
# Between 9:45 - 10:45am we'll look for stocks that have increased at least 4 % from their close on the previous day.
# If they’ve done that and they meet some other criteria, we’ll buy them, and we’ll hold them until they either
# rise high enough (meeting our price target) or fall too low (meeting our ‘stop’ level.)
###

import sys
import alpaca_trade_api as tradeapi
import requests
import time
from ta.trend import macd
import numpy as np
from datetime import datetime, timedelta
from pytz import timezone
from config import * #link to other file
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Replace these with your API connection info from the dashboard
base_url = "https://paper-api.alpaca.markets"
api_key_id = API_KEY
api_secret = SECRET_KEY

# Switch to these parameters when going to live trading
# base_url = "https://api.alpaca.markets"
# api_key_id = LIVE_API_KEY
# api_secret = LIVE_SECRET_KEY

api = tradeapi.REST(
    base_url=base_url,
    key_id=api_key_id,
    secret_key=api_secret
)

session = requests.session()

# We only consider stocks with per-share prices inside this range
min_share_price = 2.0
max_share_price = 13.0
# Minimum previous-day dollar volume for a stock we might consider
min_last_dv = 500000
# Stop limit to default to
default_stop = .95
# How much of our portfolio to allocate to any one position
risk = 0.005 #changed from default 0.001

def send_email(subject, body):

    signature = '''
    
    This is an automated notification from a Python script.'''

    body = body + signature

    # Setup the MIME
    message = MIMEMultipart()
    message['From'] = sender_address
    message['To'] = receiver_address
    message['Subject'] = subject  # The subject line
    # The body and the attachments for the mail
    message.attach(MIMEText(body, 'plain'))
    # Create SMTP session for sending the mail
    session = smtplib.SMTP('smtp.gmail.com', 587)  # use gmail with port
    session.starttls()  # enable security
    session.login(sender_address, sender_pass)  # login with mail_id and password
    text = message.as_string()
    session.sendmail(sender_address, receiver_address, text)
    session.quit()

def get_1000m_history_data(symbols):
    print('Getting historical data...')
    minute_history = {}
    nyc = timezone('America/New_York')
    today = datetime.today().astimezone(nyc)
    today_str = datetime.today().astimezone(nyc).strftime('%Y-%m-%d')
    t_prev_str = (datetime.today().astimezone(nyc) - timedelta(days=2)).strftime('%Y-%m-%d')  # subtract 2 days
    c = 0
    for symbol in symbols:
        # https://pypi.org/project/alpaca-trade-api/
        minute_history[symbol] = api.polygon.historic_agg_v2(
            timespan="minute", symbol=symbol, limit=1000, multiplier=1, _from=t_prev_str, to=today_str
        ).df
        c += 1
        print('{}/{} : {}'.format(c, len(symbols), symbol))
        # print(minute_history[symbol])
    print('Success.')
    return minute_history


def get_tickers():
    print('Getting current ticker data...')
    tickers = api.polygon.all_tickers()
    print('Success.')
    assets = api.list_assets()
    symbols = [asset.symbol for asset in assets if asset.tradable] # Can test AA American Airlines to make sure it's functioning correctly
    # print(symbols) # if we want to see all symbols available
    # print(tickers) - if we want to see all the tickers returned by polygons API

    return [ticker for ticker in tickers if (
        ticker.ticker in symbols and
        ticker.lastTrade['p'] >= min_share_price and
        ticker.lastTrade['p'] <= max_share_price and
        ticker.prevDay['v'] * ticker.lastTrade['p'] > min_last_dv and
        ticker.todaysChangePerc >= 3.5
        # comment if we're working on this over the weekend
    )]


def find_stop(current_value, minute_history, now):
    series = minute_history['low'][-100:] \
                .dropna().resample('5min').min()
    series = series[now.floor('1D'):]
    diff = np.diff(series.values)
    low_index = np.where((diff[:-1] <= 0) & (diff[1:] > 0))[0] + 1
    if len(low_index) > 0:
        return series[low_index[-1]] - 0.01
    return current_value * default_stop


def run(tickers, market_open_dt, market_close_dt):
    # Establish streaming connection
    conn = tradeapi.StreamConn(base_url=base_url, key_id=api_key_id, secret_key=api_secret)

    # Store current date so we can eventually end this call
    current_dt = datetime.today().astimezone(nyc)

    # Update initial state with information from tickers
    volume_today = {}
    prev_closes = {}
    for ticker in tickers:
        symbol = ticker.ticker
        prev_closes[symbol] = ticker.prevDay['c']
        volume_today[symbol] = ticker.day['v']
        print(symbol,' - previous day close:',prev_closes[symbol],'; volume today:',volume_today[symbol])

    symbols = [ticker.ticker for ticker in tickers]
    print('Tracking {} symbols.'.format(len(symbols)))
    minute_history = get_1000m_history_data(symbols)
    # print(minute_history) # prints top and bottom 10 if you want to see open, close, etc.

    portfolio_value = float(api.get_account().portfolio_value)
    print('Portfolio value = ${:,}'.format(portfolio_value))

    open_orders = {}
    positions = {}

    # Cancel any existing open orders on watched symbols
    existing_orders = api.list_orders(limit=500)
    for order in existing_orders:
        if order.symbol in symbols:
            api.cancel_order(order.id)

    stop_prices = {}
    latest_cost_basis = {}

    # Track any positions bought during previous executions
    existing_positions = api.list_positions()
    for position in existing_positions:
        if position.symbol in symbols:
            positions[position.symbol] = float(position.qty)
            # Recalculate cost basis and stop price
            latest_cost_basis[position.symbol] = float(position.cost_basis)
            stop_prices[position.symbol] = (
                float(position.cost_basis) * default_stop
            )
            print('Latest cost basis:',latest_cost_basis[position.symbol])
            print('Stop prices:',stop_prices[position.symbol])

    # Keep track of what we're buying/selling
    target_prices = {}
    partial_fills = {}

    # Use trade updates to keep track of our portfolio
    @conn.on(r'trade_update')
    async def handle_trade_update(conn, channel, data):
        # End if market's closed
        if (current_dt - market_close_dt) // 60 > 15:
            conn.close('Market has closed')
        print('Looking for updates to existing orders on Alpaca')
        symbol = data.order['symbol']
        last_order = open_orders.get(symbol)
        if last_order is not None:
            event = data.event
            if event == 'partial_fill':
                qty = int(data.order['filled_qty'])
                if data.order['side'] == 'sell':
                    qty = qty * -1
                positions[symbol] = (
                    positions.get(symbol, 0) - partial_fills.get(symbol, 0)
                )
                partial_fills[symbol] = qty
                positions[symbol] += qty
                open_orders[symbol] = data.order
            elif event == 'fill':
                qty = int(data.order['filled_qty'])
                if data.order['side'] == 'sell':
                    qty = qty * -1
                positions[symbol] = (
                    positions.get(symbol, 0) - partial_fills.get(symbol, 0)
                )
                partial_fills[symbol] = 0
                positions[symbol] += qty
                open_orders[symbol] = None
            elif event == 'canceled' or event == 'rejected':
                partial_fills[symbol] = 0
                open_orders[symbol] = None

    @conn.on(r'A$')
    async def handle_second_bar(conn, channel, data):
        # End if market's closed
        if (current_dt - market_close_dt) // 60 > 15:
            conn.close('Market has closed')

        symbol = data.symbol
        print('Connecting to second-level data, watching:', symbol)

        # First, aggregate 1s bars for up-to-date MACD calculations
        ts = data.start
        ts -= timedelta(seconds=ts.second, microseconds=ts.microsecond)
        try:
            current = minute_history[data.symbol].loc[ts]
        except KeyError:
            current = None
        new_data = []
        if current is None:
            new_data = [
                data.open,
                data.high,
                data.low,
                data.close,
                data.volume
            ]
        else:
            new_data = [
                current.open,
                data.high if data.high > current.high else current.high,
                data.low if data.low < current.low else current.low,
                data.close,
                current.volume + data.volume
            ]
        minute_history[symbol].loc[ts] = new_data

        # Next, check for existing orders for the stock
        existing_order = open_orders.get(symbol)
        if existing_order is not None:
            # Make sure the order's not too old
            submission_ts = existing_order.submitted_at.astimezone(
                timezone('America/New_York')
            )
            order_lifetime = ts - submission_ts
            if order_lifetime.seconds // 60 > 1:
                # Cancel it so we can try again for a fill
                api.cancel_order(existing_order.id)
            return

        # Now we check to see if it might be time to buy or sell
        since_market_open = ts - market_open_dt
        #print('Time since market open:', since_market_open)
        until_market_close = market_close_dt - ts
        if (
            since_market_open.seconds // 60 < 60 and
            since_market_open.seconds // 60 > 15 and
            1 == 1
        ):
            # Check for buy signals

            # See if we've already bought in first
            position = positions.get(symbol, 0)
            if position > 0:
                return

            # See how high the price went during the first 15 minutes
            lbound = market_open_dt
            ubound = lbound + timedelta(minutes=15)
            high_15m = 0
            try:
                high_15m = minute_history[symbol][lbound:ubound]['high'].max()
            except Exception as e:
                # Because we're aggregating on the fly, sometimes the datetime
                # index can get messy until it's healed by the minute bars
                return
            print('High during first 15 minutes: ${:.2f}'.format(high_15m))
            # Get the change since yesterday's market close
            daily_pct_change = (
                100.0 * (data.close - prev_closes[symbol]) / prev_closes[symbol]
            )
            print('Daily change: {:.2f}%'.format(daily_pct_change))
            if (
                daily_pct_change > 4 and
                data.close > high_15m and
                volume_today[symbol] > 30000
            ):
                # check for a positive, increasing MACD
                hist = macd(
                    minute_history[symbol]['close'].dropna(),
                    n_fast=12,
                    n_slow=26
                )
                if (
                    hist[-1] < 0 or
                    not (hist[-3] < hist[-2] < hist[-1])
                ):
                    return
                hist = macd(
                    minute_history[symbol]['close'].dropna(),
                    n_fast=40,
                    n_slow=60
                )
                if hist[-1] < 0 or np.diff(hist)[-1] < 0:
                    return

                # Stock has passed all checks; figure out how much to buy
                stop_price = find_stop(
                    data.close, minute_history[symbol], ts
                )

                stop_prices[symbol] = stop_price
                target_prices[symbol] = data.close + (
                    (data.close - stop_price) * 3 # 3x positive multiplier on the downside threshold
                )

                print('Stop price for {}: ${:.2f}'.format(symbol, stop_prices[symbol]))
                print('Target price for {}: ${:.2f}'.format(symbol, target_prices[symbol]))

                shares_to_buy = portfolio_value * risk // (
                    data.close - stop_price
                )
                if shares_to_buy == 0:
                    shares_to_buy = 1
                shares_to_buy -= positions.get(symbol, 0)
                if shares_to_buy <= 0:
                    return

                buy_subj = 'Submitting buy for {:.0f} shares of {} at {}'.format(
                    shares_to_buy, symbol, data.close
                )

                buy_body = 'Portfolio value = ${:,}'.format(portfolio_value)

                print(buy_subj)
                send_email(buy_subj, buy_body)

                try:
                    o = api.submit_order(
                        symbol=symbol, qty=str(shares_to_buy), side='buy',
                        type='limit', time_in_force='day',
                        limit_price=str(data.close)
                    )
                    open_orders[symbol] = o
                    latest_cost_basis[symbol] = data.close
                except Exception as e:
                    print(e)
                return
        if(
            since_market_open.seconds // 60 >= 24 and # has been at least 24 mins since market opened
            until_market_close.seconds // 60 > 15 # greater than 15 minutes before market closes
        ):
            # Check for liquidation signals

            # We can't liquidate if there's no position
            position = positions.get(symbol, 0)
            if position == 0:
                return

            # Sell for a loss if it's fallen below our stop price
            # Sell for a loss if it's below our cost basis and MACD < 0
            # Sell for a profit if it's above our target price
            hist = macd(
                minute_history[symbol]['close'].dropna(),
                n_fast=13,
                n_slow=21
            )
            if (
                data.close <= stop_prices[symbol] or
                (data.close >= target_prices[symbol] and hist[-1] <= 0) or
                (data.close <= latest_cost_basis[symbol] and hist[-1] <= 0)
            ):
                sell_subj = 'Submitting sell for {:.0f} shares of {} at {}'.format(
                    position, symbol, data.close
                )

                sell_body = 'Portfolio value = ${:,}'.format(portfolio_value)

                send_email(sell_subj, sell_body)

                print(sell_subj)

                try:
                    o = api.submit_order(
                        symbol=symbol, qty=str(position), side='sell',
                        type='limit', time_in_force='day',
                        limit_price=str(data.close)
                    )
                    open_orders[symbol] = o
                    latest_cost_basis[symbol] = data.close
                except Exception as e:
                    print(e)
            return
        elif (
            until_market_close.seconds // 60 <= 15
        ):
            # Liquidate remaining positions on watched symbols at market
            try:
                position = api.get_position(symbol)
            except Exception as e:
                # Exception here indicates that we have no position
                return
            print('Trading over, liquidating remaining position in {}'.format(
                symbol)
            )
            api.submit_order(
                symbol=symbol, qty=position.qty, side='sell',
                type='market', time_in_force='day'
            )
            symbols.remove(symbol)
            if len(symbols) <= 0:
                conn.close()
            conn.deregister([
                'A.{}'.format(symbol),
                'AM.{}'.format(symbol)
            ])

    # Replace aggregated 1s bars with incoming 1m bars
    @conn.on(r'AM$')
    async def handle_minute_bar(conn, channel, data):
        # End if market's closed
        if (current_dt - market_close_dt) // 60 > 15:
            conn.close('Market has closed')

        print('Connecting to minute-level data, watching:', data.symbol)

        ts = data.start
        ts -= timedelta(microseconds=ts.microsecond)
        minute_history[data.symbol].loc[ts] = [
            data.open,
            data.high,
            data.low,
            data.close,
            data.volume
        ]
        volume_today[data.symbol] += data.volume

    channels = ['trade_updates']
    for symbol in symbols:
        symbol_channels = ['A.{}'.format(symbol), 'AM.{}'.format(symbol)]
        channels += symbol_channels

    # print('Channels being watched: ',channels)
    print('Watching {} symbols.'.format(len(symbols)))

    run_ws(conn, channels)


# Handle failed websocket connections by reconnecting
def run_ws(conn, channels):
    try:
        # Close if Polygon won't be available
        today = datetime.today().astimezone(nyc)
        if (today.hour < 4 or today.hour > 20 or today.weekday() >4):
            sys.exit('Outside of Polygon streaming hours')

        conn.run(channels)
        print('Running')
    except Exception as e:
        print(e)
        conn.close()
        run_ws(conn, channels)


if __name__ == "__main__":
    # Get when the market opens or opened today
    nyc = timezone('America/New_York')
    today = datetime.today().astimezone(nyc)
    today_str = datetime.today().astimezone(nyc).strftime('%Y-%m-%d')
    calendar = api.get_calendar(start=today_str, end=today_str)[0]
    market_open = today.replace(
        hour=calendar.open.hour,
        minute=calendar.open.minute,
        second=0
    )
    market_open = market_open.astimezone(nyc)
    market_close = today.replace(
        hour=calendar.close.hour,
        minute=calendar.close.minute,
        second=0
    )
    market_close = market_close.astimezone(nyc)

    # Wait until just before we might want to trade
    current_dt = datetime.today().astimezone(nyc)
    print('Current time:', current_dt)
    print('Market open time:', market_open)
    since_market_open = current_dt - market_open
    print('Time since market open:',since_market_open)
    while since_market_open.seconds // 60 <= 14: # sleep until we're 14 mins before we might want to trade
        time.sleep(1)
        since_market_open = current_dt - market_open

    run(get_tickers(), market_open, market_close)
