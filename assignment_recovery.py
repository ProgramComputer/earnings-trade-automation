"""Validate Alpaca's assignment receipts; never infer settlement from an expiry."""

import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation


class AssignmentEvidenceError(ValueError):
    pass


def number(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise AssignmentEvidenceError("Assignment receipt has an invalid number") from exc
    if not result.is_finite():
        raise AssignmentEvidenceError("Assignment receipt has a non-finite number")
    return result


def integer(value):
    result = number(value)
    if result != result.to_integral_value():
        raise AssignmentEvidenceError("Assignment receipt has a fractional quantity")
    return int(result)


def activity_day(activity):
    try:
        return date.fromisoformat(str(activity.get("date") or activity.get("transaction_time"))[:10])
    except ValueError as exc:
        raise AssignmentEvidenceError("Assignment receipt has no valid date") from exc


def fetch_activities(client, after, account_fingerprint):
    """Read all option settlement activities from the already verified PAPER client.

    Activity pagination uses IDs, not order timestamps. Raw account IDs and the
    rest of the account activity history are neither returned nor persisted.
    """
    endpoint=getattr(client,"_base_url",None)
    if str(getattr(endpoint,"value",endpoint)).rstrip("/")!="https://paper-api.alpaca.markets":
        raise AssignmentEvidenceError("Assignment activities require the Alpaca PAPER endpoint")
    records = {}; token = None
    while True:
        params = {"activity_types": "OPASN,OPTRD,OPEXC,OPEXP", "after": after,
                  "direction": "asc", "page_size": 100}
        if token:
            params["page_token"] = token
        page = client.get("/account/activities", data=params)
        if not isinstance(page, list):
            raise AssignmentEvidenceError("Alpaca returned invalid option activity data")
        added = 0
        for raw in page:
            if not isinstance(raw, dict) or not str(raw.get("id") or "").strip():
                raise AssignmentEvidenceError("Alpaca activity has no stable ID")
            account_id = raw.get("account_id")
            if account_id and hashlib.sha256(f"alpaca-account-id:v1:{account_id}".encode()).hexdigest() != account_fingerprint:
                raise AssignmentEvidenceError("Option activity belongs to another account")
            record = {key: raw[key] for key in
                      ("id", "activity_type", "date", "transaction_time", "symbol", "qty", "price", "net_amount", "status")
                      if key in raw}
            key = str(record["id"])
            if key in records and records[key] != record:
                raise AssignmentEvidenceError("Alpaca changed an activity within its history pages")
            if key not in records:
                records[key] = record; added += 1
        if len(page) < 100:
            return list(records.values())
        next_token = str(page[-1]["id"])
        if not added or next_token == token:
            raise AssignmentEvidenceError("Option activity pagination stopped making progress")
        token = next_token


def match_call_assignment(trade, activities, short_contract, long_contract):
    """Accept only a complete, unambiguous standard-call assignment of this trade.

    Partial assignments, adjusted deliverables, exercises, or unrelated equity
    positions remain blocked. A contract's expiry alone is never evidence.
    """
    ticker = str(trade["Ticker"]).upper()
    short = str(trade["Short Symbol"]).upper()
    long = str(trade["Long Symbol"]).upper()
    parsed = re.fullmatch(r"([A-Z]+)(\d{6})C(\d{8})", short)
    back = re.fullmatch(r"([A-Z]+)(\d{6})C(\d{8})", long)
    if not parsed or not back or parsed[1] != ticker or back[1] != ticker or parsed[3] != back[3]:
        raise AssignmentEvidenceError("Assignment recovery requires a same-strike call calendar")
    strike = Decimal(parsed[3]) / 1000
    for symbol, contract in ((short, short_contract), (long, long_contract)):
        kind = getattr(getattr(contract, "type", None), "value", getattr(contract, "type", None))
        if (getattr(contract, "symbol", None) != symbol or str(kind) != "call"
                or str(getattr(contract, "underlying_symbol", "")).upper() != ticker
                or integer(getattr(contract, "size", None)) != 100
                or number(getattr(contract, "strike_price", None)) != strike):
            raise AssignmentEvidenceError("Broker contract does not confirm standard 100-share calls")
    opened = date.fromisoformat(trade["Open Date"])
    receipts = [a for a in activities if str(a.get("symbol", "")).upper() == short
                and a.get("activity_type") == "OPASN" and activity_day(a) >= opened]
    expected = int(trade["remaining_quantity"] or 0)
    if not receipts or sum(integer(a.get("qty")) for a in receipts) != expected or expected <= 0:
        raise AssignmentEvidenceError("No complete OPASN receipt matches the remaining short calls")
    for a in receipts:
        if a.get("status") != "executed" or integer(a.get("qty")) <= 0 or number(a.get("net_amount")) != 0:
            raise AssignmentEvidenceError("Assignment option receipt is not a confirmed removal")
    stock = []
    for day in sorted({activity_day(a) for a in receipts}):
        contracts = sum(integer(a["qty"]) for a in receipts if activity_day(a) == day)
        candidates = [a for a in activities if a.get("activity_type") == "OPTRD"
                      and str(a.get("symbol", "")).upper() == ticker and activity_day(a) == day
                      and number(a.get("price")) == strike and integer(a.get("qty")) < 0]
        if not candidates or sum(integer(a["qty"]) for a in candidates) != -contracts * 100:
            raise AssignmentEvidenceError("OPTRD share delivery is missing or ambiguous")
        for a in candidates:
            amount = number(a.get("net_amount"))
            if a.get("status") != "executed" or amount != -number(a["qty"]) * strike:
                raise AssignmentEvidenceError("Assignment share cash flow does not match strike and quantity")
        stock.extend(candidates)
    # Conflicting exercise/expiry events must be reviewed rather than guessed.
    if any(a.get("activity_type") in {"OPEXC", "OPEXP"} and a.get("symbol") in {short, long}
           and activity_day(a) >= opened for a in activities):
        raise AssignmentEvidenceError("Conflicting option settlement activities need review")
    return {
        "trade_id": trade["trade_id"], "assigned_contracts": expected,
        "stock_quantity": expected * 100,
        "proceeds_cents": int(sum(number(a["net_amount"]) for a in stock) * 100),
        "assignment_date": max(activity_day(a) for a in receipts).isoformat(),
        "activity_ids": json.dumps(sorted(str(a["id"]) for a in receipts + stock)),
    }
