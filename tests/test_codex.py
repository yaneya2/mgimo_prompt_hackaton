import json
from pathlib import Path
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator

from app.config import Settings
from app.llm import CodexAdapter, JsonlUsageSink, ModelError, create_model
from app.llm.codex import run_process
from app.llm.openrouter import OpenRouterAdapter

SCHEMA = {"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"],"additionalProperties":False}


@pytest.fixture
def setup(tmp_path):
    return Settings(_env_file=None, model_provider="codex", model_name=None, codex_command=sys.executable), JsonlUsageSink(tmp_path / "usage.jsonl")


def runner(command, prompt, timeout, cwd):
    assert command[-1] == "-"
    assert "--ignore-user-config" in command
    assert command[command.index("--sandbox")+1] == "read-only"
    assert "test input" in prompt
    assert str(Path.cwd()) != cwd
    schema = json.loads(Path(command[command.index("--output-schema")+1]).read_text())
    assert schema == SCHEMA
    Path(command[command.index("--output-last-message")+1]).write_text('{"answer":"ok"}')
    events=[{"type":"thread.started","thread_id":"thread-demo"}, {"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":4}}]
    return subprocess.CompletedProcess(command,0,"\n".join(json.dumps(e) for e in events),"")


def invoke(setup, run=runner):
    settings,sink=setup
    with CodexAdapter(settings,sink,runner=run) as adapter:
        return adapter.generate(messages=[{"role":"user","content":"test input"}],schema=SCHEMA,run_id="run_test",stage="brief")


def read_event(setup):
    return json.loads(setup[1].path.read_text())


def test_codex_success_and_usage(setup):
    result=invoke(setup)
    assert result.data == {"answer":"ok"}
    assert result.generation_id == "thread-demo"
    assert result.model is None
    event=read_event(setup)
    assert event["provider"] == "codex"
    assert event["input_tokens"] == 12
    assert event["cost"] is None and event["cost_basis"] == "unknown"
    assert result.usage_ids == [event["id"]]
    defs=json.loads((Path(__file__).resolve().parents[1]/"contracts/schema.json").read_text())["$defs"]
    Draft202012Validator({"$defs":defs,"$ref":"#/$defs/Usage"}).validate(event)


def test_explicit_model_argument_and_temp_cleanup(setup):
    setup[0].model_name="chosen-model"
    paths=[]
    def check(command,prompt,timeout,cwd):
        paths.append(cwd)
        assert command[command.index("--model")+1] == "chosen-model"
        return runner(command,prompt,timeout,cwd)
    assert invoke(setup,check).model == "chosen-model"
    assert not Path(paths[0]).exists()


@pytest.mark.parametrize("mode,code",[("exit","PROVIDER_ERROR"),("failed","PROVIDER_ERROR"),("incomplete","INCOMPLETE_RESPONSE"),("json","INVALID_RESPONSE"),("schema","INVALID_RESPONSE"),("timeout","TIMEOUT")])
def test_failures_are_accounted_without_retry(setup,mode,code):
    calls=[]
    def fail(command,prompt,timeout,cwd):
        calls.append(command)
        if mode=="timeout": raise subprocess.TimeoutExpired(command,timeout)
        result=runner(command,prompt,timeout,cwd)
        if mode=="exit": return subprocess.CompletedProcess(command,1,"","private diagnostic")
        if mode=="failed": result.stdout+='\n'+json.dumps({"type":"turn.failed","error":{"message":"private diagnostic"}})
        if mode=="incomplete": result.stdout=""
        if mode in {"json","schema"}:
            Path(command[command.index("--output-last-message")+1]).write_text("bad" if mode=="json" else '{"answer":42}')
        return result
    with pytest.raises(ModelError) as caught: invoke(setup,fail)
    assert caught.value.code==code
    assert "private diagnostic" not in str(caught.value)
    assert len(calls)==1
    assert read_event(setup)["outcome"]=="error"


def test_factory_switches_providers(setup):
    settings,sink=setup
    with create_model(settings,sink) as model: assert isinstance(model,CodexAdapter)
    settings.model_provider="openrouter"
    settings.model_name="test/model"
    from pydantic import SecretStr
    settings.model_api_key=SecretStr("fake")
    with create_model(settings,sink) as model: assert isinstance(model,OpenRouterAdapter)
    settings.model_provider="unknown"
    with pytest.raises(ModelError): create_model(settings,sink)


def test_missing_executable_is_configuration_error(setup):
    setup[0].codex_command="/nonexistent/strategy-codex"
    with pytest.raises(ModelError) as caught: create_model(*setup)
    assert caught.value.code=="CONFIGURATION_ERROR"
    assert not setup[1].path.exists()


def test_process_runner_stdin_and_secret_filter(tmp_path,monkeypatch):
    monkeypatch.setenv("APP_MODEL_API_KEY","private")
    result=run_process([sys.executable,"-c","import os,sys; assert 'APP_MODEL_API_KEY' not in os.environ; print(sys.stdin.read())"],"test input",5,str(tmp_path))
    assert result.returncode==0 and result.stdout.strip()=="test input"


def test_process_runner_timeout(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        run_process([sys.executable,"-c","import time; time.sleep(30)"],"",0.2,str(tmp_path))


def test_recovered_stream_error_does_not_discard_completed_answer(setup):
    def recovered(command, prompt, timeout, cwd):
        result = runner(command, prompt, timeout, cwd)
        intermediate = {"type": "error", "message": "Reconnecting..."}
        result.stdout = json.dumps(intermediate) + "\n" + result.stdout
        return result
    assert invoke(setup, recovered).data == {"answer": "ok"}
    assert read_event(setup)["outcome"] == "success"
    assert read_event(setup)["input_tokens"] == 12


@pytest.mark.parametrize("exit_code,terminal", [(0, None), (1, "turn.completed"), (0, "turn.failed")])
def test_stream_error_without_successful_terminal_outcome_still_fails(setup, exit_code, terminal):
    def failed(command, prompt, timeout, cwd):
        result = runner(command, prompt, timeout, cwd)
        events = [{"type": "error", "message": "private diagnostic"}]
        if terminal:
            events.append({"type": terminal})
        result.stdout = "\n".join(json.dumps(event) for event in events)
        result.returncode = exit_code
        return result
    with pytest.raises(ModelError) as error:
        invoke(setup, failed)
    assert error.value.code == "PROVIDER_ERROR"
    assert "private diagnostic" not in str(error.value)
    assert read_event(setup)["outcome"] == "error"
