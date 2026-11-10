"""Provider-neutral model interface and validation."""
import json
import math
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import unquote
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

STAGES = {"brief", "research", "options", "communications", "review", "selection", "report"}


class ModelError(Exception):
    def __init__(self, code: str, message: str, *, retryable=False, usage_ids=()):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.usage_ids = list(usage_ids)


@dataclass(frozen=True)
class ModelResult:
    data: dict
    model: str | None
    generation_id: str | None
    usage_ids: list[str]


def strict_json(text):
    def reject_constant(value):
        raise ValueError("Non-finite JSON")
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Non-finite JSON number")
        return number
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    return json.loads(text, parse_constant=reject_constant, parse_float=finite_float,
                      object_pairs_hook=pairs)


def check_local_refs(value, root=None):
    """Visit schema positions, not property names or arbitrary instance data."""
    root = value if root is None else root
    visited = set()
    schema_maps = {"$defs", "definitions", "properties", "patternProperties", "dependentSchemas"}
    schema_arrays = {"allOf", "anyOf", "oneOf", "prefixItems"}
    schema_values = {"additionalProperties", "unevaluatedProperties", "propertyNames",
                     "items", "additionalItems", "unevaluatedItems", "contains",
                     "not", "if", "then", "else", "contentSchema"}

    def visit(node):
        if not isinstance(node, dict) or id(node) in visited:
            return
        visited.add(id(node))
        if "$id" in node or "$dynamicRef" in node:
            raise ModelError("INVALID_SCHEMA", "Nested resources and dynamic references are unsupported")
        if "$ref" in node:
            reference = node["$ref"]
            if not isinstance(reference, str) or not reference.startswith("#"):
                raise ModelError("INVALID_SCHEMA", "Only local JSON Pointer references are supported")
            target = root
            try:
                pointer = unquote(reference[1:], errors="strict")
                if pointer and not pointer.startswith("/"):
                    raise ValueError("Named anchors are unsupported")
                for part in pointer[1:].split("/") if pointer else []:
                    part = part.replace("~1", "/").replace("~0", "~")
                    if isinstance(target, list):
                        if not re.fullmatch(r"0|[1-9][0-9]*", part):
                            raise ValueError("Invalid array index")
                        target = target[int(part)]
                    else:
                        target = target[part]
                if not isinstance(target, (dict, bool)):
                    raise ValueError("Reference must point to a schema")
            except (KeyError, IndexError, TypeError, ValueError):
                raise ModelError("INVALID_SCHEMA", "Unresolved local schema reference") from None
            # A reference can make an otherwise annotation-only value a schema.
            Draft202012Validator.check_schema(target)
            visit(target)
        for keyword in schema_maps:
            for child in node.get(keyword, {}).values():
                visit(child)
        for keyword in schema_arrays:
            for child in node.get(keyword, []):
                visit(child)
        for keyword in schema_values:
            visit(node.get(keyword))
        # Legacy dependencies may contain either schemas or property-name lists.
        for child in node.get("dependencies", {}).values():
            visit(child)

    visit(value)


def validate_request(messages, schema, run_id, stage):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,127}", run_id) or stage not in STAGES:
        raise ModelError("INVALID_REQUEST", "Invalid run_id or stage")
    if not messages or any(not isinstance(m, dict) or set(m) != {"role", "content"} or not isinstance(m["role"], str) or m["role"] not in {"system", "user", "assistant"} or not isinstance(m["content"], str) or not m["content"] for m in messages):
        raise ModelError("INVALID_REQUEST", "Messages require a role and nonempty text content")
    if not isinstance(schema, dict):
        raise ModelError("INVALID_SCHEMA", "Schema must be an object")
    try:
        Draft202012Validator.check_schema(schema)
        if schema.get("type") != "object":
            raise ModelError("INVALID_SCHEMA", "The output schema must declare type=object")
        check_local_refs(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
    except SchemaError:
        raise ModelError("INVALID_SCHEMA", "Invalid JSON Schema") from None
    return validator


class ModelAdapter(Protocol):
    def generate(self, *, messages: list[dict], schema: dict, run_id: str, stage: str,
                 prompt_version: str | None = None, schema_version: str | None = None) -> ModelResult: ...
    def close(self) -> None: ...
    def __enter__(self) -> "ModelAdapter": ...
    def __exit__(self, *args) -> None: ...
