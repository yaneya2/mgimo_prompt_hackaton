import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    import os
    for name in list(os.environ):
        if name.startswith("APP_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


def test_health_and_preserved_data(tmp_path):
    folder = tmp_path / "persistent"
    config = Settings(data_dir=folder)
    with TestClient(create_app(config)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert (folder / "state.sqlite3").is_file()
        (folder / "existing.txt").write_text("keep")
    with TestClient(create_app(config)) as client:
        assert client.get("/health").status_code == 200
        assert (folder / "existing.txt").read_text() == "keep"
        assert client.get("/api/v1/projects").status_code == 200


def test_env_overrides_dotenv_and_key_is_hidden(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("APP_PORT=8100\nAPP_MODEL_API_KEY=test-secret\nBACKEND_PORT=8000\n")
    monkeypatch.setenv("APP_PORT", "8200")
    settings = Settings()
    assert settings.port == 8200
    assert settings.model_api_key.get_secret_value() == "test-secret"
    assert "test-secret" not in repr(settings)


@pytest.mark.parametrize("name,value", [("APP_PORT", "0"), ("APP_PORT", "65536"), ("APP_PORT", "secret-invalid-port"), ("APP_LOG_LEVEL", "verbose"), ("APP_DATA_DIR", "")])
def test_invalid_configuration(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError) as error:
        Settings()
    assert "secret-invalid-port" not in str(error.value)


def test_data_path_cannot_be_file(tmp_path):
    path = tmp_path / "file"
    path.write_text("existing content")
    with pytest.raises(RuntimeError, match="APP_DATA_DIR"):
        with TestClient(create_app(Settings(data_dir=path))):
            pass
    assert path.read_text() == "existing content"


def test_startup_recovers_interrupted_work_but_leaves_live_worker_alone(tmp_path):
    import json
    from pathlib import Path
    from app.storage import Storage

    examples = Path(__file__).resolve().parents[1] / "contracts" / "examples"
    request = json.loads((examples / "stage_request.json").read_text())
    config = Settings(data_dir=tmp_path / "state")
    storage = Storage(config.data_dir)
    project = storage.create_project("Recovery test", request["brief"]["input"])
    run = storage.create_run(project["id"], request["limits"])
    with storage.worker() as worker:
        run = worker.claim_run(run["id"])
        worker.begin_step(run["id"], expected_revision=run["revision"])
        with TestClient(create_app(config)) as client:
            assert client.app.state.storage.get_run(run["id"])["status"] == "running"
    with TestClient(create_app(config)) as client:
        assert client.app.state.storage.get_run(run["id"])["status"] == "paused"
        assert client.app.state.storage.list_jobs(run["id"])[0]["state"] == "interrupted"
