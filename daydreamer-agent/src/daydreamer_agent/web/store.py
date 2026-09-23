import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "uploads").mkdir(exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY, client_key TEXT UNIQUE NOT NULL,
                  state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                  source TEXT NOT NULL, run_id TEXT, video TEXT, message TEXT NOT NULL DEFAULT '',
                  played INTEGER NOT NULL DEFAULT 0, recoveries INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, expires REAL NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "web.sqlite3", timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def session(self):
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM sessions LIMIT 1").fetchone():
                raise ValueError("已绑定测试手机，请使用原来的浏览器；换机需在服务器重置绑定。")
            db.execute("INSERT INTO sessions VALUES (?,?)", (hashlib.sha256(token.encode()).hexdigest(), time.time() + 30 * 86400))
        return token

    def authorized(self, token):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM sessions WHERE hash=? AND expires>?",
                                   (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone())

    def get(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def by_key(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE client_key=?", (key,)).fetchone()
            return dict(row) if row else None

    def reserve(self, key, suffix):
        job_id = secrets.token_hex(16)
        source = str(self.root / "uploads" / (job_id + suffix))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT count(*) FROM jobs WHERE state IN ('uploading','queued','processing')").fetchone()[0] >= 10:
                raise ValueError("等待中的任务较多，请稍后上传。")
            db.execute("INSERT INTO jobs(id,client_key,state,created,updated,source) VALUES (?,?,?,?,?,?)",
                       (job_id, key, "uploading", time.time(), time.time(), source))
        return self.get(job_id)

    def update(self, job_id, **fields):
        allowed = {"state", "run_id", "video", "message", "played", "recoveries"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid job fields")
        fields["updated"] = time.time()
        with self.connect() as db:
            db.execute("UPDATE jobs SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?", (*fields.values(), job_id))

    def upload_failed(self, job_id):
        job = self.get(job_id)
        if job:
            Path(job["source"] + ".part").unlink(missing_ok=True)
        with self.connect() as db:
            db.execute("DELETE FROM jobs WHERE id=? AND state='uploading'", (job_id,))

    def recover_worker(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET state=CASE WHEN recoveries<2 THEN 'queued' ELSE 'paused' END, "
                       "recoveries=recoveries+1, message='服务已重启，将从保存的进度恢复', updated=? WHERE state='processing'", (time.time(),))

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET state='processing', updated=?, message='正在检查视频' WHERE id=?", (time.time(), row["id"]))
                return dict(row)

    def resume(self, job_id):
        with self.connect() as db:
            result = db.execute("UPDATE jobs SET state='queued', message='等待恢复原任务', updated=? "
                                "WHERE id=? AND state IN ('failed','paused')", (time.time(), job_id))
            return result.rowcount == 1

    def list(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 100")]

    def next(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE state='ready' AND played=0 ORDER BY created LIMIT 1").fetchone()
            if not row:
                row = db.execute("SELECT * FROM jobs WHERE state IN ('queued','processing') ORDER BY created LIMIT 1").fetchone()
            return dict(row) if row else None


def public_job(job):
    return {key: job[key] for key in ("id", "state", "created", "updated", "message", "played")} | {
        "video_url": "/api/jobs/" + job["id"] + "/video" if job["state"] == "ready" else None,
        "player_url": "/?job=" + job["id"],
    }
