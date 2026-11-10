"""Exclusive executor ownership and atomic step/checkpoint persistence, without a graph runner."""
import fcntl
import json
import os

from app.storage.database import encode, new_id, now
from app.storage.validation import (
    COLLECTIONS, Conflict, NotFound, WorkerBusy, require, validate, validate_snapshot,
)


class WorkerSession:
    def __init__(self, storage):
        self.storage = storage
        self.owner = new_id("worker")
        self.pid = os.getpid()
        self._stream = None
        self.recovered_run_ids = []

    def __enter__(self):
        if self.pid != os.getpid():
            raise Conflict("Worker session cannot be inherited by another process")
        if self._stream is not None:
            raise Conflict("Worker session is already open")
        stream = (self.storage.data_dir / "worker.lock").open("a+b")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            raise WorkerBusy("Another worker owns this data directory") from None
        except BaseException:
            stream.close()
            raise
        self._stream = stream
        try:
            self.recovered_run_ids = self._recover()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *args):
        # Do not unlock an inherited descriptor from a forked child.
        if self._stream is not None and self.pid == os.getpid():
            self._stream.close()
            self._stream = None

    def _check(self):
        if self._stream is None or self._stream.closed or self.pid != os.getpid():
            raise Conflict("Worker session is not owned by this process")

    def _recover(self):
        self._check()
        recovered = []
        with self.storage.transaction(write=True) as connection:
            rows = connection.execute("SELECT payload FROM runs WHERE status IN ('running','pause_requested')").fetchall()
            for row in rows:
                run = json.loads(row[0])
                run.update(status="paused", revision=run["revision"] + 1, updated_at=now())
                self.storage._write_run(connection, run)
                recovered.append(run["id"])
            connection.execute("UPDATE jobs SET state = 'interrupted', finished_at = ? WHERE state = 'running'", (now(),))
        return recovered

    def claim_run(self, run_id):
        self._check()
        with self.storage.transaction(write=True) as connection:
            run = self.storage._run(connection, run_id)
            if run["status"] != "queued":
                raise Conflict("Only a queued run can be claimed")
            if connection.execute("SELECT 1 FROM runs WHERE status IN ('running','pause_requested')").fetchone():
                raise Conflict("An active run already exists")
            run.update(status="running", revision=run["revision"] + 1,
                       attempt=run["attempt"] + 1, updated_at=now())
            self.storage._write_run(connection, run)
        return run

    def begin_step(self, run_id, *, expected_revision):
        self._check()
        with self.storage.transaction(write=True) as connection:
            run = self.storage._run(connection, run_id)
            if run["revision"] != expected_revision:
                raise Conflict("Run revision conflict")
            if run["status"] != "running":
                raise Conflict("Run must be running before starting a step")
            if connection.execute("SELECT 1 FROM jobs WHERE state = 'running'").fetchone():
                raise Conflict("A step is already executing")
            run.update(revision=run["revision"] + 1, updated_at=now())
            self.storage._write_run(connection, run)
            job = dict(id=new_id("job"), run_id=run_id, stage=run["stage"], state="running",
                       owner=self.owner, run_revision=run["revision"], base_checkpoint_id=run["checkpoint_id"],
                       created_at=now(), finished_at=None, checkpoint_id=None)
            connection.execute("""INSERT INTO jobs
                (id, run_id, stage, state, owner, run_revision, base_checkpoint_id, created_at, finished_at, checkpoint_id)
                VALUES (:id, :run_id, :stage, :state, :owner, :run_revision, :base_checkpoint_id, :created_at, :finished_at, :checkpoint_id)""", job)
        return job

    def pause_at_boundary(self, run_id):
        """Operational shutdown at a committed boundary, not the future user pause API."""
        self._check()
        with self.storage.transaction(write=True) as connection:
            run = self.storage._run(connection, run_id)
            if connection.execute("SELECT 1 FROM jobs WHERE run_id = ? AND state = 'running'", (run_id,)).fetchone():
                raise Conflict("Cannot stop in the middle of a step")
            if run["status"] in ("running", "pause_requested"):
                run.update(status="paused", revision=run["revision"] + 1, updated_at=now())
                self.storage._write_run(connection, run)
        return run

    def _merge_entities(self, connection, run, previous, incoming):
        merged = {}
        for kind, collection in COLLECTIONS.items():
            updates = incoming[collection]
            require(len({item["id"] for item in updates}) == len(updates), "Duplicate entity ID")
            current = {item["id"]: item for item in previous[collection]}
            for item in updates:
                require(item["project_id"] == run["project_id"], "Cross-project entity")
                require(item["brief_version"] == run["brief_version"], "New entity has old brief")
                key = (run["project_id"], kind, item["id"], item["version"])
                old = connection.execute("""SELECT payload FROM entities
                    WHERE project_id = ? AND kind = ? AND id = ? AND version = ?""", key).fetchone()
                if old:
                    if old[0] != encode(item):
                        raise Conflict("Entity versions are immutable")
                else:
                    latest = connection.execute("""SELECT COALESCE(MAX(version), 0) FROM entities
                        WHERE project_id = ? AND kind = ? AND id = ?""", key[:3]).fetchone()[0]
                    if item["version"] != latest + 1:
                        raise Conflict("Entity versions must be consecutive")
                    connection.execute("INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?)",
                                       (*key, item["brief_version"], encode(item)))
                if item["id"] in current and current[item["id"]]["version"] > item["version"]:
                    raise Conflict("Cannot replace current context with an older entity")
                current[item["id"]] = item
            merged[collection] = list(current.values())
        return merged

    def _merge_artifacts(self, connection, run, stage, previous, incoming, data):
        current = {(item["id"], item["version"]): item for item in previous}
        require(len({(item["id"], item["version"]) for item in incoming}) == len(incoming), "Duplicate artifact version")
        for artifact in incoming:
            require(artifact["project_id"] == run["project_id"] and artifact["run_id"] == run["id"], "Cross-run artifact")
            require(artifact["stage"] == stage, "Artifact belongs to another stage")
            require(artifact["brief_version"] == run["brief_version"], "New artifact has old brief")
            require(artifact["freshness"] == "current", "A new artifact must be current")
            key = (run["project_id"], artifact["id"], artifact["version"])
            old = connection.execute("SELECT payload FROM artifacts WHERE project_id = ? AND id = ? AND version = ?", key).fetchone()
            if old:
                if old[0] != encode(artifact):
                    raise Conflict("Artifact versions are immutable")
                existing = current.get(key[1:])
                if existing and existing["freshness"] == "stale":
                    raise Conflict("A stale artifact requires a new version")
            else:
                latest = connection.execute("SELECT COALESCE(MAX(version), 0) FROM artifacts WHERE project_id = ? AND id = ?", key[:2]).fetchone()[0]
                if artifact["version"] != latest + 1:
                    raise Conflict("Artifact versions must be consecutive")
                connection.execute("INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?)",
                                   (*key, run["id"], run["brief_version"], encode(artifact)))
            current[key[1:]] = artifact.copy()
        latest_versions = {}
        entity_versions = {(kind, item["id"]): item["version"]
                           for kind, collection in COLLECTIONS.items() for item in data[collection]}
        for artifact in current.values():
            latest_versions[artifact["id"]] = max(latest_versions.get(artifact["id"], 0), artifact["version"])
            # Historical references must resolve in the archive even when absent in the current bundle.
            for target in artifact["entities"]:
                found = connection.execute("""SELECT 1 FROM entities
                    WHERE project_id = ? AND kind = ? AND id = ? AND version = ?""",
                    (run["project_id"], target["kind"], target["id"], target["version"])).fetchone()
                require(found is not None, "Historical artifact reference not found")
        incoming_keys = {(item["id"], item["version"]) for item in incoming}
        changed = True
        while changed:
            changed = False
            for key, artifact in current.items():
                outdated = (artifact["version"] < latest_versions[artifact["id"]]
                            or any(entity_versions.get((target["kind"], target["id"])) != target["version"]
                                   for target in artifact["entities"])
                            or any(current.get((dep["artifact_id"], dep["version"]), {}).get("freshness") == "stale"
                                   for dep in artifact["depends_on"]))
                if outdated and artifact["freshness"] == "current":
                    require(key not in incoming_keys, "New artifact references obsolete context")
                    artifact["freshness"] = "stale"
                    changed = True
        return list(current.values())

    def commit_step(self, job_id, result, *, next_stage=None, status="running", quality="draft", graph_state=None):
        """Commit before beginning another step. The caller, not the agent, chooses routing."""
        self._check()
        validate("StageResult", result)
        graph_state = {} if graph_state is None else graph_state
        require(isinstance(graph_state, dict), "Graph state must be an object")
        command = encode(dict(result=result, next_stage=next_stage, status=status, quality=quality, graph_state=graph_state))
        with self.storage.transaction(write=True) as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise NotFound("Job not found")
            job = dict(row)
            if job["state"] == "completed":
                saved = connection.execute("SELECT payload, commit_json FROM checkpoints WHERE job_id = ?", (job_id,)).fetchone()
                if saved["commit_json"] != command:
                    raise Conflict("Job already committed with a different result")
                return json.loads(saved["payload"])
            if job["state"] != "running" or job["owner"] != self.owner:
                raise Conflict("Job is not owned by this worker")
            run = self.storage._run(connection, job["run_id"])
            if run["revision"] != job["run_revision"] or run["checkpoint_id"] != job["base_checkpoint_id"]:
                raise Conflict("Run changed while the step was executing")
            require(run["status"] in ("running", "pause_requested"), "Run is not active")
            require((result["run_id"], result["project_id"], result["brief_version"], result["stage"])
                    == (run["id"], run["project_id"], run["brief_version"], job["stage"]), "Stage result belongs to another context")
            allowed = {"success": {"running", "completed", "paused"}, "needs_input": {"waiting_for_input"},
                       "needs_revision": {"running", "incomplete"}, "incomplete": {"incomplete"}, "failed": {"failed"}}
            require(status in allowed[result["outcome"]], "Status disagrees with stage outcome")
            require(status != "paused" or run["status"] == "pause_requested", "Pause was not requested")
            require((next_stage is not None) == (status in ("running", "paused")), "Next stage disagrees with status")
            require(status != "completed" or job["stage"] == "report", "Only report can complete a run")
            require(all(question["stage"] == job["stage"] for question in result["questions"]), "Question belongs to another stage")
            require(len({question["id"] for question in result["questions"]}) == len(result["questions"]), "Duplicate question ID")
            if result["error"]:
                require(result["error"]["error"]["run_id"] == run["id"], "Error belongs to another run")
            for identity in result["usage_ids"]:
                event = connection.execute("SELECT payload FROM usage WHERE id = ? AND run_id = ?", (identity, run["id"])).fetchone()
                require(event is not None and json.loads(event[0])["stage"] == job["stage"], "Unregistered or cross-stage usage")
            snapshot = self.storage._snapshot(connection, run)
            data = self._merge_entities(connection, run, snapshot["data"], result["data"])
            artifacts = self._merge_artifacts(connection, run, job["stage"], snapshot["artifacts"], result["artifacts"], data)
            checkpoint_id = new_id("checkpoint")
            run.update(status=status, stage=next_stage or job["stage"], quality=quality,
                       revision=run["revision"] + 1, updated_at=now(), checkpoint_id=checkpoint_id, error=result["error"])
            snapshot.update(data=data, artifacts=artifacts, questions=result["questions"])
            validate_snapshot(snapshot)
            checkpoint = dict(storage_version=1, id=checkpoint_id, run_id=run["id"], job_id=job_id,
                              parent_id=job["base_checkpoint_id"], run_revision=run["revision"],
                              brief_version=run["brief_version"], stage=job["stage"], outcome=result["outcome"],
                              next_stage=next_stage, status=status, quality=quality, created_at=now(),
                              data=data, artifacts=artifacts, questions=result["questions"], graph_state=graph_state)
            payload, result_json = encode(checkpoint), encode(result)
            self.storage._materialize("results", result_json)
            self.storage._materialize("checkpoints", payload)
            connection.execute("INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?)",
                               (checkpoint_id, run["id"], job_id, payload, result_json, command))
            self.storage._write_run(connection, run)
            connection.execute("UPDATE jobs SET state = 'completed', finished_at = ?, checkpoint_id = ? WHERE id = ?",
                               (now(), checkpoint_id, job_id))
        return checkpoint
