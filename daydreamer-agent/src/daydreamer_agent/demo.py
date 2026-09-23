from daydreamer_agent.domain.events import normalize
from daydreamer_agent.storage.files import file_hash, read_json, write_json
from daydreamer_agent.story.generator import load_skill


def demo(root, config, store, pipeline, *, with_media=False):
    inputs = normalize(read_json(root / "examples/events.json"))
    run_id = store.create(inputs, config, demo=True)
    path = store.path(run_id)
    write_json(path / "input/skill.json", load_skill(root / config["story"]["skill_directory"]))
    write_json(path / "input/original.json", inputs)
    with store.lock(run_id):
        pipeline.finish_story(run_id, read_json(root / "examples/story.json"))
        if not with_media:
            return store.load(run_id)
        media = pipeline.media
        media.require_tools()
        story = pipeline.verified_story(run_id)
        for i, shot in enumerate(story["shots"]):
            clip = path / "shots" / shot["shot_id"] / "attempts/1/clip.mp4"
            clip.parent.mkdir(parents=True, exist_ok=True)
            # Engineering test signals, not generated fantasy imagery.
            source = "testsrc2=size=854x480:rate=30" if i == 0 else "smptebars=size=854x480:rate=30"
            media.execute([media.ffmpeg, "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", source,
                           "-t", "3", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-threads", "2", str(clip)])
            info = media.check_clip(clip, {"duration": 3, "ratio": "16:9", "resolution": "480P"})
            store.save_shot(run_id, {"shot_id": shot["shot_id"], "status": "succeeded", "attempt": 1,
                                    "clip": str(clip.relative_to(path)), "sha256": file_hash(clip), "media": info, "demo": True})
        audio_dir = path / "audio/test-signals"
        audio_dir.mkdir(parents=True, exist_ok=True)
        for filename, hz, duration in (("music.wav", 220, 2), ("effect.wav", 880, 0.25)):
            media.execute([media.ffmpeg, "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
                           f"sine=frequency={hz}:duration={duration}", str(audio_dir / filename)])
        manifest = audio_dir / "manifest.json"
        write_json(manifest, {"contains_speech": False, "music": {"path": "music.wav", "gain": 0.15},
                              "sfx": [{"path": "effect.wav", "at_seconds": 3.2, "gain": 0.4}]})
        store.status(run_id, "awaiting_audio")
    return pipeline.compose(run_id, manifest)
