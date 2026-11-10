"""Current snapshot integrity must not be confused with archived history."""
import copy
import json
from pathlib import Path

import pytest

from scripts.check_contracts import check_snapshot

EXAMPLES = Path(__file__).resolve().parents[1] / "contracts" / "examples"


def example(name="success"):
    return json.loads((EXAMPLES / f"{name}.json").read_text())


def with_history():
    snapshot = example()
    snapshot["project"]["current_brief_version"] = 2
    snapshot["brief"]["version"] = 2
    snapshot["run"]["brief_version"] = 2
    for collection in snapshot["data"].values():
        for item in collection:
            item["brief_version"] = 2
    snapshot["data"]["options"][0]["version"] = 2
    report = snapshot["artifacts"][0]
    old = copy.deepcopy(report)
    old.update(id="artifact_previous", freshness="stale", stage="options")
    report["brief_version"] = 2
    report["entities"][0]["version"] = 2
    snapshot["artifacts"].append(old)
    return snapshot


def test_ready_current_report_with_archived_entity_version():
    snapshot = with_history()
    # The old option version remains in the archive, not duplicated in DataBundle.
    assert snapshot["artifacts"][1]["entities"][0]["version"] == 1
    assert snapshot["data"]["options"][0]["version"] == 2
    check_snapshot(snapshot)


def test_historical_issue_does_not_block_current_context():
    snapshot = with_history()
    snapshot["artifacts"][1]["entities"].append({"kind": "issue", "id": "issue_old", "version": 1})
    check_snapshot(snapshot)


def test_stale_only_result_cannot_be_promoted_to_ready():
    snapshot = example("stale_result")
    snapshot["run"]["quality"] = "ready"
    with pytest.raises(AssertionError, match="current report"):
        check_snapshot(snapshot)


def test_ready_without_any_report_is_rejected():
    snapshot = example()
    snapshot["artifacts"][0]["stage"] = "review"
    with pytest.raises(AssertionError, match="current report"):
        check_snapshot(snapshot)


@pytest.mark.parametrize("mutation,expected", [
    ("brief", "Current artifact has old brief"),
    ("reference", "Unknown option"),
    ("reference_version", "Wrong reference version"),
    ("dependency", "depends on stale"),
    ("old_context", "old context"),
])
def test_current_integrity_is_not_weakened(mutation, expected):
    snapshot = with_history()
    current = snapshot["artifacts"][0]
    if mutation == "brief":
        current["brief_version"] = 1
    elif mutation == "reference":
        current["entities"][0]["id"] = "option_missing"
    elif mutation == "reference_version":
        current["entities"][0]["version"] = 1
    elif mutation == "dependency":
        current["depends_on"] = [{"artifact_id": "artifact_previous", "version": 1}]
    elif mutation == "old_context":
        snapshot["data"]["messages"][0]["brief_version"] = 1
    with pytest.raises(AssertionError, match=expected):
        check_snapshot(snapshot)


def test_current_blocked_message_remains_blocking():
    snapshot = example("unsupported_promise")
    snapshot["run"]["quality"] = "ready"
    with pytest.raises(AssertionError, match="blocked promise"):
        check_snapshot(snapshot)


@pytest.mark.parametrize("status", ["open", "accepted_limitation"])
def test_current_blocking_issue_remains_blocking(status):
    snapshot = example("conflicting_sources")
    snapshot["run"]["quality"] = "ready"
    snapshot["data"]["messages"][0]["status"] = "hypothesis_only"
    snapshot["data"]["issues"][0]["status"] = status
    with pytest.raises(AssertionError, match="blocking issue"):
        check_snapshot(snapshot)


@pytest.mark.parametrize("name", ["success", "needs_input", "conflicting_sources", "unsupported_promise", "search_error", "stale_result"])
def test_existing_snapshot_examples_still_pass(name):
    check_snapshot(example(name))
