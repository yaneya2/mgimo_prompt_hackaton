"""Platform-owned results; no model output or private diagnostics are interpolated."""
import hashlib

from app.storage.database import new_id, now
from app.storage.validation import empty_data

PARENT_STAGE = dict(research="brief", options="research", communications="options", review="communications",
                    selection="review", report="selection")


def result_for(request, *, outcome="success", data=None, artifacts=None, questions=None, error=None):
    return dict(schema_version="1.0.0", run_id=request["run_id"], project_id=request["project_id"],
                brief_version=request["brief"]["version"], stage=request["stage"], outcome=outcome,
                data=empty_data() if data is None else data, artifacts=artifacts or [], questions=questions or [],
                suggested_next_stage=None, usage_ids=[], error=error)


def artifact_for(request, summary, *, entities=None, namespace="stage", dependencies=True):
    identity = "artifact_" + hashlib.sha256((request["run_id"] + ":" + request["stage"] + ":" + namespace).encode()).hexdigest()[:32]
    version = max((item["version"] for item in request["input_artifacts"] if item["id"] == identity), default=0) + 1
    parents = [item for item in request["input_artifacts"] if item["freshness"] == "current"
               and item["stage"] == PARENT_STAGE.get(request["stage"])] if dependencies else []
    # Prefer the latest version of the stage's actual output over diagnostic artifacts.
    parent = parents[-1:] if parents else []
    return dict(id=identity, version=version, project_id=request["project_id"], run_id=request["run_id"],
                brief_version=request["brief"]["version"], schema_version="1.0.0", stage=request["stage"],
                prompt_version=request["prompt_version"], model_id=None, created_at=now(), freshness="current",
                depends_on=[dict(artifact_id=item["id"], version=item["version"]) for item in parent],
                entities=entities or [], summary=summary)


def error_for(run_id, code, message, *, retryable=False):
    return {"error": dict(code=code, message=message, retryable=retryable,
                          request_id=new_id("request"), run_id=run_id, field_errors=[])}
