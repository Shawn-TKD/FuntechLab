from fractions import Fraction
import json
import math
import os
from pathlib import Path
import shutil
import subprocess

from daydreamer_agent.domain.errors import ValidationError
from daydreamer_agent.application.settings import read_env_file


def dimensions(ratio, resolution):
    x, y = map(int, ratio.split(":"))
    short = int(resolution[:-1])
    if x >= y:
        return round(short * x / y / 2) * 2, short
    return short, round(short * y / x / 2) * 2


class Media:
    def __init__(self, root):
        self.root = Path(root)
        self.ffmpeg = self.find("ffmpeg")
        self.ffprobe = self.find("ffprobe")

    def find(self, name):
        key = name.upper() + "_PATH"
        configured = os.environ.get(key) or read_env_file(self.root).get(key)
        if configured:
            return configured
        found = shutil.which(name)
        if found:
            return found
        suffix = ".exe" if os.name == "nt" else ""
        candidates = sorted((self.root / ".tools").glob("**/" + name + suffix))
        return str(candidates[0]) if candidates else None

    def require_tools(self):
        if not self.ffmpeg or not self.ffprobe:
            raise ValidationError("未找到 FFmpeg/FFprobe；放入项目 .tools 或配置 FFMPEG_PATH、FFPROBE_PATH。")

    def execute(self, arguments, *, cwd=None):
        try:
            result = subprocess.run(arguments, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=600, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.TimeoutExpired):
            raise ValidationError("媒体工具无法启动或处理超时。") from None
        if result.returncode:
            # Raw stderr may contain signed URLs or local metadata; expose no raw process output.
            raise ValidationError("媒体处理失败，请检查输入文件、编码和输出空间。")
        return result.stdout

    def probe(self, path):
        self.require_tools()
        path = Path(path).resolve()
        if not path.is_file() or path.stat().st_size == 0:
            raise ValidationError("媒体文件不存在或为空：" + path.name)
        output = self.execute([self.ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-show_streams", "-show_format", "-of", "json", str(path)])
        try:
            return json.loads(output)
        except ValueError:
            raise ValidationError("FFprobe 返回格式错误。") from None

    def check_clip(self, path, parameters, *, require_audio=False):
        result = self.probe(path)
        videos = [s for s in result.get("streams", []) if s.get("codec_type") == "video"]
        if not videos:
            raise ValidationError("文件没有视频轨道。")
        stream = videos[0]
        try:
            duration = float(stream.get("duration") or result["format"]["duration"])
            fps = float(Fraction(stream["avg_frame_rate"]))
            width, height = int(stream["width"]), int(stream["height"])
        except (KeyError, ValueError, ZeroDivisionError):
            raise ValidationError("视频缺少有效的时长、尺寸或帧率。") from None
        if not math.isfinite(duration) or abs(duration - parameters["duration"]) > 0.3 or fps <= 0 or min(width, height) <= 0:
            raise ValidationError("视频时长或帧率不符合请求。")
        expected_ratio = float(Fraction(parameters["ratio"].replace(":", "/")))
        if abs(width / height / expected_ratio - 1) > 0.02:
            raise ValidationError("视频画幅与请求不符。")
        if abs(min(width, height) - int(parameters["resolution"][:-1])) > 16:
            raise ValidationError("视频分辨率与请求不符。")
        has_audio = any(s.get("codec_type") == "audio" for s in result.get("streams", []))
        if require_audio and not has_audio:
            raise ValidationError("正式成片缺少音频轨道。")
        self.execute([self.ffmpeg, "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe", "-i", str(Path(path).resolve()), "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"])
        return {"duration": duration, "width": width, "height": height, "fps": fps, "has_audio": has_audio}

    def check_audio(self, path):
        result = self.probe(path)
        if not any(s.get("codec_type") == "audio" for s in result.get("streams", [])):
            raise ValidationError("素材没有音频轨道：" + Path(path).name)
        try:
            duration = float(result["format"]["duration"])
        except (KeyError, ValueError):
            raise ValidationError("音频时长无效。") from None
        if not math.isfinite(duration) or duration <= 0:
            raise ValidationError("音频时长无效。")
        return duration

    def prepare_clip(self, source, target, parameters):
        self.require_tools()
        width, height = dimensions(parameters["ratio"], parameters["resolution"])
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.stem + ".part.mp4")
        duration = parameters["duration"]
        vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,tpad=stop_mode=clone:stop_duration=0.3,trim=duration={duration},setpts=PTS-STARTPTS"
        self.execute([self.ffmpeg, "-nostdin", "-y", "-v", "error", "-protocol_whitelist", "file,pipe", "-i", str(Path(source).resolve()), "-map", "0:v:0", "-an", "-vf", vf,
                      "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", "-threads", "2", str(temporary)])
        self.check_clip(temporary, parameters)
        temporary.replace(target)

    def extract_tail(self, prepared_clip, target, duration):
        # Select the last frame of the exact trimmed, 30fps clip used in the final concat.
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.stem + ".part.png")
        frame_index = round(duration * 30) - 1
        self.execute([self.ffmpeg, "-nostdin", "-y", "-v", "error", "-protocol_whitelist", "file,pipe", "-i", str(Path(prepared_clip).resolve()),
                      "-vf", f"select=eq(n\\,{frame_index})", "-frames:v", "1", "-update", "1", str(temporary)])
        if not temporary.exists() or temporary.stat().st_size == 0:
            raise ValidationError("未能提取实际剪辑末帧。")
        temporary.replace(target)

    def compose(self, run_path, clips, tracks, parameters, *, preview=False, prepared=False):
        self.require_tools()
        run_path = Path(run_path).resolve()
        work = run_path / "composition"
        work.mkdir(exist_ok=True)
        parts = []
        for i, (clip, duration) in enumerate(clips):
            target = work / f"part-{i:03}.mp4"
            if prepared:
                shutil.copyfile(clip, target)
            else:
                self.prepare_clip(clip, target, {**parameters, "duration": duration})
            parts.append(target.name)
        (work / "concat.txt").write_text("".join(f"file '{name}'\n" for name in parts), encoding="utf-8")
        silent = work / "silent.mp4"
        self.execute([self.ffmpeg, "-nostdin", "-y", "-v", "error", "-f", "concat", "-safe", "1", "-protocol_whitelist", "file,pipe", "-i", "concat.txt", "-an", "-c:v", "copy", "-movflags", "+faststart", str(silent)], cwd=work)
        if preview:
            return silent
        total = parameters["duration"]
        command = [self.ffmpeg, "-nostdin", "-y", "-v", "error", "-protocol_whitelist", "file,pipe", "-i", str(silent)]
        filters, labels = [], []
        for i, track in enumerate(tracks, 1):
            if track["name"] == "music":
                command += ["-stream_loop", "-1"]
            command += ["-protocol_whitelist", "file,pipe", "-i", str(run_path / track["path"])]
            label = f"a{i}"
            delay = round(track["at_seconds"] * 1000)
            filters.append(f"[{i}:a:0]aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS,volume={track['gain']},adelay={delay}:all=1,apad,atrim=duration={total}[{label}]")
            labels.append(f"[{label}]")
        filters.append("".join(labels) + f"amix=inputs={len(labels)}:duration=longest:normalize=0,alimiter=limit=0.95:latency=1,afade=t=out:st={max(0,total-0.5)}:d=0.5[aout]")
        final = run_path / "final/video.mp4"
        final.parent.mkdir(exist_ok=True)
        temporary = final.with_name("video.part.mp4")
        command += ["-filter_complex", ";".join(filters), "-map", "0:v:0", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", str(total), "-movflags", "+faststart", str(temporary)]
        self.execute(command)
        self.check_clip(temporary, parameters, require_audio=True)
        temporary.replace(final)
        return final
