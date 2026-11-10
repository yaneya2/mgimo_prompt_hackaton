import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from app.llm.summary import aggregate_usage, summarize_usage_files


def event(id="usage_first", **fields):
    value = dict(id=id, run_id="run_test", call_id="call_first", attempt=1,
                 stage="brief", provider="codex", model=None, tool=None,
                 started_at="2026-11-06T12:00:00+00:00", duration_ms=100,
                 input_tokens=10, output_tokens=3, cost=None, cost_basis="unknown",
                 pricing_version=None, outcome="success", cached=False)
    value.update(fields)
    return value


def cost(amount, currency="USD"):
    return {"amount": amount, "currency": currency}


def write(path, events):
    path.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")
    return path


def test_currency_totals_and_estimates_are_separate():
    events = [event("usage_1", cost=cost("0.1"), cost_basis="reported"),
              event("usage_2", cost=cost("0.2"), cost_basis="estimated"),
              event("usage_3", cost=cost("12.345678", "RUB"), cost_basis="reported")]
    total = aggregate_usage(events)["totals"]
    assert total["cost_by_currency"] == {
        "RUB": {"reported": "12.345678", "estimated": "0", "known_total": "12.345678"},
        "USD": {"reported": "0.1", "estimated": "0.2", "known_total": "0.3"},
    }
    assert total["cost_complete"] is True


def test_unknown_usage_is_explicit_and_not_free():
    total = aggregate_usage([event(input_tokens=None), event("usage_2", output_tokens=None)])["totals"]
    assert total["events"] == 2
    assert total["input_tokens"] == 10 and total["unknown_input_tokens"] == 1
    assert total["output_tokens"] == 3 and total["unknown_output_tokens"] == 1
    assert total["unknown_cost_events"] == 2 and total["cost_complete"] is False
    assert total["cost_by_currency"] == {}


def test_paid_errors_retries_and_grouping():
    events = [event("usage_1", provider="openrouter", model="test/model", outcome="error", cost=cost("0.1"), cost_basis="reported"),
              event("usage_2", provider="openrouter", model="test/model", attempt=2, cost=cost("0.2"), cost_basis="reported"),
              event("usage_3", provider="search", tool="web_search"),
              event("usage_4", run_id="run_other")]
    report = aggregate_usage(events)
    assert report["totals"]["events"] == 4
    assert report["totals"]["errors"] == 1 and report["totals"]["successes"] == 3
    assert report["totals"]["retries"] == 1
    assert len(report["groups"]) == 3
    assert report["totals"]["cost_by_currency"]["USD"]["known_total"] == "0.3"
    selected = aggregate_usage(events, run_id="run_other")
    assert selected["totals"]["events"] == 1
    assert selected["groups"][0]["model"] is None


def test_same_event_id_is_deduplicated_across_files(tmp_path):
    item = event(context={"prompt_version": "v1"})
    first = write(tmp_path / "a.jsonl", [item])
    second = write(tmp_path / "b.jsonl", [item])
    report = summarize_usage_files([first, second])
    assert report["events_read"] == 2 and report["duplicate_events"] == 1
    assert report["totals"]["events"] == 1


def test_conflict_is_not_hidden_by_run_filter():
    first = event(run_id="run_other")
    changed = copy.deepcopy(first)
    changed["input_tokens"] = 99
    with pytest.raises(ValueError, match="Conflicting"):
        aggregate_usage([first, changed], run_id="run_test")


def test_large_decimal_totals_are_exact():
    report = aggregate_usage([event("usage_1", cost=cost("9999999999999999999999999999.999999"), cost_basis="reported"),
                              event("usage_2", cost=cost("0.000001"), cost_basis="reported")])
    assert report["totals"]["cost_by_currency"]["USD"]["known_total"] == "10000000000000000000000000000"


@pytest.mark.parametrize("change", [
    {"input_tokens": -1}, {"output_tokens": True}, {"duration_ms": None}, {"attempt": 0},
    {"stage": "bad"}, {"stage": []}, {"outcome": "bad"}, {"outcome": {}},
    {"cost_basis": []}, {"cost_basis": "reported"},
    {"cost": cost("0.1")}, {"cost": cost("NaN"), "cost_basis": "reported"},
    {"cost": cost("1.0", "usd"), "cost_basis": "estimated"}, {"started_at": "2026-11-06T12:00:00"},
    {"run_id": ""}, {"model": []},
])
def test_malformed_event_is_rejected(change):
    with pytest.raises(ValueError):
        aggregate_usage([event(**change)])


@pytest.mark.parametrize("content", [
    '{"secret":"do-not-print"',
    '{"id":"one","id":"two","secret":"do-not-print"}',
    '{"secret":"do-not-print","value":NaN}',
    '["do-not-print"]',
])
def test_invalid_file_reports_path_and_line_without_contents(tmp_path, content):
    path = tmp_path / "broken.jsonl"
    path.write_text("\n" + content)
    with pytest.raises(ValueError) as caught:
        summarize_usage_files([path])
    assert f"{path}:2" in str(caught.value)
    assert "do-not-print" not in str(caught.value)


def test_missing_file_is_not_silently_zero(tmp_path):
    with pytest.raises(ValueError, match="Cannot read usage file"):
        summarize_usage_files([tmp_path / "missing.jsonl"])


def test_empty_input_has_explicit_zero_events():
    report = aggregate_usage([])
    assert report["totals"]["events"] == 0 and report["groups"] == []
    assert report["totals"]["unknown_cost_events"] == 0


def test_cli_filters_explicit_files(tmp_path):
    path = write(tmp_path / "usage.jsonl", [event(), event("usage_2", run_id="run_other")])
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run([sys.executable, "-E", "-m", "scripts.usage_summary", str(path), "--run-id", "run_other"],
                               cwd=root, capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["totals"]["events"] == 1


def test_cli_defaults_to_configured_data_folder(tmp_path, monkeypatch, capsys):
    from scripts.usage_summary import main
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    write(tmp_path / "usage.jsonl", [event()])
    assert main([]) == 0
    assert json.loads(capsys.readouterr().out)["totals"]["events"] == 1


def test_cli_missing_file_returns_failure(tmp_path, capsys):
    from scripts.usage_summary import main
    assert main([str(tmp_path / "missing.jsonl")]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Cannot read usage file" in captured.err
