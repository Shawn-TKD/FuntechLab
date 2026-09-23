from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import uuid

from daydreamer_agent.domain.errors import BusyError, ValidationError
from daydreamer_agent.storage.files import identifier, read_json, write_json


def now():
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    """JSON is the recovery source of truth; SQLite is a rebuildable status index."""

    def __init__(self, runs, database):
        self.runs = Path(runs)
        self.database = Path(database)
        self.runs.mkdir(parents=True, exist_ok=True)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, status TEXT, updated TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS shots (run_id TEXT, shot_id TEXT, status TEXT, task_id TEXT, PRIMARY KEY(run_id, shot_id))")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def path(self, run_id):
        return self.runs / identifier(run_id)

    def create(self, inputs, config, *, demo=False):
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:10]
        path = self.path(run_id)
        path.mkdir()
        write_json(path / "input/normalized.json", inputs)
        write_json(path / "input/config.json", config)
        self.save(run_id, {"run_id": run_id, "status": "created", "created_at": now(), "demo": demo})
        return run_id

    def load(self, run_id):
        path = self.path(run_id) / "job.json"
        if not path.exists():
            raise ValidationError("找不到任务：" + run_id)
        return read_json(path)

    def save(self, run_id, job):
        job["updated_at"] = now()
        write_json(self.path(run_id) / "job.json", job)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?)", (run_id, job["status"], job["updated_at"]))
        self.audit(run_id, "job_status", status=job["status"])

    def audit(self, run_id, event, **fields):
        path = self.path(run_id) / "logs/events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": now(), "event": event, **fields}, ensure_ascii=False) + "\n")

    def status(self, run_id, status, **fields):
        job = self.load(run_id)
        job.update(status=status, **fields)
        self.save(run_id, job)

    def shot(self, run_id, shot_id):
        path = self.path(run_id) / "shots" / identifier(shot_id) / "state.json"
        return read_json(path) if path.exists() else {"shot_id": shot_id, "status": "pending", "attempt": 1}

    def save_shot(self, run_id, shot):
        path = self.path(run_id) / "shots" / identifier(shot["shot_id"])
        write_json(path / "state.json", shot)
        write_json(path / "attempts" / str(shot["attempt"]) / "task.json", shot)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO shots VALUES (?,?,?,?)", (run_id, shot["shot_id"], shot["status"], shot.get("task_id")))
        self.audit(run_id, "shot_status", shot_id=shot["shot_id"], status=shot["status"], attempt=shot["attempt"])

    def list(self):
        jobs = []
        for path in sorted(self.runs.glob("*/job.json"), reverse=True):
            job = read_json(path)
            with self.connect() as db:
                db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?)", (job["run_id"], job["status"], job["updated_at"]))
            jobs.append(job)
        return jobs

    @contextmanager
    def lock(self, run_id):
        self.load(run_id)
        with (self.path(run_id) / ".lock").open("a+b") as stream:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise BusyError("该任务正在被另一个进程处理。") from exc
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
