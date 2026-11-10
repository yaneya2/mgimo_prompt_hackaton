import json

import pytest

from app.llm.base import ModelError, strict_json, validate_request


def validator(schema):
    return validate_request([{"role": "user", "content": "test"}], schema, "run_test", "brief")


@pytest.mark.parametrize("number", ["1e309", "-1e309", "1.8e308", "NaN", "Infinity", "-Infinity"])
def test_strict_json_rejects_nonfinite_numbers(number):
    with pytest.raises(ValueError):
        strict_json('{"nested": [{"value": ' + number + '}]}')


def test_finite_numbers_remain_serializable():
    value = strict_json('{"values": [1e308, -1e308, 0.125, 42]}')
    assert value["values"] == [1e308, -1e308, 0.125, 42]
    json.dumps(value, allow_nan=False)


@pytest.mark.parametrize("name", ["$ref", "$id", "$dynamicRef", "properties", "items", "allOf"])
def test_schema_keyword_can_be_a_property_name(name):
    schema = {"type": "object", "properties": {name: {"type": "string"}},
              "required": [name], "additionalProperties": False}
    validator(schema).validate({name: "ordinary data"})


@pytest.mark.parametrize("keyword,value", [
    ("default", {"$ref": "https://example.com/ordinary-data", "$id": "data"}),
    ("examples", [{"$dynamicRef": "ordinary data"}]),
    ("const", {"$id": "ordinary data"}),
    ("enum", [{"$ref": "ordinary data"}]),
])
def test_schema_annotations_and_instance_values_are_not_schemas(keyword, value):
    schema = {"type": "object", "properties": {"metadata": {"type": "object", keyword: value}}}
    validator(schema)


def test_local_reference_with_escaped_property_name():
    schema = {"type": "object", "$defs": {"a/b~c": {"type": "string"}},
              "properties": {"$ref": {"$ref": "#/$defs/a~1b~0c"}}}
    validator(schema).validate({"$ref": "value"})


@pytest.mark.parametrize("reference", ["#/$defs/a%20b", "#/%24defs/a%20b"])
def test_percent_encoded_local_reference(reference):
    schema = {"type": "object", "$defs": {"a b": {"type": "string"}},
              "properties": {"value": {"$ref": reference}}}
    validator(schema).validate({"value": "text"})


@pytest.mark.parametrize("path", [
    {"properties": {"field": {"$ref": "https://example.com/schema"}}},
    {"$defs": {"item": {"$id": "https://example.com/resource"}}},
    {"allOf": [{"properties": {"field": {"$dynamicRef": "#item"}}}]},
    {"dependentSchemas": {"field": {"$ref": "https://example.com/schema"}}},
    {"properties": {"field": {"$ref": "#/$defs/missing"}}},
])
def test_actual_schema_references_still_rejected(path):
    with pytest.raises(ModelError) as error:
        validator({"type": "object", **path})
    assert error.value.code == "INVALID_SCHEMA"


def test_reference_target_is_checked_even_inside_annotation():
    schema = {"type": "object", "examples": [{"$ref": "https://example.com/schema"}],
              "properties": {"value": {"$ref": "#/examples/0"}}}
    with pytest.raises(ModelError) as error:
        validator(schema)
    assert error.value.code == "INVALID_SCHEMA"


def test_reference_to_invalid_schema_inside_annotation_is_rejected():
    schema = {"type": "object", "examples": [{"properties": 7}],
              "properties": {"value": {"$ref": "#/examples/0"}}}
    with pytest.raises(ModelError) as error:
        validator(schema)
    assert error.value.code == "INVALID_SCHEMA"


def test_recursive_local_schema_is_supported():
    schema = {"type": "object", "properties": {"child": {"$ref": "#"}}}
    validator(schema).validate({"child": {"child": {}}})
