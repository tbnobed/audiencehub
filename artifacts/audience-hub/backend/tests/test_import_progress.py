"""Progress must be readable from the jobs table while imports stay uncommitted."""

from types import SimpleNamespace

from app import worker
from app.imports import api


def test_start_import_persists_initial_progress_with_running_state(monkeypatch):
    row = {"status": "mapped", "mapping": {"columns": {"email": "email"}},
           "rows_total": 5_360_000}
    job = SimpleNamespace(id=91, progress={})
    operations = []

    class Db:
        def execute(self, statement, params):
            operations.append((str(statement), params))

        def commit(self):
            operations.append(("commit", job.progress.copy()))

    monkeypatch.setattr(api, "_get_import", lambda db, import_id: row)
    monkeypatch.setattr(api, "enqueue", lambda *args, **kwargs: job)
    result = api.start_import(42, user=SimpleNamespace(), db=Db())
    assert result == {"id": 42, "job_id": 91, "status": "running"}
    assert operations[-1] == ("commit", {
        "done": 0, "total": 5_360_000, "message": "Preparing validation…",
    })
    assert api._json_import({
        "id": 42, "status": "running", "progress": operations[-1][1],
        "file_path": "/private/path",
    }) == {"id": 42, "status": "running", "progress": operations[-1][1]}


def test_worker_commits_each_validation_and_writing_progress(monkeypatch):
    operations = []

    class Db:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def commit(self):
            operations.append(("commit",))

    monkeypatch.setattr(worker, "Session", lambda engine: Db())

    def heartbeat(db, job_id, progress=None):
        operations.append(("heartbeat", job_id, progress))
        return True

    monkeypatch.setattr(worker.queue, "heartbeat", heartbeat)
    monkeypatch.setattr(worker.queue, "succeed",
                        lambda db, job_id: operations.append(("succeed", job_id)))

    def run_import(import_id, progress_callback):
        assert import_id == 42
        progress_callback({"done": 1_200_000, "total": 5_360_000,
                           "message": "Validating 1.2M / 5.36M"})
        progress_callback({"done": 400_000, "total": 5_360_000,
                           "message": "Writing 400k / 5.36M"})

    monkeypatch.setattr("app.imports.service.run_import", run_import)
    worker.run_job(91, "import.run", {"import_id": 42})
    assert operations == [
        ("heartbeat", 91, {"done": 1_200_000, "total": 5_360_000,
                           "message": "Validating 1.2M / 5.36M"}),
        ("commit",),
        ("heartbeat", 91, {"done": 400_000, "total": 5_360_000,
                           "message": "Writing 400k / 5.36M"}),
        ("commit",), ("succeed", 91), ("commit",),
    ]