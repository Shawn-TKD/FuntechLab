from pathlib import Path
import time

from daydreamer_agent.domain.errors import ProviderError, ValidationError
from daydreamer_agent.application.settings import video_retry_policy
from daydreamer_agent.memory.repository import save_draft
from daydreamer_agent.providers.audio import stage_audio
from daydreamer_agent.providers.wan import prepare_submission
from daydreamer_agent.prompts.video_compiler import TEMPLATE_VERSION, compile_shot, validate_video_request
from daydreamer_agent.storage.files import digest, file_hash, identifier, read_json, write_json
from daydreamer_agent.story.generator import generate_story
from daydreamer_agent.story.seeded import generate_seeded_story
from daydreamer_agent.story.creativity import verify_decision
from daydreamer_agent.storage.jobs_sqlite import now
from daydreamer_agent.story.validation import validate_story


class Pipeline:
    def __init__(self, store, *, story_provider=None, video_provider=None, media=None, progress=print):
        self.store = store
        self.story_provider = story_provider
        self.video_provider = video_provider
        self.media = media
        self.progress = progress

    def plan(self, run_id):
        with self.store.lock(run_id):
            path = self.store.path(run_id)
            job = self.store.load(run_id)
            if job["status"] in {"story_ready", "video_generating", "awaiting_audio", "composing", "completed"}:
                self.verified_story(run_id)
                return job
            self.store.status(run_id, "story_generating")
            try:
                inputs = read_json(path / "input/normalized.json")
                skill = read_json(path / "input/skill.json")
                generate = generate_seeded_story if (path / "input/creativity.json").exists() else generate_story
                story = generate(path, inputs, skill, self.story_provider, self.progress)
                if story["status"] != "ready":
                    write_json(path / "story/result.json", story)
                    self.store.status(run_id, story["status"])
                else:
                    self.finish_story(run_id, story)
            except Exception:
                self.store.status(run_id, "failed", failed_stage="story")
                raise
            return self.store.load(run_id)

    def finish_story(self, run_id, story):
        path = self.store.path(run_id)
        inputs = read_json(path / "input/normalized.json")
        config = read_json(path / "input/config.json")
        validate_story(story, inputs)
        verify_decision(path, story)
        if story["status"] != "ready":
            raise ValidationError("仅 ready 故事可以准备视频请求。")
        chain = inputs["production_constraints"].get("continuity_mode") == "frame_chain"
        requests = {}
        for index, shot in enumerate(story["shots"]):
            model = config["video"].get("continuation_model") if chain and index else config["video"]["model"]
            if not model:
                raise ValidationError("未配置后续片段的图生视频模型。")
            item = compile_shot(story, shot, inputs["production_constraints"], model)
            if chain and index:
                item["predecessor_shot_id"] = story["shots"][index - 1]["shot_id"]
            requests[shot["shot_id"]] = item
        write_json(path / "story/result.json", story)
        for shot_id, compiled in requests.items():
            write_json(path / "shots" / shot_id / "video_request.json", compiled)
        save_draft(path, story)
        self.store.status(run_id, "story_ready", story_digest=digest(story), request_digests={k: digest(v) for k, v in requests.items()})

    def verified_story(self, run_id):
        path = self.store.path(run_id)
        job = self.store.load(run_id)
        story = read_json(path / "story/result.json")
        if digest(story) != job.get("story_digest"):
            raise ValidationError("故事文件已变化，请用 import-story 创建新任务，避免混用旧镜头。")
        validate_story(story, read_json(path / "input/normalized.json"))
        verify_decision(path, story)
        return story

    def is_chain(self, run_id):
        inputs = read_json(self.store.path(run_id) / "input/normalized.json")
        return inputs["production_constraints"].get("continuity_mode") == "frame_chain"

    def prepare_boundary(self, run_id, state, parameters):
        path = self.store.path(run_id)
        clip = path / state["clip"]
        prepared = clip.with_name("prepared.mp4")
        tail = clip.with_name("tail.png")
        basis = digest({"source_sha256": state["sha256"], "parameters": parameters, "preparation_version": "1.0"})
        valid = state.get("prepared_basis") == basis and prepared.exists() and tail.exists()
        if valid:
            valid = file_hash(prepared) == state.get("prepared_sha256") and file_hash(tail) == state.get("tail_sha256")
        if not valid:
            self.media.prepare_clip(clip, prepared, parameters)
            self.media.extract_tail(prepared, tail, parameters["duration"])
            state.update(prepared_clip=str(prepared.relative_to(path)), prepared_sha256=file_hash(prepared),
                         tail_frame=str(tail.relative_to(path)), tail_sha256=file_hash(tail), prepared_basis=basis)
            self.store.save_shot(run_id, state)

    @staticmethod
    def dependency(state):
        return {"shot_id": state["shot_id"], "attempt": state["attempt"],
                "prepared_sha256": state["prepared_sha256"], "tail_sha256": state["tail_sha256"]}

    def run_full(self, run_id, *, audio_manifest=None, wait_seconds=3600):
        """Run or resume each stage, stopping on any unresolved state."""
        job = self.store.load(run_id)
        if job.get("story_digest"):
            self.verified_story(run_id)
        else:
            job = self.plan(run_id)
        if job["status"] == "completed":
            return self.compose(run_id, audio_manifest) if audio_manifest else job
        if not job.get("story_digest"):
            return job
        job = self.render(run_id, wait_seconds=wait_seconds)
        if job["status"] != "awaiting_audio":
            return job
        return self.compose(run_id, audio_manifest, preview=audio_manifest is None)

    def submit_attempt(self, run_id, state, request, predecessor, frame):
        path = self.store.path(run_id)
        attempt_path = path / "shots" / state["shot_id"] / "attempts" / str(state["attempt"])
        payload = prepare_submission(request, frame, require_first_frame=predecessor is not None)
        write_json(attempt_path / "video_request.json", {"request": request, "predecessor": predecessor,
                   "first_frame": str(frame.relative_to(path)) if frame else None})
        if predecessor:
            state["predecessor"] = predecessor
        state.update(status="submitting", submission_started_at=time.time())
        self.store.save_shot(run_id, state)
        self.progress("提交镜头：" + state["shot_id"])
        try:
            state["task_id"] = self.video_provider.submit(payload)
        except ProviderError as exc:
            state["status"] = "submission_unknown" if exc.uncertain else "failed"
            self.store.save_shot(run_id, state)
            self.store.status(run_id, state["status"], failed_stage="video", failed_shot=state["shot_id"])
            raise
        state.update(status="submitted", submitted_at=time.time())
        self.store.save_shot(run_id, state)
        return attempt_path / "clip.mp4"

    def render(self, run_id, *, wait_seconds=0):
        if self.media is None or self.video_provider is None:
            raise ValidationError("视频服务与媒体检查器未配置。")
        self.media.require_tools()
        with self.store.lock(run_id):
            job = self.store.load(run_id)
            if job.get("demo"):
                raise ValidationError("离线演示任务不能提交真实模型；请创建正式任务。")
            if job["status"] == "completed":
                return job
            story = self.verified_story(run_id)
            chain = self.is_chain(run_id)
            path = self.store.path(run_id)
            timeout_seconds, max_resubmissions = video_retry_policy(read_json(path / "input/config.json"))
            compiled = {}
            # Validate all requests before spending on the first clip.
            for shot in story["shots"]:
                shot_id = shot["shot_id"]
                item = read_json(path / "shots" / shot_id / "video_request.json")
                if digest(item) != job["request_digests"].get(shot_id):
                    raise ValidationError("视频请求已变化，请建立新任务。")
                validate_video_request(item["request"])
                compiled[shot_id] = item["request"]
            self.store.status(run_id, "video_generating")
            deadline = time.monotonic() + wait_seconds
            for index, shot in enumerate(story["shots"]):
                shot_id = shot["shot_id"]
                state = self.store.shot(run_id, shot_id)
                request = compiled[shot_id]
                attempt_path = path / "shots" / shot_id / "attempts" / str(state["attempt"])
                clip = attempt_path / "clip.mp4"
                predecessor = None
                if chain and index:
                    previous = self.store.shot(run_id, story["shots"][index - 1]["shot_id"])
                    if previous["status"] != "succeeded":
                        raise ValidationError("前段尚未完成，不能生成后段。")
                    predecessor = self.dependency(previous)
                    if state["status"] != "pending" and state.get("predecessor") != predecessor:
                        self.store.status(run_id, "needs_resolution", failed_stage="continuity", outputs_stale=True)
                        raise ValidationError("前段版本已变化，后续片段失效；核对远端任务后，用 regenerate-shot 重做相应镜头及后段。")
                if state["status"] == "succeeded":
                    if clip.exists() and file_hash(clip) == state.get("sha256"):
                        if chain:
                            self.prepare_boundary(run_id, state, request["parameters"])
                        continue
                    state["status"] = "submitted"  # Query original task and re-download.
                    self.store.save_shot(run_id, state)
                if state["status"] in {"submitting", "submission_unknown"}:
                    state["status"] = "submission_unknown"
                    self.store.save_shot(run_id, state)
                    self.store.status(run_id, "submission_unknown")
                    return self.store.load(run_id)
                if state["status"] == "failed":
                    self.store.status(run_id, "failed", failed_stage="video", failed_shot=shot_id)
                    return self.store.load(run_id)
                if state["status"] == "pending":
                    frame = path / previous["tail_frame"] if predecessor else None
                    clip = self.submit_attempt(run_id, state, request, predecessor, frame)
                if "submitted_at" not in state:
                    # Legacy tasks begin their local timeout window on first observation.
                    state["submitted_at"] = time.time()
                    state["timeout_clock_basis"] = "first_local_observation"
                    self.store.save_shot(run_id, state)
                confirming_timeout = False
                while True:
                    output = self.video_provider.query(state["task_id"])
                    remote = output["task_status"]
                    state["remote_status"] = remote
                    if remote == "SUCCEEDED":
                        if not output.get("video_url"):
                            raise ProviderError("成功任务没有提供视频下载地址，可稍后恢复查询。")
                        self.video_provider.download(output["video_url"], clip)
                        try:
                            info = self.media.check_clip(clip, request["parameters"])
                        except ValidationError:
                            state["status"] = "failed"
                            state["failure"] = "technical_validation"
                            self.store.save_shot(run_id, state)
                            self.store.status(run_id, "failed", failed_stage="video", failed_shot=shot_id)
                            raise
                        state.update(status="succeeded", sha256=file_hash(clip), media=info,
                                     clip=str(clip.relative_to(path)))
                        self.store.save_shot(run_id, state)
                        if chain:
                            self.prepare_boundary(run_id, state, request["parameters"])
                        break
                    if remote in {"FAILED", "CANCELED", "UNKNOWN"}:
                        state["status"] = "submission_unknown" if remote == "UNKNOWN" else "failed"
                        self.store.save_shot(run_id, state)
                        self.store.status(run_id, state["status"], failed_stage="video", failed_shot=shot_id)
                        return self.store.load(run_id)
                    state["status"] = "running"
                    self.store.save_shot(run_id, state)
                    elapsed = max(0, time.time() - state["submitted_at"])
                    if max_resubmissions and elapsed >= timeout_seconds:
                        if not confirming_timeout:
                            confirming_timeout = True
                            continue  # Query once more before creating another billable task.
                        count = state.get("timeout_resubmissions", 0)
                        if count >= max_resubmissions:
                            self.store.status(run_id, "needs_resolution", reason="timeout_resubmissions_exhausted", failed_shot=shot_id)
                            self.progress("超时重提次数已用完，保留当前任务供后续查询：" + shot_id)
                            return self.store.load(run_id)
                        if chain and any(self.store.shot(run_id, s["shot_id"])["status"] != "pending" for s in story["shots"][index + 1:]):
                            self.store.status(run_id, "needs_resolution", reason="timeout_has_existing_descendants", failed_shot=shot_id)
                            return self.store.load(run_id)
                        state.update(superseded_at=time.time(), superseded_reason="generation_timeout", remote_task_canceled=False)
                        self.store.save_shot(run_id, state)
                        state = {"shot_id": shot_id, "status": "pending", "attempt": state["attempt"] + 1,
                                 "timeout_resubmissions": count + 1, "supersedes_task_id": state["task_id"]}
                        self.store.save_shot(run_id, state)
                        self.store.audit(run_id, "timeout_resubmission", shot_id=shot_id, attempt=state["attempt"], previous_task_id=state["supersedes_task_id"])
                        self.store.status(run_id, "video_generating", outputs_stale=True, preview=None, final=None, reason=None)
                        self.progress(f"镜头超时，自动重提 {count + 1}/{max_resubmissions}：{shot_id}")
                        frame = path / previous["tail_frame"] if predecessor else None
                        clip = self.submit_attempt(run_id, state, request, predecessor, frame)
                        confirming_timeout = False
                        continue
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return self.store.load(run_id)
                    self.progress("镜头处理中：" + shot_id)
                    until_timeout = max(0.1, timeout_seconds - elapsed) if max_resubmissions else 15
                    time.sleep(min(15, remaining, until_timeout))
            self.store.status(run_id, "awaiting_audio", reason=None, failed_shot=None, failed_stage=None)
            return self.store.load(run_id)

    def retry_shot(self, run_id, shot_id, *, regenerate=False):
        with self.store.lock(run_id):
            story = self.verified_story(run_id)
            if shot_id not in {s["shot_id"] for s in story["shots"]}:
                raise ValidationError("镜头不存在。")
            state = self.store.shot(run_id, shot_id)
            allowed = {"failed", "succeeded"} if regenerate else {"failed"}
            if state["status"] not in allowed:
                raise ValidationError("仅已明确失败或完成的镜头可以重新生成；未解决的提交需先核对平台。")
            if self.is_chain(run_id):
                start = next(i for i, shot in enumerate(story["shots"]) if shot["shot_id"] == shot_id)
                descendants = [self.store.shot(run_id, s["shot_id"]) for s in story["shots"][start + 1:]]
                if any(s["status"] in {"running", "submitted", "submitting", "submission_unknown"} for s in descendants):
                    raise ValidationError("后续片段仍有未解决的远端任务，需先核对其状态。")
                for child in descendants:
                    if child["status"] != "pending":
                        self.store.save_shot(run_id, {"shot_id": child["shot_id"], "status": "pending", "attempt": child["attempt"] + 1,
                                                     "invalidated_by": shot_id})
            self.store.save_shot(run_id, {"shot_id": shot_id, "status": "pending", "attempt": state["attempt"] + 1})
            self.store.status(run_id, "video_generating", outputs_stale=True, final=None, preview=None)

    def attach_task(self, run_id, shot_id, task_id):
        identifier(task_id)
        with self.store.lock(run_id):
            state = self.store.shot(run_id, shot_id)
            if state["status"] not in {"submitting", "submission_unknown"}:
                raise ValidationError("只有提交状态未知的镜头可以补录任务编号。")
            state.update(task_id=task_id, status="submitted")
            self.store.save_shot(run_id, state)
            self.store.status(run_id, "video_generating")

    def compose(self, run_id, audio_manifest=None, *, preview=False):
        with self.store.lock(run_id):
            path = self.store.path(run_id)
            job = self.store.load(run_id)
            story = self.verified_story(run_id)
            chain = self.is_chain(run_id)
            if job["status"] == "completed" and not audio_manifest and not preview:
                return job
            clips, selected = [], []
            previous = None
            for shot in story["shots"]:
                state = self.store.shot(run_id, shot["shot_id"])
                if state["status"] != "succeeded":
                    raise ValidationError("所有镜头成功后才能合成。")
                clip = path / state["clip"]
                if not clip.exists() or file_hash(clip) != state["sha256"]:
                    raise ValidationError("镜头文件缺失或变化，请先恢复视频任务。")
                if chain:
                    if previous and state.get("predecessor") != self.dependency(previous):
                        raise ValidationError("片段依赖的前段版本不匹配，不能混合剪辑。")
                    clip = path / state.get("prepared_clip", "missing")
                    tail = path / state.get("tail_frame", "missing")
                    if not clip.is_file() or file_hash(clip) != state.get("prepared_sha256") or not tail.is_file() or file_hash(tail) != state.get("tail_sha256"):
                        raise ValidationError("实际剪辑片段或末帧缺失、变化，请恢复视频任务。")
                clips.append((clip, shot["duration_seconds"]))
                selected.append({"shot_id": shot["shot_id"], "attempt": state["attempt"], "sha256": file_hash(clip), "clip": str(clip.relative_to(path)), "predecessor": state.get("predecessor")})
                previous = state
            constraints = read_json(path / "input/normalized.json")["production_constraints"]
            parameters = {"duration": sum(duration for _, duration in clips), "ratio": constraints["aspect_ratio"], "resolution": constraints["resolution"]}
            if not audio_manifest and not preview:
                self.store.status(run_id, "awaiting_audio")
                return self.store.load(run_id)
            tracks = [] if preview else stage_audio(audio_manifest, path, parameters["duration"], self.media)
            timeline_name = "preview-timeline.json" if preview else "timeline.json"
            write_json(path / "composition" / timeline_name, {"shots": selected, "audio": tracks, "parameters": parameters, "transitions": "hard_cuts"})
            if not preview:
                self.store.status(run_id, "composing")
            try:
                final = self.media.compose(path, clips, tracks, parameters, preview=preview, prepared=chain)
                info = self.media.check_clip(final, parameters, require_audio=not preview)
            except Exception:
                if not preview:
                    self.store.status(run_id, "failed", failed_stage="composition")
                raise
            if preview:
                write_json(path / "composition/preview-manifest.json", {"run_id": run_id, "selected_shots": selected, "media": info,
                           "continuity_method": "frame_chain" if chain else "independent", "media_content_checked": False,
                           "preview": str(final.relative_to(path)), "sha256": file_hash(final)})
                self.store.status(run_id, job["status"], preview=str(final), outputs_stale=False,
                                  work_ready_at=job.get("work_ready_at") or now())
                return {"run_id": run_id, "status": "preview_ready", "preview": str(final)}
            manifest = {"run_id": run_id, "demo": job.get("demo", False), "story_digest": job["story_digest"],
                        "skill_digest": digest(read_json(path / "input/skill.json")), "template_version": TEMPLATE_VERSION,
                        "selected_shots": selected, "audio": tracks, "media": info, "media_content_checked": False,
                        "continuity_method": "frame_chain" if chain else ("single_take" if len(clips) == 1 else "independent"),
                        "final": str(final.relative_to(path)), "sha256": file_hash(final)}
            write_json(path / "manifest.json", manifest)
            self.store.status(run_id, "completed", final=str(final), outputs_stale=False,
                              work_ready_at=job.get("work_ready_at") or now())
            return self.store.load(run_id)
