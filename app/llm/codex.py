"""Local Codex CLI adapter using the user's existing authentication."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
from app.execution import before_external_action
import tempfile
import time
from datetime import datetime, timezone
from uuid import uuid4

from jsonschema.exceptions import ValidationError

from app.config import Settings
from app.llm.base import ModelError, ModelResult, strict_json, validate_request
from app.llm.usage import UsageSink
from app.llm.accounting import UsageRecorder, call_context, pricing_from_settings


def run_process(command, prompt, timeout, cwd):
    """No shell interpolation. Kill the process group on timeout on POSIX."""
    env = os.environ.copy()
    # The child needs its own login, never the alternate provider's secret.
    env.pop("APP_MODEL_API_KEY", None)
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding="utf-8",
                          cwd=cwd, env=env, start_new_session=(os.name == "posix")) as process:
        try:
            stdout, stderr = process.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


class CodexAdapter:
    def __init__(self, settings: Settings, sink: UsageSink, *, runner=run_process):
        executable = shutil.which(settings.codex_command)
        if executable is None:
            raise ModelError("CONFIGURATION_ERROR", "Codex CLI not found. Run locally with Codex installed, or set APP_CODEX_COMMAND")
        self.command = executable
        self.settings = settings
        self.sink = sink
        self.runner = runner
        try:
            self.recorder = UsageRecorder(sink, pricing_from_settings(settings))
        except ValueError as error:
            raise ModelError("CONFIGURATION_ERROR", str(error)) from None

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def generate(self, *, messages: list[dict], schema: dict, run_id: str, stage: str,
                 prompt_version: str | None = None, schema_version: str | None = None) -> ModelResult:
        validator = validate_request(messages, schema, run_id, stage)
        try:
            context = call_context(messages, schema, prompt_version=prompt_version, schema_version=schema_version,
                                   model_requested=self.settings.model_name,
                                   parameters={"timeout_seconds": self.settings.model_timeout_seconds,
                                               "max_tokens": None, "max_retries": 0})
        except (ValueError, TypeError):
            raise ModelError("INVALID_REQUEST", "Cannot encode call metadata or version labels") from None
        start = time.monotonic()
        stamp = datetime.now(timezone.utc).isoformat()
        error = None
        data, thread_id, token_usage = None, None, {}
        # CLI may not report the selected default model: keep it unknown.
        model = self.settings.model_name
        try:
            with tempfile.TemporaryDirectory(prefix="strategy-codex-") as directory:
                work = Path(directory)
                schema_file, output = work / "schema.json", work / "answer.json"
                schema_file.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
                command = [self.command, "exec", "--ignore-user-config", "--ephemeral",
                           "--skip-git-repo-check", "--sandbox", "read-only", "--json",
                           "--disable", "shell_tool", "--disable", "hooks", "--disable", "apps",
                           "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
                           "--output-schema", str(schema_file), "--output-last-message", str(output),
                           "--cd", str(work)]
                if model:
                    command += ["--model", model]
                command += ["-"]
                prompt = ("Act as a structured text model. Process the following ordered messages. "
                          "Use only their provided content; do not use tools or inspect files. "
                          "Return only the final JSON matching the supplied output schema.\n"
                          + json.dumps(messages, ensure_ascii=False))
                before_external_action(run_id)
                result = self.runner(command, prompt, self.settings.model_timeout_seconds, str(work))
                terminal = None
                stream_error = False
                for line in result.stdout.splitlines():
                    try:
                        event = strict_json(line)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("type") == "thread.started":
                        thread_id = event.get("thread_id")
                    elif event.get("type") == "turn.completed":
                        terminal = "completed"
                        token_usage = event.get("usage") or {}
                    elif event.get("type") == "turn.failed":
                        terminal = "failed"
                    elif event.get("type") == "error":
                        # Stream errors can be followed by a successful reconnect.
                        stream_error = True
                if result.returncode != 0 or terminal == "failed" or (stream_error and terminal != "completed"):
                    error = ModelError("PROVIDER_ERROR", "Codex could not complete the request. Check codex login status and CLI/model availability")
                elif terminal != "completed" or not output.is_file():
                    error = ModelError("INCOMPLETE_RESPONSE", "Codex did not produce a completed structured answer")
                else:
                    data = strict_json(output.read_text(encoding="utf-8"))
                    validator.validate(data)
        except subprocess.TimeoutExpired:
            error = ModelError("TIMEOUT", "Codex exceeded the deadline; the local process was stopped, remote usage may be unknown")
        except (FileNotFoundError, PermissionError):
            error = ModelError("CONFIGURATION_ERROR", "Cannot start Codex or access its temporary output")
        except OSError:
            error = ModelError("PROVIDER_ERROR", "Codex process or temporary storage failed")
        except (ValueError, TypeError, RecursionError, ValidationError):
            error = ModelError("INVALID_RESPONSE", "Codex output does not match the requested JSON schema")
        if not isinstance(token_usage, dict):
            token_usage = {}
        def count(name):
            value = token_usage.get(name)
            return value if type(value) is int and value >= 0 else None
        usage_id = "usage_" + uuid4().hex
        event = dict(id=usage_id, run_id=run_id, call_id="call_" + uuid4().hex,
                     attempt=1, stage=stage, provider="codex", model=model, tool=None,
                     started_at=stamp, duration_ms=max(0, int((time.monotonic()-start)*1000)),
                     input_tokens=count("input_tokens"), output_tokens=count("output_tokens"),
                     cost=None, cost_basis="unknown", pricing_version=None,
                     outcome="error" if error else "success", cached=False)
        try:
            self.recorder.record_model(event, context, thread_id if isinstance(thread_id, str) and thread_id else None)
        except OSError:
            raise ModelError("USAGE_RECORDING_ERROR", "Could not save Codex usage; do not retry automatically", usage_ids=[usage_id]) from None
        if error:
            error.usage_ids = [usage_id]
            raise error from None
        return ModelResult(data, model, thread_id if isinstance(thread_id, str) else None, [usage_id])
