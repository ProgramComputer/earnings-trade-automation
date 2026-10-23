import io
import os
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
        datetime(2026, 9, 30, 16, 0, tzinfo=EASTERN), 10_000, exit_date=date(2026, 10, 1),
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


class ShortExpiryTests(unittest.TestCase):
    """The short leg must expire after the session the position is due to close.

    On 2026-10-16 the short legs of CFG and RF (BMO) and of AA and IBKR (AMC)
    expired the day they were closed.
    """

    def entry_run(self, now, next_open, record, expiries, **overrides):
        """Run one afternoon's openings against a chain of expiries; return the order call."""
        class Session(datetime):
            @classmethod
            def now(cls, tz=None):
                return now.astimezone(tz) if tz else now

        chain = {expiry: {10.0: {"call": SimpleNamespace(symbol=f"XYZ {expiry}")}} for expiry in expiries}
        stock_client = Mock()
        stock_client.get_stock_latest_bar.return_value = {"XYZ": SimpleNamespace(close=10.02)}
        clock = SimpleNamespace(
            next_close=Session(now.year, now.month, now.day, 16, 0, tzinfo=EASTERN),
            next_open=Session(next_open.year, next_open.month, next_open.day, 9, 30, tzinfo=EASTERN),
        )
        bmo = record["when"].startswith("Before")
        output = io.StringIO()
        with ExitStack() as stack:
            mocks = candidate_patches(
                stack, get_alpaca_option_chain=Mock(return_value=chain),
                select_expiries_and_strike_alpaca=alpaca_integration.select_expiries_and_strike_alpaca,
                **overrides,
            )
            stack.enter_context(patch("alpaca.data.historical.StockHistoricalDataClient", Mock(return_value=stock_client)))
            for target, name, value in (
                (alpaca_integration, "get_alpaca_option_chain", Mock(return_value=chain)),
                (trade_workflow, "datetime", Session),
                (trade_workflow, "get_todays_earnings", Mock(return_value=[] if bmo else [record])),
                (trade_workflow, "get_tomorrows_earnings", Mock(return_value=[record] if bmo else [])),
            ):
                stack.enter_context(patch.object(target, name, value))
            stack.enter_context(kelly_inputs())
            stack.enter_context(redirect_stdout(output))
            trade_workflow.open_new_positions(object(), clock, 10_000)
        return mocks, output.getvalue()

    def legs(self, mocks):
        mocks["place_calendar_spread_order"].assert_called_once()
        return tuple(mocks["place_calendar_spread_order"].call_args.args[:2])

    # Thursday 2026-10-15 at 2:10 pm; the next session is Friday 2026-10-16.
    THURSDAY = datetime(2026, 10, 15, 14, 10, tzinfo=EASTERN)
    FRIDAY = date(2026, 10, 16)
    WEEKLIES = ("2026-10-16", "2026-10-23", "2026-11-13", "2026-11-20", "2026-11-27")

    def test_a_bmo_short_leg_expires_after_the_earnings_day_exit(self):
        mocks, _ = self.entry_run(
            self.THURSDAY, self.FRIDAY, {"act_symbol": "XYZ", "when": "Before market open"}, self.WEEKLIES,
        )
        self.assertEqual(mocks["is_time_to_open"].call_args.args[:2], (self.FRIDAY, "BMO"))
        # The long leg stays about 30 days after the new short expiry.
        self.assertEqual(self.legs(mocks), ("XYZ 2026-10-23", "XYZ 2026-11-20"))

    def test_an_amc_short_leg_expires_after_the_next_session_exit(self):
        mocks, _ = self.entry_run(
            self.THURSDAY, self.FRIDAY, {"act_symbol": "XYZ", "when": "After market close"}, self.WEEKLIES,
        )
        self.assertEqual(mocks["is_time_to_open"].call_args.args[:2], (date(2026, 10, 15), "AMC"))
        self.assertEqual(self.legs(mocks), ("XYZ 2026-10-23", "XYZ 2026-11-20"))

    def test_a_friday_amc_short_leg_expires_after_the_monday_exit(self):
        mocks, _ = self.entry_run(
            datetime(2026, 10, 16, 14, 10, tzinfo=EASTERN), date(2026, 10, 19),
            {"act_symbol": "XYZ", "when": "After market close"},
            ("2026-10-16", "2026-10-19", "2026-10-23", "2026-11-20", "2026-11-27"),
        )
        self.assertEqual(self.legs(mocks), ("XYZ 2026-10-23", "XYZ 2026-11-20"))

    def test_no_expiry_after_the_exit_skips_the_candidate(self):
        mocks, output = self.entry_run(
            self.THURSDAY, self.FRIDAY, {"act_symbol": "XYZ", "when": "Before market open"}, ("2026-10-16",),
            select_yahoo=Mock(return_value=(None, None, None)),
        )
        mocks["create_planned_trade"].assert_not_called()
        mocks["place_calendar_spread_order"].assert_not_called()
        self.assertIn("Skipping XYZ: no short expiry after the 2026-10-16 exit session", output)

    def test_the_yahoo_fallback_also_starts_after_the_exit(self):
        yahoo = Mock(return_value=("2026-10-23", "2026-11-20", 10.0))
        chain = {
            "2026-10-23": {10.0: {"call": SimpleNamespace(symbol="SHORT")}},
            "2026-11-20": {10.0: {"call": SimpleNamespace(symbol="LONG")}},
        }
        with ExitStack() as stack:
            mocks = candidate_patches(
                stack, select_expiries_and_strike_alpaca=Mock(return_value=(None, None, None)),
                select_yahoo=yahoo, get_alpaca_option_chain=Mock(return_value=chain),
            )
            stack.enter_context(patch.object(trade_workflow.yf, "Ticker"))
            trade_workflow.open_candidate(
                object(), {"act_symbol": "XYZ"}, "BMO", self.FRIDAY,
                datetime(2026, 10, 15, 16, 0, tzinfo=EASTERN), 10_000, exit_date=self.FRIDAY,
            )
        self.assertEqual(yahoo.call_args.args[1], self.FRIDAY)
        mocks["place_calendar_spread_order"].assert_called_once()

    def test_a_short_expiring_on_the_exit_day_is_never_sold(self):
        output = io.StringIO()
        with ExitStack() as stack:
            mocks = candidate_patches(
                stack, select_expiries_and_strike_alpaca=Mock(return_value=("2026-10-16", "2026-11-13", 10.0)),
            )
            stack.enter_context(redirect_stdout(output))
            trade_workflow.open_candidate(
                object(), {"act_symbol": "XYZ"}, "BMO", self.FRIDAY,
                datetime(2026, 10, 15, 16, 0, tzinfo=EASTERN), 10_000, exit_date=self.FRIDAY,
            )
        mocks["place_calendar_spread_order"].assert_not_called()
        self.assertIn("short expiry 2026-10-16 is not after the 2026-10-16 exit session", output.getvalue())


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


def snapshot(short_bid, short_ask, long_bid, long_ask):
    return alpaca_integration.SpreadQuoteSnapshot(*(Decimal(value) for value in (short_bid, short_ask, long_bid, long_ask)))


class EntryPricingTests(unittest.TestCase):
    """Entries skip wide markets, step up in small increments, and may follow a rising ask a little."""

    # The candidate is planned against a net 0.20 x 0.50 market: midpoint 0.35, ceiling 0.50.
    PLANNED = snapshot("0.50", "0.60", "0.80", "1.00")
    # The market then rises to net 0.30 x 0.70.
    RISEN = snapshot("0.50", "0.60", "0.90", "1.20")

    def chase(self, quotes, *, quantity=5, allocation=500_000, clock=None, **settings):
        """Run one entry's real chase, where no order fills; return what was sent."""
        now = datetime.now(timezone.utc)
        client = paper_client()
        client.get_clock.return_value = SimpleNamespace(is_open=True, timestamp=now, next_close=now + timedelta(hours=2))
        sent = []

        def submit(_client, request, client_order_id):
            sent.append((Decimal(str(request.limit_price)), int(request.qty)))
            return SimpleNamespace(id=f"order-{len(sent)}")

        place = Mock(side_effect=alpaca_integration.place_calendar_spread_order)
        output = io.StringIO()
        with ExitStack() as stack:
            mocks = candidate_patches(
                stack, place_calendar_spread_order=place,
                candidate_allocation=Mock(return_value=(quantity, allocation, 0, 10_000_000)),
            )
            chase_patches(stack, client)
            fetch = Mock(side_effect=list(quotes)) if isinstance(quotes, list) else Mock(return_value=quotes)
            stack.enter_context(patch.object(alpaca_integration, "_fetch_spread_quote_snapshot", fetch))
            stack.enter_context(patch.object(alpaca_integration, "_submit_order_idempotently", Mock(side_effect=submit)))
            for name, value in (
                ("reconcile_broker_state", Mock()),
                ("account_snapshot", Mock(return_value=(Decimal("100000"), Decimal("100000")))),
                ("exposure_cents", Mock(return_value=0)),
                *settings.items(),
            ):
                stack.enter_context(patch.object(trade_workflow, name, value))
            if clock is not None:
                clock(stack)
            stack.enter_context(redirect_stdout(output))
            open_one()
        result = mocks["finalize_execution"].call_args.args[1] if mocks["finalize_execution"].called else None
        return sent, result, place.call_args.kwargs if place.called else None, output.getvalue(), mocks

    def test_an_entry_steps_a_tenth_of_the_market_width_at_a_time(self):
        sent, result, _, _, _ = self.chase(self.PLANNED)
        self.assertEqual(
            [limit for limit, _ in sent],
            [Decimal(price) for price in ("0.35", "0.38", "0.41", "0.44", "0.47", "0.50")],
        )
        self.assertEqual(result.stop_reason, "hard_price_limit_reached")

    def test_the_ceiling_follows_a_rising_ask_up_to_its_margin(self):
        sent, result, kwargs, _, _ = self.chase(
            [self.PLANNED] + [self.RISEN] * 20, OPEN_CEILING_FOLLOW_PCT=Decimal("0.10"),
        )
        self.assertEqual(kwargs["target_debit_price"], Decimal("0.50"))
        self.assertEqual(kwargs["max_debit_price"], Decimal("0.55"))
        self.assertEqual(
            [limit for limit, _ in sent],
            [Decimal(price) for price in ("0.35", "0.39", "0.43", "0.47", "0.51", "0.55")],
        )
        self.assertEqual(result.stop_reason, "hard_price_limit_reached")

    def test_a_quiet_market_is_never_paid_above_its_planned_ceiling(self):
        for fraction, ceiling in (("1", "0.50"), ("0.5", "0.43")):
            with self.subTest(fraction=fraction):
                sent, _, _, _, _ = self.chase(
                    self.PLANNED, OPEN_CEILING_FOLLOW_PCT=Decimal("0.25"),
                    OPEN_MAX_DEBIT_SPREAD_FRACTION=Decimal(fraction),
                )
                self.assertEqual(max(limit for limit, _ in sent), Decimal(ceiling))

    def test_a_higher_price_buys_fewer_contracts_within_the_position_budget(self):
        # $250 buys 5 contracts at 0.50 but only 4 at 0.51 or 0.55.
        sent, _, _, _, _ = self.chase(
            [self.PLANNED] + [self.RISEN] * 20, allocation=25_000, OPEN_CEILING_FOLLOW_PCT=Decimal("0.10"),
        )
        self.assertEqual(sent[-1], (Decimal("0.55"), 4))
        for limit, quantity in sent:
            self.assertLessEqual(limit * quantity * 100, 250)

    def test_the_logged_attempt_count_is_what_fits_in_the_time(self):
        # Every price is new, so only time ends the chase. Each attempt waits
        # out its timeout plus the measured second of broker calls.
        now = {"seconds": 0.0}

        def wait(_client, order_id, **kwargs):
            now["seconds"] += kwargs["timeout"] + 1
            return canceled_attempt(order_id, kwargs["expected_qty"])

        def clock(stack):
            stack.enter_context(patch.object(alpaca_integration.time, "monotonic", side_effect=lambda: now["seconds"]))
            stack.enter_context(patch.object(alpaca_integration, "wait_for_fill", Mock(side_effect=wait)))
            stack.enter_context(patch.object(alpaca_integration, "_chase_step", Mock(return_value=Decimal("0.01"))))

        sent, result, kwargs, _, _ = self.chase(self.PLANNED, clock=clock)
        self.assertEqual(len(sent), kwargs["max_attempts"])
        self.assertIn(result.stop_reason, {"max_attempts_reached", "order_time_budget_reached"})

    def test_opening_orders_wait_briefly_and_keep_room_to_cancel(self):
        with ExitStack() as stack:
            mocks = candidate_patches(stack)
            open_one()
        kwargs = mocks["place_calendar_spread_order"].call_args.kwargs
        self.assertLessEqual(kwargs["attempt_timeout"], 10)
        headroom = kwargs["attempt_timeout"] + kwargs["cancel_timeout"]
        # open_one starts at 0 on the patched clock, so its orders must be admitted by 180 s.
        deadline = kwargs["overall_timeout"]
        for started, refused in ((deadline - headroom - 0.5, False), (deadline - headroom, True)):
            with self.subTest(started=started), ExitStack() as stack:
                client = Mock()
                now = datetime.now(timezone.utc)
                client.get_clock.return_value = SimpleNamespace(is_open=True, timestamp=now, next_close=now + timedelta(hours=2))
                for name, value in (
                    ("reconcile_broker_state", Mock()),
                    ("account_snapshot", Mock(return_value=(Decimal("100000"), Decimal("100000")))),
                    ("exposure_cents", Mock(return_value=0)),
                ):
                    stack.enter_context(patch.object(trade_workflow, name, value))
                stack.enter_context(patch.object(trade_workflow.time_module, "monotonic", return_value=started))
                guard = lambda: kwargs["before_submit"](client, {"limit_price": Decimal("0.50"), "quantity": 5})
                if refused:
                    with self.assertRaises(trade_workflow.SubmissionWindowClosed) as caught:
                        guard()
                    self.assertEqual(caught.exception.stop_reason, "order_time_budget_reached")
                else:
                    guard()

    def test_the_guard_headroom_is_the_wait_plus_the_cancel_timeout(self):
        timing = {"overall_timeout": 180, "attempt_timeout": 10, "cancel_timeout": 30}
        for started, refused in ((59.5, False), (60, True)):
            with self.subTest(started=started), ExitStack() as stack:
                client = Mock()
                now = datetime.now(timezone.utc)
                client.get_clock.return_value = SimpleNamespace(is_open=True, timestamp=now, next_close=now + timedelta(hours=2))
                for name, value in (
                    ("reconcile_broker_state", Mock()),
                    ("account_snapshot", Mock(return_value=(Decimal("100000"), Decimal("100000")))),
                    ("exposure_cents", Mock(return_value=0)),
                ):
                    stack.enter_context(patch.object(trade_workflow, name, value))
                stack.enter_context(patch.object(trade_workflow.time_module, "monotonic", return_value=started))
                guard = trade_workflow.before_submit_guard(deadline=100, order_timing=timing)
                if refused:
                    with self.assertRaises(trade_workflow.SubmissionWindowClosed):
                        guard(client, {"limit_price": Decimal("0.50"), "quantity": 5})
                else:
                    guard(client, {"limit_price": Decimal("0.50"), "quantity": 5})

    def test_a_deadline_without_its_order_timing_is_refused(self):
        with self.assertRaisesRegex(ValueError, "order timing"):
            trade_workflow.before_submit_guard(deadline=100)

    def test_a_market_wider_than_the_cap_is_skipped(self):
        # 0.20 x 0.50 is 0.30 wide around a 0.35 midpoint: 86%.
        sent, _, _, output, mocks = self.chase(self.PLANNED, OPEN_MAX_SPREAD_WIDTH_PCT=Decimal("0.75"))
        self.assertEqual(sent, [])
        mocks["create_planned_trade"].assert_not_called()
        self.assertIn("Skipping GOOD: net market is wider than OPEN_MAX_SPREAD_WIDTH_PCT=0.75", output)

    def test_a_market_within_the_cap_is_traded(self):
        sent, _, _, output, _ = self.chase(self.PLANNED, OPEN_MAX_SPREAD_WIDTH_PCT=Decimal("0.90"))
        self.assertTrue(sent)
        self.assertIn("GOOD net market 0.20 x 0.50 is 86% wide at its midpoint.", output)

    def test_no_width_cap_applies_unless_one_is_configured(self):
        if os.environ.get("OPEN_MAX_SPREAD_WIDTH_PCT"):
            self.skipTest("OPEN_MAX_SPREAD_WIDTH_PCT is set in this environment")
        self.assertIsNone(trade_workflow.OPEN_MAX_SPREAD_WIDTH_PCT)
        sent, _, _, _, _ = self.chase(snapshot("0.01", "1.00", "0.80", "1.00"), OPEN_MAX_SPREAD_WIDTH_PCT=None)
        self.assertTrue(sent)

    def test_invalid_entry_pricing_settings_are_refused_before_contacting_the_broker(self):
        for name, value in (
            ("OPEN_MAX_SPREAD_WIDTH_PCT", Decimal("0")),
            ("OPEN_MAX_SPREAD_WIDTH_PCT", Decimal("NaN")),
            ("OPEN_CEILING_FOLLOW_PCT", Decimal("0.5")),
            ("OPEN_CEILING_FOLLOW_PCT", Decimal("-0.01")),
        ):
            with (
                self.subTest(name=name, value=value),
                patch.object(trade_workflow, name, value),
                patch.object(trade_workflow, "configured_mode"),
                patch.object(trade_workflow, "init_db"),
                patch.object(trade_workflow, "configured_broker_client") as broker,
            ):
                with self.assertRaisesRegex(trade_workflow.OperationalFailure, name):
                    trade_workflow.run_trade_workflow()
            broker.assert_not_called()


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
            # The cases below assume 60 s of headroom: a 30 s wait and a 30 s cancel.
            trade_workflow.before_submit_guard(
                deadline=deadline, order_timing={"attempt_timeout": 30, "cancel_timeout": 30}, **guard_kwargs
            )(client, {"limit_price": Decimal("0.70"), "quantity": 3})

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


class OverdueExitTests(unittest.TestCase):
    """A due position the run leaves open is a warning and a summary count, never a failure."""

    def due(self, ticker):
        return {
            "trade_id": f"trade-{ticker}", "Ticker": ticker, "Open Date": "2026-10-15", "When": "AMC",
            "earnings_date": "2026-10-15", "remaining_quantity": 3, "Short Symbol": "SHORT", "Long Symbol": "LONG",
        }

    def close_due(self, trades, results, monotonic=0):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        summary = Path(directory.name) / "summary.md"
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}),
            patch.object(trade_workflow, "get_open_trades", return_value=trades),
            patch.object(trade_workflow, "is_time_to_close", return_value=True),
            patch.object(trade_workflow.time_module, "monotonic", return_value=monotonic),
            patch.object(trade_workflow, "account_snapshot"),
            patch.object(trade_workflow, "reserve_operation_prefix", return_value="prefix"),
            patch.object(trade_workflow, "finalize_execution"),
            patch.object(trade_workflow, "close_calendar_spread_order", side_effect=results) as close_order,
            redirect_stdout(output),
        ):
            failures = trade_workflow.close_due_trades(
                object(), {"resolutions": {trade["trade_id"]: "MATCHED_SPREAD" for trade in trades}}, 10_000,
            )
        self.assertEqual(failures, [])
        return close_order, output.getvalue(), summary.read_text(encoding="utf-8") if summary.exists() else ""

    def unfilled(self, reason):
        return SimpleNamespace(filled_qty=0, remaining_qty=3, stop_reason=reason, attempts=())

    def test_a_due_close_stopped_by_its_price_limit_is_warned_about(self):
        # RF on 2026-10-16: the 4th close attempt ended unfilled at its limit.
        for reason in ("hard_price_limit_reached", "max_attempts_reached", "overall_deadline_reached"):
            with self.subTest(reason=reason):
                _, output, summary = self.close_due([self.due("RF")], [self.unfilled(reason)])
                self.assertIn(
                    f"::warning title=Due close deferred::trade=trade-RF remaining=3 stop_reason={reason}", output
                )
                self.assertIn("- Overdue exits: 1 (RF trade-RF)", summary)

    def test_the_summary_counts_every_due_position_left_open(self):
        filled = SimpleNamespace(filled_qty=3, remaining_qty=0, stop_reason="requested_quantity_filled", attempts=())
        _, output, summary = self.close_due(
            [self.due("CFG"), self.due("RF"), self.due("AA")],
            [filled, self.unfilled("hard_price_limit_reached"), filled],
        )
        self.assertEqual(output.count("::warning title=Due close deferred::"), 1)
        self.assertIn("### PAPER position exits", summary)
        self.assertIn("- Overdue exits: 1 (RF trade-RF)", summary)

    def test_closes_the_run_had_no_time_for_are_counted_as_overdue(self):
        close_order, _, summary = self.close_due([self.due("AA"), self.due("IBKR")], [], monotonic=9_950)
        close_order.assert_not_called()
        self.assertIn("- Overdue exits: 2 (AA trade-AA, IBKR trade-IBKR)", summary)

    def test_a_run_that_closes_every_due_position_reports_none_overdue(self):
        filled = SimpleNamespace(filled_qty=3, remaining_qty=0, stop_reason="requested_quantity_filled", attempts=())
        _, output, summary = self.close_due([self.due("CFG")], [filled])
        self.assertNotIn("::warning", output)
        self.assertIn("- Overdue exits: 0\n", summary)


def filled_attempt(order_id, quantity, price):
    return alpaca_integration.OrderLifecycleResult(
        order_id=order_id, symbol="SHORT", ordered_qty=quantity, filled_qty=quantity,
        canceled_qty=0, remaining_qty=0, status="filled", terminal=True, timed_out=False,
        cancel_confirmed=False, average_fill_price=Decimal(price), commission_amount=None,
        stop_reason="fully_filled", raw_order=None,
    )


class ExitChaseTests(unittest.TestCase):
    """CME on 2026-10-21: the close stopped after 3 tries when the market came back to a price already tried."""

    def chase(self, wait, **kwargs):
        """Close a 3-lot spread in a quiet net 0.80 x 0.65 credit market; return the limits sent and the result."""
        sent = []

        def submit(_client, request, client_order_id):
            sent.append(Decimal(str(request.limit_price)))
            return SimpleNamespace(id=f"order-{len(sent)}")

        with ExitStack() as stack:
            chase_patches(stack, paper_client())
            for name, value in (
                ("_submit_order_idempotently", Mock(side_effect=submit)),
                ("wait_for_fill", Mock(side_effect=wait)),
            ):
                stack.enter_context(patch.object(alpaca_integration, name, value))
            stack.enter_context(redirect_stdout(io.StringIO()))
            result = alpaca_integration.close_calendar_spread_order(
                "SHORT", "LONG", 3, client_order_id_prefix="test", max_attempts=13,
                **{"max_close_debit": Decimal("0.50"), **kwargs},
            )
        return sent, result

    def test_a_close_offers_the_market_price_again_and_can_still_fill(self):
        def wait(_client, order_id, **kwargs):
            if order_id == "order-6":
                return filled_attempt(order_id, kwargs["expected_qty"], "-0.65")
            return canceled_attempt(order_id, kwargs["expected_qty"])

        sent, result = self.chase(wait)
        # A tenth of the 0.15 width per step from the midpoint, then the market's price again.
        self.assertEqual(sent, [Decimal(price) for price in ("-0.73", "-0.71", "-0.69", "-0.67", "-0.65", "-0.65")])
        self.assertEqual((result.filled_qty, result.stop_reason), (3, "requested_quantity_filled"))

    def test_an_unfilled_close_keeps_trying_until_its_attempts_run_out(self):
        sent, result = self.chase(lambda _client, order_id, **kwargs: canceled_attempt(order_id, kwargs["expected_qty"]))
        self.assertEqual(len(sent), 13)
        self.assertEqual(sent[-1], Decimal("-0.65"))
        self.assertEqual(result.stop_reason, "max_attempts_reached")

    def test_a_close_pinned_at_its_hard_limit_still_stops(self):
        # Requiring a 0.70 credit caps the price below the 0.65 market.
        sent, result = self.chase(
            lambda _client, order_id, **kwargs: canceled_attempt(order_id, kwargs["expected_qty"]),
            max_close_debit=None, min_close_credit=Decimal("0.70"),
        )
        self.assertEqual(sent, [Decimal("-0.73"), Decimal("-0.71"), Decimal("-0.70")])
        self.assertEqual(result.stop_reason, "hard_price_limit_reached")

    def test_a_due_close_uses_all_the_attempts_that_fit_in_its_time(self):
        # Each attempt waits out its timeout plus the measured second of broker calls.
        now = {"seconds": 0.0}

        def wait(_client, order_id, **kwargs):
            now["seconds"] += kwargs["timeout"] + 1
            return canceled_attempt(order_id, kwargs["expected_qty"])

        trade = {
            "trade_id": "trade-CME", "Ticker": "CME", "Open Date": "2026-10-20", "When": "BMO",
            "earnings_date": "2026-10-21", "remaining_quantity": 2, "Short Symbol": "SHORT", "Long Symbol": "LONG",
        }
        client = paper_client()
        clock_now = datetime.now(timezone.utc)
        client.get_clock.return_value = SimpleNamespace(
            is_open=True, timestamp=clock_now, next_close=clock_now + timedelta(hours=2),
        )
        close = Mock(side_effect=alpaca_integration.close_calendar_spread_order)
        sent = []
        with ExitStack() as stack:
            chase_patches(stack, client)
            stack.enter_context(patch.object(alpaca_integration, "wait_for_fill", Mock(side_effect=wait)))
            stack.enter_context(patch.object(
                alpaca_integration, "_submit_order_idempotently",
                Mock(side_effect=lambda _client, request, _id: sent.append(request) or SimpleNamespace(id=f"order-{len(sent)}")),
            ))
            stack.enter_context(patch.object(alpaca_integration.time, "monotonic", side_effect=lambda: now["seconds"]))
            for name, value in (
                ("get_open_trades", Mock(return_value=[trade])),
                ("is_time_to_close", Mock(return_value=True)),
                ("account_snapshot", Mock(return_value=(Decimal("100000"), Decimal("100000")))),
                ("reconcile_broker_state", Mock()),
                ("reserve_operation_prefix", Mock(return_value="prefix")),
                ("finalize_execution", Mock()),
                ("close_calendar_spread_order", close),
            ):
                stack.enter_context(patch.object(trade_workflow, name, value))
            stack.enter_context(redirect_stdout(io.StringIO()))
            trade_workflow.close_due_trades(object(), {"resolutions": {"trade-CME": "MATCHED_SPREAD"}}, 100_000)
        kwargs = close.call_args.kwargs
        self.assertLessEqual(kwargs["attempt_timeout"], 10)
        self.assertEqual(len(sent), kwargs["max_attempts"])
        self.assertGreater(len(sent), 8)


class CloseQuoteAgeTests(unittest.TestCase):
    """PHM on 2026-10-22: the expiring short's quote was 620 s old, so its due close failed."""

    SHORT, LONG = "PHM261023C00115000", "PHM261120C00115000"

    def quotes(self, short_age):
        now = datetime.now(timezone.utc)
        quote = lambda bid, ask, age: SimpleNamespace(
            bid_price=bid, ask_price=ask, bid_size=10, ask_size=10, timestamp=now - timedelta(seconds=age),
        )
        data = Mock()
        data.get_option_latest_quote.return_value = {
            self.SHORT: quote("1.00", "1.10", short_age), self.LONG: quote("4.00", "4.30", 5),
        }
        return data

    def close_phm(self, short_age, **settings):
        """Run PHM's due close against real quote checks; return (failures, limits sent)."""
        trade = {
            "trade_id": "trade-PHM", "Ticker": "PHM", "Open Date": "2026-10-21", "When": "BMO",
            "earnings_date": "2026-10-22", "remaining_quantity": 5, "Short Symbol": self.SHORT, "Long Symbol": self.LONG,
        }
        client = paper_client()
        now = datetime.now(timezone.utc)
        client.get_clock.return_value = SimpleNamespace(is_open=True, timestamp=now, next_close=now + timedelta(hours=2))
        sent = []

        def submit(_client, request, _client_order_id):
            sent.append(Decimal(str(request.limit_price)))
            return SimpleNamespace(id=f"order-{len(sent)}")

        with ExitStack() as stack:
            for name, value in (
                ("init_alpaca_client", Mock(return_value=client)),
                ("_require_explicit_trading_mode", Mock()),
                ("OptionHistoricalDataClient", Mock(return_value=self.quotes(short_age))),
                ("_submit_order_idempotently", Mock(side_effect=submit)),
                ("_notify_submitted_order", Mock()),
                ("_notify_terminal_attempt", Mock()),
                ("wait_for_fill", Mock(side_effect=lambda _c, order_id, **kw: filled_attempt(order_id, kw["expected_qty"], "-3.10"))),
            ):
                stack.enter_context(patch.object(alpaca_integration, name, value))
            stack.enter_context(patch.object(alpaca_integration.time, "sleep"))
            for name, value in (
                ("get_open_trades", Mock(return_value=[trade])),
                ("is_time_to_close", Mock(return_value=True)),
                ("account_snapshot", Mock(return_value=(Decimal("100000"), Decimal("100000")))),
                ("reconcile_broker_state", Mock()),
                ("reserve_operation_prefix", Mock(return_value="prefix")),
                ("finalize_execution", Mock()),
                ("record_close_quote_failure", Mock(side_effect=lambda _tid, error: f"CLOSE_QUOTE_VALIDATION_FAILED: {error}")),
                *settings.items(),
            ):
                stack.enter_context(patch.object(trade_workflow, name, value))
            stack.enter_context(patch.object(trade_workflow.time_module, "monotonic", return_value=0))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(patch("sys.stderr", io.StringIO()))
            failures = trade_workflow.close_due_trades(object(), {"resolutions": {"trade-PHM": "MATCHED_SPREAD"}}, 10_000)
        return failures, sent

    def test_a_due_close_prices_from_a_quote_minutes_old(self):
        failures, sent = self.close_phm(620)
        self.assertEqual(failures, [])
        self.assertEqual(sent, [Decimal("-3.10")])

    def test_a_quote_older_than_the_close_limit_still_fails_the_close(self):
        failures, sent = self.close_phm(1000)
        self.assertEqual(sent, [])
        self.assertEqual(len(failures), 1)
        self.assertIn("Stale quote for PHM261023C00115000", failures[0])
        self.assertIn("allowed=900s", failures[0])

    def test_cloners_can_set_the_close_limit(self):
        failures, _ = self.close_phm(620, CLOSE_QUOTE_MAX_AGE_SECONDS=300)
        self.assertIn("allowed=300s", failures[0])
        workflow = (Path(__file__).resolve().parent / ".github" / "workflows" / "config.yml").read_text(encoding="utf-8")
        self.assertIn("CLOSE_QUOTE_MAX_AGE_SECONDS: ${{ vars.CLOSE_QUOTE_MAX_AGE_SECONDS }}", workflow)

    def test_entries_still_refuse_a_quote_older_than_two_minutes(self):
        with (
            patch.object(alpaca_integration, "_require_explicit_trading_mode"),
            patch.object(alpaca_integration, "OptionHistoricalDataClient", Mock(return_value=self.quotes(620))),
            patch.object(alpaca_integration.time, "sleep"),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(alpaca_integration.QuoteValidationError, "allowed=120s"):
                alpaca_integration.get_spread_quotes(self.SHORT, self.LONG)

    def test_an_invalid_close_limit_is_refused_before_contacting_the_broker(self):
        for value in (0, 3601):
            with (
                self.subTest(value=value),
                patch.object(trade_workflow, "CLOSE_QUOTE_MAX_AGE_SECONDS", value),
                patch.object(trade_workflow, "configured_mode"),
                patch.object(trade_workflow, "init_db"),
                patch.object(trade_workflow, "configured_broker_client") as broker,
            ):
                with self.assertRaisesRegex(trade_workflow.OperationalFailure, "CLOSE_QUOTE_MAX_AGE_SECONDS"):
                    trade_workflow.run_trade_workflow()
            broker.assert_not_called()


class ReplannedOpenTests(unittest.TestCase):
    """A same-day retry of an unfilled open must not break the check of its earlier orders."""

    TRADE_ID = "11111111-2222-3333-4444-555555555555"
    CONTRACTS = ("XYZ261023C00010000", "XYZ261120C00010000")
    OTHER_STRIKE = ("XYZ261023C00011000", "XYZ261120C00011000")

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for patcher in (
            patch.object(trade_workflow, "DB_PATH", Path(directory.name) / "trades.db"),
            patch.dict(os.environ, {"ALPACA_PAPER": "true", "APCA_API_BASE_URL": "https://paper-api.alpaca.markets"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        trade_workflow.init_db()
        fingerprint = trade_workflow.hashlib.sha256(b"alpaca-account-id:v1:account").hexdigest()
        with trade_workflow.db(write=True) as conn:
            conn.execute(
                "INSERT INTO broker_identity VALUES(1,'PAPER',?,'https://paper-api.alpaca.markets','bound','bound')",
                (fingerprint,),
            )
        self.broker_orders = []

    def client(self):
        now = datetime.now(timezone.utc)
        client = paper_client()
        client.get_account.return_value = SimpleNamespace(id="account", equity="100000", options_buying_power="100000")
        client.get_all_positions.return_value = []
        client.get_orders.side_effect = lambda filter=None: list(self.broker_orders)
        client.get_clock.return_value = SimpleNamespace(is_open=True, timestamp=now, next_close=now + timedelta(hours=2))
        return client

    def open_once(self, quantity, contracts):
        """One run's opening chase; every order is cancelled unfilled at the broker."""
        short, long = contracts
        client = self.client()

        def submit(_client, request, client_order_id):
            order_id = f"order-{len(self.broker_orders) + 1}"
            legs = [
                SimpleNamespace(symbol=short, ratio_qty="1", side="sell", position_intent="sell_to_open"),
                SimpleNamespace(symbol=long, ratio_qty="1", side="buy", position_intent="buy_to_open"),
            ]
            self.broker_orders.append(SimpleNamespace(
                id=order_id, client_order_id=client_order_id, status="canceled", qty=str(request.qty),
                filled_qty="0", symbol="", order_class="mleg", time_in_force="day", type="limit",
                asset_class="us_option", legs=legs,
            ))
            return SimpleNamespace(id=order_id, client_order_id=client_order_id, status="accepted", qty=request.qty)

        def wait(_client, order_id, **kwargs):
            result = canceled_attempt(order_id, kwargs["expected_qty"])
            kwargs["terminal_state_callback"](result)
            return result

        expiries = tuple(trade_workflow.option_expiration(symbol).isoformat() for symbol in contracts)
        chain = {expiry: {10.0: {"call": SimpleNamespace(symbol=symbol)}} for expiry, symbol in zip(expiries, contracts)}
        quotes = alpaca_integration.SpreadQuoteSnapshot(Decimal("0.50"), Decimal("0.60"), Decimal("0.80"), Decimal("1.00"))
        with ExitStack() as stack:
            for name, value in (
                ("init_alpaca_client", Mock(return_value=client)),
                ("_fetch_spread_quote_snapshot", Mock(return_value=quotes)),
                ("_submit_order_idempotently", Mock(side_effect=submit)),
                ("wait_for_fill", Mock(side_effect=wait)),
            ):
                stack.enter_context(patch.object(alpaca_integration, name, value))
            for name, value in (
                ("is_time_to_open", Mock(return_value=True)),
                ("compute_recommendation", Mock(return_value={"avg_volume": True, "iv30_rv30": True, "ts_slope_0_45": True})),
                ("select_expiries_and_strike_alpaca", Mock(return_value=(*expiries, 10.0))),
                ("get_alpaca_option_chain", Mock(return_value=chain)),
                ("get_spread_quotes", Mock(return_value=("0.50", "0.60", "0.80", "1.00"))),
                ("candidate_allocation", Mock(return_value=(quantity, 1_000_000, 0, 10_000_000))),
                ("stable_trade_id", Mock(return_value=self.TRADE_ID)),
                ("exposure_cents", Mock(return_value=0)),
            ):
                stack.enter_context(patch.object(trade_workflow, name, value))
            stack.enter_context(patch.object(trade_workflow.time_module, "monotonic", return_value=0))
            stack.enter_context(redirect_stdout(io.StringIO()))
            trade_workflow.open_candidate(
                client, {"act_symbol": "XYZ"}, "AMC", date(2026, 10, 19),
                datetime(2026, 10, 19, 16, 0, tzinfo=EASTERN), 10_000, exit_date=date(2026, 10, 20),
            )

    def trade(self):
        with trade_workflow.db() as conn:
            return dict(conn.execute("SELECT * FROM trades WHERE trade_id=?", (self.TRADE_ID,)).fetchone())

    def reconcile(self):
        with redirect_stdout(io.StringIO()):
            return trade_workflow.reconcile_broker_state(self.client(), read_only=False)

    def test_a_smaller_retry_still_runs_and_keeps_the_larger_size_on_record(self):
        # Run 1: no fill at the price limit, so the plan is retryable.
        self.open_once(7, self.CONTRACTS)
        self.assertEqual(self.trade()["lifecycle_status"], "CANCELED_NO_FILL")
        sent = len(self.broker_orders)
        # Run 2 on the same day sizes the trade at 6 after the price moved.
        self.open_once(6, self.CONTRACTS)
        self.assertGreater(len(self.broker_orders), sent)
        self.assertEqual({Decimal(order.qty) for order in self.broker_orders[sent:]}, {Decimal(6)})
        trade = self.trade()
        self.assertEqual((trade["ordered_quantity"], trade["open_generation"]), (7, 2))
        # The next run's startup check still matches the 7-lot and 6-lot orders.
        self.assertEqual(self.reconcile()["discrepancies"], [])

    def test_a_retry_on_another_strike_is_skipped(self):
        self.open_once(7, self.CONTRACTS)
        sent = len(self.broker_orders)
        self.assertGreater(sent, 0)
        self.open_once(7, self.OTHER_STRIKE)
        self.assertEqual(len(self.broker_orders), sent)
        trade = self.trade()
        self.assertEqual((trade["Short Symbol"], trade["Long Symbol"], trade["open_generation"]), (*self.CONTRACTS, 1))
        self.assertEqual(self.reconcile()["discrepancies"], [])

    def test_a_retry_on_one_changed_leg_is_skipped(self):
        for label, contracts in (
            ("short leg", ("XYZ261030C00010000", self.CONTRACTS[1])),
            ("long leg", (self.CONTRACTS[0], "XYZ261127C00010000")),
        ):
            with self.subTest(label):
                self.broker_orders.clear()
                with trade_workflow.db(write=True) as conn:
                    conn.execute("DELETE FROM broker_orders")
                    conn.execute("DELETE FROM trades")
                self.open_once(7, self.CONTRACTS)
                sent = len(self.broker_orders)
                self.open_once(7, contracts)
                self.assertEqual(len(self.broker_orders), sent)
                trade = self.trade()
                self.assertEqual((trade["Short Symbol"], trade["Long Symbol"]), self.CONTRACTS)
                self.assertEqual(self.reconcile()["discrepancies"], [])

    def test_the_plan_keeps_the_largest_size_sent_across_retries(self):
        self.open_once(7, self.CONTRACTS)
        self.open_once(8, self.CONTRACTS)
        self.open_once(7, self.CONTRACTS)
        self.assertEqual(self.trade()["ordered_quantity"], 8)
        self.assertEqual(self.reconcile()["discrepancies"], [])

    def test_a_plan_with_no_orders_sent_may_change_contracts(self):
        # Quotes failed before any order, so nothing at the broker refers to this plan.
        with redirect_stdout(io.StringIO()):
            trade_workflow.create_planned_trade({
                "trade_id": self.TRADE_ID, "Ticker": "XYZ", "When": "AMC", "Short Symbol": self.CONTRACTS[0],
                "Long Symbol": self.CONTRACTS[1], "Open Date": "2026-10-19", "earnings_date": "2026-10-19",
            }, 7)
        self.assertTrue(trade_workflow.release_unfilled_open_plan(self.TRADE_ID, "open_quote_validation_failed"))
        self.open_once(7, self.OTHER_STRIKE)
        self.assertGreater(len(self.broker_orders), 0)
        trade = self.trade()
        self.assertEqual((trade["Short Symbol"], trade["Long Symbol"]), self.OTHER_STRIKE)
        self.assertEqual(self.reconcile()["discrepancies"], [])

    def test_a_retry_that_keeps_the_contracts_and_size_still_runs(self):
        self.open_once(7, self.CONTRACTS)
        sent = len(self.broker_orders)
        self.open_once(8, self.CONTRACTS)
        self.assertGreater(len(self.broker_orders), sent)
        self.assertEqual(self.trade()["open_generation"], 2)
        self.assertEqual(self.reconcile()["discrepancies"], [])


class StaleOrderListTests(unittest.TestCase):
    """DHR on 2026-10-20: a cancel the bot had confirmed still showed as pending_cancel in the order list."""

    TRADE_ID = "4fbaa0c4-96ab-5ad2-b5a5-37713bcf909f"
    SHORT, LONG = "DHR261023C00225000", "DHR261120C00225000"
    CLIENT_ORDER_ID = "eta-o-4fbaa0c496ab5ad2b5a5-0001-open-1"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for patcher in (
            patch.object(trade_workflow, "DB_PATH", Path(directory.name) / "trades.db"),
            patch.dict(os.environ, {"ALPACA_PAPER": "true", "APCA_API_BASE_URL": "https://paper-api.alpaca.markets"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        trade_workflow.init_db()
        fingerprint = trade_workflow.hashlib.sha256(b"alpaca-account-id:v1:account").hexdigest()
        with trade_workflow.db(write=True) as conn:
            conn.execute(
                "INSERT INTO broker_identity VALUES(1,'PAPER',?,'https://paper-api.alpaca.markets','bound','bound')",
                (fingerprint,),
            )
            conn.execute(
                """INSERT INTO trades("Ticker","When","Short Symbol","Long Symbol","Open Date",earnings_date,trade_id,
                parent_trade_id,ordered_quantity,filled_quantity,remaining_quantity,lifecycle_status,
                open_client_order_prefix,open_generation)
                VALUES('DHR','BMO',?,?,'2026-10-20','2026-10-21',?,?,2,0,0,'OPEN_ORDER_SUBMITTED',?,1)""",
                (self.SHORT, self.LONG, self.TRADE_ID, self.TRADE_ID, self.CLIENT_ORDER_ID[:-7]),
            )
            # The cancel the chase confirmed by reading the order itself.
            conn.execute(
                """INSERT INTO broker_orders(order_id,trade_id,client_order_id,phase,method,ordered_quantity,
                canceled_quantity,remaining_quantity,limit_price,lifecycle_status,terminal,stop_reason,updated_at)
                VALUES('order-1',?,?,'open','calendar_spread',2,2,2,'4.23','canceled',1,'deadline_cancel_confirmed','now')""",
                (self.TRADE_ID, self.CLIENT_ORDER_ID),
            )

    def order(self, status):
        return SimpleNamespace(
            id="order-1", client_order_id=self.CLIENT_ORDER_ID, status=status, qty="2", filled_qty="0",
            symbol="", order_class="mleg", time_in_force="day", type="limit", asset_class="us_option",
            limit_price="4.23", legs=[
                SimpleNamespace(symbol=self.SHORT, ratio_qty="1", side="sell", position_intent="sell_to_open"),
                SimpleNamespace(symbol=self.LONG, ratio_qty="1", side="buy", position_intent="buy_to_open"),
            ],
        )

    def client(self, direct):
        """The order list says pending_cancel; ``direct`` is what reading the order by ID returns or raises."""
        now = datetime.now(timezone.utc)
        client = paper_client()
        client.get_account.return_value = SimpleNamespace(id="account", equity="100000", options_buying_power="100000")
        client.get_all_positions.return_value = []
        client.get_orders.side_effect = lambda filter=None: [self.order("pending_cancel")]
        client.get_clock.return_value = SimpleNamespace(is_open=True, timestamp=now, next_close=now + timedelta(hours=2))
        if isinstance(direct, Exception):
            client.get_order_by_id.side_effect = direct
        else:
            client.get_order_by_id.return_value = self.order(direct)
        return client

    def reconcile(self, client, read_only=True):
        output = io.StringIO()
        with redirect_stdout(output), patch("sys.stderr", io.StringIO()):
            try:
                return trade_workflow.reconcile_broker_state(client, read_only=read_only), output.getvalue()
            except trade_workflow.ReconciliationFailure:
                return None, output.getvalue()

    def ledger_order(self):
        with trade_workflow.db() as conn:
            return tuple(conn.execute("SELECT lifecycle_status, terminal FROM broker_orders WHERE order_id='order-1'").fetchone())

    def test_the_next_order_of_the_chase_is_admitted_once_a_direct_read_confirms_the_cancel(self):
        client = self.client("canceled")
        guard = trade_workflow.before_submit_guard(
            900_000, opening=True, deadline=10_000, order_timing={"attempt_timeout": 10, "cancel_timeout": 30},
        )
        with redirect_stdout(io.StringIO()), patch.object(trade_workflow.time_module, "monotonic", return_value=0):
            guard(client, {"limit_price": Decimal("4.40"), "quantity": 2})
        self.assertEqual(client.get_order_by_id.call_args.args[0], "order-1")

    def test_a_stale_listing_of_a_confirmed_cancel_is_not_a_discrepancy(self):
        client = self.client("canceled")
        result, output = self.reconcile(client)
        self.assertEqual(result["discrepancies"], [])
        client.get_order_by_id.assert_called_once()
        self.assertIn("showed order-1 as pending_cancel; reading it directly shows canceled", output)

    def test_an_order_still_active_when_read_directly_still_stops_the_run(self):
        for status in ("pending_cancel", "new", "partially_filled"):
            with self.subTest(status=status):
                result, _ = self.reconcile(self.client(status))
                self.assertIsNone(result)

    def test_a_failed_direct_read_still_stops_the_run(self):
        result, _ = self.reconcile(self.client(RuntimeError("timeout")))
        self.assertIsNone(result)

    def test_an_order_the_ledger_never_saw_end_is_not_re_read(self):
        with trade_workflow.db(write=True) as conn:
            conn.execute("UPDATE broker_orders SET terminal=0, lifecycle_status='new'")
        client = self.client("canceled")
        result, _ = self.reconcile(client)
        self.assertIsNone(result)
        client.get_order_by_id.assert_not_called()

    def test_start_of_run_reconciliation_keeps_the_confirmed_cancel_in_the_ledger(self):
        result, _ = self.reconcile(self.client("canceled"), read_only=False)
        self.assertEqual(result["discrepancies"], [])
        self.assertEqual(self.ledger_order(), ("canceled", 1))


if __name__ == "__main__":
    unittest.main()
