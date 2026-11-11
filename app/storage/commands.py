"""User control and immutable exports; invoked inside Storage.command's transaction."""
from copy import deepcopy
import json

from app.storage.database import encode, new_id, now
from app.storage.validation import Conflict, NotFound, RunAlreadyActive, VersionConflict, empty_data, require, validate, validate_snapshot


class Commands:
    def __init__(self, storage, renderer):
        self.storage, self.renderer = storage, renderer

    @staticmethod
    def revision(run, expected):
        if run["revision"] != expected:
            raise VersionConflict("Run revision conflict")

    def pause(self, run_id, body):
        with self.storage.transaction(write=True) as db:
            run = self.storage._run(db, run_id)
            self.revision(run, body["expected_revision"])
            if run["status"] in ("paused", "pause_requested"):
                return run
            if run["status"] not in ("queued", "running", "waiting_for_input"):
                raise Conflict("This run cannot be paused")
            run.update(status="pause_requested" if run["status"] == "running" else "paused",
                       revision=run["revision"] + 1, updated_at=now())
            self.storage._write_run(db, run)
        return run

    def resume(self, run_id, body):
        with self.storage.transaction(write=True) as db:
            run = self.storage._run(db, run_id)
            self.revision(run, body["expected_revision"])
            if run["status"] not in ("paused", "failed", "incomplete"):
                raise Conflict("Resume requires a paused, retryable failed or incomplete run")
            if run["status"] == "failed" and not (run["error"] and run["error"]["error"]["retryable"]):
                raise Conflict("This failure is not retryable")
            project = self.storage._payload(db, "SELECT payload FROM projects WHERE id = ?", (run["project_id"],))
            if project["current_brief_version"] != run["brief_version"]:
                raise VersionConflict("A historical run cannot resume with an outdated brief")
            if db.execute("""SELECT 1 FROM runs WHERE project_id = ? AND id != ?
                AND status IN ('queued','running','pause_requested','paused','waiting_for_input')""",
                (run["project_id"], run_id)).fetchone():
                raise RunAlreadyActive("Project already has an unfinished run")
            run.update(status="queued", revision=run["revision"] + 1, updated_at=now(), error=None)
            self.storage._write_run(db, run)
        return run

    def revise_brief(self, project_id, body, *, control=None):
        with self.storage.transaction(write=True) as db:
            project = self.storage._payload(db, "SELECT payload FROM projects WHERE id = ?", (project_id,))
            if project["current_brief_version"] != body["expected_brief_version"]:
                raise VersionConflict("Brief version conflict")
            if db.execute("""SELECT 1 FROM runs WHERE project_id = ?
                AND status IN ('queued','running','pause_requested')""", (project_id,)).fetchone():
                raise Conflict("Pause the run before changing its brief")
            row = db.execute("SELECT payload FROM runs WHERE project_id = ? ORDER BY rowid DESC LIMIT 1", (project_id,)).fetchone()
            run = json.loads(row[0]) if row else None
            brief = dict(project_id=project_id, version=project["current_brief_version"] + 1,
                         created_at=now(), author="user", input=body["brief"])
            validate("BriefVersion", brief)
            db.execute("INSERT INTO briefs VALUES (?, ?, ?)", (project_id, brief["version"], encode(brief)))
            project["current_brief_version"] = brief["version"]
            db.execute("UPDATE projects SET payload = ? WHERE id = ?", (encode(project), project_id))
            if run and run["status"] != "completed":
                self._invalidate(db, run, brief, control or dict(action="revise_brief"))
        return brief

    def _invalidate(self, db, run, brief, control):
        from app.reuse import plan_reuse
        snapshot = self.storage._snapshot(db, run)
        reuse_plan = plan_reuse(db, snapshot, brief)
        artifacts = [dict(item, freshness="stale") for item in snapshot["artifacts"]]
        old = self.storage.get_checkpoint(run["checkpoint_id"]) if run["checkpoint_id"] else {}
        state = {key: value for key, value in old.get("graph_state", {}).items() if key in ("workflow_version", "registry")}
        state.update(repairs_used=0, feedback=["BRIEF_CHANGED"], stop_reason=None, control=control, reuse_plan=reuse_plan)
        checkpoint_id, job_id, stamp = new_id("checkpoint"), new_id("job"), now()
        parent = run["checkpoint_id"]
        run.update(brief_version=brief["version"], revision=run["revision"] + 1, status="paused",
                   stage="brief", quality="draft", checkpoint_id=checkpoint_id, updated_at=stamp, error=None)
        data = empty_data()
        checkpoint = dict(storage_version=1, id=checkpoint_id, run_id=run["id"], job_id=job_id, parent_id=parent,
                          run_revision=run["revision"], brief_version=brief["version"], stage="brief", outcome="success",
                          next_stage="brief", status="paused", quality="draft", created_at=stamp,
                          data=data, artifacts=artifacts, questions=[], graph_state=state)
        result = dict(schema_version="1.0.0", run_id=run["id"], project_id=run["project_id"], brief_version=brief["version"],
                      stage="brief", outcome="success", data=data, artifacts=[], questions=[],
                      suggested_next_stage=None, usage_ids=[], error=None)
        snapshot.update(brief=brief, data=data, artifacts=artifacts, questions=[])
        validate_snapshot(snapshot)
        db.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (job_id, run["id"], "brief", "completed", "user-control", run["revision"], parent, stamp, stamp, checkpoint_id))
        payload, result_json = encode(checkpoint), encode(result)
        self.storage._materialize("results", result_json)
        self.storage._materialize("checkpoints", payload)
        db.execute("INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?)",
                   (checkpoint_id, run["id"], job_id, payload, result_json, encode(control)))
        self.storage._write_run(db, run)

    def exclude_source(self, run_id, body):
        with self.storage.transaction(write=True) as db:
            run = self.storage._run(db, run_id)
            self.revision(run, body["expected_revision"])
            if run["status"] not in ("paused", "waiting_for_input", "failed", "incomplete"):
                raise Conflict("Source exclusion requires a stopped run")
            latest = db.execute("SELECT id FROM runs WHERE project_id = ? ORDER BY rowid DESC LIMIT 1", (run["project_id"],)).fetchone()
            if latest[0] != run_id:
                raise Conflict("Cannot change a historical run")
            snapshot = self.storage._snapshot(db, run)
            source = next((item for item in snapshot["data"]["sources"] if item["id"] == body["source_id"]), None)
            if source is None:
                raise NotFound("Source not found in the current context")
            if source["version"] != body["source_version"]:
                raise VersionConflict("Source version conflict")
            require(body["reason"].strip(), "An exclusion reason is required")
            brief = deepcopy(snapshot["brief"]["input"])
            brief["excluded_source_ids"] = list(dict.fromkeys(brief["excluded_source_ids"] + [source["id"]]))
            self.revise_brief(run["project_id"], dict(expected_brief_version=run["brief_version"], brief=brief),
                              control=dict(action="exclude_source", source_id=source["id"], source_version=source["version"], reason=body["reason"]))
            return self.storage._run(db, run_id)

    def create_export(self, project_id, body):
        with self.storage.transaction(write=True) as db:
            self.storage._payload(db, "SELECT payload FROM projects WHERE id = ?", (project_id,))
            run = self.storage._run(db, body["run_id"])
            if run["project_id"] != project_id:
                raise NotFound("Run not found in this project")
            self.revision(run, body["expected_revision"])
            if not body["allow_draft"] and run["quality"] != "ready":
                raise Conflict("Export requires ready quality or explicit allow_draft")
            snapshot = self.storage._snapshot(db, run)
            validate_snapshot(snapshot)
            # Keep only objects belonging to current artifacts, including transitive references.
            from app.storage.exports import current_snapshot
            current = current_snapshot(snapshot)
            validate_snapshot(current)
            rendered = self.renderer(current, body["format"])
            if not isinstance(rendered, str) or not rendered.strip():
                raise RuntimeError("Renderer returned no document")
            export_id = new_id("export")
            metadata = dict(id=export_id, run_id=run["id"], run_revision=run["revision"], brief_version=run["brief_version"],
                            quality=run["quality"], format=body["format"], download_url="/api/v1/exports/" + export_id + "/download")
            validate("Export", metadata)
            db.execute("INSERT INTO exports VALUES (?, ?, ?, ?)", (export_id, run["id"], encode(metadata), rendered))
        return metadata

    def download_export(self, export_id):
        with self.storage.transaction() as db:
            row = db.execute("SELECT payload, content FROM exports WHERE id = ?", (export_id,)).fetchone()
            if row is None:
                raise NotFound("Export not found")
            return json.loads(row["payload"]), row["content"]
