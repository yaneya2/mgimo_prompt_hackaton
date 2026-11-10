"""Versioned, validated model/tool usage with optional explicit token tariffs."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker

from app.config import Settings
from app.llm.usage import UsageSink


def fingerprint(value) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def usage_validator():
    path = Path(__file__).resolve().parents[2] / "contracts" / "schema.json"
    definitions = json.loads(path.read_text(encoding="utf-8"))["$defs"]
    return Draft202012Validator({"$defs": definitions, "$ref": "#/$defs/Usage"}, format_checker=FormatChecker())


@dataclass(frozen=True)
class TokenPricing:
    """User-configured estimate, never a claim about subscription billing."""
    model: str
    input_per_million: Decimal
    output_per_million: Decimal
    currency: str = "USD"
    version: str | None = None

    def snapshot(self) -> dict:
        values = {"model": self.model, "input_per_million": str(self.input_per_million),
                  "output_per_million": str(self.output_per_million), "currency": self.currency}
        return {**values, "version": self.version or "sha256:" + fingerprint(values)}

    def estimate(self, model, input_tokens, output_tokens):
        if model != self.model or type(input_tokens) is not int or type(output_tokens) is not int:
            return None
        if input_tokens < 0 or output_tokens < 0:
            return None
        with localcontext() as ctx:
            ctx.prec = max(40, len(str(input_tokens)) + len(str(output_tokens)) + 40)
            amount = (Decimal(input_tokens) * self.input_per_million
                      + Decimal(output_tokens) * self.output_per_million) / Decimal(1_000_000)
            rounded = amount.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        return {"amount": format(rounded, "f"), "currency": self.currency}


def pricing_from_settings(settings: Settings) -> TokenPricing | None:
    incoming = settings.model_input_cost_per_million
    outgoing = settings.model_output_cost_per_million
    if incoming is None and outgoing is None:
        return None
    # Both rates and a known model are needed; never invent a default rate/model.
    if incoming is None or outgoing is None or not settings.model_name:
        raise ValueError("Token estimation requires APP_MODEL_NAME and both APP_MODEL_*_COST_PER_MILLION rates")
    return TokenPricing(settings.model_name, incoming, outgoing,
                        settings.model_cost_currency, settings.model_pricing_version)


def call_context(messages, schema, *, prompt_version=None, schema_version=None,
                 model_requested=None, parameters=None) -> dict:
    for value in (prompt_version, schema_version):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("Version labels must be nonempty strings")
    prompt_hash, schema_hash = fingerprint(messages), fingerprint(schema)
    return {"metadata_version": "1.0.0",
            "prompt_version": prompt_version or "sha256:" + prompt_hash,
            "schema_version": schema_version or "sha256:" + schema_hash,
            "prompt_hash": prompt_hash, "schema_hash": schema_hash,
            "model_requested": model_requested, "parameters": parameters or {}, "pricing": None}


class UsageRecorder:
    def __init__(self, sink: UsageSink, pricing: TokenPricing | None = None):
        self.sink = sink
        self.pricing = pricing

    def record_model(self, event: dict, context: dict, generation_id=None) -> str:
        event = deepcopy(event)
        event["context"] = deepcopy(context)
        event["generation_id"] = generation_id
        if self.pricing:
            event["context"]["pricing"] = self.pricing.snapshot()
            if event["cost"] is None:
                cost = self.pricing.estimate(event["model"], event["input_tokens"], event["output_tokens"])
                if cost is not None:
                    event.update(cost=cost, cost_basis="estimated", pricing_version=self.pricing.snapshot()["version"])
        usage_validator().validate(event)
        self.sink.record(event)
        return event["id"]

    def record_tool(self, *, run_id: str, stage: str, provider: str, tool: str,
                    duration_ms: int, outcome="success", cost=None, cost_basis=None,
                    pricing_version=None, attempt=1, started_at=None, event_id=None, call_id=None,
                    cached=False) -> dict:
        """Record a completed tool attempt without storing its query or response."""
        if type(duration_ms) is not int or duration_ms < 0:
            raise ValueError("duration_ms must be a nonnegative integer")
        if started_at is None:
            started_at = (datetime.now(timezone.utc) - timedelta(milliseconds=duration_ms)).isoformat()
        event = dict(id=event_id or "usage_" + uuid4().hex, run_id=run_id,
                     call_id=call_id or "call_" + uuid4().hex, attempt=attempt, stage=stage,
                     provider=provider, model=None, tool=tool, started_at=started_at,
                     duration_ms=duration_ms, input_tokens=None, output_tokens=None,
                     cost=cost, cost_basis=cost_basis or ("reported" if cost is not None else "unknown"),
                     pricing_version=pricing_version, outcome=outcome, cached=cached)
        usage_validator().validate(event)
        if (cost is None) != (event["cost_basis"] == "unknown"):
            raise ValueError("cost and cost_basis disagree")
        self.sink.record(event)
        return event
