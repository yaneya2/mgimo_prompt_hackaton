"""Contract and reference validation at the persistence boundary."""
import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

COLLECTIONS = {
    "capability": "capabilities", "source": "sources", "claim": "claims",
    "option": "options", "message": "messages", "experiment": "experiments",
    "decision": "decisions", "issue": "issues",
}


class StorageError(ValueError):
    """Safe error for the platform; payload contents are not included."""


class Conflict(StorageError):
    pass


class NotFound(StorageError):
    pass


class WorkerBusy(Conflict):
    pass


def require(condition, message):
    if not condition:
        raise StorageError(message)


@lru_cache(maxsize=None)
def validator(name):
    path = Path(__file__).resolve().parents[2] / "contracts" / "schema.json"
    definitions = json.loads(path.read_text(encoding="utf-8"))["$defs"]
    return Draft202012Validator({"$defs": definitions, "$ref": "#/$defs/" + name},
                               format_checker=FormatChecker())


def validate(name, value):
    # Do not expose a jsonschema exception containing private input text.
    if next(validator(name).iter_errors(value), None) is not None:
        raise StorageError("Invalid " + name)


def empty_data():
    return {name: [] for name in COLLECTIONS.values()}


def validate_snapshot(snapshot):
    validate("Snapshot", snapshot)
    project, run, brief = (snapshot[key] for key in ("project", "run", "brief"))
    require(run["project_id"] == brief["project_id"] == project["id"], "Cross-project snapshot")
    require(run["brief_version"] == brief["version"], "Wrong brief version")
    indexes = {}
    for kind, collection in COLLECTIONS.items():
        items = snapshot["data"][collection]
        indexes[kind] = {item["id"]: item for item in items}
        require(len(indexes[kind]) == len(items), "Duplicate entity ID")
        require(all(item["project_id"] == project["id"] for item in items), "Cross-project entity")

    def resolve(kind, identity, version=None):
        item = indexes[kind].get(identity)
        require(item is not None, "Unknown " + kind)
        require(version is None or item["version"] == version, "Wrong reference version")
        return item

    def evidence(value):
        source = resolve("source", value["source_id"], value["source_version"])
        require(source["verification"] == "read", "Unread evidence")
        require(value["quote"] in source["content_excerpt"], "Missing quote in saved excerpt")

    data = snapshot["data"]
    for claim in data["claims"]:
        for item in claim["evidence"]:
            evidence(item)
        if claim["kind"] == "external_fact" and claim["verification"] == "supported":
            require(any(item["relation"] == "supports" for item in claim["evidence"]), "Fact without support")
    for capability in data["capabilities"]:
        for identity in capability["source_ids"]:
            resolve("source", identity)
    for option in data["options"]:
        for identity in option["capability_ids"]:
            resolve("capability", identity)
        for identity in option["claim_ids"]:
            resolve("claim", identity)
    for message in data["messages"]:
        resolve("option", message["option_id"])
        for identity in message["claim_ids"]:
            resolve("claim", identity)
        for identity in message["capability_ids"]:
            capability = resolve("capability", identity)
            if message["status"] == "allowed":
                require(capability["status"] == "available", "Unavailable promise")
    for experiment in data["experiments"]:
        resolve("option", experiment["option_id"])
        for identity in experiment["message_ids"]:
            require(resolve("message", identity)["option_id"] == experiment["option_id"],
                    "Experiment message belongs to another option")
    for decision in data["decisions"]:
        resolve("option", decision["option_id"])
    for issue in data["issues"]:
        target = issue["target"]
        resolve(target["kind"], target["id"], target["version"])
        for item in issue["evidence"]:
            evidence(item)

    artifacts = {(item["id"], item["version"]): item for item in snapshot["artifacts"]}
    require(len(artifacts) == len(snapshot["artifacts"]), "Duplicate artifact version")
    visited = set()

    def visit(key, ancestors):
        require(key in artifacts, "Missing artifact dependency")
        require(key not in ancestors, "Cyclic artifacts")
        if key in visited:
            return
        for dependency in artifacts[key]["depends_on"]:
            visit((dependency["artifact_id"], dependency["version"]), ancestors | {key})
        visited.add(key)

    current = []
    for key, artifact in artifacts.items():
        visit(key, set())
        require(artifact["project_id"] == project["id"], "Cross-project artifact")
        if artifact["freshness"] == "stale":
            continue  # Storage separately resolves historical references from its archive.
        current.append(artifact)
        require(artifact["brief_version"] == run["brief_version"], "Current artifact has old brief")
        for dependency in artifact["depends_on"]:
            parent = artifacts[(dependency["artifact_id"], dependency["version"])]
            require(parent["freshness"] == "current", "Current artifact depends on stale data")
            require(parent["brief_version"] == run["brief_version"], "Current dependency has old brief")
        for target in artifact["entities"]:
            item = resolve(target["kind"], target["id"], target["version"])
            require(item["brief_version"] == run["brief_version"], "Current entity has old brief")
    if run["quality"] == "ready":
        require(any(item["stage"] == "report" for item in current), "Ready without a current report")
        require(all(item["brief_version"] == run["brief_version"]
                    for items in data.values() for item in items), "Ready bundle contains old context")
        require(not any(item["status"] == "blocked" for item in data["messages"]), "Ready with blocked promise")
        require(not any(item["severity"] == "blocking" and item["status"] != "resolved"
                        for item in data["issues"]), "Ready with blocking issue")
