import copy
import json
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator, FormatChecker

from app.config import Settings
from app.llm import JsonlUsageSink, ModelError, OpenRouterAdapter

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False}
SECRET = "private-test-key"


def response(content='{"answer":"ok"}', **kwargs):
    body = {"id": "gen-test", "model": "test/model", "choices": [{"finish_reason": "stop", "message": {"content": content}}], "usage": {"prompt_tokens": 10, "completion_tokens": 3, "cost": 0.000123}}
    body.update(kwargs)
    return httpx.Response(200, json=body)


@pytest.fixture
def setup(tmp_path):
    settings = Settings(_env_file=None, model_provider="openrouter", model_name="test/model", model_api_key=SECRET, model_max_retries=1)
    sink = JsonlUsageSink(tmp_path / "usage.jsonl")
    return settings, sink


def invoke(setup, handler, **kwargs):
    settings, sink = setup
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with OpenRouterAdapter(settings, sink, client=client, sleep=lambda _: None) as adapter:
            return adapter.generate(messages=[{"role": "user", "content": "test"}], schema=kwargs.get("schema", SCHEMA), run_id="run_test", stage="brief")


def events(setup):
    return [json.loads(line) for line in setup[1].path.read_text().splitlines()]


def test_success_and_contract_usage(setup):
    def handler(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer " + SECRET
        sent = json.loads(request.content)
        assert sent["response_format"]["json_schema"]["schema"] == SCHEMA
        assert sent["provider"]["require_parameters"] is True
        return response()
    result = invoke(setup, handler)
    assert result.data == {"answer": "ok"}
    assert result.generation_id == "gen-test"
    logged = events(setup)
    assert result.usage_ids == [logged[0]["id"]]
    assert logged[0]["cost"] == {"amount": "0.000123", "currency": "USD"}
    assert SECRET not in setup[1].path.read_text()
    defs = json.loads((Path(__file__).resolve().parents[1] / "contracts/schema.json").read_text())["$defs"]
    Draft202012Validator({"$defs": defs, "$ref": "#/$defs/Usage"}, format_checker=FormatChecker()).validate(logged[0])


@pytest.mark.parametrize("status,code", [(400,"INVALID_REQUEST"),(401,"AUTHENTICATION_ERROR"),(402,"INSUFFICIENT_CREDITS"),(403,"ACCESS_DENIED"),(404,"MODEL_NOT_FOUND")])
def test_permanent_errors_not_retried(setup,status,code):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(status,json={"error":{"message":SECRET}})
    with pytest.raises(ModelError) as caught:
        invoke(setup,handler)
    assert caught.value.code == code
    assert len(calls) == len(events(setup)) == 1
    assert SECRET not in str(caught.value)


def test_retry_is_bounded_and_accounts_for_each_attempt(setup):
    with pytest.raises(ModelError) as caught:
        invoke(setup,lambda _: httpx.Response(429, headers={"Retry-After":"0"}))
    assert caught.value.code == "RATE_LIMITED"
    records=events(setup)
    assert [e["attempt"] for e in records] == [1,2]
    assert len(set(e["call_id"] for e in records)) == 2
    assert caught.value.usage_ids == [e["id"] for e in records]
    assert all(e["cost"] is None for e in records)


def test_retry_then_success(setup):
    responses=iter([httpx.Response(503),response()])
    assert invoke(setup,lambda _:next(responses)).data == {"answer":"ok"}
    assert [e["outcome"] for e in events(setup)] == ["error","success"]


def test_long_retry_after_does_not_retry_early(setup):
    with pytest.raises(ModelError):
        invoke(setup,lambda _:httpx.Response(429,headers={"Retry-After":"60"}))
    assert len(events(setup)) == 1


@pytest.mark.parametrize("content", ['not json','{"answer":7}','{"answer":"a","extra":1}','{"answer":"a","answer":"b"}','{"answer":NaN}'])
def test_invalid_output_still_accounts_for_cost(setup,content):
    with pytest.raises(ModelError,match="schema") as caught:
        invoke(setup,lambda _:response(content))
    assert caught.value.code == "INVALID_RESPONSE"
    assert len(events(setup)) == 1
    assert events(setup)[0]["cost"] is not None
    assert events(setup)[0]["outcome"] == "error"


@pytest.mark.parametrize("finish,code",[("length","INCOMPLETE_RESPONSE"),("content_filter","MODEL_REFUSAL")])
def test_incomplete_or_refused(setup,finish,code):
    with pytest.raises(ModelError) as caught:
        invoke(setup,lambda _:response(choices=[{"finish_reason":finish,"message":{"content":"{}"}}]))
    assert caught.value.code == code


def test_timeout_no_automatic_duplicate(setup):
    def handler(request):
        raise httpx.ReadTimeout(SECRET,request=request)
    with pytest.raises(ModelError) as caught:
        invoke(setup,handler)
    assert caught.value.code == "TIMEOUT"
    assert not caught.value.retryable
    assert SECRET not in str(caught.value)
    assert len(events(setup)) == 1


def test_embedded_error(setup):
    with pytest.raises(ModelError) as caught:
        invoke(setup,lambda _:httpx.Response(200,json={"error":{"code":402,"message":SECRET}}))
    assert caught.value.code == "INSUFFICIENT_CREDITS"


def test_missing_usage_is_unknown(setup):
    invoke(setup,lambda _:response(usage=None))
    assert events(setup)[0]["cost_basis"] == "unknown"
    assert events(setup)[0]["input_tokens"] is None


def test_malformed_message_is_accounted(setup):
    with pytest.raises(ModelError) as caught:
        invoke(setup,lambda _:response(choices=[{"finish_reason":"stop","message":17}]))
    assert caught.value.code == "INVALID_RESPONSE"
    assert events(setup)[0]["cost"] is not None


def test_small_cost_marked_estimated(setup):
    invoke(setup,lambda _:response(usage={"cost":0.0000004}))
    assert events(setup)[0]["cost_basis"] == "estimated"


@pytest.mark.parametrize("reference",["https://example.com/schema", "#/$defs/missing"])
def test_invalid_refs_rejected_before_call(setup,reference):
    schema=copy.deepcopy(SCHEMA)
    schema["properties"]["answer"]={"$ref":reference}
    def handler(_):
        pytest.fail("must not call provider")
    with pytest.raises(ModelError) as caught:
        invoke(setup,handler,schema=schema)
    assert caught.value.code == "INVALID_SCHEMA"
    assert not setup[1].path.exists()


def test_missing_config(setup):
    with pytest.raises(ModelError) as caught:
        OpenRouterAdapter(Settings(_env_file=None,model_api_key=None,model_name=None),setup[1])
    assert caught.value.code == "CONFIGURATION_ERROR"


def test_sink_failure_not_retried(setup):
    setup[1].path.mkdir()
    with pytest.raises(ModelError) as caught:
        invoke(setup,lambda _:response())
    assert caught.value.code == "USAGE_RECORDING_ERROR"
