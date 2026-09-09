import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import alpaca_integration
import trade_workflow


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
            with self.assertRaisesRegex(
                trade_workflow.OperationalFailure,
                "1 due trade close.*remain unresolved",
            ):
                trade_workflow.close_due_trades(object(), resolution, 10_000)

        self.assertEqual(close_order.call_count, 2)
        record_failure.assert_called_once()
        self.assertEqual(record_failure.call_args.args[0], "trade-bad")
        finalize.assert_called_once_with("trade-good", good_result)


class SheetSyncStatusTests(unittest.TestCase):
    def test_each_fill_payload_uses_the_trades_current_sync_statuses(self):
        class SuccessfulResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "ok": True,
                    "status": 200,
                    "written_headers": list(trade_workflow.SHEET_REQUIRED_FILL_FIELDS),
                }

        original_path = trade_workflow.DB_PATH
        original_url = trade_workflow.GOOGLE_SCRIPT_URL
        original_secret = trade_workflow.GOOGLE_SCRIPT_SECRET
        with tempfile.TemporaryDirectory() as directory:
            trade_workflow.DB_PATH = Path(directory) / "test.db"
            trade_workflow.GOOGLE_SCRIPT_URL = (
                "https://script.google.com/macros/s/test-deployment/exec"
            )
            trade_workflow.GOOGLE_SCRIPT_SECRET = "test-only-secret"
            try:
                connection = sqlite3.connect(trade_workflow.DB_PATH)
                connection.executescript(
                    """
                    CREATE TABLE trades(
                      trade_id TEXT PRIMARY KEY,
                      open_sync_status TEXT,
                      close_sync_status TEXT
                    );
                    CREATE TABLE fills(
                      fill_id TEXT PRIMARY KEY,
                      trade_id TEXT,
                      phase TEXT,
                      realized_pnl_cents INTEGER,
                      commission_status TEXT,
                      sync_status TEXT
                    );
                    CREATE TABLE sheet_outbox(
                      event_id TEXT PRIMARY KEY,
                      trade_id TEXT,
                      fill_id TEXT,
                      phase TEXT,
                      payload_json TEXT,
                      state TEXT,
                      attempts INTEGER,
                      last_error TEXT,
                      created_at TEXT,
                      updated_at TEXT
                    );
                    CREATE TABLE broker_identity(
                      singleton_id INTEGER PRIMARY KEY,
                      broker_mode TEXT,
                      account_fingerprint TEXT
                    );
                    INSERT INTO trades VALUES('trade-1','pending','pending');
                    INSERT INTO fills VALUES('fill-open','trade-1','open',NULL,'confirmed','pending');
                    INSERT INTO fills VALUES('fill-close','trade-1','close',-100,'confirmed','pending');
                    INSERT INTO sheet_outbox VALUES(
                      'fill-open','trade-1','fill-open','open',
                      '{"Open Sync Status":"stale","Close Sync Status":"stale"}',
                      'pending',0,NULL,'2026-09-01','2026-09-01'
                    );
                    INSERT INTO sheet_outbox VALUES(
                      'fill-close','trade-1','fill-close','close',
                      '{"Open Sync Status":"stale","Close Sync Status":"stale"}',
                      'pending',0,NULL,'2026-09-02','2026-09-02'
                    );
                    INSERT INTO broker_identity VALUES(1,'PAPER','fingerprint');
                    """
                )
                connection.commit()
                connection.close()

                with patch.object(
                    trade_workflow.requests,
                    "post",
                    side_effect=[SuccessfulResponse(), SuccessfulResponse()],
                ) as post:
                    self.assertTrue(trade_workflow.sync_sheet_outbox())

                first_payload = post.call_args_list[0].kwargs["json"]
                second_payload = post.call_args_list[1].kwargs["json"]
                connection = sqlite3.connect(trade_workflow.DB_PATH)
                trade_status = connection.execute(
                    "SELECT open_sync_status,close_sync_status FROM trades"
                ).fetchone()
                outbox_states = connection.execute(
                    "SELECT state FROM sheet_outbox ORDER BY created_at"
                ).fetchall()
                connection.close()
            finally:
                trade_workflow.DB_PATH = original_path
                trade_workflow.GOOGLE_SCRIPT_URL = original_url
                trade_workflow.GOOGLE_SCRIPT_SECRET = original_secret

        self.assertEqual(first_payload["Open Sync Status"], "synced")
        self.assertEqual(first_payload["Close Sync Status"], "pending")
        self.assertEqual(second_payload["Open Sync Status"], "synced")
        self.assertEqual(second_payload["Close Sync Status"], "synced")
        self.assertEqual(trade_status, ("synced", "synced"))
        self.assertEqual(outbox_states, [("synced",), ("synced",)])


if __name__ == "__main__":
    unittest.main()
