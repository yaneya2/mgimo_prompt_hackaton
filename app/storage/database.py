"""SQLite source of truth and reproducible JSON files for the local platform."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile
from uuid import uuid4

from app.storage.validation import (
    Conflict, NotFound, RunAlreadyActive, StorageError, VersionConflict, WorkerBusy, empty_data, require, validate,
)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def now():
    return datetime.now(timezone.utc).isoformat()


def new_id(kind):
    return kind + "_" + uuid4().hex


DDL = [
    "CREATE TABLE projects (id TEXT PRIMARY KEY, payload TEXT NOT NULL)",
    """CREATE TABLE briefs (
        project_id TEXT NOT NULL REFERENCES projects(id), version INTEGER NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY(project_id, version))""",
    """CREATE TABLE runs (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, brief_version INTEGER NOT NULL,
        status TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, limits_json TEXT NOT NULL,
        FOREIGN KEY(project_id, brief_version) REFERENCES briefs(project_id, version))""",
    """CREATE UNIQUE INDEX one_active_run ON runs((1))
        WHERE status IN ('running', 'pause_requested')""",
    """CREATE UNIQUE INDEX one_unfinished_run_per_project ON runs(project_id)
        WHERE status IN ('queued', 'running', 'pause_requested', 'paused', 'waiting_for_input')""",
    """CREATE TABLE jobs (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), stage TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('running', 'completed', 'interrupted')),
        owner TEXT NOT NULL, run_revision INTEGER NOT NULL, base_checkpoint_id TEXT,
        created_at TEXT NOT NULL, finished_at TEXT, checkpoint_id TEXT)""",
    "CREATE UNIQUE INDEX one_active_job ON jobs((1)) WHERE state = 'running'",
    """CREATE TABLE checkpoints (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id), payload TEXT NOT NULL,
        result_json TEXT NOT NULL, commit_json TEXT NOT NULL)""",
    """CREATE TABLE entities (
        project_id TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL,
        brief_version INTEGER NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(project_id, kind, id, version),
        FOREIGN KEY(project_id, brief_version) REFERENCES briefs(project_id, version))""",
    """CREATE TABLE artifacts (
        project_id TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL,
        run_id TEXT NOT NULL REFERENCES runs(id), brief_version INTEGER NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(project_id, id, version),
        FOREIGN KEY(project_id, brief_version) REFERENCES briefs(project_id, version))""",
    """CREATE TABLE usage (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        call_id TEXT NOT NULL, attempt INTEGER NOT NULL, payload TEXT NOT NULL,
        UNIQUE(run_id, call_id, attempt))""",
    "CREATE INDEX runs_by_project ON runs(project_id)",
    "CREATE INDEX jobs_by_run ON jobs(run_id)",
    "CREATE INDEX usage_by_run ON usage(run_id)",
]

INPUTS_DDL = """CREATE TABLE run_inputs (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
    checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id),
    kind TEXT NOT NULL CHECK(kind IN ('answers', 'selection')), payload TEXT NOT NULL,
    created_at TEXT NOT NULL, UNIQUE(run_id, checkpoint_id))"""

COMMAND_DDL = [
    """CREATE TABLE commands (
        scope TEXT NOT NULL, key TEXT NOT NULL, request_json TEXT NOT NULL,
        response_json TEXT NOT NULL, PRIMARY KEY(scope, key))""",
    """CREATE TABLE exports (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        payload TEXT NOT NULL, content TEXT NOT NULL)""",
]


class Storage:
    """Each operation owns its connection; reads use a consistent transaction."""

    requires_execution_control = True

    def __init__(self, data_dir: Path, *, readonly=False):
        self.data_dir = Path(data_dir).resolve()
        self.database_path = self.data_dir / "state.sqlite3"
        self.readonly = readonly
        self._command_connection = ContextVar("storage_command_connection", default=None)
        if readonly:
            if not self.database_path.is_file():
                raise NotFound('Existing database required for export')
            with self.transaction() as connection:
                require(connection.execute('PRAGMA user_version').fetchone()[0] == 4,
                        'Export requires schema 4; start the application to migrate first')
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for name in ("results", "checkpoints"):
            (self.data_dir / name).mkdir(exist_ok=True)
        with self.transaction(write=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4):
                raise StorageError("Unsupported storage schema version")
            if version == 0:
                for statement in DDL:
                    connection.execute(statement)
            if version < 2:
                connection.execute(INPUTS_DDL)
            if version < 3:
                for statement in COMMAND_DDL:
                    connection.execute(statement)
            if version < 4:
                from app.storage.runtime import RUNTIME_DDL
                for statement in RUNTIME_DDL:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 4")

    @contextmanager
    def transaction(self, *, write=False):
        require(not (write and self.readonly), 'Storage is read-only')
        # A command and its replay record commit together, even when it calls several operations.
        shared = self._command_connection.get()
        if shared is not None:
            yield shared
            return
        connection = sqlite3.connect(self.database_path.as_uri() + '?mode=ro' if self.readonly else self.database_path,
                                     uri=self.readonly, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @staticmethod
    def _payload(connection, sql, parameters):
        row = connection.execute(sql, parameters).fetchone()
        if row is None:
            raise NotFound("Stored object not found")
        return json.loads(row["payload"])

    def _run(self, connection, run_id):
        return self._payload(connection, "SELECT payload FROM runs WHERE id = ?", (run_id,))

    @staticmethod
    def _write_run(connection, run):
        validate("Run", run)
        connection.execute("UPDATE runs SET status = ?, revision = ?, brief_version = ?, payload = ? WHERE id = ?",
                           (run["status"], run["revision"], run["brief_version"], encode(run), run["id"]))
        from app.storage.runtime import event
        event(connection, run['id'], run['stage'], 'run_status', run['status'])

    def create_project(self, name, brief_input, *, project_id=None):
        stamp = now()
        project = dict(id=project_id or new_id("project"), name=name, created_at=stamp, current_brief_version=1)
        brief = dict(project_id=project["id"], version=1, created_at=stamp, author="user", input=brief_input)
        validate("Project", project)
        validate("BriefVersion", brief)
        with self.transaction(write=True) as connection:
            if connection.execute("SELECT 1 FROM projects WHERE id = ?", (project["id"],)).fetchone():
                raise Conflict("Project already exists")
            connection.execute("INSERT INTO projects VALUES (?, ?)", (project["id"], encode(project)))
            connection.execute("INSERT INTO briefs VALUES (?, ?, ?)", (project["id"], 1, encode(brief)))
        return project

    def list_projects(self):
        with self.transaction() as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT payload FROM projects ORDER BY rowid")]

    def get_brief(self, project_id, version=None):
        with self.transaction() as connection:
            if version is None:
                version = self._payload(connection, "SELECT payload FROM projects WHERE id = ?", (project_id,))["current_brief_version"]
            return self._payload(connection, "SELECT payload FROM briefs WHERE project_id = ? AND version = ?",
                                 (project_id, version))

    def append_brief(self, project_id, brief_input, *, expected_version, author="user"):
        """Archive a new brief between runs. Active-run invalidation belongs to A7."""
        with self.transaction(write=True) as connection:
            project = self._payload(connection, "SELECT payload FROM projects WHERE id = ?", (project_id,))
            if project["current_brief_version"] != expected_version:
                raise VersionConflict("Brief version conflict")
            if connection.execute("""SELECT 1 FROM runs WHERE project_id = ?
                    AND status IN ('queued','running','pause_requested','paused','waiting_for_input')""",
                                  (project_id,)).fetchone():
                raise Conflict("Changing a brief during an unfinished run requires A7 invalidation")
            version = expected_version + 1
            brief = dict(project_id=project_id, version=version, created_at=now(), author=author, input=brief_input)
            validate("BriefVersion", brief)
            connection.execute("INSERT INTO briefs VALUES (?, ?, ?)", (project_id, version, encode(brief)))
            project["current_brief_version"] = version
            connection.execute("UPDATE projects SET payload = ? WHERE id = ?", (encode(project), project_id))
        return brief

    def create_run(self, project_id, limits, *, brief_version=None, run_id=None):
        validate("Limits", limits)
        with self.transaction(write=True) as connection:
            project = self._payload(connection, "SELECT payload FROM projects WHERE id = ?", (project_id,))
            version = project["current_brief_version"]
            if brief_version is not None and brief_version != version:
                raise VersionConflict("Only the current brief can start a run")
            stamp = now()
            run = dict(id=run_id or new_id("run"), project_id=project_id, brief_version=version, revision=1,
                       status="queued", stage="brief", quality="draft", created_at=stamp, updated_at=stamp,
                       checkpoint_id=None, attempt=0, error=None)
            validate("Run", run)
            try:
                connection.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                                   (run["id"], project_id, version, "queued", 1, encode(run), encode(limits)))
            except sqlite3.IntegrityError:
                raise RunAlreadyActive("Run already exists or project has an unfinished run") from None
        return run

    def get_run(self, run_id):
        with self.transaction() as connection:
            return self._run(connection, run_id)

    def list_runs(self, project_id):
        with self.transaction() as connection:
            self._payload(connection, "SELECT payload FROM projects WHERE id = ?", (project_id,))
            return [json.loads(row[0]) for row in connection.execute(
                "SELECT payload FROM runs WHERE project_id = ? ORDER BY rowid", (project_id,))]

    def get_limits(self, run_id):
        with self.transaction() as connection:
            self._run(connection, run_id)
            return json.loads(connection.execute("SELECT limits_json FROM runs WHERE id = ?", (run_id,)).fetchone()[0])

    def _snapshot(self, connection, run):
        project = self._payload(connection, "SELECT payload FROM projects WHERE id = ?", (run["project_id"],))
        brief = self._payload(connection, "SELECT payload FROM briefs WHERE project_id = ? AND version = ?",
                              (run["project_id"], run["brief_version"]))
        checkpoint = (self._payload(connection, "SELECT payload FROM checkpoints WHERE id = ?", (run["checkpoint_id"],))
                      if run["checkpoint_id"] else {})
        return dict(schema_version="1.0.0", project=project, brief=brief, run=run,
                    data=checkpoint.get("data", empty_data()), artifacts=checkpoint.get("artifacts", []),
                    questions=checkpoint.get("questions", []) if run["status"] == "waiting_for_input" else [],
                    usage=self._usage(connection, run["id"]))

    def get_snapshot(self, run_id):
        with self.transaction() as connection:
            return self._snapshot(connection, self._run(connection, run_id))

    def get_entity(self, project_id, kind, entity_id, version):
        with self.transaction() as connection:
            return self._payload(connection, """SELECT payload FROM entities
                WHERE project_id = ? AND kind = ? AND id = ? AND version = ?""",
                (project_id, kind, entity_id, version))

    def get_artifact(self, project_id, artifact_id, version):
        with self.transaction() as connection:
            return self._payload(connection, "SELECT payload FROM artifacts WHERE project_id = ? AND id = ? AND version = ?",
                                 (project_id, artifact_id, version))

    def get_checkpoint(self, checkpoint_id):
        with self.transaction() as connection:
            return self._payload(connection, "SELECT payload FROM checkpoints WHERE id = ?", (checkpoint_id,))

    def get_step_result(self, checkpoint_id):
        with self.transaction() as connection:
            row = connection.execute("SELECT result_json FROM checkpoints WHERE id = ?", (checkpoint_id,)).fetchone()
            if row is None:
                raise NotFound("Checkpoint not found")
            return json.loads(row[0])

    def list_jobs(self, run_id):
        with self.transaction() as connection:
            self._run(connection, run_id)
            return [dict(row) for row in connection.execute("SELECT * FROM jobs WHERE run_id = ? ORDER BY rowid", (run_id,))]

    @staticmethod
    def _usage(connection, run_id):
        return [json.loads(row[0]) for row in connection.execute("SELECT payload FROM usage WHERE run_id = ? ORDER BY rowid", (run_id,))]

    def list_usage(self, run_id):
        with self.transaction() as connection:
            self._run(connection, run_id)
            return self._usage(connection, run_id)

    def run_summary(self, run_id):
        from app.storage.runtime import summary
        return summary(self, run_id)

    def list_events(self, run_id):
        with self.transaction() as connection:
            self._run(connection, run_id)
            return [dict(row) for row in connection.execute('SELECT * FROM run_events WHERE run_id = ? ORDER BY rowid', (run_id,))]

    def record(self, event):
        """UsageSink: each completed call attempt is persisted independently of a step."""
        validate("Usage", event)
        require((event["cost"] is None) == (event["cost_basis"] == "unknown"), "Cost and cost_basis disagree")
        payload = encode(event)
        with self.transaction(write=True) as connection:
            self._run(connection, event["run_id"])
            if event['cached']:
                require(event['cost'] is not None and event['cost']['amount'].strip('0.') == ''
                        and event['cost_basis'] == 'reported' and event['outcome'] == 'success', 'Cache hit must have zero new cost')
            if 'cache_origin_id' in event:
                origin = self._payload(connection, 'SELECT payload FROM usage WHERE id = ?', (event['cache_origin_id'],))
                require(event['cached'] and not origin['cached'] and origin['outcome'] == 'success'
                        and all(event[key] == origin[key] for key in ('run_id', 'stage', 'provider', 'model', 'tool')),
                        'Cache hit does not match its origin')
            old = connection.execute("SELECT payload FROM usage WHERE id = ?", (event["id"],)).fetchone()
            if old:
                if old[0] != payload:
                    raise Conflict("Conflicting usage event ID")
                return
            try:
                connection.execute("INSERT INTO usage VALUES (?, ?, ?, ?, ?)",
                                   (event["id"], event["run_id"], event["call_id"], event["attempt"], payload))
            except sqlite3.IntegrityError:
                raise Conflict("Call attempt already recorded under another usage ID") from None
            reserved = connection.execute('SELECT * FROM external_calls WHERE id = ?', (event['call_id'],)).fetchone()
            if reserved is not None:
                require(reserved['run_id'] == event['run_id'] and reserved['stage'] == event['stage']
                        and (reserved['kind'] == 'model') == (event['tool'] is None)
                        and reserved['usage_id'] is None and not event['cached'], 'Usage does not match its reservation')
                connection.execute('UPDATE external_calls SET usage_id = ? WHERE id = ?', (event['id'], event['call_id']))
            from app.storage.runtime import event as log_event
            log_event(connection, event['run_id'], event['stage'], 'cache_hit' if event['cached'] else 'usage_recorded', event['outcome'])

    def _materialize(self, directory, payload):
        """Write before DB commit; only DB-referenced files are authoritative results."""
        content = payload.encode("utf-8")
        folder = self.data_dir / directory
        destination = folder / (hashlib.sha256(content).hexdigest() + ".json")
        if destination.exists() and destination.read_bytes() == content:
            return destination
        temporary = None
        try:
            with NamedTemporaryFile(dir=folder, prefix=".pending-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return destination

    def checkpoint_files(self, checkpoint_id):
        """Missing or damaged mirrors are recreated from committed SQLite content."""
        with self.transaction() as connection:
            row = connection.execute("SELECT payload, result_json FROM checkpoints WHERE id = ?", (checkpoint_id,)).fetchone()
            if row is None:
                raise NotFound("Checkpoint not found")
        return {"checkpoint": self._materialize("checkpoints", row["payload"]),
                "result": self._materialize("results", row["result_json"])}

    def requeue_paused(self, run_id, *, expected_revision):
        """Explicit recovery primitive; answers, selection and retry policy belong to A5/A7."""
        with self.transaction(write=True) as connection:
            run = self._run(connection, run_id)
            if run["revision"] != expected_revision:
                raise VersionConflict("Run revision conflict")
            if run["status"] != "paused":
                raise Conflict("Only a paused run can be requeued here")
            run.update(status="queued", revision=run["revision"] + 1, updated_at=now())
            self._write_run(connection, run)
        return run

    def next_queued_run(self):
        with self.transaction() as connection:
            row = connection.execute("SELECT payload FROM runs WHERE status = 'queued' ORDER BY rowid LIMIT 1").fetchone()
            return json.loads(row[0]) if row else None

    def list_inputs(self, run_id):
        with self.transaction() as connection:
            self._run(connection, run_id)
            items = []
            for row in connection.execute("""SELECT inputs.*, checkpoints.payload AS checkpoint_json
                    FROM run_inputs AS inputs JOIN checkpoints ON checkpoints.id = inputs.checkpoint_id
                    WHERE inputs.run_id = ? ORDER BY inputs.rowid""", (run_id,)):
                item = dict(row)
                checkpoint = json.loads(item.pop("checkpoint_json"))
                item.update(payload=json.loads(item["payload"]), questions=checkpoint["questions"],
                            stage=checkpoint["stage"], brief_version=checkpoint["brief_version"])
                items.append(item)
            return items

    def _accept_input(self, run_id, command, kind):
        validate("AnswerQuestions" if kind == "answers" else "ChooseOption", command)
        with self.transaction(write=True) as connection:
            run = self._run(connection, run_id)
            if run["revision"] != command["expected_revision"]:
                raise VersionConflict("Run revision conflict")
            if run["status"] != "waiting_for_input" or not run["checkpoint_id"]:
                raise Conflict("Run is not waiting for input")
            snapshot = self._snapshot(connection, run)
            if kind == "answers":
                if run["stage"] == "selection":
                    raise Conflict("Use selection to choose an option")
                pending = {question["id"]: question for question in snapshot["questions"]}
                answers = {item["question_id"]: item["value"] for item in command["answers"]}
                require(len(answers) == len(command["answers"]), "Duplicate answer ID")
                require(bool(answers) and answers.keys() <= pending.keys(), "Unknown or missing question ID")
                require(all(value.strip() for value in answers.values()), "Answers must not be blank")
                require(all(not question["required"] or identity in answers for identity, question in pending.items()),
                        "Required questions are unanswered")
            else:
                if run["stage"] != "selection":
                    raise Conflict("Run is not waiting for selection")
                require(command["rationale"].strip(), "Selection rationale must not be blank")
                selected = next((item for item in snapshot["data"]["options"]
                                 if item["id"] == command["option_id"] and item["version"] == command["option_version"]
                                 and item["brief_version"] == run["brief_version"]), None)
                if selected is None:
                    raise VersionConflict("Selected option is missing or outdated")
                require(any(artifact["stage"] == "options" and artifact["freshness"] == "current"
                            and {"kind": "option", "id": selected["id"], "version": selected["version"]} in artifact["entities"]
                            for artifact in snapshot["artifacts"]), "Selected option has no current artifact")
            connection.execute("INSERT INTO run_inputs VALUES (?, ?, ?, ?, ?, ?)",
                               (new_id("input"), run_id, run["checkpoint_id"], kind, encode(command), now()))
            run.update(status="queued", revision=run["revision"] + 1, updated_at=now())
            self._write_run(connection, run)
        return run

    def submit_answers(self, run_id, answers, *, expected_revision):
        return self._accept_input(run_id, dict(expected_revision=expected_revision, answers=answers), "answers")

    def submit_selection(self, run_id, option_id, option_version, rationale, *, expected_revision):
        return self._accept_input(run_id, dict(expected_revision=expected_revision, option_id=option_id,
                                              option_version=option_version, rationale=rationale), "selection")

    def worker(self):
        from app.storage.worker import WorkerSession
        return WorkerSession(self)

    def recover_if_idle(self):
        """A second HTTP process must never recover work owned by a live worker."""
        try:
            with self.worker() as worker:
                return worker.recovered_run_ids
        except WorkerBusy:
            return []

    def command(self, scope, key, body, action):
        """Durable HTTP command replay. Failed commands roll back and do not reserve a key."""
        request_json = encode(body)
        with self.transaction(write=True) as connection:
            old = connection.execute("SELECT * FROM commands WHERE scope = ? AND key = ?", (scope, key)).fetchone()
            if old:
                if old["request_json"] != request_json:
                    raise VersionConflict("Idempotency key already used with another request")
                return json.loads(old["response_json"])
            token = self._command_connection.set(connection)
            try:
                response = action()
                connection.execute("INSERT INTO commands VALUES (?, ?, ?, ?)",
                                   (scope, key, request_json, encode(response)))
            finally:
                self._command_connection.reset(token)
        return response

    def prepare_stage_result(self, request, result):
        """Allocate archive versions when a revised brief restarts with an empty current bundle."""
        from copy import deepcopy
        from app.storage.validation import COLLECTIONS
        result = deepcopy(result)
        versions = {}
        with self.transaction() as connection:
            for kind, collection in COLLECTIONS.items():
                present = {item["id"] for item in request["input_data"][collection]}
                for item in result["data"][collection]:
                    if item["id"] in present or item["version"] != 1:
                        continue
                    latest = connection.execute("""SELECT version, brief_version FROM entities
                        WHERE project_id = ? AND kind = ? AND id = ? ORDER BY version DESC LIMIT 1""",
                        (request["project_id"], kind, item["id"])).fetchone()
                    if latest and latest["brief_version"] < request["brief"]["version"]:
                        item["version"] = latest["version"] + 1
                        versions[(kind, item["id"], 1)] = item["version"]
        for collection in ("claims", "issues"):
            for item in result["data"][collection]:
                for evidence in item["evidence"]:
                    evidence["source_version"] = versions.get(
                        ("source", evidence["source_id"], evidence["source_version"]), evidence["source_version"])
        refs = [issue["target"] for issue in result["data"]["issues"]]
        refs += [ref for artifact in result["artifacts"] for ref in artifact["entities"]]
        for ref in refs:
            ref["version"] = versions.get((ref["kind"], ref["id"], ref["version"]), ref["version"])
        return result
