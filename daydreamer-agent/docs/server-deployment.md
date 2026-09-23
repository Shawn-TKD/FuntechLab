# 单手机私测部署

部署目标：手机拍摄程序自动上传生活视频，后台自动提取事件卡并生成幻想片段，交互网页在云朵中播放结果。网页本身不上传素材、不直接调用模型。手机程序的接口约定见 [mobile-upload-api.md](mobile-upload-api.md)。

## 部署范围

`deploy/package.py` 使用文件白名单生成运行包和 SHA-256 清单：代码、配置、提示词、JSON 结构、`resources/daydreamer-style-transfer` 技能及其 references、网页程序和必要图片。

不包含 `data`、`outputs`、`inputs`、`tests`、独立 `examples`、本地 `.venv`、Windows 媒体工具、原始视频、演示视频、历史事件卡和旧任务。技能目录中的 `references/examples.md` 属于技能说明，随技能部署；它不是历史运行案例。模型密钥单独通过 SSH 传输，服务端权限为 root 600，不进入运行包。

## 服务

- `/opt/daydreamer/project`：Agent 运行代码；首次部署使用全新的 data 和 outputs。
- `/opt/daydreamer/web`：原交互网页与连接页面；旧上传入口重定向到观看页。
- `/var/lib/daydreamer`：Web 任务数据库、手机上传的视频、手机会话。
- `/etc/daydreamer/service.env`：服务环境及模型连接配置。
- `daydreamer-api.service`：仅监听 127.0.0.1:8765，由 Nginx 代理 HTTPS。
- `daydreamer-worker.service`：单任务后台执行，模型调用前保存提取任务编号。
- `daydreamer-cert-renew.timer`：每天两次检查并自动续期 IP 证书，续期后重载 Nginx。

按用户后续要求，观看流程已取消登录校验。主页、下一条片段、已知任务查询、视频读取、已播放标记无需登录；知道观看地址的人可以播放片段。上传仍需手机程序的上传凭据，完整任务列表和任务恢复仍需管理凭据。历史会话保留给管理接口，观看不再使用它。

主页直接显示原交互前端的线条小人和云朵；点击“开始”进入呼吸加载并等待成片，不再进入连接页。旧 `/connect` 入口直接重定向到主页。

## 接口约定

同源观看网页无需 Cookie 或连接码。手机拍摄程序使用独立的 DAYDREAMER_UPLOAD_TOKEN，不依赖浏览器绑定；管理接口继续使用原有会话。不要把模型密钥或 SSH 密码放进客户端。

| 接口 | 行为 |
|---|---|
| POST /api/session | 保留的管理会话接口，观看流程不再调用 |
| GET /api/session | 检查会话状态 |
| POST /api/jobs | 原始视频二进制请求体，Content-Type 为 video/mp4 等，必须提供 16–100 字符 Idempotency-Key；完成上传后返回 202 和任务编号 |
| GET /api/jobs | 列出最近任务与状态 |
| GET /api/jobs/{id} | 查询该任务，完成后提供 video_url 与 player_url |
| POST /api/jobs/{id}/resume | 只恢复失败或暂停的原任务，不创建新任务 |
| GET /api/jobs/{id}/video | 已完成成片，支持 HEAD 与 Range |
| POST /api/jobs/{id}/played | 播完后标记已播放 |
| GET /api/videos/next | 返回最早未播放成片，否则返回待处理任务，无任务则返回 null |

上传限制为 250MiB，最多 10 个未完成任务；保留至少 2GiB 磁盘余量。视频实际内容由 Agent 的 FFprobe 校验，文件扩展名或 MIME 不代表媒体有效。每个模型请求沿用原 Agent 的校验和检查点，不因刷新网页重新提交。

服务重启后最多自动恢复两次仍处于 processing 的任务，之后暂停并等待操作。网络或创作失败保留具体任务状态，页面提供“恢复原任务”。未收到模型明确结果的请求仍受原 Agent 的不确定提交保护约束，不能保证第三方接口的绝对零重复计费。

## 运维

```sh
systemctl status daydreamer-api daydreamer-worker
journalctl -u daydreamer-worker -n 50 --no-pager
systemctl list-timers daydreamer-cert-renew.timer
/opt/daydreamer/certbot/bin/certbot renew --dry-run
```

重置手机绑定（会使原手机退出，需要重新输入连接码）：

```sh
python3 - <<'PY'
import sqlite3
with sqlite3.connect('/var/lib/daydreamer/web.sqlite3') as db:
    db.execute('DELETE FROM sessions')
PY
```

备份应覆盖 Web 数据库、上传素材、Agent 的 data 与 outputs；数据库使用 SQLite 备份接口或停服务后复制。没有自动删除用户素材的任务，磁盘需要按实际使用量管理。

本轮验证不调用付费模型：原有离线测试、新增 Web 授权/去重/恢复/Range 测试，以及服务器系统、FFmpeg、服务状态、HTTPS 和目录检查。真实手机上传后的首条完整生成仍须用新素材验收。
