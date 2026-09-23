"""One durable worker. Existing Agent checkpoints own all paid-call recovery."""
import contextlib
import os
import sys
import time
from pathlib import Path

from daydreamer_agent.application.settings import load_settings
from daydreamer_agent.cli import parser, run_command
from daydreamer_agent.domain.errors import AgentError
from daydreamer_agent.event_extraction.pipeline import EventExtraction
from daydreamer_agent.media.ffmpeg import Media
from daydreamer_agent.storage.jobs_sqlite import JobStore
from daydreamer_agent.web.store import Store


class Progress:
    def __init__(self, store, job_id):
        self.store, self.job_id, self.pending = store, job_id, ""

    def write(self, text):
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if "理解视频" in line or "无声视频片段" in line:
                message = "正在分析生活视频"
            elif "事件卡已保存" in line:
                message = "事件卡已生成，开始创作幻想片段"
            elif "创作" in line or "分镜" in line or "主题" in line:
                message = "正在创作故事与分镜"
            elif "视频" in line or "片段" in line or "拼接" in line:
                message = "正在生成和拼接幻想片段"
            else:
                continue
            self.store.update(self.job_id, message=message)
        return len(text)

    def flush(self):
        pass


def process(store, job, project):
    try:
        # Persist the extraction id before the first API call. A crash here cannot
        # cause paid work to be submitted under a new id on service restart.
        run_id = job["run_id"]
        if not run_id:
            config = load_settings(project)
            extraction = JobStore(project / "data/extractions", project / "data/extractions.sqlite3")
            run_id = EventExtraction(project, extraction, Media(project)).create(Path(job["source"]), config)
            store.update(job["id"], run_id=run_id)
        args = parser().parse_args(["--project", str(project), "run", "--run", run_id, "--wait", "3600"])
        with contextlib.redirect_stdout(Progress(store, job["id"])):
            result = run_command(args)
        if result.get("status") in {"preview_ready", "completed"}:
            video = Path(result["delivery"]["video"]).resolve()
            if not video.is_relative_to(project / "outputs") or not video.is_file():
                raise ValueError("Unexpected delivery path")
            store.update(job["id"], state="ready", video=str(video), message="幻想片段已准备好")
        else:
            store.update(job["id"], state="paused", message="任务需要继续处理或补充素材，可恢复原任务。")
    except AgentError as exc:
        store.update(job["id"], state="failed", message=str(exc)[:500])
    except Exception as exc:
        # Do not send provider payloads, paths, or credentials to the client/log.
        print("Worker failure:", type(exc).__name__, file=sys.stderr, flush=True)
        store.update(job["id"], state="failed", message="处理暂时中断，已保留任务进度。")


def main():
    project = Path(os.environ["DAYDREAMER_PROJECT"]).resolve()
    store = Store(os.environ["DAYDREAMER_WEB_DATA"])
    with (store.root / "worker.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.write(b"0")
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        store.recover_worker()
        while True:
            job = store.claim()
            if job:
                process(store, job, project)
            else:
                time.sleep(2)


if __name__ == "__main__":
    main()
