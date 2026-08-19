import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Optional, Tuple
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Position
from dotenv import load_dotenv
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest
from alpaca.trading.requests import GetOptionContractsRequest, GetOrderByIdRequest
from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest
from alpaca.trading.enums import OrderClass, TimeInForce, OrderSide, PositionIntent
from datetime import datetime, timedelta, timezone
import threading, time
from alpaca.common.exceptions import APIError

load_dotenv()

API_KEY = os.environ.get("APCA_API_KEY_ID")
API_SECRET = os.environ.get("APCA_API_SECRET_KEY")

CENT = Decimal("0.01")
CONTRACT_MULTIPLIER = Decimal("100")
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_OVERALL_TIMEOUT = 180
DEFAULT_ATTEMPT_TIMEOUT = 30
DEFAULT_CANCEL_TIMEOUT = 20
DEFAULT_QUOTE_MAX_AGE_SECONDS = 30
_OPTION_CHAIN_CACHE = {}


class PaperModeRequiredError(RuntimeError):
    """Legacy base error for invalid explicit Alpaca trading-mode configuration."""


class TradingModeConfigurationError(PaperModeRequiredError):
    """Raised when Alpaca PAPER/LIVE routing is not configured unambiguously."""


class QuoteValidationError(RuntimeError):
    """Raised when an option quote is missing, crossed, stale, or otherwise unsafe."""


class OrderStateUnresolvedError(RuntimeError):
    """Raised when an order may still be active after a cancellation attempt."""


class OrderExecutionError(RuntimeError):
    """Raised when the broker rejects an order or reports an impossible execution."""


@dataclass(frozen=True)
class OrderLifecycleResult:
    """Final, broker-confirmed state for one submitted order.

    ``remaining_qty`` is the unfilled part of this order. It may also be present in
    ``canceled_qty`` when cancellation was confirmed; ``active_qty`` distinguishes
    an unfilled-but-live remainder from a safely inactive remainder.
    """

    order_id: str
    symbol: str
    ordered_qty: int
    filled_qty: int
    canceled_qty: int
    remaining_qty: int
    status: str
    terminal: bool
    timed_out: bool
    cancel_confirmed: bool
    average_fill_price: Optional[Decimal]
    commission_amount: Optional[Decimal]
    stop_reason: str
    raw_order: Any

    @property
    def id(self):
        return self.order_id

    @property
    def filled_avg_price(self):
        # Preserve the attribute shape used by the existing workflow/SDK models.
        return "" if self.average_fill_price is None else str(self.average_fill_price)

    @property
    def commission(self):
        return self.commission_amount

    @property
    def active_qty(self):
        return 0 if self.terminal else self.remaining_qty


@dataclass(frozen=True)
class ExecutionResult:
    """Aggregate, safe-to-persist result of a bounded price chase."""

    operation: str
    symbol: str
    requested_qty: int
    ordered_qty: int
    filled_qty: int
    canceled_qty: int
    remaining_qty: int
    status: str
    average_fill_price: Optional[Decimal]
    commission_amount: Optional[Decimal]
    stop_reason: str
    order_ids: Tuple[str, ...]
    attempts: Tuple[OrderLifecycleResult, ...]

    @property
    def id(self):
        return self.order_ids[-1] if self.order_ids else ""

    @property
    def filled_avg_price(self):
        return "" if self.average_fill_price is None else str(self.average_fill_price)

    @property
    def commission(self):
        return self.commission_amount

    @property
    def fully_filled(self):
        return self.filled_qty == self.requested_qty


@dataclass(frozen=True)
class SpreadQuoteSnapshot:
    short_bid: Decimal
    short_ask: Decimal
    long_bid: Decimal
    long_ask: Decimal

    @property
    def opening_net_bid(self):
        return self.long_bid - self.short_ask

    @property
    def opening_net_ask(self):
        return self.long_ask - self.short_bid

    @property
    def closing_signed_bid(self):
        # Alpaca MLEG: negative is a credit received; positive is a debit paid.
        return self.short_bid - self.long_ask

    @property
    def closing_signed_ask(self):
        return self.short_ask - self.long_bid


@dataclass(frozen=True)
class SingleQuoteSnapshot:
    bid: Decimal
    ask: Decimal


def _configured_paper_mode():
    setting = os.environ.get("ALPACA_PAPER")
    if setting not in {"true", "false"}:
        raise TradingModeConfigurationError(
            "Refusing to initialize Alpaca: ALPACA_PAPER must be exactly "
            "'true' or 'false'."
        )
    return setting == "true"


def _require_explicit_trading_mode():
    paper_mode = _configured_paper_mode()
    if not os.environ.get("APCA_API_KEY_ID") or not os.environ.get("APCA_API_SECRET_KEY"):
        mode_label = "paper" if paper_mode else "live"
        raise TradingModeConfigurationError(
            f"Refusing to initialize Alpaca: {mode_label} API credentials are not configured."
        )
    return paper_mode


def _require_explicit_paper_mode():
    """Compatibility wrapper; both explicit PAPER and LIVE modes are now valid."""
    return _require_explicit_trading_mode()


def _client_mode_label(client):
    paper_mode = getattr(client, "_eta_paper_mode", None)
    if not isinstance(paper_mode, bool):
        paper_mode = _configured_paper_mode()
    return "PAPER" if paper_mode else "LIVE"


def _decimal(value, field_name):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field_name}: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"Invalid {field_name}: value must be finite")
    return result


def _nearest_cent(value):
    return _decimal(value, "price").quantize(CENT, rounding=ROUND_HALF_UP)


def _max_price_cent(value):
    """Round a hard upper price bound down so rounding can never weaken it."""
    return _decimal(value, "maximum price").quantize(CENT, rounding=ROUND_FLOOR)


def _min_price_cent(value):
    """Round a hard lower price bound up so rounding can never weaken it."""
    return _decimal(value, "minimum price").quantize(CENT, rounding=ROUND_CEILING)


def _status_value(order):
    status = getattr(order, "status", "unknown")
    value = getattr(status, "value", status)
    return str(value).strip().lower()


def _int_quantity(value, field_name):
    quantity = _decimal(0 if value is None else value, field_name)
    integral = quantity.to_integral_value()
    if quantity != integral or integral < 0:
        raise OrderExecutionError(f"Invalid {field_name} reported by broker: {value!r}")
    return int(integral)


def _average_price(order):
    value = getattr(order, "filled_avg_price", None)
    if value in (None, ""):
        return None
    try:
        return _decimal(value, "filled average price")
    except ValueError:
        # Treat malformed/non-finite broker values as unavailable so terminal
        # order state can still be persisted without publishing false cash flow.
        return None


def _commission(order):
    value = getattr(order, "commission", None)
    if value in (None, ""):
        return None
    return _decimal(value, "commission")


def _fill_price_ready(result):
    price = result.average_fill_price
    return (
        result.filled_qty <= 0
        or (price is not None and price.is_finite())
    )


def _terminal_order(status, filled_qty, ordered_qty):
    # These are the only states that prove the submitted remainder cannot fill.
    return filled_qty >= ordered_qty or status in {
        "filled", "canceled", "expired", "rejected"
    }


def _lifecycle_result(order, expected_qty=None, timed_out=False, stop_reason=None):
    ordered_qty = _int_quantity(
        getattr(order, "qty", expected_qty), "ordered quantity"
    )
    if ordered_qty == 0 and expected_qty is not None:
        ordered_qty = _int_quantity(expected_qty, "expected ordered quantity")
    if expected_qty is not None and ordered_qty != _int_quantity(
        expected_qty, "expected ordered quantity"
    ):
        raise OrderExecutionError(
            f"Broker order {getattr(order, 'id', 'unknown')} has quantity {ordered_qty}; "
            f"expected {expected_qty}."
        )
    filled_qty = _int_quantity(getattr(order, "filled_qty", 0) or 0, "filled quantity")
    if filled_qty > ordered_qty:
        raise OrderExecutionError(
            f"Broker reported filled quantity {filled_qty} above ordered quantity {ordered_qty} "
            f"for order {getattr(order, 'id', 'unknown')}."
        )
    status = _status_value(order)
    terminal = _terminal_order(status, filled_qty, ordered_qty)
    remaining_qty = ordered_qty - filled_qty
    canceled_qty = remaining_qty if status == "canceled" else 0
    if stop_reason is None:
        stop_reason = "fully_filled" if filled_qty == ordered_qty else f"order_{status}"
    return OrderLifecycleResult(
        order_id=str(getattr(order, "id", "")),
        symbol=str(getattr(order, "symbol", "") or ""),
        ordered_qty=ordered_qty,
        filled_qty=filled_qty,
        canceled_qty=canceled_qty,
        remaining_qty=remaining_qty,
        status=status,
        terminal=terminal,
        timed_out=timed_out,
        cancel_confirmed=status == "canceled",
        average_fill_price=_average_price(order),
        commission_amount=_commission(order),
        stop_reason=stop_reason,
        raw_order=order,
    )


def _execution_result(operation, symbol, requested_qty, attempts, stop_reason):
    filled_qty = sum(attempt.filled_qty for attempt in attempts)
    ordered_qty = sum(attempt.ordered_qty for attempt in attempts)
    canceled_qty = sum(attempt.canceled_qty for attempt in attempts)
    filled_value = sum(
        (attempt.average_fill_price or Decimal("0")) * attempt.filled_qty
        for attempt in attempts
    )
    average = filled_value / filled_qty if filled_qty else None
    if filled_qty == requested_qty:
        status = "filled"
    elif filled_qty:
        status = "partially_filled"
    else:
        status = "unfilled"
    commissions = [attempt.commission_amount for attempt in attempts]
    commission_amount = (
        None
        if any(value is None for value in commissions)
        else sum(commissions, Decimal("0"))
    )
    return ExecutionResult(
        operation=operation,
        symbol=symbol,
        requested_qty=requested_qty,
        ordered_qty=ordered_qty,
        filled_qty=filled_qty,
        canceled_qty=canceled_qty,
        remaining_qty=max(requested_qty - filled_qty, 0),
        status=status,
        average_fill_price=average,
        commission_amount=commission_amount,
        stop_reason=stop_reason,
        order_ids=tuple(attempt.order_id for attempt in attempts),
        attempts=tuple(attempts),
    )


def _quote_timestamp(value, symbol):
    if value is None:
        raise QuoteValidationError(f"Quote for {symbol} has no timestamp.")
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise QuoteValidationError(f"Quote for {symbol} has an invalid timestamp.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validated_quote(quote, symbol, max_age_seconds):
    if quote is None:
        raise QuoteValidationError(f"Missing quote for {symbol}.")
    bid = _decimal(getattr(quote, "bid_price", None), f"{symbol} bid")
    ask = _decimal(getattr(quote, "ask_price", None), f"{symbol} ask")
    if bid < 0 or ask <= 0:
        raise QuoteValidationError(f"Invalid non-positive quote for {symbol}: bid={bid}, ask={ask}.")
    if bid > ask:
        raise QuoteValidationError(f"Crossed quote for {symbol}: bid={bid}, ask={ask}.")
    for size_name in ("bid_size", "ask_size"):
        size = getattr(quote, size_name, None)
        if size is not None and _decimal(size, f"{symbol} {size_name}") <= 0:
            raise QuoteValidationError(f"Quote for {symbol} has zero {size_name}.")
    quote_time = _quote_timestamp(getattr(quote, "timestamp", None), symbol)
    age = (datetime.now(timezone.utc) - quote_time).total_seconds()
    if age < -5 or age > max_age_seconds:
        raise QuoteValidationError(
            f"Stale quote for {symbol}: age={age:.1f}s, allowed={max_age_seconds}s."
        )
    return bid, ask


def _fetch_spread_quote_snapshot(short_symbol, long_symbol, max_age_seconds):
    _require_explicit_trading_mode()
    options_client = OptionHistoricalDataClient(
        api_key=os.environ.get("APCA_API_KEY_ID"),
        secret_key=os.environ.get("APCA_API_SECRET_KEY"),
    )
    response = options_client.get_option_latest_quote(
        OptionLatestQuoteRequest(symbol_or_symbols=[short_symbol, long_symbol])
    )
    short_bid, short_ask = _validated_quote(
        response.get(short_symbol), short_symbol, max_age_seconds
    )
    long_bid, long_ask = _validated_quote(
        response.get(long_symbol), long_symbol, max_age_seconds
    )
    return SpreadQuoteSnapshot(short_bid, short_ask, long_bid, long_ask)


def _fetch_single_quote_snapshot(symbol, max_age_seconds):
    _require_explicit_trading_mode()
    options_client = OptionHistoricalDataClient(
        api_key=os.environ.get("APCA_API_KEY_ID"),
        secret_key=os.environ.get("APCA_API_SECRET_KEY"),
    )
    response = options_client.get_option_latest_quote(
        OptionLatestQuoteRequest(symbol_or_symbols=[symbol])
    )
    bid, ask = _validated_quote(response.get(symbol), symbol, max_age_seconds)
    return SingleQuoteSnapshot(bid, ask)


def _client_order_id(prefix, operation, attempt_number):
    if not prefix:
        raise ValueError("client_order_id_prefix is required for every broker order")
    safe = "".join(character for character in str(prefix) if character.isalnum() or character in "-_")
    if not safe:
        raise ValueError("client_order_id_prefix must contain a letter or number")
    suffix = f"-{operation}-{attempt_number}"
    return f"{safe[:max(1, 48 - len(suffix))]}{suffix}"


def _chase_step(lower, upper):
    width = max(upper - lower, Decimal("0"))
    return max((width / Decimal("4")).quantize(CENT, rounding=ROUND_CEILING), CENT)


def _durable_attempt_callback(on_terminal, on_filled):
    return on_terminal if on_terminal is not None else on_filled


def _notify_terminal_attempt(on_terminal, on_filled, result):
    if not result.terminal:
        raise OrderStateUnresolvedError(
            f"Refusing to publish nonterminal order state for {result.order_id}."
        )
    if not _fill_price_ready(result):
        raise OrderExecutionError(
            f"Refusing to publish fill accounting for terminal order {result.order_id} "
            "without a valid average fill price."
        )

    # ``on_filled`` is retained as the legacy name. When no explicit terminal
    # callback is supplied it receives fill progress plus every terminal attempt,
    # including zero-fill cancellations and expirations.
    callback = _durable_attempt_callback(on_terminal, on_filled)
    if callback is not None:
        callback(result)

    # If callers deliberately supplied separate callbacks, preserve the legacy
    # fill-only notification without duplicating a shared callback.
    if (
        on_terminal is not None
        and on_filled is not None
        and on_filled is not on_terminal
        and result.filled_qty > 0
    ):
        on_filled(result)


def _notify_submitted_order(
    callback,
    order_state_callback,
    terminal_callback,
    fill_callback,
    client,
    order,
    context,
    expected_qty,
    cancel_timeout,
):
    if callback is None:
        return
    try:
        callback(order, context)
    except Exception as callback_error:
        # An order now exists but was not durably acknowledged. Stop its live
        # remainder before surfacing the persistence failure.
        try:
            final_state = wait_for_fill(
                client,
                order.id,
                timeout=0.01,
                interval=0.01,
                cancel_timeout=cancel_timeout,
                expected_qty=expected_qty,
                progress_callback=_durable_attempt_callback(
                    terminal_callback, fill_callback
                ),
                terminal_state_callback=order_state_callback,
            )
        except OrderStateUnresolvedError as cancel_error:
            raise OrderStateUnresolvedError(
                f"Order {getattr(order, 'id', 'unknown')} could not be recorded, and its "
                "inactive state could not be confirmed."
            ) from cancel_error
        print(
            f"{_client_mode_label(client)} order persistence callback failed after "
            f"submission; order_id={order.id}, "
            f"final_status={final_state.status}, filled={final_state.filled_qty}."
        )
        _notify_terminal_attempt(terminal_callback, fill_callback, final_state)
        raise callback_error


def _run_before_submit(callback, client, context):
    if callback is not None:
        # A buying-power/reconciliation guard belongs here, immediately before
        # each physical submission rather than once before the entire chase.
        callback(client, context)


def _enum_value(value):
    if value is None:
        return None
    return str(getattr(value, "value", value)).lower()


def _leg_signature(leg):
    ratio = getattr(leg, "ratio_qty", None)
    return (
        str(getattr(leg, "symbol", "") or ""),
        None if ratio is None else _decimal(ratio, "leg ratio quantity"),
        _enum_value(getattr(leg, "side", None)),
        _enum_value(getattr(leg, "position_intent", None)),
    )


def _recovered_order_mismatches(order, request, client_order_id):
    mismatches = []
    if str(getattr(order, "client_order_id", "")) != client_order_id:
        mismatches.append("client_order_id")
    if _int_quantity(getattr(order, "qty", None), "recovered order quantity") != _int_quantity(
        getattr(request, "qty", None), "requested order quantity"
    ):
        mismatches.append("qty")
    for field in ("order_class", "time_in_force", "type"):
        if _enum_value(getattr(order, field, None)) != _enum_value(
            getattr(request, field, None)
        ):
            mismatches.append(field)
    if _decimal(getattr(order, "limit_price", None), "recovered limit price") != _decimal(
        getattr(request, "limit_price", None), "requested limit price"
    ):
        mismatches.append("limit_price")

    requested_legs = getattr(request, "legs", None) or []
    if requested_legs:
        recovered_legs = getattr(order, "legs", None) or []
        if sorted(_leg_signature(leg) for leg in recovered_legs) != sorted(
            _leg_signature(leg) for leg in requested_legs
        ):
            mismatches.append("legs")
    else:
        for field in ("symbol", "side", "position_intent"):
            recovered_value = getattr(order, field, None)
            requested_value = getattr(request, field, None)
            if field != "symbol":
                recovered_value = _enum_value(recovered_value)
                requested_value = _enum_value(requested_value)
            if recovered_value != requested_value:
                mismatches.append(field)
    return mismatches


def _submit_order_idempotently(client, request, client_order_id):
    try:
        return client.submit_order(request)
    except Exception as submit_error:
        # A response can be lost after Alpaca accepted the order. Every exception
        # therefore requires a read-after-write lookup by the mandatory stable ID.
        try:
            existing = client.get_order_by_client_id(client_order_id)
        except Exception as lookup_error:
            message = str(submit_error).lower()
            duplicate_id_error = (
                "client_order_id" in message
                and ("duplicate" in message or "unique" in message or "already" in message)
            )
            status_code = getattr(submit_error, "status_code", None)
            reliable_rejection = (
                isinstance(submit_error, (ValueError, TypeError))
                or (
                    isinstance(submit_error, APIError)
                    and status_code in {400, 401, 403, 404, 422}
                    and not duplicate_id_error
                )
            )
            if reliable_rejection:
                raise submit_error
            raise OrderStateUnresolvedError(
                f"Submission outcome for client_order_id={client_order_id} is ambiguous; "
                f"follow-up lookup failed with {type(lookup_error).__name__}. "
                "Do not submit a replacement until reconciliation proves the order absent."
            ) from submit_error
        if existing is None or str(getattr(existing, "client_order_id", "")) != client_order_id:
            raise OrderStateUnresolvedError(
                f"Submission outcome for client_order_id={client_order_id} is ambiguous; "
                "the follow-up lookup did not return the matching order."
            ) from submit_error
        if getattr(request, "legs", None):
            try:
                existing = client.get_order_by_id(
                    existing.id, GetOrderByIdRequest(nested=True)
                )
            except Exception as nested_lookup_error:
                raise OrderStateUnresolvedError(
                    f"Submission outcome for client_order_id={client_order_id} is ambiguous; "
                    "the recovered multi-leg order could not be inspected."
                ) from nested_lookup_error
        try:
            mismatches = _recovered_order_mismatches(
                existing, request, client_order_id
            )
        except Exception as comparison_error:
            raise OrderStateUnresolvedError(
                f"Submission outcome for client_order_id={client_order_id} is ambiguous; "
                "the recovered order could not be compared with the request."
            ) from comparison_error
        if mismatches:
            raise OrderStateUnresolvedError(
                f"Submission outcome for client_order_id={client_order_id} is ambiguous; "
                "the recovered order differs from the request in: "
                f"{', '.join(mismatches)}."
            ) from submit_error
        print(
            f"Recovered {_client_mode_label(client)} order after submission exception "
            f"for client_order_id={client_order_id}; "
            f"broker_order_id={getattr(existing, 'id', 'unknown')}."
        )
        return existing


def init_alpaca_client():
    paper_mode = _require_explicit_trading_mode()
    client = TradingClient(
        os.environ.get("APCA_API_KEY_ID"),
        os.environ.get("APCA_API_SECRET_KEY"),
        paper=paper_mode,
    )
    client._eta_paper_mode = paper_mode
    print(f"Alpaca {_client_mode_label(client)} trading client initialized.")
    return client


def place_calendar_spread_order(
    short_symbol,
    long_symbol,
    original_intended_quantity,
    limit_price=None,
    on_filled=None,
    max_total_cost_allowed=None,
    target_debit_price=None,
    *,
    client_order_id_prefix,
    max_attempts=DEFAULT_MAX_ATTEMPTS,
    overall_timeout=DEFAULT_OVERALL_TIMEOUT,
    attempt_timeout=DEFAULT_ATTEMPT_TIMEOUT,
    cancel_timeout=DEFAULT_CANCEL_TIMEOUT,
    quote_max_age_seconds=DEFAULT_QUOTE_MAX_AGE_SECONDS,
    before_submit=None,
    on_submitted=None,
    on_terminal=None,
    on_order_state=None,
):
    """Open a calendar spread with a bounded, cancel-confirmed price chase.

    ``target_debit_price`` (or, for compatibility, ``limit_price``) is a hard
    net-spread debit ceiling. It is never replaced by either leg's standalone
    ask. ``on_terminal`` receives every newly observed cumulative fill and every
    safely inactive order snapshot, including zero-fill cancellations and
    expirations. ``on_order_state`` is the separate terminal-state-only path used
    even if a terminal fill has no publishable price. The legacy ``on_filled``
    parameter is used for fill accounting when ``on_terminal`` is omitted.
    """
    requested_qty = _int_quantity(original_intended_quantity, "requested quantity")
    if requested_qty < 1:
        raise ValueError("original_intended_quantity must be at least 1")
    if max_attempts < 1 or overall_timeout <= 0 or attempt_timeout <= 0 or cancel_timeout <= 0:
        raise ValueError("Order attempts and deadlines must be positive")
    ceiling_input = target_debit_price if target_debit_price is not None else limit_price
    if ceiling_input is None:
        raise ValueError("A target_debit_price hard ceiling is required for spread opening")
    hard_ceiling = _max_price_cent(ceiling_input)
    if hard_ceiling <= 0:
        raise ValueError("target_debit_price must be a positive net debit")
    starting_limit = _nearest_cent(limit_price if limit_price is not None else ceiling_input)
    maximum_budget = (
        None
        if max_total_cost_allowed is None
        else _decimal(max_total_cost_allowed, "maximum total cost")
    )
    if maximum_budget is not None and maximum_budget < 0:
        raise ValueError("max_total_cost_allowed cannot be negative")

    client = init_alpaca_client()
    attempts = []
    attempted_prices = set()
    previous_limit = None
    total_cost = Decimal("0")
    deadline = time.monotonic() + overall_timeout
    stop_reason = "max_attempts_reached"

    for attempt_number in range(1, max_attempts + 1):
        filled_so_far = sum(attempt.filled_qty for attempt in attempts)
        remaining_qty = requested_qty - filled_so_far
        if remaining_qty <= 0:
            stop_reason = "requested_quantity_filled"
            break
        seconds_left = deadline - time.monotonic()
        if seconds_left <= 0:
            stop_reason = "overall_deadline_reached"
            break

        # Quotes are intentionally fetched again for every replacement attempt.
        quotes = _fetch_spread_quote_snapshot(
            short_symbol, long_symbol, quote_max_age_seconds
        )
        net_bid = quotes.opening_net_bid
        net_ask = quotes.opening_net_ask
        if net_bid > net_ask or net_ask <= 0:
            raise QuoteValidationError(
                f"Invalid opening net market for {short_symbol}/{long_symbol}: "
                f"bid={net_bid}, ask={net_ask}."
            )
        midpoint = (net_bid + net_ask) / Decimal("2")
        proposed = (
            starting_limit
            if previous_limit is None
            else previous_limit + _chase_step(net_bid, net_ask)
        )
        upper_bound = min(net_ask, hard_ceiling)
        if upper_bound <= 0:
            stop_reason = "no_positive_debit_within_hard_limit"
            break
        lower_reference = min(max(net_bid, CENT), upper_bound)
        attempt_limit = _max_price_cent(
            max(lower_reference, min(proposed, midpoint if previous_limit is None else proposed, upper_bound))
        )
        if attempt_limit > hard_ceiling:
            raise OrderExecutionError("Opening price calculation exceeded its hard debit ceiling")
        if attempt_limit in attempted_prices:
            stop_reason = "hard_price_limit_reached"
            break

        qty_for_attempt = remaining_qty
        remaining_budget = None
        if maximum_budget is not None:
            remaining_budget = maximum_budget - total_cost
            contract_limit_cost = attempt_limit * CONTRACT_MULTIPLIER
            affordable = int(
                (remaining_budget / contract_limit_cost).to_integral_value(rounding=ROUND_FLOOR)
            ) if remaining_budget > 0 else 0
            qty_for_attempt = min(qty_for_attempt, affordable)
        if qty_for_attempt < 1:
            stop_reason = "insufficient_remaining_budget"
            break

        request_kwargs = {}
        attempt_client_order_id = _client_order_id(
            client_order_id_prefix, "open", attempt_number
        )
        if attempt_client_order_id:
            request_kwargs["client_order_id"] = attempt_client_order_id
        request = LimitOrderRequest(
            order_class=OrderClass.MLEG,
            time_in_force=TimeInForce.DAY,
            qty=qty_for_attempt,
            legs=[
                OptionLegRequest(
                    symbol=short_symbol,
                    ratio_qty=1,
                    side=OrderSide.SELL,
                    position_intent=PositionIntent.SELL_TO_OPEN,
                ),
                OptionLegRequest(
                    symbol=long_symbol,
                    ratio_qty=1,
                    side=OrderSide.BUY,
                    position_intent=PositionIntent.BUY_TO_OPEN,
                ),
            ],
            limit_price=float(attempt_limit),
            **request_kwargs,
        )
        _run_before_submit(
            before_submit,
            client,
            {
                "operation": "open_calendar_spread",
                "attempt_number": attempt_number,
                "quantity": qty_for_attempt,
                "limit_price": attempt_limit,
                "symbol": short_symbol,
                "short_symbol": short_symbol,
                "long_symbol": long_symbol,
            },
        )
        if time.monotonic() >= deadline:
            stop_reason = "overall_deadline_reached_before_submission"
            break
        submitted = _submit_order_idempotently(
            client, request, attempt_client_order_id
        )
        print(
            f"Submitted {_client_mode_label(client)} spread open attempt "
            f"{attempt_number}/{max_attempts}: "
            f"order_id={submitted.id}, qty={qty_for_attempt}, net_limit={attempt_limit}, "
            f"net_bid={net_bid}, net_ask={net_ask}, hard_ceiling={hard_ceiling}."
        )
        _notify_submitted_order(
            on_submitted,
            on_order_state,
            on_terminal,
            on_filled,
            client,
            submitted,
            {
                "operation": "open_calendar_spread",
                "attempt_number": attempt_number,
                "quantity": qty_for_attempt,
                "limit_price": attempt_limit,
                "symbol": short_symbol,
                "short_symbol": short_symbol,
                "long_symbol": long_symbol,
                "client_order_id": attempt_client_order_id,
            },
            qty_for_attempt,
            cancel_timeout,
        )
        result = wait_for_fill(
            client,
            submitted.id,
            timeout=min(
                attempt_timeout,
                max(deadline - time.monotonic(), 0.01),
            ),
            interval=1,
            cancel_timeout=cancel_timeout,
            expected_qty=qty_for_attempt,
            progress_callback=_durable_attempt_callback(on_terminal, on_filled),
            terminal_state_callback=on_order_state,
        )
        attempts.append(result)
        attempted_prices.add(attempt_limit)
        previous_limit = attempt_limit
        _notify_terminal_attempt(on_terminal, on_filled, result)

        if result.filled_qty:
            if result.average_fill_price is None:
                raise OrderExecutionError(
                    f"Order {result.order_id} filled without an average fill price"
                )
            if result.average_fill_price > hard_ceiling:
                raise OrderExecutionError(
                    f"Order {result.order_id} filled above hard debit ceiling: "
                    f"{result.average_fill_price} > {hard_ceiling}"
                )
            if result.average_fill_price > attempt_limit:
                raise OrderExecutionError(
                    f"Order {result.order_id} filled above its submitted debit limit: "
                    f"{result.average_fill_price} > {attempt_limit}"
                )
            total_cost += (
                max(result.average_fill_price, Decimal("0"))
                * result.filled_qty
                * CONTRACT_MULTIPLIER
            )
        if result.status == "rejected":
            reason = getattr(result.raw_order, "reject_reason", None) or "unspecified"
            raise OrderExecutionError(f"Opening order {result.order_id} was rejected: {reason}")
        if result.status not in {"filled", "canceled"} and result.remaining_qty:
            stop_reason = result.stop_reason
            break
    else:
        stop_reason = "max_attempts_reached"

    summary = _execution_result(
        "open_calendar_spread", short_symbol, requested_qty, attempts, stop_reason
    )
    if summary.fully_filled:
        summary = _execution_result(
            "open_calendar_spread",
            short_symbol,
            requested_qty,
            attempts,
            "requested_quantity_filled",
        )
    print(
        f"{_client_mode_label(client)} spread open finished: filled={summary.filled_qty}, "
        f"remaining={summary.remaining_qty}, reason={summary.stop_reason}."
    )
    return summary


def close_calendar_spread_order(
    short_symbol,
    long_symbol,
    quantity,
    limit_price=None,
    on_filled=None,
    *,
    client_order_id_prefix,
    max_close_debit=None,
    min_close_credit=None,
    max_attempts=DEFAULT_MAX_ATTEMPTS,
    overall_timeout=DEFAULT_OVERALL_TIMEOUT,
    attempt_timeout=DEFAULT_ATTEMPT_TIMEOUT,
    cancel_timeout=DEFAULT_CANCEL_TIMEOUT,
    quote_max_age_seconds=DEFAULT_QUOTE_MAX_AGE_SECONDS,
    before_submit=None,
    on_submitted=None,
    on_terminal=None,
    on_order_state=None,
):
    """Close a calendar spread using Alpaca's signed MLEG price convention.

    Exactly one hard price policy is required: ``max_close_debit`` is the most
    positive signed limit allowed, while ``min_close_credit`` becomes a negative
    signed ceiling. Quotes and the net spread are recalculated before every try.
    ``on_terminal`` receives cumulative fill progress and every safely inactive
    attempt; ``on_order_state`` persists terminal state independently of fill
    accounting. ``on_filled`` remains the backward-compatible fill callback alias.
    """
    requested_qty = _int_quantity(quantity, "requested quantity")
    if requested_qty < 1:
        raise ValueError("quantity must be at least 1")
    if (max_close_debit is None) == (min_close_credit is None):
        raise ValueError(
            "Provide exactly one of max_close_debit or min_close_credit as a hard close bound"
        )
    if max_attempts < 1 or overall_timeout <= 0 or attempt_timeout <= 0 or cancel_timeout <= 0:
        raise ValueError("Order attempts and deadlines must be positive")
    if max_close_debit is not None:
        hard_ceiling = _max_price_cent(max_close_debit)
        if hard_ceiling < 0:
            raise ValueError("max_close_debit cannot be negative")
    else:
        credit = _min_price_cent(min_close_credit)
        if credit < 0:
            raise ValueError("min_close_credit cannot be negative")
        hard_ceiling = -credit
    configured_start = None if limit_price is None else _nearest_cent(limit_price)

    client = init_alpaca_client()
    attempts = []
    attempted_prices = set()
    previous_limit = None
    deadline = time.monotonic() + overall_timeout
    stop_reason = "max_attempts_reached"

    for attempt_number in range(1, max_attempts + 1):
        filled_so_far = sum(attempt.filled_qty for attempt in attempts)
        remaining_qty = requested_qty - filled_so_far
        if remaining_qty <= 0:
            stop_reason = "requested_quantity_filled"
            break
        seconds_left = deadline - time.monotonic()
        if seconds_left <= 0:
            stop_reason = "overall_deadline_reached"
            break

        quotes = _fetch_spread_quote_snapshot(
            short_symbol, long_symbol, quote_max_age_seconds
        )
        signed_bid = quotes.closing_signed_bid
        signed_ask = quotes.closing_signed_ask
        if signed_bid > signed_ask:
            raise QuoteValidationError(
                f"Invalid closing net market for {short_symbol}/{long_symbol}: "
                f"signed_bid={signed_bid}, signed_ask={signed_ask}."
            )
        midpoint = (signed_bid + signed_ask) / Decimal("2")
        proposed = (
            configured_start if previous_limit is None and configured_start is not None
            else midpoint if previous_limit is None
            else previous_limit + _chase_step(signed_bid, signed_ask)
        )
        natural_upper_bound = min(signed_ask, hard_ceiling)
        lower_reference = min(signed_bid, natural_upper_bound)
        attempt_limit = _max_price_cent(
            max(lower_reference, min(proposed, natural_upper_bound))
        )
        if attempt_limit > hard_ceiling:
            raise OrderExecutionError("Closing price calculation exceeded its hard signed ceiling")
        if attempt_limit in attempted_prices:
            stop_reason = "hard_price_limit_reached"
            break

        request_kwargs = {}
        attempt_client_order_id = _client_order_id(
            client_order_id_prefix, "close", attempt_number
        )
        if attempt_client_order_id:
            request_kwargs["client_order_id"] = attempt_client_order_id
        request = LimitOrderRequest(
            order_class=OrderClass.MLEG,
            time_in_force=TimeInForce.DAY,
            qty=remaining_qty,
            legs=[
                OptionLegRequest(
                    symbol=short_symbol,
                    ratio_qty=1,
                    side=OrderSide.BUY,
                    position_intent=PositionIntent.BUY_TO_CLOSE,
                ),
                OptionLegRequest(
                    symbol=long_symbol,
                    ratio_qty=1,
                    side=OrderSide.SELL,
                    position_intent=PositionIntent.SELL_TO_CLOSE,
                ),
            ],
            limit_price=float(attempt_limit),
            **request_kwargs,
        )
        _run_before_submit(
            before_submit,
            client,
            {
                "operation": "close_calendar_spread",
                "attempt_number": attempt_number,
                "quantity": remaining_qty,
                "limit_price": attempt_limit,
                "symbol": short_symbol,
                "short_symbol": short_symbol,
                "long_symbol": long_symbol,
            },
        )
        if time.monotonic() >= deadline:
            stop_reason = "overall_deadline_reached_before_submission"
            break
        submitted = _submit_order_idempotently(
            client, request, attempt_client_order_id
        )
        print(
            f"Submitted {_client_mode_label(client)} spread close attempt "
            f"{attempt_number}/{max_attempts}: "
            f"order_id={submitted.id}, qty={remaining_qty}, signed_limit={attempt_limit}, "
            f"signed_bid={signed_bid}, signed_ask={signed_ask}, hard_ceiling={hard_ceiling}."
        )
        _notify_submitted_order(
            on_submitted,
            on_order_state,
            on_terminal,
            on_filled,
            client,
            submitted,
            {
                "operation": "close_calendar_spread",
                "attempt_number": attempt_number,
                "quantity": remaining_qty,
                "limit_price": attempt_limit,
                "symbol": short_symbol,
                "short_symbol": short_symbol,
                "long_symbol": long_symbol,
                "client_order_id": attempt_client_order_id,
            },
            remaining_qty,
            cancel_timeout,
        )
        result = wait_for_fill(
            client,
            submitted.id,
            timeout=min(
                attempt_timeout,
                max(deadline - time.monotonic(), 0.01),
            ),
            interval=1,
            cancel_timeout=cancel_timeout,
            expected_qty=remaining_qty,
            progress_callback=_durable_attempt_callback(on_terminal, on_filled),
            terminal_state_callback=on_order_state,
        )
        attempts.append(result)
        attempted_prices.add(attempt_limit)
        previous_limit = attempt_limit
        _notify_terminal_attempt(on_terminal, on_filled, result)

        if result.filled_qty:
            if result.average_fill_price is None:
                raise OrderExecutionError(
                    f"Order {result.order_id} filled without an average fill price"
                )
            if result.average_fill_price > hard_ceiling:
                raise OrderExecutionError(
                    f"Order {result.order_id} filled beyond hard signed close ceiling: "
                    f"{result.average_fill_price} > {hard_ceiling}"
                )
            if result.average_fill_price > attempt_limit:
                raise OrderExecutionError(
                    f"Order {result.order_id} filled beyond its submitted signed limit: "
                    f"{result.average_fill_price} > {attempt_limit}"
                )
        if result.status == "rejected":
            reason = getattr(result.raw_order, "reject_reason", None) or "unspecified"
            raise OrderExecutionError(f"Closing order {result.order_id} was rejected: {reason}")
        if result.status not in {"filled", "canceled"} and result.remaining_qty:
            stop_reason = result.stop_reason
            break
    else:
        stop_reason = "max_attempts_reached"

    summary = _execution_result(
        "close_calendar_spread", short_symbol, requested_qty, attempts, stop_reason
    )
    if summary.fully_filled:
        summary = _execution_result(
            "close_calendar_spread",
            short_symbol,
            requested_qty,
            attempts,
            "requested_quantity_filled",
        )
    print(
        f"{_client_mode_label(client)} spread close finished: filled={summary.filled_qty}, "
        f"remaining={summary.remaining_qty}, reason={summary.stop_reason}."
    )
    return summary


def get_open_option_positions():
    client = init_alpaca_client()
    positions = client.get_all_positions()
    option_positions = [
        position
        for position in positions
        if isinstance(position, Position)
        and str(getattr(getattr(position, "asset_class", ""), "value", getattr(position, "asset_class", ""))).lower()
        in {"option", "us_option"}
    ]
    print(
        f"Open {_client_mode_label(client)} option positions: {len(option_positions)}."
    )
    return option_positions


def get_portfolio_value():
    """Fetch the current portfolio/account equity value from Alpaca (in USD)."""
    client = init_alpaca_client()
    account = client.get_account()
    equity = float(account.equity)
    print(f"Current {_client_mode_label(client)} portfolio value (equity): ${equity}")
    return equity


def get_alpaca_option_chain(symbol):
    """
    Fetch the option chain for a given symbol using Alpaca's REST API.
    Returns a dict: {expiry: {strike: {call: {...}, put: {...}}}}
    """
    cache_key = str(symbol).upper()
    if cache_key in _OPTION_CHAIN_CACHE:
        return _OPTION_CHAIN_CACHE[cache_key]
    try:
        trading_client = init_alpaca_client()
        today = datetime.now().date()
        contracts = []
        page_token = None
        seen_page_tokens = set()
        while True:
            req = GetOptionContractsRequest(
                underlying_symbols=[symbol.upper()],
                expiration_date_gte=today,
                limit=10000,
                page_token=page_token,
            )
            response = trading_client.get_option_contracts(req)
            contracts.extend(response.option_contracts or [])
            next_page_token = getattr(response, "next_page_token", None)
            if not next_page_token:
                break
            if next_page_token in seen_page_tokens:
                raise RuntimeError("Alpaca option-contract pagination token repeated")
            seen_page_tokens.add(next_page_token)
            page_token = next_page_token
        # Organize by expiry and strike
        option_chain = {}
        for contract in contracts:
            expiry = contract.expiration_date.strftime('%Y-%m-%d')
            strike = float(contract.strike_price)
            cp = str(getattr(contract.type, "value", contract.type)).lower()
            if expiry not in option_chain:
                option_chain[expiry] = {}
            if strike not in option_chain[expiry]:
                option_chain[expiry][strike] = {}
            option_chain[expiry][strike][cp] = contract
        _OPTION_CHAIN_CACHE[cache_key] = option_chain
        return option_chain
    except Exception as e:
        print(f"Error fetching Alpaca option chain for {symbol}: {e}")
        return None


def select_expiries_and_strike_alpaca(symbol, earnings_date):
    """
    Use Alpaca's option chain to select front and back month expiries and ATM strike for the calendar spread.
    Returns (expiry_short, expiry_long, strike) or (None, None, None) if not found.
    """
    option_chain = get_alpaca_option_chain(symbol)
    if not option_chain:
        return None, None, None
    try:
        exp_dates = sorted([datetime.strptime(d, "%Y-%m-%d").date() for d in option_chain.keys()])
        # Find front month expiry (first after earnings)
        expiry_short = next((d for d in exp_dates if d > earnings_date), None)
        if not expiry_short:
            return None, None, None
        # Find back month expiry (closest to 30 days after front)
        target_back = expiry_short + timedelta(days=30)
        expiry_long = min((d for d in exp_dates if d > expiry_short), key=lambda d: abs((d - target_back).days), default=None)
        if not expiry_long:
            return None, None, None
        # Get ATM strike (closest to underlying price)
        # Fetch underlying price from Alpaca (latest bar)
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestBarRequest
        stock_client = StockHistoricalDataClient(API_KEY, API_SECRET)
        bar_resp = stock_client.get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols=symbol))
        if not bar_resp or symbol.upper() not in bar_resp:
            print(f"No price data for {symbol}")
            return None, None, None
        underlying_price = bar_resp[symbol.upper()].close
        strikes = list(option_chain[expiry_short.strftime('%Y-%m-%d')].keys())
        strike = min(strikes, key=lambda x: abs(x - underlying_price))
        return expiry_short.strftime('%Y-%m-%d'), expiry_long.strftime('%Y-%m-%d'), strike
    except Exception as e:
        print(f"Error selecting expiries/strike from Alpaca: {e}")
        return None, None, None


def get_option_spread_mid_price(symbol, expiry_short, expiry_long, strike, callput='C'):
    """
    Fetch the latest quotes for both legs and return the mid price for the calendar spread (long_mid - short_mid).
    Returns float or None if unavailable.
    """
    def make_option_symbol(symbol, expiry, strike, callput):
        expiry_fmt = expiry.replace('-', '')[2:]
        strike_fmt = f"{int(float(strike) * 1000):08d}"
        return f"{symbol.upper()}{expiry_fmt}{callput.upper()}{strike_fmt}"
    try:
        options_client = OptionHistoricalDataClient(
            api_key=os.environ.get("APCA_API_KEY_ID"),
            secret_key=os.environ.get("APCA_API_SECRET_KEY")
        )
        call_symbol_short = make_option_symbol(symbol, expiry_short, strike, 'C')
        call_symbol_long = make_option_symbol(symbol, expiry_long, strike, 'C')
        req = OptionLatestQuoteRequest(symbol_or_symbols=[call_symbol_short, call_symbol_long])
        quote_resp = options_client.get_option_latest_quote(req)
        quote_short = quote_resp.get(call_symbol_short)
        quote_long = quote_resp.get(call_symbol_long)
        if not quote_short or not quote_long:
            return None
        short_bid = quote_short.bid_price
        short_ask = quote_short.ask_price
        long_bid = quote_long.bid_price
        long_ask = quote_long.ask_price
        if None in (short_bid, short_ask, long_bid, long_ask):
            return None
        short_mid = (short_bid + short_ask) / 2
        long_mid = (long_bid + long_ask) / 2
        return float(long_mid - short_mid)
    except Exception as e:
        print(f"Error fetching Alpaca spread mid price: {e}")
        return None


def _raise_after_final_callback(callback, final_result, callback_error):
    try:
        callback(final_result)
    except Exception as final_callback_error:
        raise OrderExecutionError(
            f"Order {final_result.order_id} became terminal, but both the progress "
            "and final persistence callbacks failed."
        ) from final_callback_error
    raise callback_error


def _cancel_and_confirm_order(
    client,
    order_id,
    *,
    expected_qty,
    interval,
    cancel_timeout,
    timed_out,
    cancel_cause,
    progress_callback=None,
    terminal_state_callback=None,
    published_filled_qty=0,
    callback_error=None,
    prior_error=None,
):
    cancel_error = None
    try:
        client.cancel_order_by_id(order_id)
    except Exception as exc:
        # A failed cancellation request is not proof that an order remains active
        # or became terminal. Poll through the confirmation deadline either way.
        cancel_error = exc
        print(
            f"{_client_mode_label(client)} cancellation request raised "
            f"{type(exc).__name__} for order_id={order_id}; "
            "verifying final state."
        )

    cancel_deadline = time.monotonic() + cancel_timeout
    last_candidate = None
    last_poll_error = None
    while True:
        try:
            order = client.get_order_by_id(order_id)
            candidate = _lifecycle_result(
                order,
                expected_qty=expected_qty,
                timed_out=timed_out,
            )
            last_candidate = candidate
            last_poll_error = None
        except Exception as poll_error:
            last_poll_error = poll_error
            seconds_left = cancel_deadline - time.monotonic()
            if seconds_left <= 0:
                break
            time.sleep(min(interval, seconds_left))
            continue

        final_result = None
        if candidate.terminal:
            if cancel_cause == "deadline":
                if candidate.filled_qty == candidate.ordered_qty:
                    reason = "filled_during_cancellation_race"
                elif candidate.status == "canceled":
                    reason = "deadline_cancel_confirmed"
                else:
                    reason = f"deadline_order_{candidate.status}"
            elif candidate.filled_qty == candidate.ordered_qty:
                reason = "filled_during_progress_callback_failure"
            elif candidate.status == "canceled":
                reason = "progress_callback_failed_cancel_confirmed"
            else:
                reason = f"progress_callback_failed_order_{candidate.status}"
            final_result = _lifecycle_result(
                order,
                expected_qty=expected_qty,
                timed_out=timed_out,
                stop_reason=reason,
            )
            if not _fill_price_ready(final_result):
                final_result = _lifecycle_result(
                    order,
                    expected_qty=expected_qty,
                    timed_out=timed_out,
                    stop_reason=f"{reason}_fill_price_unavailable",
                )

        observed = final_result or candidate
        if final_result is not None and terminal_state_callback is not None:
            terminal_state_callback(final_result)
        if (
            callback_error is None
            and progress_callback is not None
            and observed.filled_qty > published_filled_qty
            and _fill_price_ready(observed)
        ):
            try:
                progress_callback(observed)
                published_filled_qty = observed.filled_qty
            except Exception as exc:
                callback_error = exc

        if final_result is not None:
            print(
                f"{_client_mode_label(client)} order inactive after cancellation: "
                f"order_id={order_id}, "
                f"status={final_result.status}, ordered={final_result.ordered_qty}, "
                f"filled={final_result.filled_qty}, canceled={final_result.canceled_qty}, "
                f"remaining={final_result.remaining_qty}."
            )
            if callback_error is not None:
                if not _fill_price_ready(final_result):
                    raise OrderExecutionError(
                        f"Order {order_id} became terminal with filled quantity "
                        f"{final_result.filled_qty}, but no valid average fill "
                        "price was available; fill accounting was not published."
                    ) from callback_error
                _raise_after_final_callback(
                    progress_callback, final_result, callback_error
                )
            if not _fill_price_ready(final_result):
                raise OrderExecutionError(
                    f"Order {order_id} became terminal with filled quantity "
                    f"{final_result.filled_qty}, but no valid average fill "
                    "price was available; fill accounting was not published."
                )
            return final_result, published_filled_qty

        seconds_left = cancel_deadline - time.monotonic()
        if seconds_left <= 0:
            break
        time.sleep(min(interval, seconds_left))

    if last_candidate is None:
        state = "latest broker state could not be read"
    else:
        state = (
            f"status={last_candidate.status}, ordered={last_candidate.ordered_qty}, "
            f"filled={last_candidate.filled_qty}, "
            f"active_remainder={last_candidate.active_qty}"
        )
    unresolved = OrderStateUnresolvedError(
        f"Order {order_id} remains unresolved after cancellation: {state}. "
        "No replacement is safe."
    )
    cause = callback_error or cancel_error or last_poll_error or prior_error
    if cause is not None:
        raise unresolved from cause
    raise unresolved


def wait_for_fill(
    client,
    order_id,
    timeout=30,
    interval=1,
    *,
    cancel_timeout=DEFAULT_CANCEL_TIMEOUT,
    expected_qty=None,
    progress_callback=None,
    terminal_state_callback=None,
):
    """Publish cumulative fill progress, then return only a terminal order state.

    Each newly observed positive cumulative fill is sent to ``progress_callback``
    only after a valid average price is available. ``terminal_state_callback``
    receives every confirmed terminal snapshot before fill accounting. A callback
    failure with an active remainder triggers cancellation and terminal confirmation,
    followed by one final callback attempt, before the failure is propagated. If
    inactivity cannot be proven, ``OrderStateUnresolvedError`` is raised so a caller
    cannot safely submit a replacement.
    """
    if timeout <= 0 or interval <= 0 or cancel_timeout <= 0:
        raise ValueError("timeout, interval, and cancel_timeout must be positive")
    order_id = str(order_id)
    deadline = time.monotonic() + timeout
    last_result = None
    observed_filled_qty = 0
    published_filled_qty = 0
    last_poll_error = None

    while True:
        try:
            order = client.get_order_by_id(order_id)
            last_result = _lifecycle_result(order, expected_qty=expected_qty)
            last_poll_error = None
        except Exception as poll_error:
            last_poll_error = poll_error
            seconds_left = deadline - time.monotonic()
            if seconds_left <= 0:
                break
            time.sleep(min(interval, seconds_left))
            continue
        if last_result.filled_qty < observed_filled_qty:
            raise OrderExecutionError(
                f"Broker cumulative fill moved backwards for order {order_id}: "
                f"{last_result.filled_qty} < {observed_filled_qty}."
            )
        observed_filled_qty = last_result.filled_qty

        if last_result.terminal and not _fill_price_ready(last_result):
            last_result = _lifecycle_result(
                order,
                expected_qty=expected_qty,
                stop_reason="terminal_fill_price_unavailable",
            )
        if last_result.terminal and terminal_state_callback is not None:
            terminal_state_callback(last_result)

        if (
            progress_callback is not None
            and last_result.filled_qty > published_filled_qty
            and _fill_price_ready(last_result)
        ):
            try:
                progress_callback(last_result)
                published_filled_qty = last_result.filled_qty
            except Exception as callback_error:
                if last_result.terminal:
                    _raise_after_final_callback(
                        progress_callback, last_result, callback_error
                    )
                _cancel_and_confirm_order(
                    client,
                    order_id,
                    expected_qty=expected_qty,
                    interval=interval,
                    cancel_timeout=cancel_timeout,
                    timed_out=False,
                    cancel_cause="progress_callback_failure",
                    progress_callback=progress_callback,
                    terminal_state_callback=terminal_state_callback,
                    published_filled_qty=published_filled_qty,
                    callback_error=callback_error,
                )
                raise AssertionError("unreachable")

        if last_result.terminal:
            if not _fill_price_ready(last_result):
                raise OrderExecutionError(
                    f"Order {order_id} became terminal with filled quantity "
                    f"{last_result.filled_qty}, but no valid average fill "
                    "price was available; fill accounting was not published."
                )
            print(
                f"{_client_mode_label(client)} order reached terminal state: "
                f"order_id={order_id}, "
                f"status={last_result.status}, ordered={last_result.ordered_qty}, "
                f"filled={last_result.filled_qty}, remaining={last_result.remaining_qty}."
            )
            return last_result
        seconds_left = deadline - time.monotonic()
        if seconds_left <= 0:
            break
        time.sleep(min(interval, seconds_left))

    print(
        f"{_client_mode_label(client)} order deadline reached; requesting cancellation: "
        f"order_id={order_id}, "
        f"status={last_result.status if last_result else 'unknown'}, "
        f"filled={last_result.filled_qty if last_result else 'unknown'}."
    )
    result, _ = _cancel_and_confirm_order(
        client,
        order_id,
        expected_qty=expected_qty,
        interval=interval,
        cancel_timeout=cancel_timeout,
        timed_out=True,
        cancel_cause="deadline",
        progress_callback=progress_callback,
        terminal_state_callback=terminal_state_callback,
        published_filled_qty=published_filled_qty,
        prior_error=last_poll_error,
    )
    return result


def monitor_fill_async(
    client, order, on_filled, timeout=30, interval=1, *, on_order_state=None
):
    """
    Compatibility helper for legacy callers.

    New order functions already wait synchronously and return terminal structured
    results. ``on_order_state`` is the terminal-state-only callback; the legacy
    ``on_filled`` callback receives priced fills and zero-fill terminal results.
    Errors are attached to the returned thread as ``thread.error`` so a joining
    caller can fail its workflow instead of silently ignoring them.
    """
    def _poll():
        try:
            if isinstance(order, (ExecutionResult, OrderLifecycleResult)):
                filled = order
                if (
                    isinstance(filled, OrderLifecycleResult)
                    and filled.terminal
                    and on_order_state is not None
                ):
                    on_order_state(filled)
            else:
                filled = wait_for_fill(
                    client,
                    order.id,
                    timeout=timeout,
                    interval=interval,
                    terminal_state_callback=on_order_state,
                )
            if filled.filled_qty > 0 and not _fill_price_ready(filled):
                raise OrderExecutionError(
                    f"Order {filled.id} has positive filled quantity without a valid "
                    "average fill price; fill accounting was not published."
                )
            on_filled(filled)
        except Exception as e:
            threading.current_thread().error = e
            print(f"{_client_mode_label(client)} fill monitor error for {order.id}: {e}")
    # spawn and return the thread object for external join
    t = threading.Thread(target=_poll, daemon=True)
    t.error = None
    t.start()
    return t


def get_spread_quotes(
    short_symbol,
    long_symbol,
    max_age_seconds=DEFAULT_QUOTE_MAX_AGE_SECONDS,
):
    """
    Return validated, current bid and ask prices for both option legs.
    """
    quote = _fetch_spread_quote_snapshot(
        short_symbol, long_symbol, max_age_seconds
    )
    # Keep the historic tuple/float interface for non-order callers.
    return tuple(
        float(value)
        for value in (quote.short_bid, quote.short_ask, quote.long_bid, quote.long_ask)
    )


def get_single_option_quotes(
    symbol: str,
    max_age_seconds=DEFAULT_QUOTE_MAX_AGE_SECONDS,
):
    """
    Return bid and ask prices for a single option leg.
    Raises RuntimeError if quotes are not available.
    """
    quote = _fetch_single_quote_snapshot(symbol, max_age_seconds)
    return float(quote.bid), float(quote.ask)


def close_single_option_leg_order(
    symbol: str,
    quantity: int,
    position_intent: PositionIntent,
    on_filled=None,
    *,
    client_order_id_prefix,
    min_sell_price=None,
    max_buy_price=None,
    max_attempts=DEFAULT_MAX_ATTEMPTS,
    overall_timeout=DEFAULT_OVERALL_TIMEOUT,
    attempt_timeout=DEFAULT_ATTEMPT_TIMEOUT,
    cancel_timeout=DEFAULT_CANCEL_TIMEOUT,
    quote_max_age_seconds=DEFAULT_QUOTE_MAX_AGE_SECONDS,
    before_submit=None,
    on_submitted=None,
    on_terminal=None,
    on_order_state=None,
):
    """Close one option leg without overlapping partial-fill replacements.

    ``on_terminal`` receives cumulative fill progress and every safely inactive
    attempt; ``on_order_state`` persists terminal state independently of fill
    accounting. ``on_filled`` remains the backward-compatible fill callback alias.
    """
    requested_qty = _int_quantity(quantity, "requested quantity")
    if requested_qty < 1:
        raise ValueError("quantity must be at least 1")
    if max_attempts < 1 or overall_timeout <= 0 or attempt_timeout <= 0 or cancel_timeout <= 0:
        raise ValueError("Order attempts and deadlines must be positive")

    intent_value = str(getattr(position_intent, "value", position_intent)).lower()
    sell_to_close = intent_value == str(
        getattr(PositionIntent.SELL_TO_CLOSE, "value", PositionIntent.SELL_TO_CLOSE)
    ).lower()
    buy_to_close = intent_value == str(
        getattr(PositionIntent.BUY_TO_CLOSE, "value", PositionIntent.BUY_TO_CLOSE)
    ).lower()
    if not (sell_to_close or buy_to_close):
        raise ValueError(
            "position_intent must be SELL_TO_CLOSE for a long leg or BUY_TO_CLOSE for a short leg"
        )
    if sell_to_close and max_buy_price is not None:
        raise ValueError("max_buy_price does not apply to SELL_TO_CLOSE")
    if buy_to_close and min_sell_price is not None:
        raise ValueError("min_sell_price does not apply to BUY_TO_CLOSE")
    side = OrderSide.SELL if sell_to_close else OrderSide.BUY

    hard_floor = None if min_sell_price is None else _min_price_cent(min_sell_price)
    hard_ceiling = None if max_buy_price is None else _max_price_cent(max_buy_price)
    if hard_floor is not None and hard_floor < 0:
        raise ValueError("min_sell_price cannot be negative")
    if hard_ceiling is not None and hard_ceiling <= 0:
        raise ValueError("max_buy_price must be positive")

    client = init_alpaca_client()
    attempts = []
    attempted_prices = set()
    previous_limit = None
    deadline = time.monotonic() + overall_timeout
    stop_reason = "max_attempts_reached"

    for attempt_number in range(1, max_attempts + 1):
        filled_so_far = sum(attempt.filled_qty for attempt in attempts)
        remaining_qty = requested_qty - filled_so_far
        if remaining_qty <= 0:
            stop_reason = "requested_quantity_filled"
            break
        seconds_left = deadline - time.monotonic()
        if seconds_left <= 0:
            stop_reason = "overall_deadline_reached"
            break

        quote = _fetch_single_quote_snapshot(symbol, quote_max_age_seconds)
        step = _chase_step(quote.bid, quote.ask)
        if sell_to_close:
            if hard_floor is None:
                # Freeze the first executable bid as the safety floor. Later quote
                # movement can improve this floor but can never weaken it.
                hard_floor = _min_price_cent(quote.bid)
            proposed = quote.ask if previous_limit is None else previous_limit - step
            current_reference = max(quote.bid, hard_floor)
            attempt_limit = _min_price_cent(
                max(hard_floor, min(proposed, max(quote.ask, current_reference)))
            )
            if attempt_limit < hard_floor:
                raise OrderExecutionError("Single-leg sell price crossed its hard floor")
        else:
            if hard_ceiling is None:
                # Freeze the first executable ask as the safety ceiling.
                hard_ceiling = _max_price_cent(quote.ask)
            proposed = quote.bid if previous_limit is None else previous_limit + step
            current_reference = min(quote.ask, hard_ceiling)
            attempt_limit = _max_price_cent(
                min(hard_ceiling, max(proposed, min(quote.bid, current_reference)))
            )
            if attempt_limit > hard_ceiling:
                raise OrderExecutionError("Single-leg buy price crossed its hard ceiling")

        if attempt_limit in attempted_prices:
            stop_reason = "hard_price_limit_reached"
            break
        request_kwargs = {}
        attempt_client_order_id = _client_order_id(
            client_order_id_prefix, "single-close", attempt_number
        )
        if attempt_client_order_id:
            request_kwargs["client_order_id"] = attempt_client_order_id
        request = LimitOrderRequest(
            symbol=symbol,
            qty=remaining_qty,
            side=side,
            time_in_force=TimeInForce.DAY,
            limit_price=float(attempt_limit),
            order_class=OrderClass.SIMPLE,
            position_intent=position_intent,
            **request_kwargs,
        )
        _run_before_submit(
            before_submit,
            client,
            {
                "operation": "close_single_option_leg",
                "attempt_number": attempt_number,
                "quantity": remaining_qty,
                "limit_price": attempt_limit,
                "symbol": symbol,
            },
        )
        if time.monotonic() >= deadline:
            stop_reason = "overall_deadline_reached_before_submission"
            break
        submitted = _submit_order_idempotently(
            client, request, attempt_client_order_id
        )
        print(
            f"Submitted {_client_mode_label(client)} single-leg close attempt "
            f"{attempt_number}/{max_attempts}: "
            f"order_id={submitted.id}, symbol={symbol}, qty={remaining_qty}, "
            f"limit={attempt_limit}, bid={quote.bid}, ask={quote.ask}."
        )
        _notify_submitted_order(
            on_submitted,
            on_order_state,
            on_terminal,
            on_filled,
            client,
            submitted,
            {
                "operation": "close_single_option_leg",
                "attempt_number": attempt_number,
                "quantity": remaining_qty,
                "limit_price": attempt_limit,
                "symbol": symbol,
                "client_order_id": attempt_client_order_id,
            },
            remaining_qty,
            cancel_timeout,
        )
        result = wait_for_fill(
            client,
            submitted.id,
            timeout=min(
                attempt_timeout,
                max(deadline - time.monotonic(), 0.01),
            ),
            interval=1,
            cancel_timeout=cancel_timeout,
            expected_qty=remaining_qty,
            progress_callback=_durable_attempt_callback(on_terminal, on_filled),
            terminal_state_callback=on_order_state,
        )
        attempts.append(result)
        attempted_prices.add(attempt_limit)
        previous_limit = attempt_limit
        _notify_terminal_attempt(on_terminal, on_filled, result)

        if result.filled_qty:
            if result.average_fill_price is None:
                raise OrderExecutionError(
                    f"Order {result.order_id} filled without an average fill price"
                )
            if sell_to_close and result.average_fill_price < hard_floor:
                raise OrderExecutionError(
                    f"Order {result.order_id} filled below hard sell floor: "
                    f"{result.average_fill_price} < {hard_floor}"
                )
            if sell_to_close and result.average_fill_price < attempt_limit:
                raise OrderExecutionError(
                    f"Order {result.order_id} filled below its submitted sell limit: "
                    f"{result.average_fill_price} < {attempt_limit}"
                )
            if buy_to_close and result.average_fill_price > hard_ceiling:
                raise OrderExecutionError(
                    f"Order {result.order_id} filled above hard buy ceiling: "
                    f"{result.average_fill_price} > {hard_ceiling}"
                )
            if buy_to_close and result.average_fill_price > attempt_limit:
                raise OrderExecutionError(
                    f"Order {result.order_id} filled above its submitted buy limit: "
                    f"{result.average_fill_price} > {attempt_limit}"
                )
        if result.status == "rejected":
            reason = getattr(result.raw_order, "reject_reason", None) or "unspecified"
            raise OrderExecutionError(f"Single-leg order {result.order_id} was rejected: {reason}")
        if result.status not in {"filled", "canceled"} and result.remaining_qty:
            stop_reason = result.stop_reason
            break
    else:
        stop_reason = "max_attempts_reached"

    operation = "sell_long_option" if sell_to_close else "buy_short_option"
    summary = _execution_result(operation, symbol, requested_qty, attempts, stop_reason)
    if summary.fully_filled:
        summary = _execution_result(
            operation, symbol, requested_qty, attempts, "requested_quantity_filled"
        )
    print(
        f"{_client_mode_label(client)} single-leg close finished: symbol={symbol}, "
        f"filled={summary.filled_qty}, "
        f"remaining={summary.remaining_qty}, reason={summary.stop_reason}."
    )
    return summary
