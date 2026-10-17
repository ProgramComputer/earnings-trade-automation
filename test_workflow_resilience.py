import io
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
from alpaca.trading.enums import PositionIntent

import alpaca_integration
import automation
import trade_workflow

EASTERN = trade_workflow.EASTERN
FILLED = SimpleNamespace(filled_qty=5, remaining_qty=0, stop_reason="requested_quantity_filled", attempts=())


def candidate_patches(stack, **overrides):
    """Patch everything open_candidate touches; the quotes give net 0.20 x 0.50."""
    chain = {
        "2026-10-02": {10.0: {"call": SimpleNamespace(symbol="SHORT")}},
        "2026-11-06": {10.0: {"call": SimpleNamespace(symbol="LONG")}},
    }
    mocks = {
        "is_time_to_open": Mock(return_value=True),
        "compute_recommendation": Mock(return_value={
            "avg_volume": True, "iv30_rv30": True, "ts_slope_0_45": True, "expected_move": "5%",
        }),
        "select_expiries_and_strike_alpaca": Mock(return_value=("2026-10-02", "2026-11-06", 10.0)),
        "get_alpaca_option_chain": Mock(return_value=chain),
        "get_spread_quotes": Mock(return_value=("0.50", "0.60", "0.80", "1.00")),
        "candidate_allocation": Mock(return_value=(5, 500_000, 0, 1_000_000)),
        "stable_trade_id": Mock(side_effect=lambda ticker, *_args: f"trade-{ticker}"),
        "create_planned_trade": Mock(return_value="created"),
        "reserve_operation_prefix": Mock(return_value="prefix"),
        "place_calendar_spread_order": Mock(return_value=FILLED),
        "finalize_execution": Mock(),
        "mark_no_fill": Mock(),
    }
    mocks.update(overrides)
    for name, mock in mocks.items():
        stack.enter_context(patch.object(trade_workflow, name, mock))
    stack.enter_context(patch.object(trade_workflow.time_module, "monotonic", return_value=0))
    return mocks


def open_one(ticker="GOOD"):
    return trade_workflow.open_candidate(
        object(), {"act_symbol": ticker}, "AMC", date(2026, 9, 30),
        datetime(2026, 9, 30, 16, 0, tzinfo=EASTERN), 10_000,
    )


class CandidateIsolationTests(unittest.TestCase):
    def test_missing_option_chain_skips_only_that_ticker(self):
        with ExitStack() as stack:
            mocks = candidate_patches(stack)
            good_chain = mocks["get_alpaca_option_chain"].return_value
            mocks["get_alpaca_option_chain"].side_effect = (
                lambda ticker: None if ticker == "BAD" else good_chain
            )
            open_one("BAD")
            open_one("GOOD")
        mocks["place_calendar_spread_order"].assert_called_once()
        self.assertEqual(mocks["create_planned_trade"].call_args.args[0]["Ticker"], "GOOD")

    def test_invalid_quotes_skip_the_ticker_without_planning_a_trade(self):
        with ExitStack() as stack:
            mocks = candidate_patches(
                stack,
                get_spread_quotes=Mock(side_effect=alpaca_integration.QuoteValidationError("stale")),
            )
            open_one()
        mocks["create_planned_trade"].assert_not_called()
        mocks["place_calendar_spread_order"].assert_not_called()

    def test_quote_failure_during_an_unfilled_open_releases_the_plan(self):
        with ExitStack() as stack:
            mocks = candidate_patches(
                stack,
                place_calendar_spread_order=Mock(
                    side_effect=alpaca_integration.QuoteValidationError("stale")
                ),
            )
            release = stack.enter_context(
                patch.object(trade_workflow, "release_unfilled_open_plan", return_value=True)
            )
            open_one()
        release.assert_called_once_with("trade-GOOD", "open_quote_validation_failed")
        mocks["finalize_execution"].assert_not_called()

    def test_quote_failure_after_a_fill_still_stops_the_run(self):
        with ExitStack() as stack:
            candidate_patches(
                stack,
                place_calendar_spread_order=Mock(
                    side_effect=alpaca_integration.QuoteValidationError("stale")
                ),
            )
            stack.enter_context(
                patch.object(trade_workflow, "release_unfilled_open_plan", return_value=False)
            )
            with self.assertRaises(alpaca_integration.QuoteValidationError):
                open_one()


class OpeningPriceCeilingTests(unittest.TestCase):
    def ceiling_for(self, fraction):
        with ExitStack() as stack:
            mocks = candidate_patches(stack)
            stack.enter_context(
                patch.object(trade_workflow, "OPEN_MAX_DEBIT_SPREAD_FRACTION", Decimal(fraction))
            )
            open_one()
        kwargs = mocks["place_calendar_spread_order"].call_args.kwargs
        return kwargs["limit_price"], kwargs["target_debit_price"]

    def test_default_ceiling_allows_the_natural_price_after_starting_at_mid(self):
        self.assertEqual(self.ceiling_for("1"), (Decimal("0.35"), Decimal("0.50")))

    def test_partial_fraction_stops_between_mid_and_ask(self):
        self.assertEqual(self.ceiling_for("0.5"), (Decimal("0.35"), Decimal("0.43")))

    def test_zero_fraction_keeps_the_fixed_slippage_ceiling(self):
        self.assertEqual(self.ceiling_for("0"), (Decimal("0.35"), Decimal("0.40")))


class ReleaseUnfilledOpenPlanTests(unittest.TestCase):
    def run_release(self, filled_quantity, active_order):
        original_path = trade_workflow.DB_PATH
        with tempfile.TemporaryDirectory() as directory:
            trade_workflow.DB_PATH = Path(directory) / "test.db"
            try:
                connection = sqlite3.connect(trade_workflow.DB_PATH)
                connection.execute(
                    "CREATE TABLE trades (trade_id TEXT PRIMARY KEY, filled_quantity INTEGER, "
                    "lifecycle_status TEXT, reconciliation_status TEXT, updated_at TEXT)"
                )
                connection.execute(
                    "CREATE TABLE broker_orders (trade_id TEXT, phase TEXT, terminal INTEGER)"
                )
                connection.execute(
                    "INSERT INTO trades VALUES('trade-1', ?, 'OPEN_ORDER_SUBMITTED', NULL, NULL)",
                    (filled_quantity,),
                )
                connection.execute(
                    "INSERT INTO broker_orders VALUES('trade-1', 'open', ?)", (0 if active_order else 1,)
                )
                connection.commit()
                connection.close()
                released = trade_workflow.release_unfilled_open_plan("trade-1", "open_quote_validation_failed")
                connection = sqlite3.connect(trade_workflow.DB_PATH)
                stored = connection.execute(
                    "SELECT lifecycle_status, reconciliation_status FROM trades"
                ).fetchone()
                connection.close()
            finally:
                trade_workflow.DB_PATH = original_path
        return released, stored

    def test_unfilled_plan_without_live_orders_becomes_retryable(self):
        self.assertEqual(
            self.run_release(0, active_order=False),
            (True, ("CANCELED_NO_FILL", "open_quote_validation_failed")),
        )

    def test_plan_with_a_live_order_is_left_alone(self):
        self.assertEqual(
            self.run_release(0, active_order=True),
            (False, ("OPEN_ORDER_SUBMITTED", None)),
        )

    def test_partially_filled_plan_is_left_alone(self):
        self.assertEqual(
            self.run_release(3, active_order=False),
            (False, ("OPEN_ORDER_SUBMITTED", None)),
        )


class CloseFailureOrderingTests(unittest.TestCase):
    def test_unresolved_close_is_reported_after_new_entries_run(self):
        client = Mock()
        client.get_clock.return_value = SimpleNamespace(is_open=True)
        calls = []
        with (
            patch.object(trade_workflow, "configured_mode"),
            patch.object(trade_workflow, "init_db"),
            patch.object(trade_workflow, "configured_broker_client", return_value=(client, "PAPER")),
            patch.object(trade_workflow, "bind_or_validate_broker_identity"),
            patch.object(trade_workflow, "reconcile_broker_state", return_value={"resolutions": {}}),
            patch.object(
                trade_workflow,
                "close_due_trades",
                side_effect=lambda *_args: calls.append("close") or ["trade=stuck CLOSE_QUOTE_VALIDATION_FAILED"],
            ),
            patch.object(
                trade_workflow,
                "open_new_positions",
                side_effect=lambda *_args: calls.append("open"),
            ),
        ):
            with self.assertRaisesRegex(trade_workflow.OperationalFailure, "1 due trade close"):
                trade_workflow.run_trade_workflow()
        self.assertEqual(calls, ["close", "open"])


def volume_history(symbols_and_volumes):
    index = pd.date_range("2026-07-01", periods=45, freq="B")
    columns = pd.MultiIndex.from_product([list(symbols_and_volumes), ["Volume"]])
    frame = pd.DataFrame(index=index, columns=columns, dtype=float)
    for symbol, volume in symbols_and_volumes.items():
        frame[(symbol, "Volume")] = volume
    return frame


def earnings_dates(*stamps):
    return pd.DataFrame(index=pd.DatetimeIndex([pd.Timestamp(s, tz="America/New_York") for s in stamps]))


class MissingTimingFallbackTests(unittest.TestCase):
    def fill(self, rows, dates_by_symbol, volumes):
        ticker = Mock(side_effect=lambda symbol: SimpleNamespace(
            get_earnings_dates=lambda limit: dates_by_symbol.get(symbol)
        ))
        with (
            patch.object(automation.yf, "download", return_value=volume_history(volumes)),
            patch.object(automation.yf, "Ticker", ticker),
        ):
            result = automation.fill_missing_timing(rows, date(2026, 10, 1))
        return result, ticker

    def test_liquid_blank_rows_get_yahoo_timing_and_illiquid_rows_are_not_looked_up(self):
        rows = [
            {"act_symbol": "EARLY", "when": None},
            {"act_symbol": "LATE", "when": None},
            {"act_symbol": "THIN", "when": None},
            {"act_symbol": "KNOWN", "when": "After market close"},
        ]
        result, ticker = self.fill(
            rows,
            {
                "EARLY": earnings_dates("2026-12-01 07:00", "2026-10-01 07:00"),
                "LATE": earnings_dates("2026-10-01 16:05"),
            },
            {"EARLY": 2_000_000, "LATE": 3_000_000, "THIN": 100_000},
        )
        self.assertEqual(
            [row["when"] for row in result],
            ["Before market open", "After market close", None, "After market close"],
        )
        self.assertEqual(sorted(call.args[0] for call in ticker.call_args_list), ["EARLY", "LATE"])

    def test_placeholder_and_midnight_times_stay_unknown(self):
        rows = [{"act_symbol": "EST", "when": None}, {"act_symbol": "MID", "when": None}]
        result, _ticker = self.fill(
            rows,
            {"EST": earnings_dates("2026-10-01 15:00"), "MID": earnings_dates("2026-10-01 00:00")},
            {"EST": 2_000_000, "MID": 2_000_000},
        )
        self.assertEqual([row["when"] for row in result], [None, None])

    def test_yahoo_failure_leaves_rows_unchanged(self):
        rows = [{"act_symbol": "EARLY", "when": None}]
        with patch.object(automation.yf, "download", side_effect=RuntimeError("rate limited")):
            result = automation.fill_missing_timing(rows, date(2026, 10, 1))
        self.assertEqual(result, [{"act_symbol": "EARLY", "when": None}])


def kelly_inputs(win_rate=None, avg_win=None, avg_loss=None, fraction="0.10"):
    stack = ExitStack()
    for name, value in (
        ("KELLY_WIN_RATE", win_rate),
        ("KELLY_AVG_WIN", avg_win),
        ("KELLY_AVG_LOSS", avg_loss),
        ("KELLY_FRACTION", fraction),
    ):
        stack.enter_context(patch.object(trade_workflow, name, value))
    return stack


class KellySizingTests(unittest.TestCase):
    def test_fixed_allocation_is_used_until_kelly_inputs_are_set(self):
        with kelly_inputs():
            allocation, basis = trade_workflow.position_allocation()
        self.assertEqual(allocation, trade_workflow.POSITION_ALLOCATION_PCT)
        self.assertIn("fixed", basis)

    def test_allocation_is_the_configured_fraction_of_full_kelly(self):
        with kelly_inputs("0.60", "0.40", "0.30"):
            allocation, basis = trade_workflow.position_allocation()
        self.assertEqual(allocation, Decimal("0.030"))
        self.assertIn("full Kelly 0.3000", basis)

    def test_negative_edge_allocates_nothing(self):
        # The 2025 sheet: 17% wins, +113% average win, -59% average loss.
        with kelly_inputs("0.17", "1.13", "0.59"):
            allocation, _basis = trade_workflow.position_allocation()
        self.assertEqual(allocation, 0)

    def test_partial_or_invalid_inputs_are_refused(self):
        for inputs, message in (
            (("0.60", None, "0.30"), "KELLY_AVG_WIN"),
            (("1.5", "0.40", "0.30"), "0 < KELLY_WIN_RATE < 1"),
            (("0.60", "abc", "0.30"), "decimal numbers"),
            (("0.60", "0.40", "NaN"), "0 < KELLY_WIN_RATE < 1"),
        ):
            with self.subTest(inputs=inputs), kelly_inputs(*inputs):
                with self.assertRaisesRegex(trade_workflow.OperationalFailure, message):
                    trade_workflow.position_allocation()

    def test_candidate_quantity_uses_the_kelly_allocation(self):
        with (
            kelly_inputs("0.60", "0.40", "0.30"),
            patch.object(trade_workflow, "account_snapshot", return_value=(Decimal("100000"), Decimal("100000"))),
            patch.object(trade_workflow, "exposure_cents", return_value=0),
        ):
            quantity, allocation, _existing, _cap = trade_workflow.candidate_allocation(object(), Decimal("0.50"))
        self.assertEqual(allocation, 300_000)
        self.assertEqual(quantity, 60)

    def test_no_edge_skips_openings_before_fetching_earnings(self):
        class Afternoon(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 9, 30, 13, 10, tzinfo=EASTERN)

        # Broker times are built from the patched class the workflow type-checks against.
        clock = SimpleNamespace(
            next_close=Afternoon(2026, 9, 30, 16, 0, tzinfo=EASTERN),
            next_open=Afternoon(2026, 10, 1, 9, 30, tzinfo=EASTERN),
        )
        with (
            kelly_inputs("0.17", "1.13", "0.59"),
            patch.object(trade_workflow, "datetime", Afternoon),
            patch.object(trade_workflow, "get_todays_earnings") as todays,
        ):
            trade_workflow.open_new_positions(object(), clock, 10_000)
        todays.assert_not_called()


class QuoteAgeTests(unittest.TestCase):
    def test_default_quote_age_accepts_a_quiet_thirty_five_second_quote(self):
        quote = SimpleNamespace(
            bid_price="0.03",
            ask_price="0.05",
            bid_size=10,
            ask_size=10,
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=35),
        )
        self.assertEqual(
            alpaca_integration._validated_quote(
                quote, "NIO261002C00004500", alpaca_integration.DEFAULT_QUOTE_MAX_AGE_SECONDS
            ),
            (Decimal("0.03"), Decimal("0.05")),
        )


# A quiet market: opening net 0.65 x 0.80, closing signed -0.80 x -0.65.
CHASE_QUOTES = alpaca_integration.SpreadQuoteSnapshot(
    Decimal("0.10"), Decimal("0.15"), Decimal("0.80"), Decimal("0.90")
)


def canceled_attempt(order_id, quantity):
    return alpaca_integration.OrderLifecycleResult(
        order_id=order_id, symbol="SHORT", ordered_qty=quantity, filled_qty=0,
        canceled_qty=quantity, remaining_qty=quantity, status="canceled", terminal=True,
        timed_out=True, cancel_confirmed=True, average_fill_price=None,
        commission_amount=None, stop_reason="deadline_cancel_confirmed", raw_order=None,
    )


def chase_patches(stack, client, quotes=CHASE_QUOTES):
    """Patch the broker edges of every adapter; each order times out unfilled."""
    submitted = []

    def submit(_client, _request, client_order_id):
        submitted.append(client_order_id)
        return SimpleNamespace(id=f"order-{len(submitted)}")

    for name, value in (
        ("init_alpaca_client", Mock(return_value=client)),
        ("_fetch_spread_quote_snapshot", Mock(return_value=quotes)),
        ("_fetch_single_quote_snapshot", Mock(return_value=alpaca_integration.SingleQuoteSnapshot(Decimal("0.40"), Decimal("0.50")))),
        ("_submit_order_idempotently", Mock(side_effect=submit)),
        ("_notify_submitted_order", Mock()),
        ("_notify_terminal_attempt", Mock()),
        ("wait_for_fill", Mock(side_effect=lambda _client, order_id, **kwargs: canceled_attempt(order_id, kwargs["expected_qty"]))),
    ):
        stack.enter_context(patch.object(alpaca_integration, name, value))
    return submitted


def paper_client():
    client = Mock()
    client._eta_paper_mode = True
    return client


def refuse_on_attempt(number, error):
    def guard(_client, context):
        if context["attempt_number"] == number:
            raise error
    return guard


ADAPTERS = {
    "open spread": lambda guard: alpaca_integration.place_calendar_spread_order(
        "SHORT", "LONG", 3, limit_price=Decimal("0.70"), target_debit_price=Decimal("0.80"),
        before_submit=guard, client_order_id_prefix="test",
    ),
    "close spread": lambda guard: alpaca_integration.close_calendar_spread_order(
        "SHORT", "LONG", 3, max_close_debit=Decimal("0.50"), before_submit=guard,
        client_order_id_prefix="test",
    ),
    "close single leg": lambda guard: alpaca_integration.close_single_option_leg_order(
        "LONG", 3, PositionIntent.SELL_TO_CLOSE, before_submit=guard, client_order_id_prefix="test",
    ),
}


class TimingRefusalTests(unittest.TestCase):
    """Running out of order time or meeting the close ends a chase, not the run."""

    def test_each_adapter_stops_cleanly_when_only_timing_refuses_the_next_order(self):
        refusal = trade_workflow.SubmissionWindowClosed("no time left", "order_time_budget_reached")
        for name, run in ADAPTERS.items():
            with self.subTest(adapter=name), ExitStack() as stack:
                submitted = chase_patches(stack, paper_client())
                result = run(refuse_on_attempt(2, refusal))
            self.assertEqual(result.stop_reason, "order_time_budget_reached")
            self.assertEqual(len(submitted), 1)
            self.assertEqual(len(result.attempts), 1)
            self.assertEqual((result.filled_qty, result.remaining_qty), (0, 3))

    def test_a_refusal_before_the_first_order_submits_nothing(self):
        refusal = trade_workflow.SubmissionWindowClosed("closed", "market_closed")
        with ExitStack() as stack:
            submitted = chase_patches(stack, paper_client())
            result = ADAPTERS["open spread"](refuse_on_attempt(1, refusal))
        self.assertEqual(submitted, [])
        self.assertEqual(result.attempts, ())
        self.assertEqual(result.stop_reason, "market_closed")

    def test_other_guard_refusals_still_fail_the_chase(self):
        for error in (
            trade_workflow.OperationalFailure("Insufficient configured-account buying power"),
            trade_workflow.ReconciliationFailure("Broker reconciliation found 1 unresolved discrepancy(ies)"),
        ):
            for name, run in ADAPTERS.items():
                with self.subTest(adapter=name, error=type(error).__name__), ExitStack() as stack:
                    submitted = chase_patches(stack, paper_client())
                    with self.assertRaises(type(error)):
                        run(refuse_on_attempt(2, error))
                self.assertEqual(len(submitted), 1)

    def guard_with_clock(self, clock, deadline=10_000, monotonic=(0, 0), **guard_kwargs):
        client = Mock()
        client.get_clock.return_value = clock
        with (
            patch.object(trade_workflow, "reconcile_broker_state"),
            patch.object(trade_workflow, "account_snapshot", return_value=(Decimal("100000"), Decimal("100000"))),
            patch.object(trade_workflow, "exposure_cents", return_value=0),
            # The guard reads the clock before and after its broker calls.
            patch.object(trade_workflow.time_module, "monotonic", side_effect=list(monotonic)),
        ):
            trade_workflow.before_submit_guard(deadline=deadline, **guard_kwargs)(
                client, {"limit_price": Decimal("0.70"), "quantity": 3}
            )

    def open_clock(self, minutes_to_close=120, clock_age_minutes=0):
        now = datetime.now(timezone.utc)
        return SimpleNamespace(
            is_open=True, timestamp=now - timedelta(minutes=clock_age_minutes),
            next_close=now + timedelta(minutes=minutes_to_close),
        )

    def test_the_guard_marks_only_timing_refusals_as_timing(self):
        for label, clock, deadline, monotonic, reason in (
            ("time budget", self.open_clock(), 59, (0, 0), "order_time_budget_reached"),
            ("time budget after broker calls", self.open_clock(), 180, (119, 121), "order_time_budget_reached"),
            ("market closed", SimpleNamespace(is_open=False), 10_000, (0, 0), "market_closed"),
            ("pre-close cutoff", self.open_clock(minutes_to_close=2), 10_000, (0, 0), "close_cutoff_reached"),
            # The broker's clock was read 5 minutes ago; the local re-check catches the cutoff.
            ("pre-close cutoff after broker calls", self.open_clock(minutes_to_close=2, clock_age_minutes=5), 10_000, (0, 0), "close_cutoff_reached"),
        ):
            with self.subTest(label):
                with self.assertRaises(trade_workflow.SubmissionWindowClosed) as caught:
                    self.guard_with_clock(clock, deadline, monotonic)
                self.assertEqual(caught.exception.stop_reason, reason)
                self.assertIn(reason, trade_workflow.TIMING_STOP_REASONS)
                # Outside an adapter it still fails closed.
                self.assertIsInstance(caught.exception, trade_workflow.OperationalFailure)

    def test_an_unclear_clock_or_a_risk_limit_is_not_a_timing_refusal(self):
        cases = (
            ("clock without is_open", SimpleNamespace(), {}),
            ("buying power", self.open_clock(), {"opening": True, "allocation_cents": 10}),
        )
        for label, clock, kwargs in cases:
            with self.subTest(label):
                with self.assertRaises(trade_workflow.OperationalFailure) as caught:
                    self.guard_with_clock(clock, **kwargs)
                self.assertNotIsInstance(caught.exception, alpaca_integration.SubmissionTimingRefusal)

    def test_an_open_that_meets_the_close_mid_chase_ends_without_failing_the_run(self):
        now = datetime.now(timezone.utc)
        client = paper_client()
        client.get_clock.side_effect = [
            SimpleNamespace(is_open=True, timestamp=now, next_close=now + timedelta(hours=2)),
            SimpleNamespace(is_open=False),
        ]
        with ExitStack() as stack:
            mocks = candidate_patches(stack, place_calendar_spread_order=alpaca_integration.place_calendar_spread_order)
            # The same 0.20 x 0.50 market the candidate was planned against.
            quotes = alpaca_integration.SpreadQuoteSnapshot(Decimal("0.50"), Decimal("0.60"), Decimal("0.80"), Decimal("1.00"))
            submitted = chase_patches(stack, client, quotes)
            for name, value in (
                ("reconcile_broker_state", Mock()),
                ("account_snapshot", Mock(return_value=(Decimal("100000"), Decimal("100000")))),
                ("exposure_cents", Mock(return_value=0)),
            ):
                stack.enter_context(patch.object(trade_workflow, name, value))
            open_one()
        self.assertEqual(len(submitted), 1)
        result = mocks["finalize_execution"].call_args.args[1]
        self.assertEqual(result.stop_reason, "market_closed")
        mocks["mark_no_fill"].assert_not_called()

    def test_only_a_price_stop_makes_an_unfilled_open_retryable(self):
        # A retry re-plans the trade, and orders already sent would then fail
        # reconciliation against the new plan, so timing stops are not retried.
        for reason, retryable in (
            ("order_time_budget_reached", False),
            ("market_closed", False),
            ("close_cutoff_reached", False),
            ("hard_price_limit_reached", True),
        ):
            unfilled = SimpleNamespace(filled_qty=0, remaining_qty=5, stop_reason=reason, attempts=())
            with self.subTest(reason=reason), ExitStack() as stack:
                mocks = candidate_patches(stack, place_calendar_spread_order=Mock(return_value=unfilled))
                open_one()
            self.assertEqual(mocks["mark_no_fill"].called, retryable)

    def test_a_due_close_stopped_by_timing_is_deferred_with_a_warning(self):
        trade = {
            "trade_id": "trade-due", "Open Date": "2026-10-15", "When": "AMC",
            "earnings_date": "2026-10-15", "remaining_quantity": 3,
            "Short Symbol": "SHORT", "Long Symbol": "LONG",
        }
        now = datetime.now(timezone.utc)
        client = paper_client()
        client.get_clock.side_effect = [
            SimpleNamespace(is_open=True, timestamp=now, next_close=now + timedelta(hours=2)),
            SimpleNamespace(is_open=True, timestamp=now, next_close=now + timedelta(minutes=2)),
        ]
        output = io.StringIO()
        with ExitStack() as stack:
            chase_patches(stack, client)
            for name, value in (
                ("get_open_trades", Mock(return_value=[trade])),
                ("is_time_to_close", Mock(return_value=True)),
                ("account_snapshot", Mock(return_value=(Decimal("100000"), Decimal("100000")))),
                ("reserve_operation_prefix", Mock(return_value="prefix")),
                ("reconcile_broker_state", Mock()),
                ("finalize_execution", Mock()),
            ):
                stack.enter_context(patch.object(trade_workflow, name, value))
            stack.enter_context(patch.object(trade_workflow.time_module, "monotonic", return_value=0))
            stack.enter_context(redirect_stdout(output))
            failures = trade_workflow.close_due_trades(
                object(), {"resolutions": {"trade-due": "MATCHED_SPREAD"}}, 10_000
            )
            finalize = trade_workflow.finalize_execution
        self.assertEqual(failures, [])
        self.assertEqual(finalize.call_args.args[1].stop_reason, "close_cutoff_reached")
        self.assertIn("::warning title=Due close deferred::trade=trade-due remaining=3", output.getvalue())


class RunTimeBudgetTests(unittest.TestCase):
    """The 35-minute run budget and the pre-close cutoff defer work instead of failing."""

    DUE = {"trade_id": "trade-due", "Open Date": "2026-10-15", "When": "AMC", "earnings_date": "2026-10-15"}

    def close_due(self, *, monotonic=0, market_close=None):
        output = io.StringIO()
        with (
            patch.object(trade_workflow, "get_open_trades", return_value=[self.DUE]),
            patch.object(trade_workflow, "is_time_to_close", return_value=True),
            patch.object(trade_workflow.time_module, "monotonic", return_value=monotonic),
            patch.object(trade_workflow, "account_snapshot") as snapshot,
            patch.object(trade_workflow, "close_calendar_spread_order") as close_order,
            redirect_stdout(output),
        ):
            failures = trade_workflow.close_due_trades(object(), {"resolutions": {}}, 10_000, market_close)
        return failures, close_order, snapshot, output.getvalue()

    def test_due_closes_past_the_run_budget_are_deferred_not_failed(self):
        failures, close_order, snapshot, output = self.close_due(monotonic=9_950)
        self.assertEqual(failures, [])
        close_order.assert_not_called()
        snapshot.assert_not_called()
        self.assertIn("::warning title=Due close deferred::Run time ran out", output)

    def test_due_closes_after_the_pre_close_cutoff_are_deferred_not_failed(self):
        closing = datetime.now(EASTERN) + timedelta(minutes=2)
        failures, close_order, _snapshot, output = self.close_due(market_close=closing)
        self.assertEqual(failures, [])
        close_order.assert_not_called()
        self.assertIn("::warning title=Due close deferred::The pre-close cutoff", output)

    def opening_run(self, open_candidate, monotonic=0):
        class Afternoon(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 14, 14, 10, tzinfo=EASTERN)

        clock = SimpleNamespace(
            next_close=Afternoon(2026, 10, 14, 16, 0, tzinfo=EASTERN),
            next_open=Afternoon(2026, 10, 15, 9, 30, tzinfo=EASTERN),
        )
        record = lambda symbol: {"act_symbol": symbol, "when": "before market open"}
        with (
            kelly_inputs(),
            patch.object(trade_workflow, "datetime", Afternoon),
            patch.object(trade_workflow.time_module, "monotonic", return_value=monotonic),
            patch.object(trade_workflow, "get_todays_earnings", return_value=[]) as todays,
            patch.object(trade_workflow, "get_tomorrows_earnings", return_value=[record("BNY"), record("CLF")]),
            patch.object(trade_workflow, "open_candidate", open_candidate),
        ):
            trade_workflow.open_new_positions(object(), clock, 10_000)
        return todays

    def test_running_out_of_run_time_skips_the_remaining_candidates(self):
        open_candidate = Mock(side_effect=trade_workflow.RunTimeBudgetReached("Run time budget reached before candidate processing"))
        self.opening_run(open_candidate)
        open_candidate.assert_called_once()

    def test_no_earnings_are_fetched_once_the_run_time_is_spent(self):
        open_candidate = Mock()
        todays = self.opening_run(open_candidate, monotonic=9_950)
        todays.assert_not_called()
        open_candidate.assert_not_called()

    def test_open_candidate_raises_the_budget_stop_rather_than_a_run_failure(self):
        with ExitStack() as stack:
            candidate_patches(stack)
            stack.enter_context(patch.object(trade_workflow.time_module, "monotonic", return_value=9_950))
            with self.assertRaises(trade_workflow.RunTimeBudgetReached):
                open_one()


if __name__ == "__main__":
    unittest.main()
