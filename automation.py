"""
DISCLAIMER: 

This software is provided solely for educational and research purposes. 
It is not intended to provide investment advice, and no investment recommendations are made herein. 
The developers are not financial advisors and accept no responsibility for any financial decisions or losses resulting from the use of this software. 
Always consult a professional financial advisor before making any investment decisions.
"""


import requests
import yfinance as yf
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from scipy.interpolate import interp1d
import numpy as np
import threading
import urllib.parse
import os
from dotenv import load_dotenv
import argparse
import time as time_module
from alpaca_integration import get_alpaca_option_chain, init_alpaca_client
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest, OptionSnapshotRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestBarRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import Adjustment, DataFeed
import pandas as pd

# Load environment variables from .env file
load_dotenv()
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")
EASTERN = ZoneInfo("America/New_York")


def get_json_with_retries(url, attempts=3):
    """Fetch JSON without allowing an upstream request to hang the workflow."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, timeout=(5, 20))
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as error:
            last_error = error
            if attempt < attempts:
                time_module.sleep(2 ** (attempt - 1))
    raise RuntimeError(
        f"Earnings data request failed after {attempts} attempts: {type(last_error).__name__}"
    ) from last_error

def filter_dates(dates):
    today = datetime.now(EASTERN).date()
    cutoff_date = today + timedelta(days=45)
    
    sorted_dates = sorted(datetime.strptime(date, "%Y-%m-%d").date() for date in dates)

    arr = []
    for i, date in enumerate(sorted_dates):
        if date >= cutoff_date:
            arr = [d.strftime("%Y-%m-%d") for d in sorted_dates[:i+1]]  
            break
    
    if len(arr) > 0:
        if arr[0] == today.strftime("%Y-%m-%d"):
            return arr[1:]
        return arr

    raise ValueError("No date 45 days or more in the future found.")


def yang_zhang(price_data, window=30, trading_periods=252, return_last_only=True):
    """Return rolling annualized Yang-Zhang volatility.

    Yang-Zhang combines de-meaned overnight and open-to-close variances with
    the Rogers-Satchell range estimator.  Close-to-close squared returns are
    not a substitute here because they double-count the overnight move.
    """
    required_columns = ['Open', 'High', 'Low', 'Close']
    if any(column not in price_data for column in required_columns):
        raise ValueError("Yang-Zhang requires Open, High, Low, and Close columns.")
    prices = price_data[required_columns].astype(float).dropna()
    if len(prices) < window + 1:
        raise ValueError(f"Yang-Zhang {window}-day volatility requires at least {window + 1} complete bars.")
    if (prices <= 0).any().any():
        raise ValueError("Yang-Zhang requires strictly positive OHLC prices.")

    log_ho = np.log(prices['High'] / prices['Open'])
    log_lo = np.log(prices['Low'] / prices['Open'])
    log_co = np.log(prices['Close'] / prices['Open'])

    overnight_return = np.log(prices['Open'] / prices['Close'].shift(1))
    open_to_close_return = log_co
    rs = log_ho * (log_ho - log_co) + log_lo * (log_lo - log_co)

    overnight_var = overnight_return.rolling(window=window, center=False).var(ddof=1)
    open_to_close_var = open_to_close_return.rolling(window=window, center=False).var(ddof=1)
    window_rs = rs.rolling(window=window, center=False).mean()

    k = 0.34 / (1.34 + ((window + 1) / (window - 1)) )
    variance = overnight_var + k * open_to_close_var + (1 - k) * window_rs
    result = variance.clip(lower=0).apply(np.sqrt) * np.sqrt(trading_periods)

    if return_last_only:
        value = result.iloc[-1]
        if not np.isfinite(value) or value <= 0:
            raise ValueError(f"Yang-Zhang {window}-day volatility is not finite and positive.")
        return value
    else:
        return result.dropna()


def completed_daily_history(price_data, as_of=None):
    """Exclude a still-forming current-session daily bar from screening."""
    if price_data is None or price_data.empty:
        return price_data
    completed = price_data.copy()
    timestamps = pd.DatetimeIndex(pd.to_datetime(completed.index, errors='coerce'))
    if timestamps.tz is None:
        timestamps = timestamps.tz_localize(EASTERN)
    else:
        timestamps = timestamps.tz_convert(EASTERN)
    valid = ~timestamps.isna()
    completed = completed.loc[valid]
    timestamps = timestamps[valid]
    now_eastern = (as_of or datetime.now(timezone.utc)).astimezone(EASTERN)
    if now_eastern.hour < 16:
        completed = completed.loc[np.asarray(timestamps.date) < now_eastern.date()]
    return completed
    

def build_term_structure(days, ivs):
    days = np.array(days)
    ivs = np.array(ivs)

    if len(np.unique(days)) < 2:
        raise ValueError("Not enough unique expiry dates to build term structure.")

    sort_idx = days.argsort()
    days = days[sort_idx]
    ivs = ivs[sort_idx]


    spline = interp1d(days, ivs, kind='linear', fill_value="extrapolate")

    def term_spline(dte):
        if dte < days[0]:  
            return ivs[0]
        elif dte > days[-1]:
            return ivs[-1]
        else:  
            return float(spline(dte))

    return term_spline

def get_current_price(ticker):
    todays_data = ticker.history(period='1d')
    return todays_data['Close'].iloc[0]

def compute_recommendation(ticker):
    try:
        ticker = ticker.strip().upper()
        if not ticker:
            return "No stock symbol provided."
        # Try Alpaca first
        option_chain = get_alpaca_option_chain(ticker)
        atm_iv = {}
        straddle = None
        alpaca_success = False
        if option_chain:
            try:
                print(f"[{ticker}] Attempting to use Alpaca option chain data")
                exp_dates = sorted(option_chain.keys())
                # apply 45-day window and drop 0DTE using filter_dates()
                try:
                    exp_dates_filtered = filter_dates(exp_dates)
                except ValueError:
                    print(f"[{ticker}] Not enough option data from Alpaca")
                    return "Error: Not enough option data."
                underlying_price = None
                try:
                    API_KEY = os.environ.get("APCA_API_KEY_ID")
                    API_SECRET = os.environ.get("APCA_API_SECRET_KEY")
                    stock_client = StockHistoricalDataClient(API_KEY, API_SECRET)
                    bar_resp = stock_client.get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols=ticker))
                    if bar_resp and ticker.upper() in bar_resp:
                        underlying_price = bar_resp[ticker.upper()].close
                        print(f"[{ticker}] Got current price from Alpaca: {underlying_price}")
                except Exception as e:
                    print(f"[{ticker}] Error getting Alpaca current price: {e}")
                    pass
                if underlying_price is None:
                    stock = yf.Ticker(ticker)
                    underlying_price = stock.history(period='1d')['Close'].iloc[0]
                    print(f"[{ticker}] Using Yahoo for current price: {underlying_price}")
                options_client = OptionHistoricalDataClient(
                    api_key=os.environ.get("APCA_API_KEY_ID"),
                    secret_key=os.environ.get("APCA_API_SECRET_KEY")
                )
                for exp_date in exp_dates_filtered:
                    strikes = option_chain[exp_date].keys()
                    if not strikes:
                        continue
                    sorted_strikes = sorted(strikes, key=lambda s: abs(s - underlying_price))
                    for strike in sorted_strikes:
                        call_contract = option_chain[exp_date][strike].get('call')
                        put_contract  = option_chain[exp_date][strike].get('put')
                        if not call_contract or not put_contract:
                            continue
                        call_symbol = call_contract.symbol
                        put_symbol  = put_contract.symbol
                        req         = OptionSnapshotRequest(symbol_or_symbols=[call_symbol, put_symbol])
                        snap_resp   = options_client.get_option_snapshot(req)
                        call_snap   = snap_resp.get(call_symbol)
                        put_snap    = snap_resp.get(put_symbol)
                        if not call_snap or not put_snap:
                            continue
                        call_quote = call_snap.latest_quote
                        put_quote  = put_snap.latest_quote
                        if not call_quote or not put_quote:
                            continue
                        call_bid   = call_quote.bid_price
                        call_ask   = call_quote.ask_price
                        put_bid    = put_quote.bid_price
                        put_ask    = put_quote.ask_price
                        call_iv    = call_snap.implied_volatility
                        put_iv     = put_snap.implied_volatility
                        if call_iv is None or put_iv is None:
                            continue
                        atm_iv_value = (call_iv + put_iv) / 2.0
                        atm_iv[exp_date] = atm_iv_value
                        if straddle is None:
                            if None not in (call_bid, call_ask, put_bid, put_ask):
                                call_mid = (call_bid + call_ask) / 2.0
                                put_mid = (put_bid + put_ask) / 2.0
                                straddle = (call_mid + put_mid)
                        break
                    else:
                        # no valid IV on nearby strikes, skip this expiry
                        continue
                # Only accept Alpaca data if there are at least two expiries worth of IVs
                if len(atm_iv) >= 2:
                    alpaca_success = True
                    print(f"[{ticker}] Successfully retrieved Alpaca IV data for {len(atm_iv)} expiries")
                    # Calculate term structure from Alpaca IV data
                    today = datetime.now(EASTERN).date()
                    dtes = []
                    ivs = []
                    for exp_date, iv in atm_iv.items():
                        exp_date_obj = datetime.strptime(exp_date, "%Y-%m-%d").date()
                        days_to_expiry = (exp_date_obj - today).days
                        dtes.append(days_to_expiry)
                        ivs.append(iv)
                    term_spline = build_term_structure(dtes, ivs)
                    ts_slope_0_45 = (term_spline(45) - term_spline(dtes[0])) / (45-dtes[0])
                    
                    # Now that we have Alpaca IV data, calculate RV using Alpaca data too
                    print(f"[{ticker}] Attempting to calculate RV using Alpaca price history...")
                    try:
                        now_utc = datetime.now(timezone.utc)
                        end_dt = now_utc
                        start_dt = end_dt - timedelta(days=90)  # Get 3 months of data
                        
                        bars_request = StockBarsRequest(
                            symbol_or_symbols=ticker,
                            timeframe=TimeFrame.Day,
                            start=start_dt,
                            end=end_dt,
                            feed=DataFeed.IEX,
                            adjustment=Adjustment.ALL
                        )
                        
                        bars_response = stock_client.get_stock_bars(bars_request)
                        
                        # Process bar data for RV calculation
                        ticker_data_list = []
                        if bars_response:
                            try:
                                # Try to access data properly - different Alpaca API versions may structure data differently
                                if hasattr(bars_response, 'data') and hasattr(bars_response.data, 'get'):
                                    # Newer Alpaca SDK structure
                                    ticker_data_list = bars_response.data.get(ticker, [])
                                else:
                                    # Direct dictionary access (older style)
                                    ticker_data_list = bars_response[ticker]
                            except (KeyError, AttributeError) as e:
                                print(f"[{ticker}] Error accessing bars data structure: {e}")
                                
                            
                            if ticker_data_list:
                                bars_data = []
                                for bar in ticker_data_list:
                                    bars_data.append({
                                        'Open': bar.open,
                                        'High': bar.high,
                                        'Low': bar.low,
                                        'Close': bar.close,
                                        'Volume': bar.volume,
                                        'Date': bar.timestamp
                                    })
                                
                                if bars_data:
                                    price_df = pd.DataFrame(bars_data)
                                    price_df.set_index('Date', inplace=True)
                                    price_df.sort_index(inplace=True)  # Ensure data is sorted by date
                                    price_df = completed_daily_history(price_df)

                                    # Calculate RV using Yang-Zhang
                                    rv30 = yang_zhang(price_df)
                                    iv30_rv30 = term_spline(30) / rv30
                                    print(f"[{ticker}] USING ALPACA FOR BOTH IV AND RV. IV30={term_spline(30):.4f}, RV30={rv30:.4f}, Ratio={iv30_rv30:.4f}")
                                    
                                    # Always use Yahoo for average volume calculation
                                    print(f"[{ticker}] Fetching volume data from Yahoo Finance")
                                    stock_yf = yf.Ticker(ticker)
                                    price_history = completed_daily_history(
                                        stock_yf.history(period='3mo', auto_adjust=True)
                                    )
                                    avg_volume = price_history['Volume'].rolling(30).mean().dropna().iloc[-1]
                                    
                                    expected_move = str(round(straddle / underlying_price * 100, 2)) + "%" if straddle else None
                                    
                                    return {'avg_volume': avg_volume >= 1500000, 
                                            'iv30_rv30': iv30_rv30 >= 1.25, 
                                            'ts_slope_0_45': ts_slope_0_45 <= -0.00406, 
                                            'expected_move': expected_move}
                                else:
                                    print(f"[{ticker}] Not enough bars from Alpaca (need >= 31, got {len(bars_data)}). Falling back to Yahoo.")
                            else:
                                print(f"[{ticker}] No bar data found in the Alpaca response. Falling back to Yahoo.")
                    except Exception as e:
                        print(f"[{ticker}] Error calculating RV from Alpaca data: {e}. Falling back to Yahoo.")
            except Exception as e:
                print(f"[{ticker}] Alpaca option chain processing error: {e}")

        # Use Yahoo Finance for both IV and RV if Alpaca failed
        print(f"[{ticker}] USING YAHOO FINANCE FOR BOTH IV AND RV CALCULATIONS")
        try:
            stock = yf.Ticker(ticker)
            if len(stock.options) == 0:
                raise KeyError()
        except KeyError:
            return f"Error: No options found for stock symbol '{ticker}'."
        exp_dates = list(stock.options)
        try:
            exp_dates = filter_dates(exp_dates)
        except:
            return "Error: Not enough option data."
        options_chains = {}
        for exp_date in exp_dates:
            options_chains[exp_date] = stock.option_chain(exp_date)
        try:
            underlying_price = stock.history(period='1d')['Close'].iloc[0]
        except Exception:
            return "Error: Unable to retrieve underlying stock price."
        i = 0
        atm_iv = {}  # Reset atm_iv for Yahoo data
        straddle = None  # Reset straddle for Yahoo data
        for exp_date, chain in options_chains.items():
            calls = getattr(chain, 'calls', None)
            puts = getattr(chain, 'puts', None)
            if calls is None or puts is None or calls.empty or puts.empty:
                continue
            call_diffs = (calls['strike'] - underlying_price).abs()
            call_idx = call_diffs.idxmin()
            call_iv = calls.loc[call_idx, 'impliedVolatility']
            put_diffs = (puts['strike'] - underlying_price).abs()
            put_idx = put_diffs.idxmin()
            put_iv = puts.loc[put_idx, 'impliedVolatility']
            atm_iv_value = (call_iv + put_iv) / 2.0
            atm_iv[exp_date] = atm_iv_value
            if i == 0:
                call_bid = calls.loc[call_idx, 'bid']
                call_ask = calls.loc[call_idx, 'ask']
                put_bid = puts.loc[put_idx, 'bid']
                put_ask = puts.loc[put_idx, 'ask']
                if call_bid is not None and call_ask is not None:
                    call_mid = (call_bid + call_ask) / 2.0
                else:
                    call_mid = None
                if put_bid is not None and put_ask is not None:
                    put_mid = (put_bid + put_ask) / 2.0
                else:
                    put_mid = None
                if call_mid is not None and put_mid is not None:
                    straddle = (call_mid + put_mid)
            i += 1
        if not atm_iv:
            return "Error: Could not determine ATM IV for any expiration dates."
        today = datetime.now(EASTERN).date()
        dtes = []
        ivs = []
        for exp_date, iv in atm_iv.items():
            exp_date_obj = datetime.strptime(exp_date, "%Y-%m-%d").date()
            days_to_expiry = (exp_date_obj - today).days
            dtes.append(days_to_expiry)
            ivs.append(iv)
        term_spline = build_term_structure(dtes, ivs)
        ts_slope_0_45 = (term_spline(45) - term_spline(dtes[0])) / (45-dtes[0])
        
        # Use Yahoo for RV calculation
        price_history = completed_daily_history(
            stock.history(period='3mo', auto_adjust=True)
        )
        rv30 = yang_zhang(price_history)
        iv30_rv30 = term_spline(30) / rv30
        print(f"[{ticker}] Yahoo IV30={term_spline(30):.4f}, RV30={rv30:.4f}, Ratio={iv30_rv30:.4f}")
        avg_volume = price_history['Volume'].rolling(30).mean().dropna().iloc[-1]
        expected_move = str(round(straddle / underlying_price * 100,2)) + "%" if straddle else None
        return {'avg_volume': avg_volume >= 1500000, 'iv30_rv30': iv30_rv30 >= 1.25, 'ts_slope_0_45': ts_slope_0_45 <= -0.00406, 'expected_move': expected_move}
    except Exception as e:
        print(f"Error for {ticker}: {e}")
        return f"Error: {e}"
        

def get_tomorrows_earnings(next_open=None):
    """Return earnings for Alpaca's next confirmed paper-market session."""
    if next_open is None:
        next_open = init_alpaca_client().get_clock().next_open
    if isinstance(next_open, datetime):
        next_open_date = next_open.astimezone(EASTERN).date()
    elif hasattr(next_open, "isoformat"):
        next_open_date = next_open
    else:
        raise RuntimeError("Alpaca PAPER clock did not provide a valid next-open date")
    tomorrow = next_open_date.strftime('%Y-%m-%d')
    base_url = "https://www.dolthub.com/api/v1alpha1/post-no-preference/earnings/master"
    query = f"SELECT * FROM `earnings_calendar` where date = '{tomorrow}' ORDER BY `act_symbol` ASC, `date` ASC LIMIT 1000;"
    url = f"{base_url}?q={urllib.parse.quote(query)}"
    data = get_json_with_retries(url)
    # Return a list of dicts with act_symbol and when
    tickers = [
        {'act_symbol': row['act_symbol'], 'when': row.get('when')}
        for row in data.get('rows', []) if 'act_symbol' in row
    ]
    return tickers

def get_todays_earnings():
    today = datetime.now(EASTERN).strftime('%Y-%m-%d')
    base_url = "https://www.dolthub.com/api/v1alpha1/post-no-preference/earnings/master"
    query = f"SELECT * FROM `earnings_calendar` where date = '{today}' ORDER BY `act_symbol` ASC, `date` ASC LIMIT 1000;"
    url = f"{base_url}?q={urllib.parse.quote(query)}"
    data = get_json_with_retries(url)
    # Return a list of dicts with act_symbol and when
    tickers = [
        {'act_symbol': row['act_symbol'], 'when': row.get('when')}
        for row in data.get('rows', []) if 'act_symbol' in row
    ]
    return tickers

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ignore-filters', action='store_true', help='Print all results regardless of filter criteria')
    args = parser.parse_args()
    ignore_filters = args.ignore_filters

    # Process AMC earnings for today
    todays = get_todays_earnings()
    amc_tickers = [t for t in todays if t.get('when') and 'after' in t['when'].lower()]
    print("\n--- AMC Earnings (Today) ---")
    results_amc = []
    for ticker in amc_tickers:
        try:
            symbol = ticker['act_symbol'] if isinstance(ticker, dict) else ticker
            result = compute_recommendation(symbol)
            if ignore_filters:
                results_amc.append({'ticker': symbol, 'result': result})
            else:
                if (
                    isinstance(result, dict)
                    and result.get('avg_volume')
                    and result.get('iv30_rv30')
                    and result.get('ts_slope_0_45')
                ):
                    results_amc.append({'ticker': symbol, 'result': result})
        except Exception as e:
            print(f"Error for {ticker}: {e}")
            continue
    for entry in results_amc:
        print(entry)

    # Process BMO earnings for tomorrow
    tomorrows = get_tomorrows_earnings()
    bmo_tickers = [t for t in tomorrows if t.get('when') and 'before' in t['when'].lower()]
    print("\n--- BMO Earnings (Tomorrow) ---")
    results_bmo = []
    for ticker in bmo_tickers:
        try:
            symbol = ticker['act_symbol'] if isinstance(ticker, dict) else ticker
            result = compute_recommendation(symbol)
            if ignore_filters:
                results_bmo.append({'ticker': symbol, 'result': result})
            else:
                if (
                    isinstance(result, dict)
                    and result.get('avg_volume')
                    and result.get('iv30_rv30')
                    and result.get('ts_slope_0_45')
                ):
                    results_bmo.append({'ticker': symbol, 'result': result})
        except Exception as e:
            print(f"Error for {ticker}: {e}")
            continue
    for entry in results_bmo:
        print(entry)

if __name__ == "__main__":
    main()
