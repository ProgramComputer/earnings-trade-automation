import hashlib
import io
import json
import os
import tempfile
import unittest
import uuid
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import assignment_recovery as evidence
import alpaca_integration as adapter
import trade_workflow as workflow


SHORT = "XYZ261030C00010000"
LONG = "XYZ261127C00010000"
FINGERPRINT = hashlib.sha256(b"alpaca-account-id:v1:test-account").hexdigest()


def activities():
    return [
        {"id": "assignment-1", "activity_type": "OPASN", "date": "2026-10-30",
         "symbol": SHORT, "qty": "2", "net_amount": "0", "status": "executed"},
        {"id": "delivery-1", "activity_type": "OPTRD", "date": "2026-10-30",
         "symbol": "XYZ", "qty": "-200", "price": "10", "net_amount": "2000", "status": "executed"},
    ]


def contract(symbol, size=100):
    return SimpleNamespace(symbol=symbol, type="call", underlying_symbol="XYZ", size=size, strike_price="10")


def order(oid, qty, price, phase="open", filled=None, status="filled"):
    stamp = datetime(2026, 10, 30, 15, tzinfo=timezone.utc)
    stock = phase == "assignment_cover"
    single = phase == "close"
    return SimpleNamespace(
        id=oid, symbol="XYZ" if stock else LONG if single else SHORT, qty=str(qty),
        filled_qty=str(qty if filled is None else filled), filled_avg_price=str(price),
        status=status, filled_at=stamp, submitted_at=stamp, updated_at=stamp,
        client_order_id=f"external-{oid}", commission=None,
        order_class="simple" if stock or single else "mleg", type="limit", time_in_force="day",
        side="buy" if stock or not single else "sell", limit_price=str(price),
        position_intent="buy_to_close" if stock else "sell_to_close" if single else "buy_to_open",
        asset_class="us_equity" if stock else "us_option",
        legs=[] if stock or single else [
            SimpleNamespace(symbol=SHORT, ratio_qty="1", side="sell", position_intent="sell_to_open"),
            SimpleNamespace(symbol=LONG, ratio_qty="1", side="buy", position_intent="buy_to_open"),
        ],
    )


class AssignmentTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "trades.db"
        self.stack.enter_context(patch.object(workflow, "DB_PATH", self.path))
        self.stack.enter_context(patch.dict(os.environ, {
            "ALPACA_PAPER": "true", "APCA_API_BASE_URL": "https://paper-api.alpaca.markets",
            "APCA_API_KEY_ID": "test-key", "APCA_API_SECRET_KEY": "test-secret",
        }))
        workflow.init_db()
        with workflow.db(write=True) as db:
            db.execute("INSERT INTO broker_identity VALUES(1,'PAPER',?,'https://paper-api.alpaca.markets','now','now')", (FINGERPRINT,))
        self.tid = str(uuid.uuid4())
        workflow.create_planned_trade({"trade_id": self.tid, "Ticker": "XYZ", "When": "BMO",
            "Open Date": "2026-10-27", "earnings_date": "2026-10-28", "Short Symbol": SHORT, "Long Symbol": LONG}, 2)
        self.orders = [order("opening", 2, "1")]
        workflow.record_fill(self.tid, "open", "calendar_spread", self.orders[0])
        self.shares = -200
        self.longs = 2
        self.client = Mock(_base_url="https://paper-api.alpaca.markets", _eta_paper_mode=True)
        self.client.get_account.return_value = SimpleNamespace(id="test-account")
        self.client.get.return_value = activities()
        self.client.get_orders.side_effect = lambda filter: [] if str(filter.status.value) == "open" else self.orders
        self.client.get_all_positions.side_effect = lambda: [
            SimpleNamespace(symbol="XYZ", asset_class="us_equity", qty=str(self.shares)),
            SimpleNamespace(symbol=LONG, asset_class="us_option", qty=str(self.longs)),
        ]
        self.client.get_option_contract.side_effect = contract
        self.output = self.stack.enter_context(redirect_stdout(io.StringIO()))

    def trade(self):
        with workflow.db(read_only=True) as db:
            return dict(db.execute("SELECT * FROM trades WHERE trade_id=?", (self.tid,)).fetchone())

    def reconcile(self, read_only=False):
        return workflow.reconcile_broker_state(self.client, read_only=read_only)

    def cover(self, oid="cover-1", quantity=200, filled=None):
        result = order(oid, quantity, "11", phase="assignment_cover", filled=filled,
                       status="filled" if filled is None or filled == quantity else "canceled")
        workflow.record_assignment_cover(self.tid, result)
        self.orders.append(result)
        self.shares += quantity if filled is None else filled
        return result

    def test_actual_receipt_shapes_match_without_mutating_read_only_ledger(self):
        before = self.path.read_bytes()
        result = self.reconcile(read_only=True)
        self.assertEqual(result["resolutions"][self.tid], "ASSIGNED_SHORT_STOCK")
        self.assertEqual(before, self.path.read_bytes())
        self.client.submit_order.assert_not_called()
        self.client.get.assert_called_once_with("/account/activities", data={
            "activity_types": "OPASN,OPTRD,OPEXC,OPEXP", "after": "2026-10-26", "direction": "asc", "page_size": 100})

    def test_missing_or_inconsistent_evidence_keeps_reconciliation_blocked(self):
        for records in ([], activities()[:1], [activities()[0], {**activities()[1], "net_amount": "1"}],
                        [{**activities()[0], "qty": "1"}, activities()[1]],
                        [*activities(), {**activities()[1], "id": "another-delivery"}]):
            with self.subTest(records=records):
                self.client.get.return_value = records
                with self.assertRaises(workflow.ReconciliationFailure):
                    self.reconcile()
                with workflow.db(read_only=True) as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM paper_assignments").fetchone()[0], 0)

    def test_adjusted_contracts_and_unmatched_stock_are_not_adopted(self):
        self.client.get_option_contract.side_effect = lambda symbol: contract(symbol, 10)
        with self.assertRaises(workflow.ReconciliationFailure): self.reconcile()
        self.client.get_option_contract.side_effect = contract
        self.shares = -300
        with self.assertRaises(workflow.ReconciliationFailure): self.reconcile()

    def test_paper_endpoint_and_account_checks_precede_receipt_matching(self):
        self.client._base_url = "https://api.alpaca.markets"
        with self.assertRaises(workflow.ReconciliationFailure): self.reconcile()
        self.client.get.assert_not_called()
        self.client._base_url = "https://paper-api.alpaca.markets"
        self.client.get.return_value = [{**a, "account_id": "another-account"} for a in activities()]
        with self.assertRaises(workflow.ReconciliationFailure): self.reconcile()

    def test_cover_is_idempotent_and_partial_covers_retain_the_calls(self):
        self.reconcile()
        result = self.cover(filled=50)
        workflow.record_assignment_cover(self.tid, result)
        self.assertEqual(self.reconcile()["resolutions"][self.tid], "ASSIGNED_SHORT_STOCK")
        with self.assertRaises(workflow.ReconciliationFailure):
            workflow.record_fill(self.tid, "close", "single_long_sell", order("early-sale", 2, "2", phase="close"))
        self.assertEqual(self.trade()["remaining_quantity"], 2)
        self.cover("cover-2", quantity=150)
        self.assertEqual(self.reconcile()["resolutions"][self.tid], "ASSIGNED_STOCK_COVERED_LONG_REMAINS")
        with workflow.db(read_only=True) as db:
            self.assertEqual(workflow.assignment_cover_totals(db, self.tid)["quantity"], 200)

    def test_final_trade_includes_assignment_cover_and_long_sale_once(self):
        self.reconcile()
        self.cover()
        self.reconcile()
        sale = order("long-sale", 2, "2", phase="close")
        workflow.record_fill(self.tid, "close", "single_long_sell", sale, "confirmed_assignment_stock_cover_long_sale")
        workflow.record_fill(self.tid, "close", "single_long_sell", sale)
        trade = self.trade()
        # -200 entry +2000 assignment -2200 share cover +400 long sale = 0.
        self.assertEqual((trade["remaining_quantity"], trade["closed_quantity"], trade["realized_pnl_cents"]), (0, 2, 0))
        with workflow.db(read_only=True) as db:
            payload = workflow.trade_sheet_payload(db, self.tid)
            self.assertEqual(payload["Close Cash Flow"], 200)
            self.assertEqual(payload["Realized P&L"], 0)
            self.assertEqual(payload["P&L Status"], "PROVISIONAL_FEES_UNKNOWN")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fills WHERE phase='close'").fetchone()[0], 1)
            self.assertTrue(db.execute("SELECT 1 FROM sheet_outbox WHERE state='pending'").fetchone())

    def test_partial_long_sales_allocate_settlement_without_double_counting(self):
        self.reconcile(); self.cover()
        for oid in ("sale-a", "sale-b"):
            result = order(oid, 1, "2", phase="close")
            workflow.record_fill(self.tid, "close", "single_long_sell", result)
            workflow.record_fill(self.tid, "close", "single_long_sell", result)
        self.assertEqual(self.trade()["realized_pnl_cents"], 0)
        with workflow.db(read_only=True) as db:
            self.assertEqual(db.execute("SELECT SUM(settlement_cash_flow_cents) FROM fills").fetchone()[0], -20000)

    def test_outstanding_assignment_pauses_new_entries(self):
        self.reconcile()
        with patch.object(workflow, "get_todays_earnings") as earnings:
            workflow.open_new_positions(self.client, Mock(), 10000)
        earnings.assert_not_called()

    def test_activity_pagination_reads_past_a_full_page_and_rejects_a_stuck_cursor(self):
        page = [{"id": f"old-{i}", "activity_type": "OPEXP"} for i in range(100)]
        self.client.get.side_effect = [page, activities()]
        result = evidence.fetch_activities(self.client, "2026-10-26", FINGERPRINT)
        self.assertEqual(len(result), 102)
        self.assertEqual(self.client.get.call_args.kwargs["data"]["page_token"], "old-99")
        self.client.get.side_effect = [page, page]
        with self.assertRaises(evidence.AssignmentEvidenceError):
            evidence.fetch_activities(self.client, "2026-10-26", FINGERPRINT)

    def test_reconciliation_recovers_a_share_fill_after_a_callback_was_interrupted(self):
        self.reconcile()
        result = order("interrupted-cover", 200, "11", phase="assignment_cover")
        workflow.upsert_submitted_order(self.tid,"assignment_cover","assignment_stock_buy",result,
                                       {"quantity": 200, "submission_only": True})
        self.orders.append(result); self.shares = 0
        self.assertEqual(self.reconcile()["resolutions"][self.tid], "ASSIGNED_STOCK_COVERED_LONG_REMAINS")
        with workflow.db(read_only=True) as db:
            totals = workflow.assignment_cover_totals(db,self.tid)
            self.assertEqual((totals["quantity"],totals["cashflow"]),(200,-220000))

    def test_partial_cover_is_deferred_without_selling_calls(self):
        self.reconcile()
        partial = SimpleNamespace(filled_qty=50, remaining_qty=150, attempts=())
        with patch.object(workflow,"get_open_trades",return_value=[self.trade()]), \
             patch.object(workflow,"is_time_to_close",return_value=True), \
             patch.object(workflow,"account_snapshot"), \
             patch.object(workflow.time_module,"monotonic",return_value=0), \
             patch.object(workflow,"close_assigned_stock_order",return_value=partial) as cover, \
             patch.object(workflow,"close_single_option_leg_order") as sell:
            workflow.close_due_trades(self.client,{"resolutions":{self.tid:"ASSIGNED_SHORT_STOCK"}},10000)
        self.assertEqual(cover.call_args.args[2],200)
        sell.assert_not_called()

    def test_an_unrelated_stock_position_still_blocks_the_entire_run(self):
        original = self.client.get_all_positions.side_effect
        self.client.get_all_positions.side_effect = lambda: original()+[
            SimpleNamespace(symbol="OTHER", asset_class="us_equity", qty="1")]
        with self.assertRaises(workflow.ReconciliationFailure): self.reconcile()
        self.client.submit_order.assert_not_called()

    def test_share_cover_limit_order_is_paper_only_and_keeps_existing_lifecycle_guards(self):
        quote = SimpleNamespace(bid_price=10.99, ask_price=11, bid_size=100, ask_size=100,
                                timestamp=datetime.now(timezone.utc))
        callback = Mock()
        broker_order = order("stock-order", 200, "11", phase="assignment_cover")
        result = SimpleNamespace(filled_qty=200, average_fill_price=Decimal("11"), stop_reason="fully_filled")
        with patch("alpaca.data.historical.StockHistoricalDataClient") as data, \
             patch.object(adapter, "_submit_order_idempotently", return_value=broker_order) as submit, \
             patch.object(adapter, "_notify_submitted_order"), \
             patch.object(adapter, "wait_for_fill", return_value=result), \
             patch.object(adapter, "_execution_result", return_value="complete"):
            data.return_value.get_stock_latest_quote.return_value = {"XYZ": quote}
            self.assertEqual(adapter.close_assigned_stock_order(self.client, "XYZ", 200,
                client_order_id_prefix="eta-a-test", before_submit=callback,
                on_submitted=Mock(), on_terminal=Mock()), "complete")
            request = submit.call_args.args[1]
            self.assertEqual((int(request.qty), request.limit_price, request.position_intent.value), (200, 11, "buy_to_close"))
            callback.assert_called_once()
            self.client._base_url = "https://api.alpaca.markets"
            with self.assertRaises(adapter.PaperModeRequiredError):
                adapter.close_assigned_stock_order(self.client, "XYZ", 200, client_order_id_prefix="test",
                    before_submit=callback, on_submitted=Mock(), on_terminal=Mock())


if __name__ == "__main__":
    unittest.main()
