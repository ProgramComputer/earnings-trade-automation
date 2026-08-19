# Earnings Trade Automation

Automated trading bot for executing earnings calendar spread strategies using options in a workflow automation. Integrates with Google Sheets for trade tracking and Alpaca for order execution. 

## Features
- **Automated Earnings Calendar Spread Trading**: Opens and closes calendar spreads around earnings events based on strict screening criteria.
- **Kelly Criterion Position Sizing**: Uses a 10% Kelly fraction for optimal, risk-managed position sizing.
- **Google Sheets Integration**: Tracks trades and workflow status in a Google Sheet via Apps Script.
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

#### Apps Script Request Authentication
Generate one long random secret and store the same value in both places:

- In Apps Script, open **Project Settings > Script Properties** and add `GOOGLE_SCRIPT_SECRET`.
- In GitHub, open **Settings > Secrets and variables > Actions** and add `GOOGLE_SCRIPT_SECRET`.

Never commit this secret, place it in Sheet cells, or print it in logs.

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

The included GitHub Actions workflow is explicitly configured for PAPER trading. A separate live application configuration must explicitly select live mode and the live Alpaca endpoint, use a non-default ledger path, and bind that ledger to the intended account.


## Example Workflow
- **Screen for Earnings**: Bot fetches tomorrow's earnings tickers.
- **Screening & Sizing**: For each ticker, applies IV/volume/slope criteria and calculates position size using Kelly.
- **Open Trades**: Places calendar spread trades at the correct time (BMO/AMC logic).
- **Track & Close**: Monitors open trades and closes them at the correct time, updating Google Sheets.



## Disclaimer
This software is provided solely for educational and research purposes. It is not intended to provide investment advice. The developers are not financial advisors and accept no responsibility for any financial decisions or losses resulting from the use of this software. Always consult a professional financial advisor before making any investment decisions.

---

*Happy trading, and trade responsibly!* 
