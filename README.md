# Earnings Trade Automation

Automated trading bot for executing earnings calendar spread strategies using options in a workflow automation. Integrates with Google Sheets for trade tracking and Alpaca for order execution. 

## Features
- **Automated Earnings Calendar Spread Trading**: Opens and closes calendar spreads around earnings events based on strict screening criteria.
- **Position Sizing**: A fixed 6% of equity per position by default, or a 10% Kelly fraction once your backtest's win rate and average win and loss are set.
- **Optional Google Sheets Integration**: Queues trade updates in SQLite and syncs them separately through Apps Script.
- **Alpaca API Integration**: Places and closes trades automatically using Alpaca brokerage API.
- **Configurable and Extensible**: Modular codebase for easy strategy tweaks and integration.

## Strategy Overview
We implement an earnings volatility selling strategy focusing on calendar spreads around earnings events:

- Rationale: Implied volatility spikes ahead of earnings due to hedgers and speculators, creating an opportunity to profit from IV crush and muted stock moves.
- Trade Structure: At-the-money calendar spreads with a 30-day expiration gap for stability, offering controlled risk compared to straddles. The short leg is the first expiry after the session the position closes in, so it never expires on its exit day; the long leg is the expiry closest to 30 days after it.
- Screening Criteria: Filter for high-probability trades using:
  - **Term Structure Slope**: Negative slope between front-month and 45-day expirations (backwardation), at most −0.00406.
  - **30-Day Average Volume**: At least 1.5 million shares, for sufficient liquidity and price-insensitive demand.
  - **IV/RV Ratio**: 30-day implied volatility at least 1.25 times realized volatility, which is estimated using the 30-day Yang–Zhang estimator.
- Position Sizing: A fixed share of equity until Kelly inputs are configured, then a 10% Kelly fraction (see Settings).

## Quick Start

### 1. Clone the Repository
```bash
git clone https://github.com/<your-account>/earnings-trade-automation.git
cd earnings-trade-automation
```

### 2. Google Sheets Set Up (Optional)
Create a copy of https://docs.google.com/spreadsheets/d/1qOu4PJtcpYwLZgFFIpVr8FXD12dXoWZaKxSeg9FR7lU/ and add code.gs to App Script

Sheet synchronization is optional. Without `GOOGLE_SCRIPT_URL` and `GOOGLE_SCRIPT_SECRET`, trade updates remain queued in SQLite and the workflow continues. When configured, a separate best-effort step delivers queued events through the connected Sheet's Apps Script deployment; a Sheet failure does not block reconciliation, position management, or new PAPER orders.

After changing `code.gs`, create a **New deployment** (**Deploy > New deployment**, type **Web app**) with the same settings as the current one: **Execute as: Me** and **Who has access: Anyone**. The bot's requests carry no Google sign-in and are checked against `GOOGLE_SCRIPT_SECRET` instead, so narrower access breaks the sync. Each deployment has its own ID, and its URL, `https://script.google.com/macros/s/<deployment ID>/exec`, changes with it: set the `GOOGLE_SCRIPT_URL` secret to the new URL. Archive the old deployment only after a run's Sheet sync succeeds against the new one. The `GOOGLE_SCRIPT_SECRET` script property belongs to the project and carries over.

The first authenticated trade update adds any missing tracking columns to the template and writes each trade as one row holding its entry and exit, folding in rows left by the older one-row-per-fill version and preserving historical trades. No manual entry is needed. Trading runs and market-closed runs both send queued updates, and the Actions summary reports how many are pending and how long the oldest has waited.

To authenticate requests, generate one long random secret and store the same value in both places:

- In Apps Script, open **Project Settings > Script Properties** and add `GOOGLE_SCRIPT_SECRET`.
- In GitHub, open **Settings > Secrets and variables > Actions** and add `GOOGLE_SCRIPT_SECRET`.

Never commit this secret, place it in Sheet cells, or print it in logs. The Apps Script source uses `@OnlyCurrentDoc` so its spreadsheet access is limited to the connected Sheet.

### 3. Set Up Environment Variables
Create a `.env` file in the root directory with your credentials:
```
APCA_API_KEY_ID=your-alpaca-key
APCA_API_SECRET_KEY=your-alpaca-secret
GOOGLE_SCRIPT_URL=your-google-apps-script-url
GOOGLE_SCRIPT_SECRET=your-shared-secret
ALPACA_PAPER=true  # 'false' for live trading, with the live APCA_API_BASE_URL and a separate TRADES_DB_PATH
APCA_API_BASE_URL=https://paper-api.alpaca.markets
```

### 4. Install Dependencies
```bash
pip install -r requirements.txt
```

### 5. Reset Local Trade Database 
`trades.db` is the bot's durable ledger, bound to one Alpaca account. In a fork that will trade a different account, delete it before the first run. Never delete it in a running deployment: the bot would lose its record of open positions and orders.
```bash
rm trades.db  # Mac/Linux
del trades.db # Windows (PowerShell)
```

### 6. Run the Bot
```bash
python trade_workflow.py                   # reconcile, close due positions, open new ones
python trade_workflow.py --reconcile-only  # compare the broker with the ledger; changes nothing
python trade_workflow.py --sync-sheet      # send queued Sheet updates; never contacts Alpaca
```
`python automation.py` only prints the day's screened earnings candidates; it does not trade.

### 7. Automate with GitHub Actions
- Fork the repository to your GitHub account (required to enable Actions).
- In your fork, navigate to **Settings > Secrets and variables > Actions** and add the secrets `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY`, plus `GOOGLE_SCRIPT_URL` and `GOOGLE_SCRIPT_SECRET` for the Sheet. The workflow sets `ALPACA_PAPER` and `APCA_API_BASE_URL` itself, so secrets or variables with those names are ignored.
- Enable the GitHub Actions workflow in the **Actions** tab.

#### Workflow Modes
- `paper-trade`: Reconciles state first, then permits Alpaca PAPER orders.
- `reconcile-only`: The default manual mode; compares the broker with the ledger, never submits orders, never changes the ledger, and never syncs the Sheet.
- `market-closed`: A scheduled run while the market is closed. It never trades or reconciles; it only sends queued Sheet updates and commits the ledger changes that mark them sent.

#### Settings
Set these as repository variables under **Settings > Secrets and variables > Actions > Variables**. Leave one unset to use its default.

| Variable | Default | Meaning |
|---|---|---|
| `ENTRY_WINDOW_MINUTES` | `240` | New entries may start this many minutes before the close (from noon on a regular session) and stop 3 minutes before it, so a run GitHub starts late can still trade. `25` restores the strategy's late-day entry. |
| `QUOTE_MAX_AGE_SECONDS` | `120` | Oldest option quote accepted for pricing an entry. Thinly traded contracts often keep an unchanged quote for more than 30 seconds. |
| `MAX_CLOSE_DEBIT` | `0.50` | Most a due close pays per spread as a net debit while its short leg has time left. |
| `EXPIRY_MARKET_CLOSE_DAYS` | `2` | Once a due close's short leg expires within this many weekdays (0 means its expiry day only), the close may pay the market's price above `MAX_CLOSE_DEBIT`, so the short isn't left to be assigned. |
| `CLOSE_QUOTE_MAX_AGE_SECONDS` | `900` | Oldest option quote accepted for pricing a due close, up to `3600`. An option near expiry can keep an unchanged quote for many minutes; a close is still a limit order priced from that quote. |
| `OPEN_MAX_DEBIT_SPREAD_FRACTION` | `1` | How far from the spread's midpoint toward its ask an opening order may go. Orders start at the midpoint and step up a tenth of the spread's width at a time; on Alpaca PAPER no first order at the midpoint has filled, and fills come near the ask. |
| `OPEN_CEILING_FOLLOW_PCT` | `0.05` | If the ask rises during an entry, the price may follow it up to this share above the planned ceiling (0 to 0.25), still within the position and exposure limits. |
| `OPEN_MAX_SPREAD_WIDTH_PCT` | unset | Skip an entry whose net spread (ask − bid) is wider than this share of its midpoint, e.g. `0.75`. Unset accepts any width; every entry filled through October 2026 was at least 30% wide. Each run logs every candidate's width. |
| `KELLY_WIN_RATE` | unset | Expected share of winning trades from your backtest, e.g. `0.60`. |
| `KELLY_AVG_WIN` | unset | Average winning trade as a fraction of the debit paid, e.g. `0.40` for +40%. |
| `KELLY_AVG_LOSS` | unset | Average losing trade as a positive fraction of the debit paid, e.g. `0.30` for −30%. |
| `KELLY_FRACTION` | `0.10` | Share of full Kelly to bet. |
| `POSITION_ALLOCATION_PCT` | `0.06` | Fixed share of equity per position, used until all three Kelly inputs are set. |
| `MAX_AGGREGATE_EXPOSURE_PCT` | `0.36` | Cap on total open exposure as a share of equity. |
| `EXPOSURE_CAP_HEADROOM_PCT` | `0.05` | Share of the exposure cap kept free when sizing an entry (0 to 0.5), so equity moving during the entry doesn't push its later orders over the cap. `0` sizes up to the full cap. |

With the Kelly inputs set, each position gets `KELLY_FRACTION × (W − (1 − W) ÷ R)` of equity, where `W` is the win rate and `R` is the average win divided by the average loss. For example, `W = 0.60`, average win `0.40` and average loss `0.30` give a full Kelly of 30% and a 10% Kelly allocation of 3%. When the inputs show no edge, the bot opens nothing. Each run logs the sizing it used.

`MIN_LONG_LEG_CLOSE_PRICE` (`0.01`) and `OPEN_MAX_DEBIT_SLIPPAGE` (`0.05`) can be set only in `.env`; the workflow does not pass them through.

Earnings calendar rows with no before/after-market time are looked up on Yahoo Finance when the stock's 30-day average volume passes the screen; rows Yahoo cannot place stay skipped.

#### On-Time Runs With an External Scheduler
GitHub can start scheduled runs hours late, which delays exits past 9:40 ET. Runs started through `workflow_dispatch` are not held in the scheduled-run queue, so an external scheduler gives on-time exits. Like every run, they still wait for a run already in progress:

1. Create a fine-grained personal access token for this repository only, with **Actions: Read and write** permission.
2. In any cron service, schedule a weekday request at 9:45 ET (and, for late-day entries, at 15:36 ET):

```
POST https://api.github.com/repos/<owner>/<repo>/actions/workflows/config.yml/dispatches
Authorization: Bearer <token>
Accept: application/vnd.github+json

{"ref": "main", "inputs": {"mode": "paper-trade"}}
```

Dispatched `paper-trade` runs follow the same rules as scheduled ones: exits from 9:40 ET, entries only inside the entry window, and a neutral skip when the market is closed. The existing schedule stays in place as a fallback.

The included GitHub Actions workflow is explicitly configured for PAPER trading. A separate live application configuration must explicitly select live mode and the live Alpaca endpoint, use a non-default ledger path, and bind that ledger to the intended account.


## Example Workflow
- **Screen for Earnings**: Bot fetches today's after-close and the next session's before-open earnings tickers.
- **Screening & Sizing**: For each ticker, applies IV/volume/slope criteria and calculates position size (a fixed share of equity, or Kelly once configured).
- **Open Trades**: Places calendar spread trades at the correct time (BMO/AMC logic).
- **Track & Close**: Closes each position from 9:40 ET in the session after entry. A due position a run leaves open raises a "Due close deferred" warning and counts in the summary's "Overdue exits"; the next run retries it. Optional Sheet updates are delivered from the SQLite outbox.



## Disclaimer
This software is provided solely for educational and research purposes. It is not intended to provide investment advice. The developers are not financial advisors and accept no responsibility for any financial decisions or losses resulting from the use of this software. Always consult a professional financial advisor before making any investment decisions.

---

*Happy trading, and trade responsibly!* 
