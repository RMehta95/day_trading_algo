####
# DESCRIPTION
# Between 9:45 - 10:30am we'll look for stocks that have increased at least 4 % from their close on the previous day.
# If they’ve done that and they meet some other criteria, we’ll buy them, and we’ll hold them until they either
# rise high enough (meeting our price target) or fall too low (meeting our ‘stop’ level.)
# currently programmed to not trade with margin
###

import sys
import alpaca_trade_api as tradeapi
import requests
import time
from ta.trend import macd, macd_diff, macd_signal
from ta.momentum import rsi
from ta.utils import dropna
from ta.volatility import BollingerBands
import numpy as np
from datetime import datetime, timedelta
from pytz import timezone
from config import *  # link to other file
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import logging
import os

# Redirect print
t = datetime.today()
file_name_format = '{year:04d}{month:02d}{day:02d}-' \
                   '{hour:02d}{minute:02d}{second:02d}_print.log'
file_name = file_name_format.format(year=t.year, month=t.month, day=t.day,
                                    hour=t.hour, minute=t.minute, second=t.second)

# set up logging to file - see previous section for more details
if not os.path.exists("day_trading_algo_paper_log"):
    os.makedirs("day_trading_algo_paper_log")

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)-8s %(message)s',
                    datefmt='%m-%d %H:%M',
                    filename='day_trading_algo_paper_log/' + file_name,
                    filemode='w')
# define a Handler which writes INFO messages or higher to the sys.stderr
console = logging.StreamHandler()
console.setLevel(logging.DEBUG)
# set a format which is simpler for console use
formatter = logging.Formatter('%(name)-12s: %(levelname)-8s %(message)s')
# tell the handler to use this format
console.setFormatter(formatter)
# add the handler to the root logger
logging.getLogger().addHandler(console)

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
default_stop = 0.95
# How much of our portfolio to allocate to any one position
risk = 0.05  # can change parameter to adjust; e.g. 0.05 * 30K = max of 1.5K going to any one position until we run out of cash
max_to_trade_with = 1000000  # max to trade with, or limit to portfolio value. arbitrarily high right now
# Minutes to wait before trading
min_to_wait = 15


def send_email(subject, body):
    signature = '''\n\nThis is an automated notification from a Python script.'''

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
    logging.info('Getting historical minute and daily data...')
    minute_history = {}
    daily_history = {}
    nyc = timezone('America/New_York')
    today = datetime.today().astimezone(nyc)
    today_str = datetime.today().astimezone(nyc).strftime('%Y-%m-%d')
    t_prev_str = (datetime.today().astimezone(nyc) - timedelta(days=61)).strftime('%Y-%m-%d')
    c = 0
    for symbol in symbols:
        # https://pypi.org/project/alpaca-trade-api/
        minute_history[symbol] = api.polygon.historic_agg_v2(
            timespan="minute", symbol=symbol, limit=1000, multiplier=1, _from=t_prev_str, to=today_str
        ).df
        logging.debug('Minute and daily history for symbol: %s', symbol)
        logging.debug(minute_history[symbol])
        daily_history[symbol] = api.polygon.historic_agg_v2(
            timespan="day", symbol=symbol, limit=60, multiplier=1, _from=t_prev_str, to=today_str
        ).df
        logging.debug(daily_history[symbol])
        c += 1

        logging.info('%d/%d : %s', c, len(symbols), symbol)
    return minute_history, daily_history


def get_tickers():
    logging.info('Getting current ticker data...')
    tickers = api.polygon.all_tickers()
    logging.info('Success.')
    assets = api.list_assets()
    symbols = [asset.symbol for asset in assets if
               asset.tradable]
    # logging.info(symbols) # if we want to see all symbols available
    # logging.info(tickers) - if we want to see all the tickers returned by polygons API

    return [ticker for ticker in tickers if (
            ticker.ticker in symbols and
            ticker.lastTrade['p'] >= min_share_price and  # don't buy if it's at a low
            ticker.lastTrade['p'] <= max_share_price and  # don't buy if it's at a high
            ticker.prevDay['v'] * ticker.lastTrade['p'] > min_last_dv  # $ volume should be above a certain amount
        #  and ticker.todaysChangePerc >= 3.5  # No restrictions on open vs yesterday
    )]


def find_stop(current_value, minute_history, now):
    series = minute_history['low'][-100:] \
        .dropna().resample('5min').min()  # group into 5 min chunks and take min
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
        logging.info('%6s - previous day close: $%8.2f; volume today: %12d', symbol, prev_closes[symbol],
                     volume_today[symbol])

    symbols = [ticker.ticker for ticker in tickers]
    logging.info('Tracking %d symbols.', len(symbols))
    minute_history, daily_history = get_1000m_history_data(symbols)
    # logging.info(minute_history) # prints top and bottom 10 if you want to see open, close, etc.

    cash_value = float(api.get_account().cash)
    equity = float(api.get_account().equity)
    logging.info('Portfolio value = $%1.2f', equity)

    open_orders = {}
    positions = {}

    # Cancel any existing open orders on watched symbols
    existing_orders = api.list_orders(limit=500)
    for order in existing_orders:
        if order.symbol in symbols:
            api.cancel_order(order.id)

    # Initialize arrays of stop prices and cost bases
    stop_prices = {}
    latest_cost_basis = {}

    # Track any positions bought during previous executions
    existing_positions = api.list_positions()
    for position in existing_positions:
        logging.info('Currently holding %d qty of stock %s', int(position.qty), position.symbol)

        #
        # if position.symbol in symbols:
        #     positions[position.symbol] = float(position.qty)
        #     # Recalculate cost basis and stop price
        #     latest_cost_basis[position.symbol] = float(position.cost_basis)
        #     stop_prices[position.symbol] = (
        #             float(position.cost_basis) * default_stop
        #     )
        # else:

        # if stock we hold isn't making it on the day trading list for today, let's sell it
        # logging.info('Stock is not in symbol tracking list')
        logging.info('We shouldn''t be holding stock %s right now so let''s sell it', position)
        sell_subj = 'Submitting sell for {:.0f} shares of {} at market price'.format(
            int(position.qty), position.symbol
        )

        curr_equity = float(api.get_account().equity)
        sell_body = 'Portfolio value = ${:,}'.format(curr_equity)

        send_email(sell_subj, sell_body)

        logging.info(sell_subj)

        o = api.submit_order(
            symbol=position.symbol, qty=position.qty, side='sell',
            type='market', time_in_force='day'
        )

    # Keep track of what we're buying/selling
    target_prices = {}
    partial_fills = {}

    # Use trade updates to keep track of our portfolio
    @conn.on(r'trade_update')
    async def handle_trade_update(conn, channel, data):
        # End if market's closed
        # if ((current_dt - market_close_dt).seconds // 60) > 15:
        #     conn.close()
        logging.info('Looking for updates to existing orders on Alpaca')
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
        # if ((current_dt - market_close_dt).seconds // 60) > 15:
        #     conn.close()

        symbol = data.symbol
        logging.info('Connecting to second-level data, watching: %s', symbol)

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
        # logging.info('Time since market open: %s', since_market_open)
        until_market_close = market_close_dt - ts

        # current cash on hand
        curr_cash = float(api.get_account().cash)
        logging.debug('Current cash on hand: %$1.2f', curr_cash)
        # Check for buy signals
        if (
                # since_market_open.seconds // 60 < 60  # by commenting out, we're allowing trades at all times of day
                until_market_close.seconds // 60 > 60 and  # let's not buy in the last hour of the day
                since_market_open.seconds // 60 > min_to_wait and
                curr_cash > 500  # if we have less than $500 available let's not bother trading
        ):

            # See if we've already bought in first
            position = positions.get(symbol, 0)
            if position > 0:
                return

            # See how high the price went during the first 10-15 minutes
            lbound = market_open_dt
            ubound = lbound + timedelta(minutes=min_to_wait)
            high_15m = 0
            try:
                high_15m = minute_history[symbol][lbound:ubound]['high'].max()
            except Exception as e:
                # Because we're aggregating on the fly, sometimes the datetime
                # index can get messy until it's healed by the minute bars
                return
            # logging.info('High during first 15 minutes: %1.2f',high_15m)
            # Get the change since yesterday's market close
            daily_pct_change = (
                    100.0 * (data.close - prev_closes[symbol]) / prev_closes[symbol]
            )
            logging.debug('Daily change: %1.2f %%',daily_pct_change)

            # check for a positive, increasing MACD
            hist_fast = macd_diff(
                daily_history[symbol]['close'].dropna(),
                n_fast=12,
                n_slow=26,
                n_sign=9
            )

            hist_slow = macd_diff(
                daily_history[symbol]['close'].dropna(),
                n_fast=40,
                n_slow=60,
                n_sign=9
            )  # check slower / less sensitive MACD

            # hist_fastest = macd_diff(
            #     daily_history[symbol]['close'].dropna(),
            #     n_fast=5,
            #     n_slow=35,
            #     n_sign=5
            # )  # check fastest macd

            # Check RSI indicator to make sure it's not overbought (>= 70 overbought, <= 30 oversold/undervalued)
            rsi_ind = rsi(daily_history[symbol]['close'].dropna())

            logging.debug('Last 3 MACD (12,26,9) values for %s: %1.2f, %1.2f, %1.2f', symbol, hist_fast[-3],
                         hist_fast[-2], hist_fast[-1])
            logging.debug('Last 3 MACD (40,60,9) values for %s: %1.2f, %1.2f, %1.2f', symbol, hist_slow[-3],
                          hist_slow[-2], hist_slow[-1])
            logging.debug('Last 3 RSI values for %s: %1.2f, %1.2f, %1.2f', symbol, rsi_ind[-3], rsi_ind[-2], rsi_ind[-1])

            if (
                    #  daily_pct_change > 2 and # since we're buying at all times of day, don't focus on daily % change
                    #  data.close > 0.95 * high_15m and  # at 95% of 15m high # since we're buying at all times of day, don't need to be near high
                    volume_today[symbol] > 30000
                    and hist_fast[-1] >= 0
                    and (hist_fast[-3] < hist_fast[-2] < hist_fast[-1])
                    and rsi_ind[-1] <= .33
                    and hist_slow[-1] >= 0
                    and np.diff(hist_slow)[-1] >= 0  # exit if MACD < 0 or 2nd order derivative shows slowing
            ):

                # Stock has passed all checks; figure out how much to buy
                stop_price = find_stop(
                    data.close, daily_history[symbol], ts
                )

                stop_prices[symbol] = stop_price

                target_prices[symbol] = data.close + (
                        (data.close - stop_price) * 1.5  # goal is to sell 1.5x more than downside threshold
                )

                logging.debug('Stop price for %s: $%1.2f', symbol, stop_prices[symbol])
                logging.debug('Target price for %s: $%1.2f}', symbol, target_prices[symbol])

                # use risk * starting cash value as your leading indicator,
                # but make sure this is below max to trade with and the amount of cash in your account
                shares_to_buy = min(
                    min(cash_value, max_to_trade_with) * risk
                    , curr_cash) // data.close

                if shares_to_buy <= 0:  # exit if we don't want to buy any shares
                    return
                else:
                    shares_to_buy -= positions.get(symbol, 0)

                logging.info('Shares to buy for %s: $%1.2f}', symbol, shares_to_buy)

                buy_subj = 'Submitting buy for {:.0f} shares of {} at ${:0,.2f}'.format(
                    shares_to_buy, symbol, data.close
                )

                curr_equity = float(api.get_account().equity)
                buy_body = 'Portfolio value = ${:,}'.format(curr_equity)

                logging.info(buy_subj)
                send_email(buy_subj, buy_body)

                try:
                    o = api.submit_order(
                        symbol=symbol, qty=str(shares_to_buy), side='buy',
                        type='limit', time_in_force='day',
                        limit_price=str(data.close),
                        stop_loss=dict(stop_price=stop_price)
                    )
                    open_orders[symbol] = o
                    latest_cost_basis[symbol] = data.close
                except Exception as e:
                    logging.info(e)
                return
        if (
                since_market_open.seconds // 60 >= 24 and  # has been at least 24 mins since market opened
                until_market_close.seconds // 60 > 20  # greater than 20 minutes before market closes
        ):
            # Check for liquidation signals

            # We can't liquidate if there's no position
            position = positions.get(symbol, 0)
            if position == 0:
                return

            # Sell for a loss if it's fallen below our stop price
            # Sell for a loss if it's below our cost basis and MACD < 0
            # Sell for a profit if it's above our target price
            hist_med = macd_diff(
                daily_history[symbol]['close'].dropna(),
                n_fast=19,
                n_slow=39,
                n_sign=9
            )  # using a slightly slower MACD for exit
            if (
                    data.close <= stop_prices[symbol] or
                    (data.close >= target_prices[symbol] and hist_med[-1] < 0) or
                    (data.close <= latest_cost_basis[symbol] and hist_med[-1] < 0)
            ):
                sell_subj = 'Submitting sell for {:.0f} shares of {} at ${:0,.2f}'.format(
                    position, symbol, data.close
                )

                curr_equity = float(api.get_account().equity)
                sell_body = 'Portfolio value = ${:,}'.format(curr_equity)

                send_email(sell_subj, sell_body)

                logging.info(sell_subj)

                try:
                    o = api.submit_order(
                        symbol=symbol, qty=str(position), side='sell',
                        type='limit', time_in_force='day',
                        limit_price=str(data.close)
                    )
                    open_orders[symbol] = o
                    latest_cost_basis[symbol] = data.close
                except Exception as e:
                    logging.info(e)
            return
        elif (
                until_market_close.seconds // 60 <= 20
        ):
            # Liquidate remaining positions on watched symbols at market
            try:
                position = api.get_position(symbol)
                logging.info('Trading over, liquidating remaining position in %s', symbol)

                api.submit_order(
                    symbol=symbol, qty=position.qty, side='sell',
                    type='market', time_in_force='day'
                )
                symbols.remove(symbol)
                if len(symbols) <= 0:
                    conn.close()
                    sys.exit('Trading day over')
                conn.deregister([
                    'A.{}'.format(symbol),
                    'AM.{}'.format(symbol)
                ])
            except Exception as e:
                # Exception here indicates that we have no position
                logging.info('No positions left to liquidate')
                return
        elif (
                since_market_open.seconds // 60 <= -12  # more than 12 hours until next trading day
        ):
            conn.close('Market has closed')

    # Replace aggregated 1s bars with incoming 1m bars
    @conn.on(r'AM$')
    async def handle_minute_bar(conn, channel, data):
        # End if market's closed
        # if ((current_dt - market_close_dt).seconds // 60) > 15:
        #     conn.close('Market has closed')

        logging.info('Connecting to minute-level data, watching: %s', data.symbol)

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

    # logging.info('Channels being watched: ',channels)
    logging.info('Watching %d symbols.', len(symbols))

    run_ws(conn, channels)


# Function to liquidate current positions
def liquidate_current_positions():
    # Get a list of all of our positions.
    portfolio = api.list_positions()

    # Print the quantity of shares for each position.
    for position in portfolio:
        sell_subj = 'Submitting sell for {:.0f} shares of {} at market price'.format(
            int(position.qty), position.symbol
        )

        curr_equity = float(api.get_account().equity)
        sell_body = 'Portfolio value = ${:,}'.format(curr_equity)

        send_email(sell_subj, sell_body)

        logging.info(sell_subj)

        o = api.submit_order(
            symbol=position.symbol, qty=position.qty, side='sell',
            type='market', time_in_force='day'
        )

    sys.exit('Liquidated all positions, trading day over')



# Handle failed websocket connections by reconnecting
def run_ws(conn, channels):
    try:
        # Close if Polygon won't be available
        today = datetime.today().astimezone(nyc)
        if (today.hour < 4 or today.hour > 20 or today.weekday() > 4):
            sys.exit('Outside of Polygon streaming hours')

        conn.run(channels)
        logging.info('Running')
    except Exception as e:
        logging.info(e)
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
    logging.info('Current time: %s', current_dt)
    logging.info('Market open time: %s', market_open)
    logging.info('Market close time: %s', market_close)
    since_market_open = current_dt - market_open
    if (current_dt < market_open):
        logging.info('Time until market opens: %s', market_open - current_dt)
    else:
        logging.info('Time since market open: %s', since_market_open)

    while (
            since_market_open.seconds // 60 < (
            min_to_wait - 1) or since_market_open.days != 0):  # sleep until we're 10-15 mins before we might want to trade
        time.sleep(30)
        current_dt = datetime.today().astimezone(nyc)
        since_market_open = current_dt - market_open
        if (current_dt < market_open):
            logging.info('Time until market opens: %f', market_open - current_dt)
        else:
            logging.info('Time since market open: %f', since_market_open)

    run(get_tickers(), market_open, market_close)
