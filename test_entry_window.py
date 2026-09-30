import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import trade_workflow

EASTERN = trade_workflow.EASTERN
SESSION_CLOSE = datetime(2026, 9, 30, 16, 0, tzinfo=EASTERN)


def frozen_now(moment):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment

    return patch.object(trade_workflow, "datetime", FrozenDatetime)


def eastern(hour, minute):
    return datetime(2026, 9, 30, hour, minute, tzinfo=EASTERN)


class EntryWindowTests(unittest.TestCase):
    def test_default_window_accepts_a_delayed_early_afternoon_run(self):
        with frozen_now(eastern(13, 10)):
            self.assertTrue(trade_workflow.is_time_to_open(date(2026, 9, 30), "AMC", SESSION_CLOSE))
            self.assertTrue(trade_workflow.is_time_to_open(date(2026, 10, 1), "BMO", SESSION_CLOSE))

    def test_default_window_keeps_the_pre_close_cutoff(self):
        with frozen_now(eastern(15, 57)):
            self.assertFalse(trade_workflow.is_time_to_open(date(2026, 9, 30), "AMC", SESSION_CLOSE))

    def test_default_window_still_requires_the_session_before_the_event(self):
        with frozen_now(eastern(13, 10)):
            self.assertFalse(trade_workflow.is_time_to_open(date(2026, 10, 1), "AMC", SESSION_CLOSE))

    def test_short_window_restores_late_day_entry(self):
        with patch.object(trade_workflow, "ENTRY_WINDOW_MINUTES", 25):
            with frozen_now(eastern(13, 10)):
                self.assertFalse(trade_workflow.is_time_to_open(date(2026, 9, 30), "AMC", SESSION_CLOSE))
            with frozen_now(eastern(15, 40)):
                self.assertTrue(trade_workflow.is_time_to_open(date(2026, 9, 30), "AMC", SESSION_CLOSE))

    def test_invalid_window_is_refused_before_contacting_the_broker(self):
        with (
            patch.object(trade_workflow, "ENTRY_WINDOW_MINUTES", 3),
            patch.object(trade_workflow, "configured_mode"),
            patch.object(trade_workflow, "init_db"),
            patch.object(trade_workflow, "configured_broker_client") as broker,
        ):
            with self.assertRaisesRegex(trade_workflow.OperationalFailure, "ENTRY_WINDOW_MINUTES"):
                trade_workflow.run_trade_workflow()
        broker.assert_not_called()


class EarningsCalendarFailureTests(unittest.TestCase):
    def test_calendar_timeout_is_an_operational_failure_after_position_management(self):
        client = Mock()
        with (
            frozen_now(eastern(13, 10)),
            patch.object(trade_workflow, "configured_mode"),
            patch.object(trade_workflow, "init_db"),
            patch.object(trade_workflow, "configured_broker_client", return_value=(client, "PAPER")),
            patch.object(trade_workflow, "bind_or_validate_broker_identity"),
            patch.object(trade_workflow, "reconcile_broker_state", return_value={"resolutions": {}}),
            patch.object(trade_workflow, "close_due_trades") as close_due,
            patch.object(
                trade_workflow,
                "get_todays_earnings",
                side_effect=RuntimeError("Earnings data request failed after 4 attempts: ReadTimeout"),
            ),
            patch.object(trade_workflow, "get_tomorrows_earnings", return_value=[]),
            patch.object(trade_workflow, "open_candidate") as open_candidate,
        ):
            # The workflow type-checks broker times against its patched datetime.
            client.get_clock.return_value = SimpleNamespace(
                is_open=True,
                next_close=trade_workflow.datetime(2026, 9, 30, 16, 0, tzinfo=EASTERN),
                next_open=trade_workflow.datetime(2026, 10, 1, 9, 30, tzinfo=EASTERN),
            )
            with self.assertRaisesRegex(trade_workflow.OperationalFailure, "Earnings calendar unavailable"):
                trade_workflow.run_trade_workflow()

        close_due.assert_called_once()
        open_candidate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
