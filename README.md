# Earnings Trade Automation

Automated trading bot for executing earnings calendar spread strategies using options in a workflow automation. Integrates with Google Sheets for trade tracking and Alpaca for order execution. 

## Features
- **Automated Earnings Calendar Spread Trading**: Opens and closes calendar spreads around earnings events based on strict screening criteria.
- **Kelly Criterion Position Sizing**: Uses a 10% Kelly fraction for optimal, risk-managed position sizing.
- **Optional Google Sheets Integration**: Queues trade updates in SQLite and syncs them separately through Apps Script.
- **Alpaca API Integration**: Places and closes trades automatically using Alpaca brokerage API.
- **Configurable and Extensible**: Modular codebase for easy strategy tweaks and integration.

## Strategy Overview
We implement an earnings volatility selling strategy focusing on calendar spreads around earnings events:

- Rationale: Implied volatility spikes ahead of earnings due to hedgers and speculators, creating an opportunity to profit from IV crush and muted stock moves.
- Trade Structure: At-the-money calendar spreads with a 30-day expiration gap for stability, offering controlled risk compared to straddles.
- Screening Criteria: Filter for high-probability trades using:
  - **Term Structure Slope**: Negative slope between front-month and 45-day expirations (backwardation).
  - **30-Day Average Volume**: Ensures sufficient liquidity and price-insensitive demand.
  - **IV/RV Ratio**: High implied-to-realized volatility ratio indicates overpriced options; realized volatility is estimated using the 30-day Yang–Zhang estimator.
- Position Sizing: Apply a 10% Kelly fraction for optimal, risk-managed sizing.

## Quick Start

### 1. Clone the Repository
```bash
git clone https://github.com/yourusername/earnings-calendar-spread-bot.git
cd earnings-calendar-spread-bot
```

### 2. Google Sheets Set Up (Optional)
Create a copy of https://docs.google.com/spreadsheets/d/1qOu4PJtcpYwLZgFFIpVr8FXD12dXoWZaKxSeg9FR7lU/ and add code.gs to App Script

Sheet synchronization is optional. Without `GOOGLE_SCRIPT_URL` and `GOOGLE_SCRIPT_SECRET`, fill events remain queued in SQLite and the workflow continues. When configured, a separate best-effort step delivers queued events through the connected Sheet's Apps Script deployment; a Sheet failure does not block reconciliation, position management, or new PAPER orders.

After updating `code.gs`, deploy a new version of the existing Apps Script web app. The first authenticated fill request adds missing tracking columns to the original template and updates its return formulas for fill-based records, preserving historical trades. No manual fill entry is needed. The Actions summary reports pending Sheet updates even when trading succeeds.

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
ALPACA_PAPER=true  # Set explicitly to 'false' to use live trading
APCA_API_BASE_URL=https://paper-api.alpaca.markets
```

### 4. Install Dependencies
```bash
pip install -r requirements.txt
```

### 5. Reset Local Trade Database 
Before running the bot for the first time (or to start fresh), delete the existing SQLite database file:
```bash
rm trades.db  # Mac/Linux
del trades.db # Windows (PowerShell)
```

### 6. Run the Bot
```bash
python automation.py
```
### 7. Automate with GitHub Actions
- Fork the repository to your GitHub account (required to enable Actions).
- In your fork, navigate to **Settings > Secrets > Actions** and add your environment variables.
- Enable the GitHub Actions workflow in the **Actions** tab.

#### Workflow Modes
- `paper-trade`: Reconciles state first, then permits Alpaca PAPER orders.
- `reconcile-only`: The default manual mode; reconciles state and never submits orders.
- `market-closed`: A neutral scheduled skip with no Python, synchronization, or database-persistence work.

#### Settings
Set these as repository variables under **Settings > Secrets and variables > Actions > Variables**. Leave one unset to use its default.

| Variable | Default | Meaning |
|---|---|---|
| `ENTRY_WINDOW_MINUTES` | `240` | New entries may start this many minutes before the close (from noon on a regular session) and stop 3 minutes before it, so a run GitHub starts late can still trade. `25` restores the strategy's late-day entry. |
| `QUOTE_MAX_AGE_SECONDS` | `120` | Oldest option quote accepted for pricing an order. Thinly traded contracts often keep an unchanged quote for more than 30 seconds. |
| `OPEN_MAX_DEBIT_SPREAD_FRACTION` | `1` | How far from the spread's midpoint toward its ask an opening order may go. Orders start at the midpoint and step up; Alpaca PAPER has not filled spreads below the ask. |
| `KELLY_WIN_RATE` | unset | Expected share of winning trades from your backtest, e.g. `0.60`. |
| `KELLY_AVG_WIN` | unset | Average winning trade as a fraction of the debit paid, e.g. `0.40` for +40%. |
| `KELLY_AVG_LOSS` | unset | Average losing trade as a positive fraction of the debit paid, e.g. `0.30` for −30%. |
| `KELLY_FRACTION` | `0.10` | Share of full Kelly to bet. |
| `POSITION_ALLOCATION_PCT` | `0.06` | Fixed share of equity per position, used until all three Kelly inputs are set. |
| `MAX_AGGREGATE_EXPOSURE_PCT` | `0.36` | Cap on total open exposure as a share of equity. |

With the Kelly inputs set, each position gets `KELLY_FRACTION × (W − (1 − W) ÷ R)` of equity, where `W` is the win rate and `R` is the average win divided by the average loss. For example, `W = 0.60`, average win `0.40` and average loss `0.30` give a full Kelly of 30% and a 10% Kelly allocation of 3%. When the inputs show no edge, the bot opens nothing. Each run logs the sizing it used.

Earnings calendar rows with no before/after-market time are looked up on Yahoo Finance when the stock's 30-day average volume passes the screen; rows Yahoo cannot place stay skipped.

#### On-Time Runs With an External Scheduler
GitHub can start scheduled runs hours late, which delays exits past 9:40 ET. Runs started through `workflow_dispatch` are not held in the scheduled-run queue, so an external scheduler gives on-time exits:

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
- **Screen for Earnings**: Bot fetches tomorrow's earnings tickers.
- **Screening & Sizing**: For each ticker, applies IV/volume/slope criteria and calculates position size using Kelly.
- **Open Trades**: Places calendar spread trades at the correct time (BMO/AMC logic).
- **Track & Close**: Monitors open trades and closes them at the correct time, with optional Sheet updates delivered from the SQLite outbox.



## Disclaimer
This software is provided solely for educational and research purposes. It is not intended to provide investment advice. The developers are not financial advisors and accept no responsibility for any financial decisions or losses resulting from the use of this software. Always consult a professional financial advisor before making any investment decisions.

---

*Happy trading, and trade responsibly!* 
