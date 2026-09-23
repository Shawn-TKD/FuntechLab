# Agent 源码与部署文件包

本包来自当前本地项目，包含 Agent 源码、配置、提示词、结构定义、部署脚本、接口文档、交互网页及 Agent 使用的 daydreamer-style-transfer skill 全部参考资料。

## 已排除
- 历史上传视频、生成视频、事件卡案例、任务数据库、日志和运行结果。
- examples、tests、inputs、data、outputs，以及前端测试视频和验证产物。
- 针对旧案例的 refine_continuity_sample.py、finalize_continuity_sample.py。
- 虚拟环境、缓存、FFmpeg 二进制和下载包。
- 真实 API Key、密钥 CSV、SSH 凭据、连接码及上传令牌。

Skill 的 references/examples.md 是技能参考文档，因此保留。src 中的 demo 命令实现也保留以保持源码完整，但由于案例数据未打包，不能直接运行旧 demo。原 README 中指向已排除示例的命令也需要换成自己的输入文件。

## 使用
1. 阅读 daydreamer-agent/README.md 和 docs/dependencies.md，准备 Python 3.12、FFmpeg 和 FFprobe，并重新创建运行环境。
2. 按 .env.example / 部署文档自行配置凭据。config/default.toml 的本机密钥 CSV 路径已置空。
3. 服务器部署参见 daydreamer-agent/docs/server-deployment.md；手机上传接口参见 docs/mobile-upload-api.md。
4. 网页交互端/config.js 使用同源服务器接口，需与后端配合使用；静态单独打开不能完成视频生成和获取。
5. 在上述两个目录保持同级的情况下，运行 daydreamer-agent/deploy/package.py 可生成服务器运行包。部署脚本中的地址、证书和环境配置需按目标服务器检查。

本包不包含服务器运行数据；解压不会修改现有服务器。manifest.json 列出每个文件的大小和 SHA-256。
