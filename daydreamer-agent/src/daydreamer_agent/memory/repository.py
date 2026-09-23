from copy import deepcopy
from pathlib import Path

from daydreamer_agent.domain.errors import ValidationError
from daydreamer_agent.storage.files import identifier, read_json, write_json


def load_memory(directory, world_id=None, explicit=None):
    if explicit:
        memory = read_json(explicit)
    elif world_id:
        pointer = Path(directory) / identifier(world_id) / "current.json"
        if not pointer.exists():
            raise ValidationError("指定世界没有 current.json；请提供 --memory 或先建立记忆。")
        memory = read_json(pointer)
    else:
        return None
    if not isinstance(memory, dict):
        raise ValidationError("记忆文件必须是 JSON 对象。")
    return memory


def narrative_memory(memory):
    """Remove obvious production fields; remaining prose is governed by skill rules."""
    excluded = {"visual_style", "style", "palette", "lighting", "materials", "prompt", "prompts", "shots", "video_request"}
    if isinstance(memory, dict):
        return {k: narrative_memory(v) for k, v in memory.items() if k not in excluded}
    if isinstance(memory, list):
        return [narrative_memory(v) for v in memory]
    return deepcopy(memory)


def save_draft(run_path, story):
    write_json(Path(run_path) / "story/world_delta_draft.json", {
        "committed": False, "memory_basis": story.get("memory_basis"),
        "changes": story.get("world_delta_draft", []),
    })
