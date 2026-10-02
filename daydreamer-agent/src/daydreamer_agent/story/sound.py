"""Shared creative guidance; no extra generation stage or audio service."""

SOUND_DIRECTION = """声音以已有剧情和氛围为依据。背景音乐可选，全片或任一镜头无配乐都是正常结果，不默认铺满、不要求至少出现一次、不强制结尾升华。
先判断环境声与动作声是否足够；仅在有明确剧情依据时使用克制的纯器乐，少量乐器、稀疏音符和留白，低于关键动作音效。
music_direction说明全片是否需要音乐、适用的剧情节点及使用边界，不得因此要求每镜都有音乐。
每镜audio.music必须明确：不用音乐，或进入前无配乐、进入的动作/氛围触发点、乐器节奏与强度、退出触发点以及退出后的无配乐区间。可用本镜相对秒数辅助，但以本镜已确定动作顺序为依据，不编造新情节。
audio.sfx只描述本镜实际动作与环境对应的声音，关键音效与动作同步，不为每次变化堆叠强调音。audio.continuity分别说明环境/动作声和音乐是否延续，不能默认音乐跨镜铺满。
无配乐时明确写“本镜不使用背景音乐”，不要省略或留空；不添加旁白、对白、演唱、吟唱、人声采样或其他人声。无配乐不等于环境声和动作声静音。"""

SOUND_EXECUTION = "背景音乐可选，只在本镜明确指定的剧情节点或区间出现，其余部分不添加配乐；本镜无配乐决定优先，不自行补片头、高潮或煽情收尾。音乐克制并给关键音效让位，音效与本镜实际动作同步，不增加新事件。无旁白、对白、演唱、吟唱、人声采样或其他人声。"

# Keep v1 constants byte-for-byte stable for persisted creative checkpoints.
LOW_NOISE_DIRECTION = """环境声克制、低存在感，不默认铺满持续风噪、交通底噪、机械轰鸣、宽频嘶声或科幻嗡鸣；不要为了真实感和跨镜连续而维持无意义声垫。
优先保留关键动作发生点、必要尾音及少量有叙事作用的空间线索。关键声音之间允许自然安静，不用音乐填补留白。
持续运动确需轻微滚动声、剧情依赖风暴或设备运转时可以保留；明确声源、必要区间、强弱变化和退出条件，不自动贯穿全片。
audio.sfx逐项写出声源、动作触发、持续范围与衰减；audio.continuity只延续确需跨镜的声音，不默认延续所有底声，不突然切断合理尾音。
关键动作音效最清楚，环境声和可选音乐辅助；不夸大每一步声音，不强制绝对静音。"""

LOW_NOISE_EXECUTION = "环境声克制、低存在感，不默认铺满持续风噪、交通底噪、机械轰鸣、宽频嘶声或科幻嗡鸣。关键动作声清楚，保留必要尾音与自然安静，不用音乐填补留白。只在本镜明确需要的区间保留持续声，按指定声源、强弱变化及退出条件执行；跨镜不自动延续底噪，也不突然切断合理尾音，不夸大每一步声音或强制绝对静音。"


def sound_direction(version):
    if version == 0:
        return ""
    if version == 1:
        return SOUND_DIRECTION
    if version == 2:
        return SOUND_DIRECTION + "\n" + LOW_NOISE_DIRECTION
    from daydreamer_agent.domain.errors import ValidationError
    raise ValidationError("未知的声音提示词版本。")


def sound_execution(version):
    sound_direction(version)  # Validate even when only compiling a video prompt.
    return SOUND_EXECUTION + ("\n" + LOW_NOISE_EXECUTION if version == 2 else "")
