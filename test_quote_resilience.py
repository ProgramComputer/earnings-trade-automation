import io
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from alpaca.trading.enums import PositionIntent

import alpaca_integration
import trade_workflow


def option_quote(bid, ask, bid_size=10, ask_size=10):
    return SimpleNamespace(
        bid_price=bid,
        ask_price=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        timestamp=datetime.now(timezone.utc),
    )


# NKE on 2026-10-02: the expiring short call had no bid at all.
NO_BID_SHORT = option_quote("0", "0.01", bid_size=0)
LIVE_LONG = option_quote("0.48", "0.53")


class StopAfterCapture(Exception):
    pass


class EmptyBidTests(unittest.TestCase):
    def fake_quotes(self, quotes):
        client = Mock()
        client.get_option_latest_quote.return_value = quotes
        return (
            patch.object(alpaca_integration, "OptionHistoricalDataClient", return_value=client),
            patch.object(alpaca_integration, "_require_explicit_trading_mode"),
        )

    def test_empty_bid_is_rejected_unless_allowed(self):
        with self.assertRaisesRegex(alpaca_integration.QuoteValidationError, "zero bid_size"):
            alpaca_integration._validated_quote(NO_BID_SHORT, "SHORT", 120)
        self.assertEqual(
            alpaca_integration._validated_quote(NO_BID_SHORT, "SHORT", 120, allow_empty_bid=True),
            (Decimal("0"), Decimal("0.01")),
        )

    def test_allowance_still_requires_a_consistent_bid_and_an_offer(self):
        for quote, message in (
            (option_quote("0.05", "0.10", bid_size=0), "zero bid_size"),
            (option_quote("0", "0.01", bid_size=0, ask_size=0), "zero ask_size"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(alpaca_integration.QuoteValidationError, message):
                    alpaca_integration._validated_quote(quote, "LEG", 120, allow_empty_bid=True)

    def test_closing_accepts_an_empty_short_bid_but_opening_and_the_long_leg_do_not(self):
        client_patch, mode_patch = self.fake_quotes({"SHORT": NO_BID_SHORT, "LONG": LIVE_LONG})
        with client_patch, mode_patch:
            snapshot = alpaca_integration._fetch_spread_quote_snapshot(
                "SHORT", "LONG", 120, allow_empty_short_bid=True, validation_retry_delay_seconds=0
            )
            with self.assertRaisesRegex(alpaca_integration.QuoteValidationError, "zero bid_size"):
                alpaca_integration._fetch_spread_quote_snapshot(
                    "SHORT", "LONG", 120, validation_retry_delay_seconds=0
                )
        self.assertEqual(snapshot.closing_signed_ask, Decimal("-0.47"))

        client_patch, mode_patch = self.fake_quotes({"SHORT": LIVE_LONG, "LONG": NO_BID_SHORT})
        with client_patch, mode_patch:
            with self.assertRaisesRegex(alpaca_integration.QuoteValidationError, "zero bid_size"):
                alpaca_integration._fetch_spread_quote_snapshot(
                    "SHORT", "LONG", 120, allow_empty_short_bid=True, validation_retry_delay_seconds=0
                )

    def test_calendar_close_requests_the_empty_short_bid_allowance(self):
        fetch = Mock(side_effect=StopAfterCapture)
        with (
            patch.object(alpaca_integration, "init_alpaca_client"),
            patch.object(alpaca_integration, "_fetch_spread_quote_snapshot", fetch),
        ):
            with self.assertRaises(StopAfterCapture):
                alpaca_integration.close_calendar_spread_order(
                    "SHORT", "LONG", 1, max_close_debit=Decimal("0.50"), client_order_id_prefix="test"
                )
        self.assertTrue(fetch.call_args.kwargs["allow_empty_short_bid"])

    def test_single_leg_allowance_follows_the_side_and_a_no_bid_buy_starts_at_one_cent(self):
        submitted = []

        def capture(_client, request, _client_order_id):
            submitted.append(request)
            raise StopAfterCapture

        for intent, allowed in ((PositionIntent.BUY_TO_CLOSE, True), (PositionIntent.SELL_TO_CLOSE, False)):
            fetch = Mock(return_value=alpaca_integration.SingleQuoteSnapshot(Decimal("0"), Decimal("0.01")))
            with (
                self.subTest(intent=intent),
                patch.object(alpaca_integration, "init_alpaca_client"),
                patch.object(alpaca_integration, "_fetch_single_quote_snapshot", fetch),
                patch.object(alpaca_integration, "_submit_order_idempotently", side_effect=capture),
            ):
                with self.assertRaises(StopAfterCapture):
                    alpaca_integration.close_single_option_leg_order(
                        "OPTION", 1, intent, client_order_id_prefix="test"
                    )
            self.assertIs(fetch.call_args.kwargs["allow_empty_bid"], allowed)
        self.assertEqual(submitted[0].limit_price, 0.01)


class QuoteRetryTests(unittest.TestCase):
    def test_missing_numeric_price_is_a_retryable_validation_failure(self):
        quote = SimpleNamespace(
            bid_price=None,
            ask_price="0.10",
            bid_size=1,
            ask_size=1,
            timestamp=datetime.now(timezone.utc),
        )

        with self.assertRaises(alpaca_integration.QuoteValidationError):
            alpaca_integration._validated_quote(quote, "OPTION", 30)

    def test_quote_validation_retry_accepts_only_a_later_valid_snapshot(self):
        valid_snapshot = object()
        fetch_once = Mock(
            side_effect=[
                alpaca_integration.QuoteValidationError("stale quote"),
                alpaca_integration.QuoteValidationError("zero bid_size"),
                valid_snapshot,
            ]
        )

        with patch.object(alpaca_integration.time, "sleep") as sleep:
            result = alpaca_integration._retry_validated_quote_fetch(
                fetch_once,
                "SHORT/LONG",
                attempts=3,
                retry_delay_seconds=2,
            )

        self.assertIs(result, valid_snapshot)
        self.assertEqual(fetch_once.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(2)

    def test_quote_validation_retry_remains_bounded_and_fails_closed(self):
        fetch_once = Mock(
            side_effect=alpaca_integration.QuoteValidationError("zero ask_size")
        )

        with patch.object(alpaca_integration.time, "sleep") as sleep:
            with self.assertRaisesRegex(
                alpaca_integration.QuoteValidationError, "zero ask_size"
            ):
                alpaca_integration._retry_validated_quote_fetch(
                    fetch_once,
                    "SHORT/LONG",
                    attempts=3,
                    retry_delay_seconds=2,
                )

        self.assertEqual(fetch_once.call_count, 3)
        self.assertEqual(sleep.call_count, 2)


class CloseIsolationTests(unittest.TestCase):
    def test_close_quote_failure_is_persisted_with_redacted_single_line_detail(self):
        original_path = trade_workflow.DB_PATH
        with tempfile.TemporaryDirectory() as directory:
            trade_workflow.DB_PATH = Path(directory) / "test.db"
            try:
                connection = sqlite3.connect(trade_workflow.DB_PATH)
                connection.execute(
                    "CREATE TABLE trades "
                    "(trade_id TEXT PRIMARY KEY, reconciliation_status TEXT, updated_at TEXT)"
                )
                connection.execute("INSERT INTO trades(trade_id) VALUES('trade-1')")
                connection.commit()
                connection.close()

                status = trade_workflow.record_close_quote_failure(
                    "trade-1", "bad quote\nunsafe snapshot"
                )
                connection = sqlite3.connect(trade_workflow.DB_PATH)
                stored = connection.execute(
                    "SELECT reconciliation_status, updated_at FROM trades WHERE trade_id='trade-1'"
                ).fetchone()
                connection.close()
            finally:
                trade_workflow.DB_PATH = original_path

        self.assertEqual(
            status,
            "CLOSE_QUOTE_VALIDATION_FAILED: bad quote unsafe snapshot",
        )
        self.assertEqual(stored[0], status)
        self.assertTrue(stored[1])

    def test_one_bad_quote_does_not_block_later_due_trade_management(self):
        failed_trade = {
            "trade_id": "trade-bad",
            "Open Date": "2026-08-31",
            "When": "AMC",
            "earnings_date": "2026-09-01",
            "remaining_quantity": 1,
            "Short Symbol": "BAD-SHORT",
            "Long Symbol": "BAD-LONG",
        }
        managed_trade = {
            "trade_id": "trade-good",
            "Open Date": "2026-08-31",
            "When": "AMC",
            "earnings_date": "2026-09-01",
            "remaining_quantity": 1,
            "Short Symbol": "GOOD-SHORT",
            "Long Symbol": "GOOD-LONG",
        }
        resolution = {
            "resolutions": {
                "trade-bad": "MATCHED_SPREAD",
                "trade-good": "MATCHED_SPREAD",
            }
        }
        good_result = SimpleNamespace(
            filled_qty=1,
            remaining_qty=0,
            stop_reason="requested_quantity_filled",
            attempts=(),
        )

        def close_spread(short_symbol, _long_symbol, _quantity, **_kwargs):
            if short_symbol == "BAD-SHORT":
                raise alpaca_integration.QuoteValidationError("still stale")
            return good_result

        with (
            patch.object(trade_workflow, "get_open_trades", return_value=[failed_trade, managed_trade]),
            patch.object(trade_workflow, "is_time_to_close", return_value=True),
            patch.object(trade_workflow.time_module, "monotonic", return_value=0),
            patch.object(trade_workflow, "account_snapshot"),
            patch.object(
                trade_workflow,
                "reserve_operation_prefix",
                side_effect=lambda trade_id, _phase, _method: f"prefix-{trade_id}",
            ),
            patch.object(
                trade_workflow,
                "close_calendar_spread_order",
                side_effect=close_spread,
            ) as close_order,
            patch.object(
                trade_workflow,
                "record_close_quote_failure",
                return_value="CLOSE_QUOTE_VALIDATION_FAILED: still stale",
            ) as record_failure,
            patch.object(trade_workflow, "finalize_execution") as finalize,
        ):
            failures = trade_workflow.close_due_trades(object(), resolution, 10_000)

        self.assertEqual(
            failures,
            ["trade=trade-bad CLOSE_QUOTE_VALIDATION_FAILED: still stale"],
        )
        self.assertEqual(close_order.call_count, 2)
        record_failure.assert_called_once()
        self.assertEqual(record_failure.call_args.args[0], "trade-bad")
        finalize.assert_called_once_with("trade-good", good_result)


class SheetResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


def trade_row_response(payload_fields=None):
    return SheetResponse({
        "ok": True,
        "status": 200,
        "layout": "trade-rows",
        "written_headers": sorted(payload_fields or trade_workflow.SHEET_REQUIRED_TRADE_FIELDS),
    })


class SheetTradeRowTests(unittest.TestCase):
    """NIO: 178 contracts opened in two fills of 89, then closed in one fill."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for name, value in (
            ("DB_PATH", Path(directory.name) / "trades.db"),
            ("GOOGLE_SCRIPT_URL", "https://script.google.com/macros/s/test-deployment/exec"),
            ("GOOGLE_SCRIPT_SECRET", "test-only-secret"),
        ):
            patcher = patch.object(trade_workflow, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        sleep = patch.object(trade_workflow.time_module, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        trade_workflow.init_db()
        with trade_workflow.db(write=True) as conn:
            conn.execute(
                """INSERT INTO broker_identity VALUES(1,'PAPER','fingerprint',
                'https://paper-api.alpaca.markets','2026-08-31','2026-08-31')"""
            )
            conn.execute(
                """INSERT INTO trades("Ticker","Implied Move","Side","When","Size","Short Symbol",
                "Long Symbol","Open Date","Close Date",trade_id,parent_trade_id,ordered_quantity,
                lifecycle_status,close_method,close_reason,open_sync_status,close_sync_status)
                VALUES('NIO','8%','debit','BMO',178,'NIO-SHORT','NIO-LONG','2026-08-31',
                '2026-09-02','trade-1','trade-1',178,'CLOSED','calendar','scheduled_exit',
                'pending','pending')"""
            )
            for order_id, phase in (("order-open", "open"), ("order-close", "close")):
                conn.execute(
                    """INSERT INTO broker_orders(order_id,trade_id,phase,method,ordered_quantity,
                    filled_quantity,lifecycle_status,terminal,updated_at)
                    VALUES(?,?,?,'calendar',178,178,'filled',1,'2026-09-02')""",
                    (order_id, "trade-1", phase),
                )
        self.add_fill("fill-open-1", "order-open", "open", 89, -106800, None, "2026-08-31T19:00:00+00:00")
        self.add_fill("fill-open-2", "order-open", "open", 178, -106800, None, "2026-08-31T19:01:00+00:00")

    def add_fill(self, fill_id, order_id, phase, cumulative, cash_flow_cents, realized_pnl_cents, occurred_at):
        quantity = 89 if phase == "open" else 178
        with trade_workflow.db(write=True) as conn:
            conn.execute(
                """INSERT INTO fills(fill_id,trade_id,parent_trade_id,broker_order_id,
                broker_activity_id,phase,method,cumulative_order_filled_quantity,filled_quantity,
                price,cash_flow_cents,fees_cents,realized_pnl_cents,commission_status,occurred_at)
                VALUES(?,'trade-1','trade-1',?,?,?,'calendar',?,?,?,?,0,?,'confirmed',?)""",
                (fill_id, order_id, f"activity-{fill_id}", phase, cumulative, quantity,
                 "0.12" if phase == "open" else "0.04", cash_flow_cents,
                 realized_pnl_cents, occurred_at),
            )
            conn.execute(
                """INSERT INTO sheet_outbox(event_id,trade_id,fill_id,phase,payload_json,state,
                attempts,created_at,updated_at) VALUES(?,'trade-1',?,?,'{}','pending',0,?,?)""",
                (fill_id, fill_id, phase, occurred_at, occurred_at),
            )

    def close_trade(self):
        self.add_fill("fill-close", "order-close", "close", 178, 71200, -142400, "2026-09-02T19:00:00+00:00")
        with trade_workflow.db(write=True) as conn:
            conn.execute("UPDATE trades SET remaining_quantity=0 WHERE trade_id='trade-1'")

    def ledger(self, query):
        with trade_workflow.db() as conn:
            return [tuple(row) for row in conn.execute(query).fetchall()]

    def test_an_open_trade_is_one_row_with_empty_exit_columns(self):
        with trade_workflow.db() as conn:
            payload = trade_workflow.trade_sheet_payload(conn, "trade-1")

        self.assertEqual(payload["Sync Type"], "trade")
        self.assertEqual(payload["Record ID"], "")
        self.assertEqual(payload["Size"], 178)
        self.assertEqual(payload["Open Price"], 0.12)
        self.assertEqual(payload["Open Cash Flow"], -2136.0)
        self.assertEqual(payload["Close Date"], "")
        self.assertEqual(payload["Close Price"], "")
        self.assertEqual(payload["Remaining Quantity"], 178)
        self.assertEqual(payload["Realized P&L"], "")
        self.assertEqual(payload["P&L Status"], "NOT_REALIZED")
        self.assertEqual(payload["Close Sync Status"], "not_applicable")

    def test_every_pending_fill_of_a_trade_is_sent_as_one_row(self):
        self.close_trade()
        with patch.object(trade_workflow.requests, "post", return_value=trade_row_response()) as post:
            self.assertTrue(trade_workflow.sync_sheet_outbox())

        post.assert_called_once()
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["auth_token"], "test-only-secret")
        self.assertEqual(payload["Trade ID"], "trade-1")
        self.assertEqual(payload["Record ID"], "")
        self.assertEqual(payload["Fill Phase"], "")
        self.assertEqual(payload["Broker Order ID"], "order-open, order-close")
        self.assertEqual(payload["Size"], 178)
        self.assertEqual(payload["Open Price"], 0.12)
        self.assertEqual(payload["Close Date"], "2026-09-02")
        self.assertEqual(payload["Close Price"], -0.04)
        self.assertEqual(payload["Open Cash Flow"], -2136.0)
        self.assertEqual(payload["Close Cash Flow"], 712.0)
        self.assertEqual(payload["Remaining Quantity"], 0)
        self.assertEqual(payload["Realized P&L"], -1424.0)
        self.assertEqual(payload["P&L Status"], "CONFIRMED")
        self.assertEqual(payload["Open Sync Status"], "synced")
        self.assertEqual(payload["Close Sync Status"], "synced")
        self.assertEqual(self.ledger("SELECT DISTINCT state FROM sheet_outbox"), [("synced",)])
        self.assertEqual(self.ledger("SELECT DISTINCT sync_status FROM fills"), [("synced",)])
        self.assertEqual(
            self.ledger("SELECT open_sync_status,close_sync_status FROM trades"),
            [("synced", "synced")],
        )

    def test_an_apps_script_without_trade_rows_keeps_the_events_queued(self):
        old_script = SheetResponse({
            "ok": True,
            "status": 200,
            "written_headers": sorted(trade_workflow.SHEET_REQUIRED_TRADE_FIELDS),
        })
        with patch.object(trade_workflow.requests, "post", return_value=old_script) as post:
            self.assertFalse(trade_workflow.sync_sheet_outbox())

        self.assertEqual(post.call_count, 3)
        states = self.ledger("SELECT state,last_error FROM sheet_outbox")
        self.assertEqual(len(states), 2)
        for state, last_error in states:
            self.assertEqual(state, "pending")
            self.assertIn("one row per trade", last_error)
        self.assertEqual(self.ledger("SELECT DISTINCT sync_status FROM fills"), [("pending",)])

    def test_unconfirmed_trade_fields_keep_the_events_queued(self):
        partial = trade_row_response(trade_workflow.SHEET_REQUIRED_TRADE_FIELDS - {"Close Price"})
        with patch.object(trade_workflow.requests, "post", return_value=partial):
            self.assertFalse(trade_workflow.sync_sheet_outbox())

        for state, last_error in self.ledger("SELECT state,last_error FROM sheet_outbox"):
            self.assertEqual(state, "pending")
            self.assertIn("Close Price", last_error)

    def test_rows_synced_one_per_fill_are_sent_again_once(self):
        with trade_workflow.db(write=True) as conn:
            conn.execute("UPDATE sheet_outbox SET state='synced'")
            conn.execute("DELETE FROM schema_migrations WHERE migration_name='sheet_trade_rows_v1'")
        trade_workflow.init_db()
        self.assertEqual(self.ledger("SELECT DISTINCT state FROM sheet_outbox"), [("pending",)])

        with trade_workflow.db(write=True) as conn:
            conn.execute("UPDATE sheet_outbox SET state='synced'")
        trade_workflow.init_db()
        self.assertEqual(self.ledger("SELECT DISTINCT state FROM sheet_outbox"), [("synced",)])


class SheetBacklogTests(unittest.TestCase):
    """A backlog of closed trades drains in one sync instead of five trades per run."""

    TICKERS = ("NKE", "STZ", "LEVI", "CFG", "RF", "AA", "IBKR")

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for name, value in (
            ("DB_PATH", Path(directory.name) / "trades.db"),
            ("GOOGLE_SCRIPT_URL", "https://script.google.com/macros/s/test-deployment/exec"),
            ("GOOGLE_SCRIPT_SECRET", "test-only-secret"),
        ):
            patcher = patch.object(trade_workflow, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        sleep = patch.object(trade_workflow.time_module, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        trade_workflow.init_db()
        with trade_workflow.db(write=True) as conn:
            conn.execute(
                """INSERT INTO broker_identity VALUES(1,'PAPER','fingerprint',
                'https://paper-api.alpaca.markets','2026-10-15','2026-10-15')"""
            )
            for number, ticker in enumerate(self.TICKERS):
                trade_id, order_id, fill_id = f"trade-{ticker}", f"order-{ticker}", f"fill-{ticker}"
                queued_at = f"2026-10-16T17:{number:02d}:00+00:00"
                conn.execute(
                    """INSERT INTO trades("Ticker","Size","Short Symbol","Long Symbol","Open Date",trade_id,
                    parent_trade_id,ordered_quantity,lifecycle_status) VALUES(?,1,'SHORT','LONG','2026-10-15',?,?,1,'OPEN')""",
                    (ticker, trade_id, trade_id),
                )
                conn.execute(
                    """INSERT INTO broker_orders(order_id,trade_id,phase,method,ordered_quantity,filled_quantity,
                    lifecycle_status,terminal,updated_at) VALUES(?,?,'open','calendar_spread',1,1,'filled',1,?)""",
                    (order_id, trade_id, queued_at),
                )
                conn.execute(
                    """INSERT INTO fills(fill_id,trade_id,parent_trade_id,broker_order_id,phase,method,
                    cumulative_order_filled_quantity,filled_quantity,price,cash_flow_cents,commission_status,occurred_at)
                    VALUES(?,?,?,?,'open','calendar_spread',1,1,'1.00',-10000,'confirmed',?)""",
                    (fill_id, trade_id, trade_id, order_id, queued_at),
                )
                conn.execute(
                    """INSERT INTO sheet_outbox(event_id,trade_id,fill_id,phase,payload_json,created_at,updated_at)
                    VALUES(?,?,?,'open','{}',?,?)""",
                    (fill_id, trade_id, fill_id, queued_at, queued_at),
                )

    def pending(self):
        with trade_workflow.db() as conn:
            return [row[0] for row in conn.execute(
                "SELECT trade_id FROM sheet_outbox WHERE state!='synced' ORDER BY created_at"
            )]

    def test_the_whole_backlog_is_sent_in_one_run(self):
        output = io.StringIO()
        with (
            patch.object(trade_workflow, "configured_mode"),
            patch.object(trade_workflow.requests, "post", return_value=trade_row_response()) as post,
            redirect_stdout(output),
        ):
            self.assertEqual(trade_workflow.run_sheet_sync_only(), 0)
        self.assertEqual(post.call_count, len(self.TICKERS))
        self.assertEqual(self.pending(), [])
        self.assertNotIn("Deferred", output.getvalue())

    def test_the_time_budget_defers_the_rest_and_says_so(self):
        # Each Apps Script request takes 60 s on this clock.
        clock = {"now": 0}

        def post(*_args, **_kwargs):
            clock["now"] += 60
            return trade_row_response()

        output = io.StringIO()
        with (
            patch.object(trade_workflow.time_module, "monotonic", side_effect=lambda: clock["now"]),
            patch.object(trade_workflow.requests, "post", side_effect=post),
            redirect_stdout(output),
        ):
            self.assertTrue(trade_workflow.sync_sheet_outbox(deadline=trade_workflow.SHEET_TRADE_MAX_SECONDS + 150))
        # Trades start at 0, 60 and 120 s; at 180 s one more could overrun the deadline.
        self.assertEqual(self.pending(), ["trade-CFG", "trade-RF", "trade-AA", "trade-IBKR"])
        self.assertIn(
            "Deferred 4 trade(s) of Sheet updates to a later run: this run's Sheet sync time budget ran out.",
            output.getvalue(),
        )
        self.assertNotIn("workflow deadline", output.getvalue())

    def test_a_slow_apps_script_answer_is_waited_for(self):
        with patch.object(trade_workflow.requests, "post", return_value=trade_row_response()) as post:
            trade_workflow.sync_sheet_outbox()
        connect, read = post.call_args.kwargs["timeout"]
        # Requests take about 10 s, and 15 s was too close.
        self.assertGreaterEqual(read, 30)


WORKFLOW = Path(__file__).resolve().parent / ".github" / "workflows" / "config.yml"


def workflow_steps():
    """Map each workflow step's name to its block of YAML text."""
    text = WORKFLOW.read_text(encoding="utf-8")
    steps = {}
    for block in text.split("\n      - name: ")[1:]:
        name, newline, body = block.partition("\n")
        steps[name.strip()] = newline + body
    return steps


def step_modes(body):
    """The run modes a step's `if:` condition names."""
    if "\n        if:" not in body:
        return set()
    condition = body.split("\n        if:", 1)[1].split("\n        shell:", 1)[0].split("\n        uses:", 1)[0]
    return set(re.findall(r"run_mode == '([a-z-]+)'", condition))


class WorkflowSheetStepTests(unittest.TestCase):
    def test_market_closed_runs_only_set_up_sync_report_and_persist_the_ledger(self):
        steps = workflow_steps()
        closed = {name for name, body in steps.items() if "market-closed" in step_modes(body)}
        self.assertEqual(closed, {
            "Set up Python 3.10", "Install dependencies", "Sync optional Sheet outbox",
            "Commit durable ledger updates", "Report optional Sheet synchronization",
        })
        self.assertNotIn("market-closed", step_modes(steps["Run PAPER trading workflow"]))
        self.assertNotIn("market-closed", step_modes(steps["Wait for PAPER entry window"]))

    def test_reconcile_only_runs_never_sync_the_sheet(self):
        steps = workflow_steps()
        self.assertEqual(step_modes(steps["Sync optional Sheet outbox"]), {"paper-trade", "market-closed"})
        self.assertNotIn("reconcile-only", step_modes(steps["Report optional Sheet synchronization"]))

    def test_the_sync_step_outlasts_the_sync_budget(self):
        body = workflow_steps()["Sync optional Sheet outbox"]
        minutes = int(re.search(r"\n        timeout-minutes: (\d+)", body).group(1))
        self.assertGreater(minutes * 60, trade_workflow.SHEET_SYNC_SECONDS + 60)

    def run_report(self, events, outcome="success"):
        body = workflow_steps()["Report optional Sheet synchronization"]
        script = textwrap.dedent(body.split("python - <<'PY'\n", 1)[1].split("\n          PY\n", 1)[0])
        with tempfile.TemporaryDirectory() as directory:
            connection = sqlite3.connect(Path(directory) / "trades.db")
            connection.execute(
                "CREATE TABLE sheet_outbox (event_id TEXT, state TEXT, last_error TEXT, created_at TEXT, updated_at TEXT)"
            )
            connection.executemany("INSERT INTO sheet_outbox VALUES(?,?,?,?,?)", events)
            connection.commit()
            connection.close()
            summary = Path(directory) / "summary.md"
            completed = subprocess.run(
                [sys.executable, "-c", script], cwd=directory, capture_output=True, text=True, check=True,
                env={**os.environ, "GITHUB_STEP_SUMMARY": str(summary), "SHEET_SYNC_OUTCOME": outcome},
            )
            return completed.stdout, summary.read_text(encoding="utf-8")

    def test_the_warning_says_how_long_the_oldest_update_has_waited(self):
        now = datetime.now(timezone.utc)
        queued = lambda **age: (now - timedelta(**age) - timedelta(minutes=1)).isoformat()
        stdout, summary = self.run_report([
            ("synced-1", "synced", None, queued(days=12), queued(days=12)),
            ("open-RF", "pending", "Read timed out", queued(days=4, hours=2), queued(minutes=5)),
            ("close-RF", "pending", "Read timed out", queued(hours=20), queued(minutes=5)),
        ])
        self.assertIn("::warning title=Google Sheet synchronization::", stdout)
        self.assertIn("2 event(s) remain queued; the oldest has waited 4d 2h.", stdout)
        self.assertIn("- Oldest pending update waiting: 4d 2h", summary)

    def test_a_backlog_under_a_day_is_shown_in_hours_and_minutes(self):
        now = datetime.now(timezone.utc)
        stdout, summary = self.run_report([
            ("open-AA", "pending", None, (now - timedelta(hours=3, minutes=7, seconds=30)).isoformat(), now.isoformat()),
        ])
        self.assertIn("the oldest has waited 3h 7m.", stdout)

    def test_an_empty_queue_reports_nothing_waiting_and_no_warning(self):
        now = datetime.now(timezone.utc).isoformat()
        stdout, summary = self.run_report([("synced-1", "synced", None, now, now)])
        self.assertNotIn("::warning", stdout)
        self.assertIn("- Oldest pending update waiting: nothing pending", summary)


if __name__ == "__main__":
    unittest.main()
