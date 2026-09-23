# 本地依赖与接口依据

## Python

- `.venv` 使用 Python 3.12.14，不共享系统第三方包。
- 运行与测试均使用标准库，不需要 pip 安装业务依赖。

## FFmpeg

项目本地 Windows essentials 构建，包含 FFmpeg 与 FFprobe。目录 `.tools/ffmpeg/` 被版本管理忽略，不修改系统 PATH。

- 构建：9.0.2 essentials。
- 下载页：https://www.gyan.dev/ffmpeg/builds/
- 校验来源：https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip.sha256
- ZIP SHA-256：`60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba`
- 下载后先比较供应方 SHA-256，再解压；原始许可证保存在解压目录中。
- 项目不将二进制包纳入代码分发；如另行分发，应保留供应方许可证及其要求。

## 模型接口依据

视频接口核对日期：2026-09-23。

- 万相3.0视频生成：https://help.aliyun.com/zh/model-studio/wan3-video-generation-api-reference
- Qwen OpenAI 兼容聊天：https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions
- 深度思考参数：https://help.aliyun.com/zh/model-studio/deep-thinking
- FFmpeg 滤镜：https://ffmpeg.org/ffmpeg-filters.html

WanVideo适配wan3.0-video-prime，采用异步提交、任务编号查询与下载；提示词按20000字符校验。首帧续接使用ratio=adaptive，关闭原生音轨和提示词自动改写。本地单镜制作时长仍限定3–15秒。Qwen使用结构化JSON输出，当前关闭思考模式。
