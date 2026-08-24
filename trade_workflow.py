"""Explicit-mode Alpaca trade workflow backed by a durable SQLite execution ledger."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time as time_module
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
from dotenv import load_dotenv
from alpaca.trading.enums import PositionIntent

from automation import compute_recommendation, get_todays_earnings, get_tomorrows_earnings
from alpaca_integration import (
    close_calendar_spread_order, close_single_option_leg_order,
    get_alpaca_option_chain, get_spread_quotes, init_alpaca_client,
    place_calendar_spread_order, select_expiries_and_strike_alpaca,
)

load_dotenv()
EASTERN = ZoneInfo("America/New_York")
DEFAULT_DB_PATH = Path("trades.db")
TRADES_DB_PATH_SETTING = os.environ.get("TRADES_DB_PATH")
DB_PATH = Path(TRADES_DB_PATH_SETTING.strip()).expanduser() if TRADES_DB_PATH_SETTING and TRADES_DB_PATH_SETTING.strip() else DEFAULT_DB_PATH
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")
GOOGLE_SCRIPT_SECRET = os.environ.get("GOOGLE_SCRIPT_SECRET")
POSITION_ALLOCATION_PCT = Decimal(os.environ.get("POSITION_ALLOCATION_PCT", "0.06"))
MAX_AGGREGATE_EXPOSURE_PCT = Decimal(os.environ.get("MAX_AGGREGATE_EXPOSURE_PCT", "0.20"))
UUID_NAMESPACE = uuid.UUID("f8eaa5b8-685f-4d83-9308-b426ad5f95a1")
KNOWN_DEBITS = {"DG", "ORCL", "NKE"}
KNOWN_SINGLE_CREDITS = {"CHPT", "LULU", "DRI", "KMX", "KR", "KMI"}
KNOWN_ZERO_UNVERIFIED = {"GAP", "ASAN", "PL", "DOCU", "WOOF", "ERIC"}
TERMINAL_STATUSES = {"filled", "canceled", "expired", "rejected"}
SHEET_REQUIRED_FILL_FIELDS = {
    "Ticker", "Short Symbol", "Long Symbol", "Open Date",
    "Record ID", "Trade ID", "Parent Trade ID", "Broker Order ID", "Broker Fill ID",
    "Sync Type", "Fill Phase", "Ordered Quantity", "Filled Quantity", "Remaining Quantity",
    "Lifecycle Status", "Open Sync Status", "Close Sync Status", "Open Cash Flow",
    "Close Cash Flow", "Fees", "Realized P&L", "Close Method", "Close Reason",
    "Broker Mode", "Broker Account Fingerprint", "P&L Status",
}


class OperationalFailure(RuntimeError):
    pass


class ReconciliationFailure(OperationalFailure):
    pass


def dec(value, default="0"):
    if value is None or value == "":
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise OperationalFailure(f"Invalid numeric value {value!r}") from exc


def dollars_to_cents(value):
    return int((dec(value) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def contract_cashflow_cents(price, quantity):
    return int((dec(price) * int(quantity) * 10000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_dollars(value):
    return None if value is None else float((Decimal(int(value)) / 100).quantize(Decimal("0.01")))


def enum_text(value):
    return str(getattr(value, "value", value) or "").lower()


def is_terminal_order(order):
    explicit=getattr(order,"terminal",None)
    return bool(explicit) if explicit is not None else enum_text(getattr(order,"status","")) in TERMINAL_STATUSES


def stamp(value=None):
    value = value or datetime.now(timezone.utc)
    if isinstance(value, str):
        return value
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def redact(value):
    text = str(value)
    for secret in (os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY"), GOOGLE_SCRIPT_SECRET):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


@contextmanager
def db(write=False, read_only=False):
    if read_only:
        conn = sqlite3.connect(f"file:{DB_PATH.resolve().as_posix()}?mode=ro", uri=True, timeout=30)
    else:
        conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.commit()
    except Exception:
        if write:
            conn.rollback()
        raise
    finally:
        conn.close()


TRADE_COLUMNS = {
    "trade_id": "TEXT", "parent_trade_id": "TEXT", "ordered_quantity": "INTEGER",
    "filled_quantity": "INTEGER", "closed_quantity": "INTEGER", "remaining_quantity": "INTEGER",
    "lifecycle_status": "TEXT", "open_sync_status": "TEXT", "close_sync_status": "TEXT",
    "opening_cash_flow_cents": "INTEGER", "closing_cash_flow_cents": "INTEGER",
    "opening_fees_cents": "INTEGER", "closing_fees_cents": "INTEGER",
    "allocated_open_cash_flow_cents": "INTEGER", "allocated_open_fees_cents": "INTEGER",
    "realized_pnl_cents": "INTEGER", "open_order_id": "TEXT", "close_order_id": "TEXT",
    "open_client_order_prefix": "TEXT", "close_client_order_prefix": "TEXT",
    "close_method": "TEXT", "close_reason": "TEXT", "reconciliation_status": "TEXT",
    "open_generation": "INTEGER", "close_generation": "INTEGER",
    "earnings_date": "TEXT",
    "created_at": "TEXT", "updated_at": "TEXT",
}


def init_db():
    """Additively migrate the committed database; historical rows are never replaced."""
    with db(write=True) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS trades (
            "Ticker" TEXT,"Implied Move" TEXT,"Structure" TEXT,"Side" TEXT,"When" TEXT,
            "Size" INTEGER,"Short Symbol" TEXT,"Long Symbol" TEXT,"Open Date" TEXT,
            "Open Price" REAL,"Open Comm." REAL,"Close Date" TEXT,"Close Price" REAL,"Close Comm." REAL)''')
        existing = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
        if "When" not in existing:
            conn.execute('ALTER TABLE trades ADD COLUMN "When" TEXT')
            existing.add("When")
        for name, definition in TRADE_COLUMNS.items():
            if name not in existing:
                conn.execute(f'ALTER TABLE trades ADD COLUMN "{name}" {definition}')
        conn.execute('''CREATE TABLE IF NOT EXISTS schema_migrations(
          migration_name TEXT PRIMARY KEY,applied_at TEXT NOT NULL)''')
        # Populate stable parent keys and make them unique before creating any
        # child table that references trades(trade_id). SQLite rejects even
        # otherwise-unrelated writes when a foreign key targets a non-unique
        # legacy column.
        historical_migration="historical_cashflow_identity_v1"
        if not conn.execute("SELECT 1 FROM schema_migrations WHERE migration_name=?",(historical_migration,)).fetchone():
            migrate_historical(conn)
            conn.execute("INSERT INTO schema_migrations VALUES(?,?)",(historical_migration,stamp()))
        trade_index=conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_trades_id'").fetchone()
        if trade_index and " WHERE " in str(trade_index[0] or "").upper():
            conn.execute("DROP INDEX ux_trades_id")
            trade_index=None
        if not trade_index:
            conn.execute("CREATE UNIQUE INDEX ux_trades_id ON trades(trade_id)")
        schema_statements = (
        '''CREATE TABLE IF NOT EXISTS broker_orders(
          order_id TEXT PRIMARY KEY,trade_id TEXT NOT NULL,client_order_id TEXT,phase TEXT NOT NULL,
          method TEXT NOT NULL,symbol TEXT,ordered_quantity INTEGER NOT NULL DEFAULT 0,
          filled_quantity INTEGER NOT NULL DEFAULT 0,canceled_quantity INTEGER NOT NULL DEFAULT 0,
          remaining_quantity INTEGER NOT NULL DEFAULT 0,filled_cash_flow_cents INTEGER NOT NULL DEFAULT 0,
          fees_cents INTEGER NOT NULL DEFAULT 0,commission_status TEXT,limit_price TEXT,filled_avg_price TEXT,
          lifecycle_status TEXT NOT NULL,terminal INTEGER NOT NULL DEFAULT 0,stop_reason TEXT,
          submitted_at TEXT,updated_at TEXT NOT NULL,FOREIGN KEY(trade_id) REFERENCES trades(trade_id))''',
        '''CREATE UNIQUE INDEX IF NOT EXISTS ux_broker_orders_client ON broker_orders(client_order_id) WHERE client_order_id IS NOT NULL''',
        '''CREATE TABLE IF NOT EXISTS fills(
          fill_id TEXT PRIMARY KEY,trade_id TEXT NOT NULL,parent_trade_id TEXT,broker_order_id TEXT,
          broker_activity_id TEXT,phase TEXT NOT NULL,method TEXT NOT NULL,
          cumulative_order_filled_quantity INTEGER NOT NULL,filled_quantity INTEGER NOT NULL CHECK(filled_quantity>0),
          price TEXT NOT NULL,cash_flow_cents INTEGER NOT NULL,fees_cents INTEGER NOT NULL DEFAULT 0,
          allocated_open_cash_flow_cents INTEGER NOT NULL DEFAULT 0,allocated_open_fees_cents INTEGER NOT NULL DEFAULT 0,
          realized_pnl_cents INTEGER,commission_status TEXT,occurred_at TEXT NOT NULL,close_reason TEXT,sync_status TEXT NOT NULL DEFAULT 'pending',
          FOREIGN KEY(trade_id) REFERENCES trades(trade_id),FOREIGN KEY(broker_order_id) REFERENCES broker_orders(order_id),
          UNIQUE(broker_order_id,phase,cumulative_order_filled_quantity))''',
        '''CREATE TABLE IF NOT EXISTS sheet_outbox(
          event_id TEXT PRIMARY KEY,trade_id TEXT NOT NULL,fill_id TEXT NOT NULL,phase TEXT NOT NULL,
          payload_json TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
          FOREIGN KEY(trade_id) REFERENCES trades(trade_id),FOREIGN KEY(fill_id) REFERENCES fills(fill_id))''',
        '''CREATE TABLE IF NOT EXISTS reconciliation_runs(
          reconciliation_id TEXT PRIMARY KEY,checked_at TEXT NOT NULL,result TEXT NOT NULL,
          discrepancy_count INTEGER NOT NULL,summary_json TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS broker_identity(
          singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
          broker_mode TEXT NOT NULL CHECK(broker_mode IN ('PAPER','LIVE')),
          account_fingerprint TEXT NOT NULL,base_url TEXT NOT NULL,
          bound_at TEXT NOT NULL,updated_at TEXT NOT NULL)''',
        )
        for statement in schema_statements:
            conn.execute(statement)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_fills_activity ON fills(broker_activity_id) WHERE broker_activity_id IS NOT NULL")
        for table in ("broker_orders", "fills"):
            if "commission_status" not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN commission_status TEXT")


def migrate_historical(conn):
    seen = set()
    for row in conn.execute("SELECT rowid,* FROM trades ORDER BY rowid").fetchall():
        ticker = str(row["Ticker"] or "").upper()
        group = "|".join(str(row[key] or "") for key in ("Ticker", "Open Date", "Short Symbol", "Long Symbol", "When"))
        parent_id = row["parent_trade_id"] or str(uuid.uuid5(UUID_NAMESPACE, f"parent|{group}"))
        trade_id = row["trade_id"]
        if not trade_id or trade_id in seen:
            fingerprint = "|".join(str(row[key] or "") for key in ("Ticker", "Open Date", "Short Symbol", "Long Symbol", "Size", "Open Price", "Close Date", "Close Price"))
            trade_id = str(uuid.uuid5(UUID_NAMESPACE, f"legacy|{row['rowid']}|{fingerprint}"))
        seen.add(trade_id)
        size = max(0, int(row["Size"] or 0))
        closed = bool(row["Close Date"])
        try:
            legacy_open=datetime.strptime(row["Open Date"],"%Y-%m-%d").date()
            inferred_earnings=(legacy_open+timedelta(days=1) if (row["When"] or "AMC")=="BMO" else legacy_open).isoformat()
        except (TypeError,ValueError):
            inferred_earnings=row["Open Date"] or None
        open_cf = -abs(contract_cashflow_cents(row["Open Price"] or 0, size))
        open_fees = dollars_to_cents(row["Open Comm."] or 0)
        close_fees = dollars_to_cents(row["Close Comm."] or 0)
        close_cf = None
        method, reason, recon = row["close_method"], row["close_reason"], row["reconciliation_status"]
        if closed:
            price = dec(row["Close Price"] or 0)
            if price < 0:
                close_cf, method, recon = abs(contract_cashflow_cents(price, size)), method or "legacy_calendar_credit", recon or "HISTORICAL_SIGNED_CREDIT"
            elif ticker in KNOWN_DEBITS:
                close_cf, method, reason, recon = -abs(contract_cashflow_cents(price, size)), method or "legacy_calendar_debit_corrected", reason or "known_signed_multi_leg_debit", recon or "HISTORICAL_KNOWN_DEBIT_CORRECTED"
            elif ticker in KNOWN_SINGLE_CREDITS:
                close_cf, method, reason, recon = abs(contract_cashflow_cents(price, size)), method or "legacy_single_long_credit", reason or "short_expired_long_leg_sale_inferred", recon or "HISTORICAL_INFERRED_SINGLE_LEG_CREDIT"
            elif price == 0 or ticker in KNOWN_ZERO_UNVERIFIED:
                close_cf, method, reason, recon = 0, method or "legacy_zero_unverified", reason or "zero_close_not_broker_confirmed", "HISTORICAL_ZERO_UNVERIFIED"
            else:
                close_cf, method, recon = abs(contract_cashflow_cents(price, size)), method or "legacy_positive_close_unverified", recon or "HISTORICAL_POSITIVE_CLOSE_UNCERTAIN"
            realized = open_cf + close_cf - open_fees - close_fees
            lifecycle = row["lifecycle_status"] or ("CLOSED_LEGACY_UNVERIFIED" if "UNVERIFIED" in recon else "CLOSED")
        else:
            realized, lifecycle, recon = int(row["realized_pnl_cents"] or 0), row["lifecycle_status"] or "OPEN", recon or "PENDING_BROKER_RECONCILIATION"
        conn.execute('''UPDATE trades SET trade_id=?,parent_trade_id=?,ordered_quantity=COALESCE(ordered_quantity,?),
          filled_quantity=COALESCE(filled_quantity,?),closed_quantity=COALESCE(closed_quantity,?),
          remaining_quantity=COALESCE(remaining_quantity,?),lifecycle_status=?,open_sync_status=COALESCE(open_sync_status,'legacy'),
          close_sync_status=COALESCE(close_sync_status,?),opening_cash_flow_cents=COALESCE(opening_cash_flow_cents,?),
          closing_cash_flow_cents=COALESCE(closing_cash_flow_cents,?),opening_fees_cents=COALESCE(opening_fees_cents,?),
          closing_fees_cents=COALESCE(closing_fees_cents,?),allocated_open_cash_flow_cents=COALESCE(allocated_open_cash_flow_cents,?),
          allocated_open_fees_cents=COALESCE(allocated_open_fees_cents,?),realized_pnl_cents=COALESCE(realized_pnl_cents,?),
          close_method=?,close_reason=?,reconciliation_status=?,earnings_date=COALESCE(earnings_date,?),
          created_at=COALESCE(created_at,?),updated_at=COALESCE(updated_at,?) WHERE rowid=?''',
          (trade_id,parent_id,size,size,size if closed else 0,0 if closed else size,lifecycle,"legacy" if closed else "not_applicable",
           open_cf,close_cf,open_fees,close_fees,open_cf if closed else 0,open_fees if closed else 0,realized,
           method,reason,recon,inferred_earnings,stamp(),stamp(),row["rowid"]))


def configured_mode():
    mode_setting=os.environ.get("ALPACA_PAPER")
    if mode_setting not in {"true","false"}:
        raise OperationalFailure("ALPACA_PAPER must be explicitly set to exactly 'true' or 'false'")
    paper_mode=mode_setting=="true"
    configured_url=os.environ.get("APCA_API_BASE_URL","").strip().rstrip("/")
    expected_url="https://paper-api.alpaca.markets" if paper_mode else "https://api.alpaca.markets"
    if configured_url!=expected_url:
        raise OperationalFailure(
            f"APCA_API_BASE_URL must be exactly '{expected_url}' when ALPACA_PAPER is '{mode_setting}'"
        )
    if not paper_mode:
        explicit_path=(TRADES_DB_PATH_SETTING or "").strip()
        if not explicit_path:
            raise OperationalFailure("LIVE mode requires an explicit TRADES_DB_PATH distinct from trades.db")
        configured_path=Path(explicit_path).expanduser().resolve()
        if os.path.normcase(str(configured_path))==os.path.normcase(str(DEFAULT_DB_PATH.resolve())):
            raise OperationalFailure("LIVE mode cannot use the default trades.db ledger; set a distinct TRADES_DB_PATH")
    return mode_setting,"PAPER" if paper_mode else "LIVE",expected_url


def configured_broker_client():
    _,mode_name,_=configured_mode()
    if not os.environ.get("APCA_API_KEY_ID") or not os.environ.get("APCA_API_SECRET_KEY"):
        raise OperationalFailure("Alpaca credentials are unavailable for the explicitly selected account mode")
    client = init_alpaca_client()
    if client is None:
        raise OperationalFailure("Could not initialize the explicitly configured Alpaca client")
    print(f"Confirmed Alpaca {mode_name} mode and matching endpoint.")
    return client,mode_name


def broker_account_identity(client):
    try:
        account=client.get_account()
    except Exception as exc:
        raise OperationalFailure(f"Unable to identify the configured Alpaca account: {redact(exc)}") from exc
    account_id=str(getattr(account,"id","") or "").strip()
    if not account_id:
        raise OperationalFailure("Configured Alpaca account response did not contain an account ID")
    # Persist only a one-way identity binding; the raw broker account ID never
    # enters the SQLite database, outbox, logs, or reconciliation reports.
    return hashlib.sha256(f"alpaca-account-id:v1:{account_id}".encode("utf-8")).hexdigest()


def bind_or_validate_broker_identity(client, broker_mode, *, allow_bind):
    """Bind once, then reject mode/account/path drift before any reconciliation or submit."""
    _,configured_name,base_url=configured_mode()
    if broker_mode!=configured_name:
        raise OperationalFailure("Configured broker mode changed during workflow startup")
    fingerprint=broker_account_identity(client)
    with db(write=allow_bind,read_only=not allow_bind) as conn:
        table_exists=conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='broker_identity'"
        ).fetchone()
        if not table_exists:
            raise OperationalFailure(
                "SQLite ledger has no broker identity metadata; run --migrate-only, then --bind-only"
            )
        bound=conn.execute("SELECT * FROM broker_identity WHERE singleton_id=1").fetchone()
        if bound:
            if bound["broker_mode"]!=broker_mode:
                raise ReconciliationFailure(
                    f"Ledger is bound to {bound['broker_mode']} mode and cannot be used in {broker_mode} mode"
                )
            if bound["account_fingerprint"]!=fingerprint:
                raise ReconciliationFailure(
                    "Configured Alpaca account does not match the account bound to this ledger "
                    f"(expected fingerprint {bound['account_fingerprint']}, got {fingerprint})"
                )
            if str(bound["base_url"]).rstrip("/")!=base_url:
                raise ReconciliationFailure("Configured Alpaca endpoint does not match the endpoint bound to this ledger")
            return {
                "broker_mode":bound["broker_mode"],"account_fingerprint":bound["account_fingerprint"],
                "base_url":bound["base_url"],
            }
        if not allow_bind:
            raise ReconciliationFailure(
                "Ledger is not bound to an Alpaca account; run --bind-only before read-only reconciliation"
            )
        trade_count=int(conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0])
        if trade_count:
            if broker_mode!="PAPER":
                raise ReconciliationFailure("A nonempty unbound ledger may only be adopted by a PAPER account")
            incomplete=int(conn.execute('''SELECT COUNT(*) FROM trades WHERE
              trade_id IS NULL OR trim(trade_id)='' OR lifecycle_status IS NULL OR trim(lifecycle_status)=''
              OR reconciliation_status IS NULL OR trim(reconciliation_status)='' ''').fetchone()[0])
            unresolved=int(conn.execute('''SELECT COUNT(*) FROM trades WHERE
              COALESCE(remaining_quantity,0)>0 OR lifecycle_status NOT LIKE 'CLOSED%' ''').fetchone()[0])
            operational=sum(int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                            for table in ("broker_orders","fills","sheet_outbox","reconciliation_runs"))
            if incomplete or unresolved or operational:
                raise ReconciliationFailure(
                    "Unbound ledger contains non-historical or unresolved execution state and cannot be adopted automatically"
                )
        now=stamp()
        conn.execute('''INSERT INTO broker_identity(singleton_id,broker_mode,account_fingerprint,
          base_url,bound_at,updated_at) VALUES(1,?,?,?,?,?)''',
          (broker_mode,fingerprint,base_url,now,now))
    print(f"Bound SQLite ledger to Alpaca {broker_mode} account fingerprint {fingerprint}.")
    return {"broker_mode":broker_mode,"account_fingerprint":fingerprint,"base_url":base_url}


def stable_trade_id(ticker, earnings_date, when, short_symbol=None, long_symbol=None):
    # One logical position per account-scoped earnings event; selected contracts
    # may change between retries. Historical migration deliberately keeps its
    # legacy identifiers rather than rewriting user-visible history.
    try:
        with db(read_only=True) as conn:
            identity=conn.execute(
                "SELECT broker_mode,account_fingerprint FROM broker_identity WHERE singleton_id=1"
            ).fetchone()
    except sqlite3.OperationalError as exc:
        raise OperationalFailure("Cannot create a Trade ID before --migrate-only and --bind-only") from exc
    if not identity:
        raise OperationalFailure("Cannot create a Trade ID before the ledger is bound with --bind-only")
    material=(f"trade-v2|{identity['broker_mode']}|{identity['account_fingerprint']}|"
              f"{str(ticker).upper()}|{earnings_date}|{str(when).upper()}")
    return str(uuid.uuid5(UUID_NAMESPACE,material))


def client_prefix(phase, trade_id, generation, method="calendar_spread"):
    code = "o" if phase == "open" else ("c" if method == "calendar_spread" else "s")
    return f"eta-{code}-{uuid.UUID(trade_id).hex[:20]}-{generation:04d}"


def reserve_operation_prefix(trade_id, phase, method):
    """Reserve a durable generation after proving earlier attempts are terminal."""
    generation_column = "open_generation" if phase == "open" else "close_generation"
    prefix_column = "open_client_order_prefix" if phase == "open" else "close_client_order_prefix"
    with db(write=True) as conn:
        active = conn.execute(
            "SELECT order_id FROM broker_orders WHERE trade_id=? AND phase=? AND terminal=0 LIMIT 1",
            (trade_id, phase),
        ).fetchone()
        if active:
            raise ReconciliationFailure(f"Trade {trade_id} still has an active {phase} order")
        row = conn.execute(f"SELECT COALESCE({generation_column},0) FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
        if not row:
            raise OperationalFailure(f"Cannot reserve order generation for unknown trade {trade_id}")
        generation = int(row[0]) + 1
        prefix = client_prefix(phase, trade_id, generation, method)
        conn.execute(
            f"UPDATE trades SET {generation_column}=?,{prefix_column}=?,updated_at=? WHERE trade_id=?",
            (generation, prefix, stamp(), trade_id),
        )
    return prefix


def order_id(order):
    return str(getattr(order, "order_id", None) or getattr(order, "id", None) or "")


def quantities(order, context=None):
    context = context or {}
    ordered = int(dec(getattr(order, "ordered_qty", None) or getattr(order, "qty", None) or context.get("quantity", 0)))
    filled = int(dec(getattr(order, "filled_qty", 0) or 0))
    canceled = int(dec(getattr(order, "canceled_qty", 0) or 0))
    raw_remaining = getattr(order, "remaining_qty", None)
    remaining = max(0, ordered-filled-canceled) if raw_remaining is None else int(dec(raw_remaining))
    return ordered, filled, canceled, remaining


def validate_order_identity(conn, oid, trade_id, phase, method, client_id=None):
    existing=conn.execute("SELECT trade_id,phase,method,client_order_id FROM broker_orders WHERE order_id=?",(oid,)).fetchone()
    if existing and (existing["trade_id"]!=trade_id or existing["phase"]!=phase or existing["method"]!=method):
        raise ReconciliationFailure(f"Broker order {oid} conflicts with its stored trade identity")
    if existing and client_id and existing["client_order_id"] and existing["client_order_id"]!=client_id:
        raise ReconciliationFailure(f"Broker order {oid} conflicts with its stored client order ID")
    if client_id:
        alias=conn.execute("SELECT order_id,trade_id FROM broker_orders WHERE client_order_id=? AND order_id!=?",(client_id,oid)).fetchone()
        if alias:
            raise ReconciliationFailure(f"Client order ID aliases broker order {alias['order_id']} on trade {alias['trade_id']}")


def create_planned_trade(data, ordered_quantity):
    trade_id = data["trade_id"]
    with db(write=True) as conn:
        existing = conn.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
        if existing:
            active=conn.execute("SELECT 1 FROM broker_orders WHERE trade_id=? AND phase='open' AND terminal=0 LIMIT 1",(trade_id,)).fetchone()
            retryable=int(existing["filled_quantity"] or 0)==0 and int(existing["remaining_quantity"] or 0)==0 and existing["lifecycle_status"] in {"PLANNED","CANCELED_NO_FILL"}
            if active:
                raise ReconciliationFailure(f"Trade {trade_id} has an unresolved active opening order")
            if not retryable:
                print(f"Skipping earnings event {trade_id}: durable state already exists.")
                return "skipped"
            conn.execute('''UPDATE trades SET "Implied Move"=?,"Short Symbol"=?,"Long Symbol"=?,"Open Date"=?,earnings_date=?,
              ordered_quantity=?,lifecycle_status='PLANNED',reconciliation_status='ORDER_NOT_SUBMITTED',updated_at=? WHERE trade_id=?''',
              (data.get("Implied Move"),data["Short Symbol"],data["Long Symbol"],data["Open Date"],data["earnings_date"],ordered_quantity,stamp(),trade_id))
            return "reused"
        conn.execute('''INSERT INTO trades("Ticker","Implied Move","Structure","Side","When","Size","Short Symbol","Long Symbol",
          "Open Date","Open Comm.","Close Date","Close Comm.",trade_id,parent_trade_id,ordered_quantity,filled_quantity,
          closed_quantity,remaining_quantity,lifecycle_status,open_sync_status,close_sync_status,opening_cash_flow_cents,
          closing_cash_flow_cents,opening_fees_cents,closing_fees_cents,allocated_open_cash_flow_cents,
          allocated_open_fees_cents,realized_pnl_cents,open_client_order_prefix,reconciliation_status,earnings_date,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (data["Ticker"],data.get("Implied Move"),"Calendar Spread","debit",data["When"],0,data["Short Symbol"],
           data["Long Symbol"],data["Open Date"],0,"",0,trade_id,trade_id,ordered_quantity,0,0,0,"PLANNED",
           "not_applicable","not_applicable",0,0,0,0,0,0,0,None,"ORDER_NOT_SUBMITTED",data["earnings_date"],stamp(),stamp()))
    return "created"


def upsert_submitted_order(trade_id, phase, method, order, context=None):
    oid = order_id(order)
    if not oid:
        raise OperationalFailure("Broker submission returned no order ID")
    context = context or {}
    ordered, filled, canceled, remaining = quantities(order, context)
    if context.get("submission_only"):
        # The terminal callback owns fill accounting, even if submit already reports a fill.
        filled,canceled,remaining=0,0,ordered
    client_id = str(getattr(order,"client_order_id",None) or context.get("client_order_id") or "") or None
    with db(write=True) as conn:
        if not conn.execute("SELECT 1 FROM trades WHERE trade_id=?",(trade_id,)).fetchone():
            raise OperationalFailure(f"Unknown trade {trade_id} for submitted order")
        validate_order_identity(conn,oid,trade_id,phase,method,client_id)
        conn.execute('''INSERT INTO broker_orders(order_id,trade_id,client_order_id,phase,method,symbol,ordered_quantity,
          filled_quantity,canceled_quantity,remaining_quantity,lifecycle_status,terminal,limit_price,submitted_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET
          client_order_id=COALESCE(excluded.client_order_id,broker_orders.client_order_id),ordered_quantity=MAX(broker_orders.ordered_quantity,excluded.ordered_quantity),
          filled_quantity=MAX(broker_orders.filled_quantity,excluded.filled_quantity),canceled_quantity=MAX(broker_orders.canceled_quantity,excluded.canceled_quantity),
          remaining_quantity=excluded.remaining_quantity,lifecycle_status=excluded.lifecycle_status,terminal=excluded.terminal,updated_at=excluded.updated_at''',
          (oid,trade_id,client_id,phase,method,str(getattr(order,"symbol",None) or context.get("symbol") or ""),ordered,filled,canceled,
          remaining,enum_text(getattr(order,"status","submitted")) or "submitted",int(is_terminal_order(order)),
           str(context.get("limit_price", "")),stamp(getattr(order,"submitted_at",None)),stamp()))
        column = "open_order_id" if phase=="open" else "close_order_id"
        if not is_terminal_order(order):
            conn.execute(f"UPDATE trades SET {column}=?,lifecycle_status=?,updated_at=? WHERE trade_id=?",
                         (oid,"OPEN_ORDER_SUBMITTED" if phase=="open" else "CLOSE_ORDER_SUBMITTED",stamp(),trade_id))
        else:
            conn.execute(f"UPDATE trades SET {column}=?,updated_at=? WHERE trade_id=?",(oid,stamp(),trade_id))
    return oid


def finalize_execution(trade_id, result):
    """Persist terminal state for zero-fill and filled attempts alike."""
    for attempt in getattr(result, "attempts", ()) or ():
        oid=order_id(attempt)
        if not oid: continue
        ordered,filled,canceled,remaining=quantities(attempt)
        with db(write=True) as conn:
            conn.execute('''UPDATE broker_orders SET ordered_quantity=?,filled_quantity=MAX(filled_quantity,?),
              canceled_quantity=?,remaining_quantity=?,lifecycle_status=?,terminal=?,stop_reason=?,updated_at=?
              WHERE order_id=? AND trade_id=?''',
              (ordered,filled,canceled,remaining,enum_text(getattr(attempt,"status","")),int(is_terminal_order(attempt)),
               str(getattr(attempt,"stop_reason","") or ""),stamp(),oid,trade_id))


def fill_cashflow(phase, method, price, quantity):
    signed = contract_cashflow_cents(price, quantity)
    if phase == "open":
        return -abs(signed)
    if method == "calendar_spread":
        return -signed  # Alpaca MLEG: positive debit, negative credit.
    if method == "single_long_sell":
        return abs(signed)
    if method == "single_short_buy":
        return -abs(signed)
    raise OperationalFailure(f"Unknown cash-flow method {method}")


def pnl_status(realized_pnl_cents, commission_status):
    if realized_pnl_cents is None:
        return "NOT_REALIZED"
    return "PROVISIONAL_FEES_UNKNOWN" if commission_status=="unavailable" else "CONFIRMED"


def sheet_payload(conn, trade_id, fill_id):
    trade = conn.execute("SELECT * FROM trades WHERE trade_id=?",(trade_id,)).fetchone()
    fill = conn.execute("SELECT * FROM fills WHERE fill_id=?",(fill_id,)).fetchone()
    order = conn.execute("SELECT * FROM broker_orders WHERE order_id=?",(fill["broker_order_id"],)).fetchone()
    identity=conn.execute("SELECT broker_mode,account_fingerprint FROM broker_identity WHERE singleton_id=1").fetchone()
    if not identity:
        raise OperationalFailure("Cannot enqueue a Sheet event before the ledger is bound to a broker account")
    opening_fee_unknown=bool(conn.execute('''SELECT 1 FROM fills WHERE trade_id=? AND phase='open'
      AND commission_status='unavailable' LIMIT 1''',(trade_id,)).fetchone())
    effective_commission_status=("unavailable" if opening_fee_unknown or fill["commission_status"]=="unavailable"
                                 else fill["commission_status"])
    fill_pnl_status=pnl_status(fill["realized_pnl_cents"],effective_commission_status)
    return {
      "action":"upsert","Record ID":fill["fill_id"],"Trade ID":trade["trade_id"],
      "Parent Trade ID":trade["parent_trade_id"],"Broker Order ID":fill["broker_order_id"] or "",
      "Broker Fill ID":fill["broker_activity_id"] or "","Sync Type":"fill","Fill Phase":fill["phase"],
      "Broker Mode":identity["broker_mode"],"Broker Account Fingerprint":identity["account_fingerprint"],
      "Ordered Quantity":int(order["ordered_quantity"] if order else fill["filled_quantity"]),
      "Filled Quantity":int(fill["filled_quantity"]),"Remaining Quantity":int(trade["remaining_quantity"] or 0),
      "Lifecycle Status":trade["lifecycle_status"],"Open Sync Status":trade["open_sync_status"],
      "Close Sync Status":trade["close_sync_status"],
      "Open Cash Flow":cents_to_dollars(fill["cash_flow_cents"] if fill["phase"]=="open" else fill["allocated_open_cash_flow_cents"]),
      "Close Cash Flow":cents_to_dollars(fill["cash_flow_cents"] if fill["phase"]=="close" else 0),
      "Fees":"" if fill["commission_status"]=="unavailable" else cents_to_dollars(fill["fees_cents"]+fill["allocated_open_fees_cents"]),
      "Realized P&L":"" if fill["realized_pnl_cents"] is None else cents_to_dollars(fill["realized_pnl_cents"]),
      "P&L Status":fill_pnl_status,
      "Close Method":trade["close_method"] or "","Close Reason":trade["close_reason"] or "",
      "Ticker":trade["Ticker"],"Implied Move":trade["Implied Move"] or "","Structure":trade["Structure"] or "Calendar Spread",
      "Side":trade["Side"] or "debit","When":trade["When"] or "","Size":int(fill["filled_quantity"]),
      "Short Symbol":trade["Short Symbol"] or "","Long Symbol":trade["Long Symbol"] or "",
      "Open Date":trade["Open Date"] or "","Open Price":float(fill["price"]) if fill["phase"]=="open" else "",
      "Open Comm.":cents_to_dollars(fill["fees_cents"]) if fill["phase"]=="open" else 0,
      "Close Date":trade["Close Date"] or "","Close Price":float(fill["price"]) if fill["phase"]=="close" else "",
      "Close Comm.":cents_to_dollars(fill["fees_cents"]) if fill["phase"]=="close" else 0,
    }


def enqueue_fill(conn, trade_id, fill_id, phase):
    payload = json.dumps(sheet_payload(conn,trade_id,fill_id),separators=(",",":"))
    conn.execute('''INSERT INTO sheet_outbox(event_id,trade_id,fill_id,phase,payload_json,state,attempts,created_at,updated_at)
      VALUES(?,?,?,?,?,'pending',0,?,?) ON CONFLICT(event_id) DO UPDATE SET payload_json=excluded.payload_json,
      state=CASE WHEN sheet_outbox.state='synced' THEN 'synced' ELSE 'pending' END,updated_at=excluded.updated_at''',
      (fill_id,trade_id,fill_id,phase,payload,stamp(),stamp()))


def persist_unpriced_order_state(trade_id, phase, method, order, stop_reason):
    """Persist observed broker quantities/state without fabricating fill cashflow."""
    oid=order_id(order)
    ordered,cumulative,canceled,order_remaining=quantities(order)
    client_id=str(getattr(order,"client_order_id","") or "") or None
    with db(write=True) as conn:
        if not conn.execute("SELECT 1 FROM trades WHERE trade_id=?",(trade_id,)).fetchone():
            raise OperationalFailure(f"Unknown trade {trade_id} for broker order state")
        validate_order_identity(conn,oid,trade_id,phase,method,client_id)
        previous=conn.execute("SELECT filled_quantity FROM broker_orders WHERE order_id=?",(oid,)).fetchone()
        if previous and cumulative<int(previous["filled_quantity"] or 0):
            raise OperationalFailure(f"Broker order {oid} cumulative fill moved backwards")
        now=stamp(getattr(order,"updated_at",None))
        conn.execute('''INSERT INTO broker_orders(order_id,trade_id,client_order_id,phase,method,symbol,ordered_quantity,
          filled_quantity,canceled_quantity,remaining_quantity,filled_cash_flow_cents,fees_cents,commission_status,
          lifecycle_status,terminal,stop_reason,submitted_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET
          client_order_id=COALESCE(broker_orders.client_order_id,excluded.client_order_id),
          ordered_quantity=excluded.ordered_quantity,filled_quantity=excluded.filled_quantity,
          canceled_quantity=excluded.canceled_quantity,remaining_quantity=excluded.remaining_quantity,
          lifecycle_status=excluded.lifecycle_status,terminal=excluded.terminal,
          stop_reason=excluded.stop_reason,updated_at=excluded.updated_at''',
          (oid,trade_id,client_id,phase,method,str(getattr(order,"symbol","") or ""),ordered,cumulative,canceled,
           order_remaining,0,0,"unavailable",enum_text(getattr(order,"status","")) or "unknown",
           int(is_terminal_order(order)),stop_reason,now,now))
        column="open_order_id" if phase=="open" else "close_order_id"
        conn.execute(f"UPDATE trades SET {column}=?,reconciliation_status=?,updated_at=? WHERE trade_id=?",
                     (oid,stop_reason,now,trade_id))


def record_fill(trade_id, phase, method, order, close_reason=None):
    """Persist only the newly confirmed cumulative fill delta for one broker order."""
    oid = order_id(order)
    if not oid:
        raise OperationalFailure("Confirmed fill has no broker order ID")
    ordered,cumulative,canceled,order_remaining = quantities(order)
    if cumulative <= 0:
        persist_unpriced_order_state(
            trade_id,phase,method,order,str(getattr(order,"stop_reason","") or "no_fill")
        )
        return None
    raw_price=getattr(order,"average_fill_price",None)
    if raw_price in (None, ""):
        raw_price=getattr(order,"filled_avg_price",None)
    if raw_price in (None, ""):
        reason="FILLED_QUANTITY_WITHOUT_BROKER_PRICE"
        persist_unpriced_order_state(trade_id,phase,method,order,reason)
        raise ReconciliationFailure(
            f"Broker order {oid} reports {cumulative} filled contract(s) without an average fill price; cashflow accounting is deferred"
        )
    price = dec(raw_price)
    if not price.is_finite():
        reason="FILLED_QUANTITY_WITH_INVALID_BROKER_PRICE"
        persist_unpriced_order_state(trade_id,phase,method,order,reason)
        raise ReconciliationFailure(
            f"Broker order {oid} reports a non-finite average fill price; cashflow accounting is deferred"
        )
    commission_value=getattr(order,"commission_amount",None)
    if commission_value is None: commission_value=getattr(order,"commission",None)
    fees_total=dollars_to_cents(commission_value or 0)
    commission_status="confirmed" if commission_value is not None else "unavailable"
    activity = str(getattr(order,"broker_fill_id",None) or getattr(order,"activity_id",None) or getattr(order,"fill_activity_id",None) or "") or None
    occurred = stamp(getattr(order,"filled_at",None) or getattr(order,"updated_at",None))
    with db(write=True) as conn:
        trade = conn.execute("SELECT * FROM trades WHERE trade_id=?",(trade_id,)).fetchone()
        if not trade:
            raise OperationalFailure(f"Unknown trade {trade_id} for confirmed fill")
        client_id=str(getattr(order,"client_order_id","") or "") or None
        validate_order_identity(conn,oid,trade_id,phase,method,client_id)
        previous = conn.execute("SELECT * FROM broker_orders WHERE order_id=?",(oid,)).fetchone()
        accounted=conn.execute('''SELECT COALESCE(MAX(cumulative_order_filled_quantity),0) AS quantity,
          COALESCE(SUM(cash_flow_cents),0) AS cashflow,COALESCE(SUM(fees_cents),0) AS fees
          FROM fills WHERE broker_order_id=? AND phase=?''',(oid,phase)).fetchone()
        old_qty=int(accounted["quantity"] or 0)
        old_cf=int(accounted["cashflow"] or 0)
        old_fees=int(accounted["fees"] or 0)
        if (previous and cumulative<int(previous["filled_quantity"] or 0)) or cumulative < old_qty:
            raise OperationalFailure(f"Broker order {oid} cumulative fill moved backwards")
        delta_qty = cumulative-old_qty
        cumulative_cf = fill_cashflow(phase,method,price,cumulative)
        delta_cf,delta_fees = cumulative_cf-old_cf,max(0,fees_total-old_fees)
        conn.execute('''INSERT INTO broker_orders(order_id,trade_id,client_order_id,phase,method,symbol,ordered_quantity,
          filled_quantity,canceled_quantity,remaining_quantity,filled_cash_flow_cents,fees_cents,commission_status,filled_avg_price,
          lifecycle_status,terminal,stop_reason,submitted_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(order_id) DO UPDATE SET client_order_id=COALESCE(broker_orders.client_order_id,excluded.client_order_id),
          filled_quantity=excluded.filled_quantity,canceled_quantity=excluded.canceled_quantity,
          remaining_quantity=excluded.remaining_quantity,filled_cash_flow_cents=excluded.filled_cash_flow_cents,fees_cents=excluded.fees_cents,
          commission_status=excluded.commission_status,
          filled_avg_price=excluded.filled_avg_price,lifecycle_status=excluded.lifecycle_status,terminal=excluded.terminal,
          stop_reason=excluded.stop_reason,updated_at=excluded.updated_at''',
          (oid,trade_id,client_id,phase,method,str(getattr(order,"symbol","") or ""),
           ordered,cumulative,canceled,order_remaining,cumulative_cf,fees_total,commission_status,str(price),enum_text(getattr(order,"status","filled")) or "filled",
           int(is_terminal_order(order)),str(getattr(order,"stop_reason","") or ""),occurred,occurred))
        if delta_qty == 0:
            if cumulative_cf!=old_cf:
                raise ReconciliationFailure(f"Broker order {oid} changed its accounted cumulative fill price")
            return None
        if phase == "open":
            available=max(0,int(trade["ordered_quantity"] or 0)-int(trade["filled_quantity"] or 0))
            if delta_qty>available:
                raise OperationalFailure(f"Opening fill exceeds trade {trade_id} ordered quantity")
            alloc_cf=alloc_fees=0
            realized=None
        else:
            remaining=int(trade["remaining_quantity"] or 0)
            filled_total=int(trade["filled_quantity"] or 0)
            if delta_qty>remaining or filled_total<=0:
                raise OperationalFailure(f"Closing fill exceeds trade {trade_id} remaining quantity")
            used_cf=int(trade["allocated_open_cash_flow_cents"] or 0)
            used_fees=int(trade["allocated_open_fees_cents"] or 0)
            open_cf=int(trade["opening_cash_flow_cents"] or 0)
            open_fees=int(trade["opening_fees_cents"] or 0)
            if conn.execute("SELECT 1 FROM fills WHERE trade_id=? AND phase='open' AND commission_status='unavailable' LIMIT 1",(trade_id,)).fetchone():
                commission_status="unavailable"
            if delta_qty==remaining:
                alloc_cf,alloc_fees=open_cf-used_cf,open_fees-used_fees
            else:
                alloc_cf=int((Decimal(open_cf)*delta_qty/filled_total).quantize(Decimal("1"),rounding=ROUND_HALF_UP))
                alloc_fees=int((Decimal(open_fees)*delta_qty/filled_total).quantize(Decimal("1"),rounding=ROUND_HALF_UP))
            realized=alloc_cf+delta_cf-alloc_fees-delta_fees
        fill_id=activity or str(uuid.uuid5(UUID_NAMESPACE,f"fill|{oid}|{phase}|{cumulative}"))
        conn.execute('''INSERT INTO fills(fill_id,trade_id,parent_trade_id,broker_order_id,broker_activity_id,phase,method,
          cumulative_order_filled_quantity,filled_quantity,price,cash_flow_cents,fees_cents,allocated_open_cash_flow_cents,
          allocated_open_fees_cents,realized_pnl_cents,commission_status,occurred_at,close_reason,sync_status)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending')''',
          (fill_id,trade_id,trade["parent_trade_id"] or trade_id,oid,activity,phase,method,cumulative,delta_qty,str(price),
           delta_cf,delta_fees,alloc_cf,alloc_fees,realized,commission_status,occurred,close_reason))
        if phase=="open":
            new_filled=int(trade["filled_quantity"] or 0)+delta_qty
            new_remaining=int(trade["remaining_quantity"] or 0)+delta_qty
            new_cf=int(trade["opening_cash_flow_cents"] or 0)+delta_cf
            new_fees=int(trade["opening_fees_cents"] or 0)+delta_fees
            avg=Decimal(abs(new_cf))/Decimal(new_filled*10000)
            conn.execute('''UPDATE trades SET "Size"=?,"Open Price"=?,"Open Comm."=?,filled_quantity=?,remaining_quantity=?,
              opening_cash_flow_cents=?,opening_fees_cents=?,lifecycle_status='OPEN',open_sync_status='pending',
              reconciliation_status='PENDING_RECONCILIATION',updated_at=? WHERE trade_id=?''',
              (new_filled,float(avg),cents_to_dollars(new_fees),new_filled,new_remaining,new_cf,new_fees,stamp(),trade_id))
        else:
            new_closed=int(trade["closed_quantity"] or 0)+delta_qty
            new_remaining=int(trade["remaining_quantity"] or 0)-delta_qty
            close_cf=int(trade["closing_cash_flow_cents"] or 0)+delta_cf
            close_fees=int(trade["closing_fees_cents"] or 0)+delta_fees
            total_pnl=int(trade["realized_pnl_cents"] or 0)+int(realized)
            lifecycle="CLOSED" if new_remaining==0 else "PARTIALLY_CLOSED"
            close_date=datetime.now(EASTERN).date().isoformat() if new_remaining==0 else ""
            conn.execute('''UPDATE trades SET "Close Date"=?,"Close Price"=?,"Close Comm."=?,closed_quantity=?,remaining_quantity=?,
              closing_cash_flow_cents=?,closing_fees_cents=?,allocated_open_cash_flow_cents=allocated_open_cash_flow_cents+?,
              allocated_open_fees_cents=allocated_open_fees_cents+?,realized_pnl_cents=?,lifecycle_status=?,close_sync_status='pending',
              close_method=?,close_reason=?,reconciliation_status=?,updated_at=? WHERE trade_id=?''',
              (close_date,float(price),cents_to_dollars(close_fees),new_closed,new_remaining,close_cf,close_fees,alloc_cf,alloc_fees,
               total_pnl,lifecycle,method,close_reason or "confirmed_fill",
               "PENDING_FEE_RECONCILIATION" if commission_status=="unavailable" else "PENDING_RECONCILIATION",stamp(),trade_id))
        enqueue_fill(conn,trade_id,fill_id,phase)
    return fill_id


def sync_sheet_outbox(*, deadline=None, max_events=5):
    if not GOOGLE_SCRIPT_URL or not GOOGLE_SCRIPT_SECRET:
        print("Google Sheet synchronization is not configured; queued events remain in SQLite.")
        return True
    if not re.fullmatch(r"https://script\.google\.com/macros/s/[^/]+/exec", GOOGLE_SCRIPT_URL):
        raise OperationalFailure("GOOGLE_SCRIPT_URL is not a valid Apps Script web deployment URL")
    with db() as conn:
        events=conn.execute('''SELECT sheet_outbox.*,fills.realized_pnl_cents AS outbox_realized_pnl_cents,
          CASE WHEN fills.commission_status='unavailable' OR EXISTS(
            SELECT 1 FROM fills AS opening_fill WHERE opening_fill.trade_id=sheet_outbox.trade_id
            AND opening_fill.phase='open' AND opening_fill.commission_status='unavailable'
          ) THEN 'unavailable' ELSE fills.commission_status END AS outbox_commission_status
          FROM sheet_outbox JOIN fills
          ON fills.fill_id=sheet_outbox.fill_id WHERE sheet_outbox.state!='synced'
          ORDER BY sheet_outbox.created_at''').fetchall()
        identity=conn.execute("SELECT broker_mode,account_fingerprint FROM broker_identity WHERE singleton_id=1").fetchone()
    if events and not identity:
        raise OperationalFailure("Cannot sync Sheet events from an unbound broker ledger")
    failures=[]
    processed=0
    for event in events:
        if processed>=max_events:
            break
        # A failed Apps Script call can consume about a minute across bounded
        # retries. Keep that work outside the order-management safety reserve.
        if deadline is not None and time_module.monotonic()+70>=deadline:
            break
        processed+=1
        payload=json.loads(event["payload_json"])
        payload["Broker Mode"]=identity["broker_mode"]
        payload["Broker Account Fingerprint"]=identity["account_fingerprint"]
        payload["P&L Status"]=pnl_status(event["outbox_realized_pnl_cents"],event["outbox_commission_status"])
        payload["auth_token"]=GOOGLE_SCRIPT_SECRET
        payload["Open Sync Status"]="synced" if event["phase"]=="open" else payload.get("Open Sync Status","")
        payload["Close Sync Status"]="synced" if event["phase"]=="close" else payload.get("Close Sync Status","")
        ok=False; last_error="unknown error"
        for attempt in range(1,4):
            try:
                response=requests.post(GOOGLE_SCRIPT_URL,json=payload,timeout=(5,15))
                response.raise_for_status()
                body=response.json()
                if not isinstance(body,dict) or body.get("ok") is not True or body.get("status") != 200:
                    raise OperationalFailure(f"Sheet logical error: {body.get('error') if isinstance(body,dict) else 'invalid JSON'}")
                written_headers=set(body.get("written_headers") or ())
                missing_written=SHEET_REQUIRED_FILL_FIELDS-written_headers
                if missing_written:
                    raise OperationalFailure(
                        "Sheet response did not confirm all required fill fields: "
                        + ", ".join(sorted(missing_written))
                    )
                ok=True; break
            except (requests.RequestException,ValueError,OperationalFailure) as exc:
                last_error=redact(exc)
                if attempt<3: time_module.sleep(attempt)
        with db(write=True) as conn:
            if ok:
                conn.execute("UPDATE sheet_outbox SET state='synced',attempts=attempts+1,last_error=NULL,updated_at=? WHERE event_id=?",(stamp(),event["event_id"]))
                conn.execute("UPDATE fills SET sync_status='synced' WHERE fill_id=?",(event["fill_id"],))
                pending=conn.execute("SELECT COUNT(*) FROM sheet_outbox WHERE trade_id=? AND phase=? AND state!='synced'",(event["trade_id"],event["phase"])).fetchone()[0]
                column="open_sync_status" if event["phase"]=="open" else "close_sync_status"
                conn.execute(f"UPDATE trades SET {column}=? WHERE trade_id=?",("pending" if pending else "synced",event["trade_id"]))
            else:
                conn.execute("UPDATE sheet_outbox SET state='pending',attempts=attempts+1,last_error=?,updated_at=? WHERE event_id=?",(last_error,stamp(),event["event_id"]))
                failures.append(f"{event['event_id']}: {last_error}")
    deferred=max(0,len(events)-processed)
    if deferred:
        print(f"Deferred {deferred} Sheet outbox event(s) to a later run to preserve the workflow deadline.")
    if failures:
        print(f"{len(failures)} Sheet outbox event(s) remain queued: {failures[0]}", file=sys.stderr)
        return False
    return True


def get_open_trades(read_only=False):
    with db(read_only=read_only) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM trades WHERE COALESCE(remaining_quantity,0)>0 AND lifecycle_status NOT LIKE 'CLOSED%' ORDER BY \"Open Date\",rowid")]


def account_snapshot(client):
    try:
        account=client.get_account()
        equity=dec(getattr(account,"equity",None))
        buying=dec(getattr(account,"options_buying_power",None) or getattr(account,"buying_power",None) or getattr(account,"cash",None))
    except Exception as exc:
        raise OperationalFailure(f"Unable to fetch configured Alpaca account buying power: {redact(exc)}") from exc
    if equity<=0 or buying<0:
        raise OperationalFailure("Configured Alpaca account returned invalid equity or buying power")
    return equity,buying


def exposure_cents():
    total=0
    with db() as conn:
        for row in conn.execute("SELECT opening_cash_flow_cents,filled_quantity,remaining_quantity FROM trades WHERE remaining_quantity>0"):
            filled=int(row["filled_quantity"] or 0)
            if filled:
                total+=int((Decimal(abs(int(row["opening_cash_flow_cents"] or 0)))*int(row["remaining_quantity"])/filled).quantize(Decimal("1"),rounding=ROUND_HALF_UP))
        for row in conn.execute("SELECT limit_price,remaining_quantity FROM broker_orders WHERE phase='open' AND terminal=0 AND remaining_quantity>0"):
            if row["limit_price"]: total+=abs(contract_cashflow_cents(row["limit_price"],row["remaining_quantity"]))
    return total


def before_submit_guard(allocation_cents=0, *, opening=False, close_requires_debit=True, deadline=None):
    def guard(client,context):
        if deadline is not None and time_module.monotonic()+60 >= deadline:
            raise OperationalFailure("Insufficient run time remains to submit and cancel-confirm an order safely")
        reconcile_broker_state(client,read_only=True)
        try: clock=client.get_clock()
        except Exception as exc: raise OperationalFailure(f"Unable to refresh broker market clock: {redact(exc)}") from exc
        if not bool(getattr(clock,"is_open",False)):
            raise OperationalFailure("Market closed before order submission")
        next_close=getattr(clock,"next_close",None)
        now=getattr(clock,"timestamp",None) or datetime.now(timezone.utc)
        if isinstance(next_close,datetime) and isinstance(now,datetime) and now>=next_close-timedelta(minutes=3):
            raise OperationalFailure("Order submission cutoff reached before market close")
        equity,buying=account_snapshot(client)
        price=dec(context.get("limit_price",0)); quantity=int(context.get("quantity",0))
        required=(abs(price) if opening else (max(price,Decimal(0)) if close_requires_debit else Decimal(0)))*quantity*100
        required_cents=dollars_to_cents(required)
        if required>buying:
            raise OperationalFailure(f"Insufficient configured-account buying power for {context.get('operation','order')}: required ${required:.2f}")
        if opening:
            if allocation_cents and required_cents>allocation_cents:
                raise OperationalFailure("Order debit exceeds the reserved per-position allocation")
            aggregate_cap=dollars_to_cents(equity*MAX_AGGREGATE_EXPOSURE_PCT)
            if exposure_cents()+required_cents>aggregate_cap:
                raise OperationalFailure("Order would exceed the aggregate exposure cap")
        # The reconciliation, clock, and account requests above are network
        # calls. Re-check both boundaries immediately before the adapter is
        # allowed to submit a physical order.
        if isinstance(next_close,datetime) and datetime.now(timezone.utc)>=next_close-timedelta(minutes=3):
            raise OperationalFailure("Order submission cutoff reached before market close")
        if deadline is not None and time_module.monotonic()+60>=deadline:
            raise OperationalFailure("Insufficient run time remains to submit and cancel-confirm an order safely")
    return guard


def fetch_orders(client):
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        open_request=GetOrdersRequest(status=QueryOrderStatus.OPEN,limit=500,nested=True)
        recent_request=GetOrdersRequest(status=QueryOrderStatus.ALL,after=datetime.now(timezone.utc)-timedelta(days=30),limit=500,nested=True)
        open_orders=list(client.get_orders(filter=open_request))
        recent_orders=list(client.get_orders(filter=recent_request))
        if len(open_orders)>=500 or len(recent_orders)>=500:
            raise OperationalFailure("Broker order history reached the 500-order safety limit; pagination is required")
        deduplicated={order_id(order):order for order in open_orders+recent_orders}
        return list(deduplicated.values())
    except Exception as exc:
        raise OperationalFailure(f"Unable to fetch broker orders: {redact(exc)}") from exc


def position_maps(positions):
    options,equities={},{}
    for position in positions:
        asset=enum_text(getattr(position,"asset_class",""))
        symbol=str(getattr(position,"symbol","") or "").upper()
        qty=dec(getattr(position,"qty",0))
        if enum_text(getattr(position,"side",""))=="short" and qty>0: qty=-qty
        target=options if asset in {"option","us_option"} or "option" in asset else equities
        target[symbol]=target.get(symbol,Decimal(0))+qty
    return options,equities


def option_expiration(symbol):
    match=re.search(r"(\d{6})[CP]\d{8}$",str(symbol or "").upper())
    if not match: return None
    try: return datetime.strptime(match.group(1),"%y%m%d").date()
    except ValueError: return None


def confirmed_expired(client,symbol):
    expiration=option_expiration(symbol)
    if not expiration or expiration>=datetime.now(EASTERN).date(): return False
    try:
        contract=client.get_option_contract(symbol)
        broker_expiration=getattr(contract,"expiration_date",None)
        if isinstance(broker_expiration,datetime): broker_expiration=broker_expiration.date()
        return bool(broker_expiration and broker_expiration<datetime.now(EASTERN).date())
    except Exception:
        return False


def known_order(conn,order):
    oid=order_id(order)
    row=conn.execute("SELECT trade_id,phase,method FROM broker_orders WHERE order_id=?",(oid,)).fetchone()
    if row: return row["trade_id"],row["phase"],row["method"]
    cid=str(getattr(order,"client_order_id","") or "")
    by_prefix=conn.execute('''SELECT trade_id,
      CASE WHEN ? LIKE COALESCE(open_client_order_prefix,'!')||'%' THEN 'open' ELSE 'close' END AS phase
      FROM trades WHERE ? LIKE COALESCE(open_client_order_prefix,'!')||'%'
         OR ? LIKE COALESCE(close_client_order_prefix,'!')||'%' LIMIT 1''',(cid,cid,cid)).fetchone()
    if by_prefix:
        if "-o-" in cid or "-c-" in cid:
            method="calendar_spread"
        else:
            intent=enum_text(getattr(order,"position_intent",""))
            method="single_short_buy" if intent=="buy_to_close" else "single_long_sell" if intent=="sell_to_close" else "single_leg"
        return by_prefix["trade_id"],by_prefix["phase"],method
    return None


def validate_order_link(conn,order,link):
    if not link: return False,"unlinked"
    trade_id,phase,method=link
    trade=conn.execute("SELECT * FROM trades WHERE trade_id=?",(trade_id,)).fetchone()
    if not trade: return False,"missing_trade"
    symbols={str(getattr(order,"symbol","") or "").upper()}
    for leg in getattr(order,"legs",None) or []:
        symbols.add(str(getattr(leg,"symbol","") or "").upper())
    symbols.discard("")
    expected={str(trade["Short Symbol"] or "").upper(),str(trade["Long Symbol"] or "").upper()}
    order_class=enum_text(getattr(order,"order_class",""))
    tif=enum_text(getattr(order,"time_in_force",""))
    order_type=enum_text(getattr(order,"type","") or getattr(order,"order_type",""))
    if tif!="day" or order_type!="limit": return False,"order_type_or_tif_mismatch"
    if method=="calendar_spread":
        if order_class!="mleg" or not expected.issubset(symbols): return False,"spread_class_or_symbols_mismatch"
        expected_legs={
          (str(trade["Short Symbol"] or "").upper(),Decimal(1),"sell" if phase=="open" else "buy","sell_to_open" if phase=="open" else "buy_to_close"),
          (str(trade["Long Symbol"] or "").upper(),Decimal(1),"buy" if phase=="open" else "sell","buy_to_open" if phase=="open" else "sell_to_close"),
        }
        actual_legs=set()
        for leg in getattr(order,"legs",None) or []:
            ratio=getattr(leg,"ratio_qty",None)
            actual_legs.add((str(getattr(leg,"symbol","") or "").upper(),None if ratio is None else dec(ratio),
                             enum_text(getattr(leg,"side","")),enum_text(getattr(leg,"position_intent",""))))
        if actual_legs!=expected_legs: return False,"spread_leg_signature_mismatch"
    if method.startswith("single_") or method=="single_leg":
        symbol=str(getattr(order,"symbol","") or "").upper()
        inferred="single_long_sell" if symbol==str(trade["Long Symbol"] or "").upper() else "single_short_buy" if symbol==str(trade["Short Symbol"] or "").upper() else None
        if method not in {"single_long_sell","single_short_buy"} or method!=inferred:
            return False,"single_leg_method_mismatch"
        actual_method=method
        expected_side="sell" if actual_method=="single_long_sell" else "buy"
        expected_intent="sell_to_close" if actual_method=="single_long_sell" else "buy_to_close"
        if order_class not in {"simple",""} or not inferred or enum_text(getattr(order,"side",""))!=expected_side or enum_text(getattr(order,"position_intent",""))!=expected_intent:
            return False,"single_leg_signature_mismatch"
    qty=int(dec(getattr(order,"qty",0) or 0))
    ceiling=int(trade["ordered_quantity"] if phase=="open" else trade["remaining_quantity"] or 0)
    if qty<=0 or qty>max(ceiling,int(trade["filled_quantity"] or 0)): return False,"order_quantity_out_of_bounds"
    return True,"matched"


def broker_order_summary(order):
    """Return non-secret order facts needed to investigate reconciliation blocks."""
    legs=[]
    for leg in getattr(order,"legs",None) or []:
        legs.append({
            "symbol":str(getattr(leg,"symbol","") or ""),
            "ratio_quantity":str(getattr(leg,"ratio_qty","") or ""),
            "side":enum_text(getattr(leg,"side","")),
            "position_intent":enum_text(getattr(leg,"position_intent","")),
        })
    return {
        "order_id":order_id(order),
        "client_order_id":str(getattr(order,"client_order_id","") or ""),
        "status":enum_text(getattr(order,"status","")),
        "symbol":str(getattr(order,"symbol","") or ""),
        "quantity":str(getattr(order,"qty","") or ""),
        "filled_quantity":str(getattr(order,"filled_qty","") or ""),
        "side":enum_text(getattr(order,"side","")),
        "position_intent":enum_text(getattr(order,"position_intent","")),
        "asset_class":enum_text(getattr(order,"asset_class","")),
        "order_class":enum_text(getattr(order,"order_class","")),
        "order_type":enum_text(getattr(order,"type","") or getattr(order,"order_type","")),
        "time_in_force":enum_text(getattr(order,"time_in_force","")),
        "limit_price":str(getattr(order,"limit_price","") or ""),
        "legs":legs,
    }


def reconcile_broker_state(client,read_only=False,broker_mode=None,broker_identity=None):
    _,configured_name,_=configured_mode()
    if broker_mode is not None and broker_mode!=configured_name:
        raise ReconciliationFailure("Requested reconciliation mode does not match the configured broker mode")
    broker_mode=configured_name
    if broker_identity is None:
        broker_identity=bind_or_validate_broker_identity(client,broker_mode,allow_bind=False)
    elif broker_identity.get("broker_mode")!=broker_mode:
        raise ReconciliationFailure("Broker identity binding does not match the configured reconciliation mode")
    try: positions=list(client.get_all_positions())
    except Exception as exc: raise OperationalFailure(f"Unable to fetch broker positions: {redact(exc)}") from exc
    orders=fetch_orders(client)
    if not read_only:
        with db() as conn:
            linked=[]
            for order in orders:
                link=known_order(conn,order); valid,reason=validate_order_link(conn,order,link)
                linked.append((order,link,valid,reason))
        for order,link,valid,_ in linked:
            if not link or not valid: continue
            trade_id,phase,method=link
            if int(dec(getattr(order,"filled_qty",0) or 0))>0:
                if method=="single_leg":
                    intent=enum_text(getattr(order,"position_intent",""))
                    method="single_short_buy" if "buy_to_close" in intent else "single_long_sell"
                record_fill(trade_id,phase,method,order,"recovered_during_reconciliation")
            else:
                upsert_submitted_order(trade_id,phase,method,order,{"quantity":int(dec(getattr(order,"qty",0) or 0)),"client_order_id":getattr(order,"client_order_id",None),"symbol":getattr(order,"symbol",None)})
    options,equities=position_maps(positions)
    with db(read_only=read_only) as conn:
        trades=[dict(row) for row in conn.execute("SELECT * FROM trades WHERE COALESCE(remaining_quantity,0)>0 AND lifecycle_status NOT LIKE 'CLOSED%'")]
        links={}
        for order in orders:
            link=known_order(conn,order); valid,reason=validate_order_link(conn,order,link)
            links[order_id(order)]=(link,valid,reason)
    discrepancies=[]; resolutions={}; expected=set()
    for trade in trades:
        tid=trade["trade_id"]; qty=Decimal(int(trade["remaining_quantity"] or 0))
        short=str(trade["Short Symbol"] or "").upper(); long=str(trade["Long Symbol"] or "").upper(); ticker=str(trade["Ticker"] or "").upper()
        expected.update({short,long}); long_qty=options.get(long,Decimal(0)); short_qty=options.get(short,Decimal(0))
        if equities.get(ticker,Decimal(0))!=0:
            discrepancies.append({"type":"possible_assignment_or_stock_exposure","trade_id":tid,"ticker":ticker,"quantity":str(equities[ticker])})
        elif long_qty==qty and short_qty==-qty: resolutions[tid]="MATCHED_SPREAD"
        elif long_qty==qty and short_qty==0 and confirmed_expired(client,short): resolutions[tid]="SHORT_EXPIRED_LONG_REMAINS"
        elif long_qty==0 and short_qty==0 and confirmed_expired(client,short) and confirmed_expired(client,long):
            discrepancies.append({"type":"expired_positions_absent_settlement_unverified","trade_id":tid,"short_symbol":short,"long_symbol":long})
        else: discrepancies.append({"type":"position_quantity_mismatch","trade_id":tid,"short_symbol":short,"expected_short":str(-qty),"broker_short":str(short_qty),"long_symbol":long,"expected_long":str(qty),"broker_long":str(long_qty)})
    for symbol,qty in options.items():
        if qty and symbol not in expected: discrepancies.append({"type":"unmatched_broker_option_position","symbol":symbol,"quantity":str(qty)})
    for symbol,qty in equities.items():
        if qty: discrepancies.append({"type":"unmatched_or_assignment_equity_position","symbol":symbol,"quantity":str(qty)})
    for order in orders:
        status=enum_text(getattr(order,"status",""))
        link,valid,reason=links.get(order_id(order),(None,False,"unlinked"))
        cid=str(getattr(order,"client_order_id","") or "")
        asset=enum_text(getattr(order,"asset_class",""))
        relevant=cid.startswith("eta-") or asset in {"option","us_option"} or bool(getattr(order,"legs",None))
        if link and not valid:
            discrepancies.append({"type":"broker_order_link_validation_failed","reason":reason,**broker_order_summary(order)})
        elif not is_terminal_order(order):
            discrepancies.append({"type":"known_order_still_active" if link else "unmatched_active_broker_order",**broker_order_summary(order)})
        elif relevant and not link:
            discrepancies.append({"type":"unmatched_recent_option_order",**broker_order_summary(order)})
    result={"checked_at":stamp(),"broker_mode":broker_mode,
            "broker_account_fingerprint":broker_identity["account_fingerprint"],
            "configured_endpoint_confirmed":True,"broker_option_position_count":len(options),"broker_recent_order_count":len(orders),
            "open_local_trade_count":len(trades),"resolutions":resolutions,"discrepancies":discrepancies}
    if not read_only:
        with db(write=True) as conn:
            conn.execute("INSERT INTO reconciliation_runs VALUES(?,?,?,?,?)",(str(uuid.uuid4()),result["checked_at"],"failed" if discrepancies else "matched",len(discrepancies),json.dumps(result,separators=(",",":"))))
            for tid,status in resolutions.items(): conn.execute("UPDATE trades SET reconciliation_status=?,updated_at=? WHERE trade_id=?",(status,stamp(),tid))
    print(json.dumps(result,indent=2,sort_keys=True))
    if discrepancies: raise ReconciliationFailure(f"Broker reconciliation found {len(discrepancies)} unresolved discrepancy(ies)")
    return result


def is_time_to_open(earnings_date,when,market_close):
    now=datetime.now(EASTERN); close_at=market_close.astimezone(EASTERN)
    intended=close_at.date() if when=="BMO" else earnings_date
    return close_at.date()==intended and close_at-timedelta(minutes=25)<=now<close_at-timedelta(minutes=3)


def is_time_to_close(earnings_date,when):
    day=earnings_date if when=="BMO" else earnings_date+timedelta(days=1)
    return datetime.now(EASTERN)>=datetime.combine(day,time(9,40),tzinfo=EASTERN)


def select_yahoo(stock,earnings_date):
    try:
        expirations=sorted(datetime.strptime(item,"%Y-%m-%d").date() for item in stock.options)
        short=next((item for item in expirations if item>earnings_date),None)
        long=min((item for item in expirations if short and item>short),key=lambda item:abs((item-short).days-30),default=None)
        if not short or not long: return None,None,None
        underlying=stock.history(period="1d")["Close"].iloc[0]
        strikes=stock.option_chain(short.isoformat()).calls["strike"].tolist()
        return short.isoformat(),long.isoformat(),min(strikes,key=lambda item:abs(item-underlying))
    except Exception as exc:
        print(f"Yahoo expiry fallback skipped: {redact(exc)}"); return None,None,None


def mark_no_fill(trade_id,result):
    with db(write=True) as conn:
        row=conn.execute("SELECT filled_quantity FROM trades WHERE trade_id=?",(trade_id,)).fetchone()
        if row and int(row["filled_quantity"] or 0)==0:
            conn.execute("UPDATE trades SET lifecycle_status='CANCELED_NO_FILL',reconciliation_status=?,updated_at=? WHERE trade_id=?",(str(getattr(result,"stop_reason","no_fill") or "no_fill"),stamp(),trade_id))


def close_due_trades(client,reconciliation,run_deadline):
    for trade in get_open_trades():
        if time_module.monotonic()+60>=run_deadline:
            raise OperationalFailure("Run deadline reached before all due positions were processed")
        opened=datetime.strptime(trade["Open Date"],"%Y-%m-%d").date(); when=trade.get("When") or "AMC"
        earnings=datetime.strptime(trade["earnings_date"],"%Y-%m-%d").date() if trade.get("earnings_date") else (opened+timedelta(days=1) if when=="BMO" else opened)
        if not is_time_to_close(earnings,when): continue
        tid=trade["trade_id"]; qty=int(trade["remaining_quantity"] or 0); state=reconciliation["resolutions"].get(tid)
        account_snapshot(client)
        operation_deadline=min(time_module.monotonic()+240,run_deadline)
        common={"max_attempts":8,"overall_timeout":240,"attempt_timeout":30,"cancel_timeout":30}
        if state=="MATCHED_SPREAD":
            method="calendar_spread"
            prefix=reserve_operation_prefix(tid,"close",method)
            result=close_calendar_spread_order(trade["Short Symbol"],trade["Long Symbol"],qty,max_close_debit=Decimal(os.environ.get("MAX_CLOSE_DEBIT","0.50")),
              on_terminal=lambda event,tid=tid:record_fill(tid,"close","calendar_spread",event,"scheduled_post_earnings_close"),
              on_order_state=lambda event,tid=tid:persist_unpriced_order_state(tid,"close","calendar_spread",event,str(getattr(event,"stop_reason","") or "terminal_order_state")),
              on_submitted=lambda order,context,tid=tid:upsert_submitted_order(tid,"close","calendar_spread",order,{**context,"submission_only":True}),
              before_submit=before_submit_guard(close_requires_debit=True,deadline=operation_deadline),
              client_order_id_prefix=prefix,**common)
        elif state=="SHORT_EXPIRED_LONG_REMAINS":
            method="single_long_sell"
            prefix=reserve_operation_prefix(tid,"close",method)
            result=close_single_option_leg_order(trade["Long Symbol"],qty,PositionIntent.SELL_TO_CLOSE,min_sell_price=Decimal(os.environ.get("MIN_LONG_LEG_CLOSE_PRICE","0.01")),
              on_terminal=lambda event,tid=tid:record_fill(tid,"close","single_long_sell",event,"confirmed_short_expiry_long_leg_sale"),
              on_order_state=lambda event,tid=tid:persist_unpriced_order_state(tid,"close","single_long_sell",event,str(getattr(event,"stop_reason","") or "terminal_order_state")),
              on_submitted=lambda order,context,tid=tid:upsert_submitted_order(tid,"close","single_long_sell",order,{**context,"submission_only":True}),
              before_submit=before_submit_guard(close_requires_debit=False,deadline=operation_deadline),
              client_order_id_prefix=prefix,**common)
        else: raise ReconciliationFailure(f"Trade {tid} has no safe close path")
        finalize_execution(tid,result)
        print(f"Close result trade={tid} method={method} filled={getattr(result,'filled_qty',0)} remaining={getattr(result,'remaining_qty',qty)} stop_reason={getattr(result,'stop_reason','')}")


def candidate_allocation(client,spread_cost):
    equity,buying=account_snapshot(client); existing=exposure_cents()
    cap=dollars_to_cents(equity*MAX_AGGREGATE_EXPOSURE_PCT)
    per_position=dollars_to_cents(equity*POSITION_ALLOCATION_PCT)
    allocation=min(per_position,max(0,cap-existing),dollars_to_cents(buying))
    cost=abs(contract_cashflow_cents(spread_cost,1))
    return (allocation//cost if cost else 0),allocation,existing,cap


def open_candidate(client,item,when,earnings_date,market_close,run_deadline):
    if time_module.monotonic()+60>=run_deadline:
        raise OperationalFailure("Run deadline reached before candidate processing")
    ticker=str(item["act_symbol"]).upper()
    if not is_time_to_open(earnings_date,when,market_close):
        print(f"Skipping {ticker}: outside the broker-confirmed opening window."); return
    recommendation=compute_recommendation(ticker)
    if not (isinstance(recommendation,dict) and recommendation.get("avg_volume") and recommendation.get("iv30_rv30") and recommendation.get("ts_slope_0_45")):
        print(f"Skipping {ticker}: strategy screening thresholds were not all satisfied."); return
    filter_date=earnings_date-timedelta(days=1) if when=="BMO" else earnings_date
    short_expiry,long_expiry,strike=select_expiries_and_strike_alpaca(ticker,filter_date)
    if not short_expiry or not long_expiry or strike is None:
        short_expiry,long_expiry,strike=select_yahoo(yf.Ticker(ticker),filter_date)
    if not short_expiry or not long_expiry or strike is None:
        print(f"Skipping {ticker}: expiries or strike unavailable"); return
    chain=get_alpaca_option_chain(ticker)
    if not chain: raise OperationalFailure(f"Option chain unavailable for {ticker}")
    short_contract=chain.get(short_expiry,{}).get(strike,{}).get("call")
    long_contract=chain.get(long_expiry,{}).get(strike,{}).get("call")
    short_symbol=getattr(short_contract,"symbol",None); long_symbol=getattr(long_contract,"symbol",None)
    if not short_symbol or not long_symbol:
        print(f"Skipping {ticker}: selected contracts unavailable"); return
    try:
        short_bid,short_ask,long_bid,long_ask=(dec(value) for value in get_spread_quotes(short_symbol,long_symbol))
    except Exception as exc:
        raise OperationalFailure(f"Validated option quotes unavailable for {ticker}: {redact(exc)}") from exc
    net_bid=long_bid-short_ask; net_ask=long_ask-short_bid
    if net_ask<=0 or net_bid>net_ask:
        print(f"Skipping {ticker}: invalid net spread market bid={net_bid} ask={net_ask}"); return
    midpoint=((net_bid+net_ask)/2).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
    if midpoint<=0: midpoint=Decimal("0.01")
    slippage=Decimal(os.environ.get("OPEN_MAX_DEBIT_SLIPPAGE", "0.05"))
    target_debit=min(net_ask,midpoint+max(slippage,Decimal(0))).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
    if target_debit<=0:
        print(f"Skipping {ticker}: no positive hard debit ceiling"); return
    quantity,allocation,existing,cap=candidate_allocation(client,target_debit)
    if quantity<1:
        print(f"Skipping {ticker}: aggregate exposure cap reached (existing=${existing/100:.2f}, cap=${cap/100:.2f})"); return
    trade_id=stable_trade_id(ticker,earnings_date,when,short_symbol,long_symbol)
    plan_status=create_planned_trade({"trade_id":trade_id,"Ticker":ticker,"Implied Move":recommendation.get("expected_move",""),"When":when,
                                     "Short Symbol":short_symbol,"Long Symbol":long_symbol,"Open Date":datetime.now(EASTERN).date().isoformat(),
                                     "earnings_date":earnings_date.isoformat()},quantity)
    if plan_status=="skipped": return
    print(f"Trade plan {trade_id}: {plan_status} with ordered quantity {quantity}.")
    prefix=reserve_operation_prefix(trade_id,"open","calendar_spread")
    operation_deadline=min(time_module.monotonic()+180,run_deadline)
    result=place_calendar_spread_order(short_symbol,long_symbol,quantity,limit_price=midpoint,
      target_debit_price=target_debit,max_total_cost_allowed=Decimal(allocation)/100,
      on_terminal=lambda event,tid=trade_id:record_fill(tid,"open","calendar_spread",event),
      on_order_state=lambda event,tid=trade_id:persist_unpriced_order_state(tid,"open","calendar_spread",event,str(getattr(event,"stop_reason","") or "terminal_order_state")),
      on_submitted=lambda order,context,tid=trade_id:upsert_submitted_order(tid,"open","calendar_spread",order,{**context,"submission_only":True}),
      before_submit=before_submit_guard(allocation,opening=True,deadline=operation_deadline),max_attempts=8,overall_timeout=180,attempt_timeout=30,cancel_timeout=30,
      client_order_id_prefix=prefix)
    finalize_execution(trade_id,result)
    if int(dec(getattr(result,"filled_qty",0) or 0))==0: mark_no_fill(trade_id,result)
    print(f"Open result trade={trade_id} filled={getattr(result,'filled_qty',0)} remaining={getattr(result,'remaining_qty',quantity)} stop_reason={getattr(result,'stop_reason','')}")


def run_trade_workflow():
    # Validate mode/endpoint and the LIVE ledger-path separation before any
    # local migration can touch the selected database.
    configured_mode()
    init_db()
    run_deadline=time_module.monotonic()+35*60
    if not Decimal(0)<POSITION_ALLOCATION_PCT<=Decimal(1) or not Decimal(0)<MAX_AGGREGATE_EXPOSURE_PCT<=Decimal(1):
        raise OperationalFailure("Exposure percentages must be greater than zero and no more than one")
    if POSITION_ALLOCATION_PCT>MAX_AGGREGATE_EXPOSURE_PCT:
        raise OperationalFailure("Per-position allocation exceeds aggregate exposure cap")
    client,broker_mode=configured_broker_client()
    broker_identity=bind_or_validate_broker_identity(client,broker_mode,allow_bind=True)
    reconciliation=reconcile_broker_state(client,read_only=False,broker_mode=broker_mode,broker_identity=broker_identity)
    try: clock=client.get_clock()
    except Exception as exc: raise OperationalFailure(f"Unable to fetch broker market clock: {redact(exc)}") from exc
    if not bool(getattr(clock,"is_open",False)):
        print(f"Market closed; neutral skip. Next open: {getattr(clock,'next_open','unknown')}"); return 0
    close_due_trades(client,reconciliation,run_deadline)
    now=datetime.now(EASTERN)
    if now.time()<time(12):
        print("Morning position-management run complete; new openings skipped."); return 0
    market_close=getattr(clock,"next_close",None)
    if not isinstance(market_close,datetime): raise OperationalFailure("Broker clock did not provide this session's close")
    if now>=market_close.astimezone(EASTERN)-timedelta(minutes=3):
        print("PAPER entry cutoff reached; reconciliation and position management complete; new openings skipped."); return 0
    next_open=getattr(clock,"next_open",None)
    if not isinstance(next_open,datetime): raise OperationalFailure("Broker clock did not provide the next session open")
    next_session_date=next_open.astimezone(EASTERN).date()
    todays=get_todays_earnings(); tomorrows=get_tomorrows_earnings(next_open=next_open)
    if not isinstance(todays,list) or not isinstance(tomorrows,list): raise OperationalFailure("Earnings source returned invalid data")
    print(f"Earnings source returned {len(todays)} current-session and {len(tomorrows)} next-session records.")
    for item in tomorrows:
        if "before" in str(item.get("when") or "").lower(): open_candidate(client,item,"BMO",next_session_date,market_close,run_deadline)
        elif item.get("act_symbol"): print(f"Skipping {item['act_symbol']}: next-session record is not BMO.")
    for item in todays:
        if "after" in str(item.get("when") or "").lower(): open_candidate(client,item,"AMC",now.date(),market_close,run_deadline)
        elif item.get("act_symbol"): print(f"Skipping {item['act_symbol']}: current-session record is not AMC.")
    return 0


def run_reconcile_only():
    """Strictly non-mutating broker/SQLite check for manual dispatch."""
    if not DB_PATH.exists(): raise OperationalFailure(f"SQLite ledger does not exist at {DB_PATH}")
    client,broker_mode=configured_broker_client()
    broker_identity=bind_or_validate_broker_identity(client,broker_mode,allow_bind=False)
    return 0 if reconcile_broker_state(client,read_only=True,broker_mode=broker_mode,broker_identity=broker_identity) else 2


def run_bind_only():
    """Migrate and bind an otherwise safe ledger; never reconcile or submit/cancel orders."""
    configured_mode()
    init_db()
    client,broker_mode=configured_broker_client()
    identity=bind_or_validate_broker_identity(client,broker_mode,allow_bind=True)
    print(json.dumps({"broker_mode":identity["broker_mode"],
                      "broker_account_fingerprint":identity["account_fingerprint"],
                      "ledger":str(DB_PATH)},sort_keys=True))
    return 0


def run_migrate_only():
    """Apply only the additive local SQLite migration; never contact external services."""
    if "ALPACA_PAPER" in os.environ:
        configured_mode()
    init_db()
    print(f"SQLite migration complete: {DB_PATH}")
    return 0


def run_sheet_sync_only():
    """Deliver queued Sheet events without contacting Alpaca or blocking trading."""
    configured_mode()
    if not DB_PATH.exists():
        raise OperationalFailure(f"SQLite ledger does not exist at {DB_PATH}")
    init_db()
    return 0 if sync_sheet_outbox() else 2


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("--reconcile-only",action="store_true",help="compare the explicitly configured Alpaca account and SQLite without mutations")
    parser.add_argument("--migrate-only",action="store_true",help="apply only the additive SQLite migration")
    parser.add_argument("--bind-only",action="store_true",help="migrate and bind a safe ledger to the explicitly configured Alpaca account without trading")
    parser.add_argument("--sync-sheet",action="store_true",help="deliver queued Sheet events without contacting Alpaca")
    args=parser.parse_args(argv)
    if sum((args.reconcile_only,args.migrate_only,args.bind_only,args.sync_sheet))>1:
        parser.error("choose only one workflow mode")
    try:
        if args.migrate_only: return run_migrate_only()
        if args.bind_only: return run_bind_only()
        if args.sync_sheet: return run_sheet_sync_only()
        return run_reconcile_only() if args.reconcile_only else run_trade_workflow()
    except ReconciliationFailure as exc:
        print(f"Reconciliation failure: {redact(exc)}",file=sys.stderr); return 4
    except OperationalFailure as exc:
        print(f"Operational failure: {redact(exc)}",file=sys.stderr); return 2
    except Exception as exc:
        print(f"Unexpected workflow failure ({type(exc).__name__}): {redact(exc)}",file=sys.stderr); return 3


if __name__=="__main__": sys.exit(main())
