"""Read and aggregate recorded usage without calling a model or estimating prices."""
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal, localcontext
import json
from pathlib import Path
import re

_ID = re.compile(r"[a-z][a-z0-9_-]{1,127}\Z")
_STAGES = {"brief", "research", "options", "communications", "review", "selection", "report"}


def _string(value, label, *, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value:
        raise ValueError(f"Invalid usage field: {label}")


def _integer(value, label, *, minimum=0, nullable=False):
    if nullable and value is None:
        return
    if type(value) is not int or value < minimum:
        raise ValueError(f"Invalid usage field: {label}")


def _validate(event):
    if not isinstance(event, dict):
        raise ValueError("Usage event must be an object")
    required = {"id", "run_id", "call_id", "attempt", "stage", "provider", "model", "tool",
                "started_at", "duration_ms", "input_tokens", "output_tokens", "cost",
                "cost_basis", "pricing_version", "outcome", "cached"}
    if not required <= event.keys():
        raise ValueError("Usage event is missing required fields")
    for name in ("id", "run_id", "call_id"):
        value = event[name]
        if not isinstance(value, str) or _ID.fullmatch(value) is None:
            raise ValueError(f"Invalid usage field: {name}")
    _string(event["provider"], "provider")
    for name in ("model", "tool", "pricing_version"):
        _string(event[name], name, nullable=True)
    _integer(event["attempt"], "attempt", minimum=1)
    _integer(event["duration_ms"], "duration_ms")
    for name in ("input_tokens", "output_tokens"):
        _integer(event[name], name, nullable=True)
    _string(event["stage"], "stage")
    if event["stage"] not in _STAGES:
        raise ValueError("Invalid usage field: stage")
    _string(event["outcome"], "outcome")
    if event["outcome"] not in {"success", "error"}:
        raise ValueError("Invalid usage field: outcome")
    if type(event["cached"]) is not bool:
        raise ValueError("Invalid usage field: cached")
    try:
        stamp = datetime.fromisoformat(event["started_at"])
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Invalid usage field: started_at") from None
    cost = event["cost"]
    basis = event["cost_basis"]
    _string(basis, "cost_basis")
    if basis not in {"reported", "estimated", "unknown"}:
        raise ValueError("Invalid usage field: cost_basis")
    if cost is None:
        if basis != "unknown":
            raise ValueError("Missing cost must use cost_basis=unknown")
    else:
        if basis == "unknown" or not isinstance(cost, dict):
            raise ValueError("Known cost must be reported or estimated")
        if set(cost) != {"amount", "currency"}:
            raise ValueError("Invalid usage field: cost")
        amount, currency = cost.get("amount"), cost.get("currency")
        if not isinstance(amount, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]{1,6})?", amount) is None:
            raise ValueError("Invalid usage field: cost.amount")
        if not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None:
            raise ValueError("Invalid usage field: cost.currency")
    # Validate optional metadata is still plain JSON, without exposing it on error.
    try:
        return json.dumps(event, sort_keys=True, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Usage metadata must be finite JSON") from None


def _add_decimal(left, right):
    # Default Decimal precision must not round large batches or large amounts.
    scale = min(left.as_tuple().exponent, right.as_tuple().exponent)
    precision = max(left.adjusted(), right.adjusted()) - scale + 2
    with localcontext() as context:
        context.prec = max(28, precision)
        return left + right


def _decimal_string(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _stats(events):
    result = {"events": len(events), "successes": 0, "errors": 0, "retries": 0,
              "cached_events": 0, "external_attempts": 0,
              "duration_ms": 0, "input_tokens": 0, "output_tokens": 0,
              "unknown_input_tokens": 0, "unknown_output_tokens": 0,
              "cost_by_currency": {}, "unknown_cost_events": 0, "cost_complete": True}
    costs = {}
    for event in events:
        result['cached_events'] += int(event['cached'])
        result['external_attempts'] += int(not event['cached'])
        result["successes" if event["outcome"] == "success" else "errors"] += 1
        result["retries"] += int(event["attempt"] > 1)
        result["duration_ms"] += event["duration_ms"]
        for name in ("input_tokens", "output_tokens"):
            if event[name] is None:
                result["unknown_" + name] += 1
            else:
                result[name] += event[name]
        if event["cost"] is None:
            result["unknown_cost_events"] += 1
            continue
        currency = event["cost"]["currency"]
        sums = costs.setdefault(currency, {"reported": Decimal(0), "estimated": Decimal(0)})
        basis = event["cost_basis"]
        sums[basis] = _add_decimal(sums[basis], Decimal(event["cost"]["amount"]))
    for currency, sums in sorted(costs.items()):
        result["cost_by_currency"][currency] = {
            "reported": _decimal_string(sums["reported"]),
            "estimated": _decimal_string(sums["estimated"]),
            "known_total": _decimal_string(_add_decimal(sums["reported"], sums["estimated"])),
        }
    result["cost_complete"] = result["unknown_cost_events"] == 0
    return result


def aggregate_usage(events: Iterable[dict], run_id: str | None = None) -> dict:
    """Deduplicate all input before filtering. Group costs without currency conversion.

    events_read/duplicate_events describe the whole input. totals/groups describe
    the selected run, or all runs when run_id is None. Conflicting IDs anywhere
    in the input fail even when that run is not selected.
    """
    if run_id is not None and (not isinstance(run_id, str) or _ID.fullmatch(run_id) is None):
        raise ValueError("Invalid run_id filter")
    seen = {}
    chosen = []
    events_read = duplicates = 0
    for event in events:
        events_read += 1
        canonical = _validate(event)
        previous = seen.get(event["id"])
        if previous is not None:
            if previous != canonical:
                raise ValueError("Conflicting records share one usage event id")
            duplicates += 1
            continue
        seen[event["id"]] = canonical
        if run_id is None or event["run_id"] == run_id:
            chosen.append(event)
    grouped = {}
    for event in chosen:
        key = (event["run_id"], event["provider"], event["model"], event["tool"])
        grouped.setdefault(key, []).append(event)
    groups = []
    for key in sorted(grouped, key=lambda key: tuple(value or "" for value in key)):
        groups.append(dict(zip(("run_id", "provider", "model", "tool"), key)) | _stats(grouped[key]))
    return {"run_id_filter": run_id, "events_read": events_read,
            "duplicate_events": duplicates, "totals": _stats(chosen), "groups": groups}


def _json_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("Non-finite JSON constant")


def _read_events(paths):
    for path in paths:
        path = Path(path)
        try:
            with path.open(encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line, object_pairs_hook=_json_pairs,
                                           parse_constant=_reject_constant)
                        _validate(event)
                    except (TypeError, ValueError, OverflowError):
                        raise ValueError(f"Invalid usage event at {path}:{line_number}") from None
                    yield event
        except (OSError, UnicodeError):
            raise ValueError(f"Cannot read usage file: {path}") from None


def summarize_usage_files(paths: Iterable[Path], run_id: str | None = None) -> dict:
    return aggregate_usage(_read_events(paths), run_id=run_id)
