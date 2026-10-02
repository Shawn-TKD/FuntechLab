"""Existing fixtures represent frozen Wan jobs, independent of new defaults."""
from daydreamer_agent.application.settings import load_settings as current_settings


def load_settings(root):
    result = current_settings(root)
    result["video"]["model"] = result["video"]["continuation_model"] = "wan3.0-video-prime"
    result["workflow"]["resolution"] = "720P"
    result["audio"].pop("mode", None)
    result["story"].pop("sound_prompt_version", None)
    result["story"].pop("pacing_prompt_version", None)
    result["workflow"].pop("preferred_min_duration_seconds", None)
    result["workflow"].pop("preferred_max_duration_seconds", None)
    result["audio"]["preserve_generated_clip_audio"] = False
    return result
