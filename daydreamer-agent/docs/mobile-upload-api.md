# 手机拍摄程序接入

手机端程序在拍摄结束后自动发送视频，用户不需要打开上传页面。服务器接收完成后自动排队执行：分析视频 → 保存事件卡 → 创作故事与分镜 → 生成幻想片段 → 拼接保存。网页只负责等待和播放成片。

## 接收地址

`POST https://your-server.example.com/api/jobs`

请求体直接放视频文件的二进制内容，不是 JSON，也不是 multipart/form-data。

| 请求头 | 值 |
|---|---|
| Authorization | `Bearer <upload_token>` |
| Content-Type | MP4 使用 `video/mp4`；也接受 video/quicktime、video/webm、video/x-matroska、video/3gpp |
| Content-Length | 视频文件的字节大小，由 HTTP 客户端根据文件长度设置 |
| Idempotency-Key | 每次拍摄生成一个 UUID；同一视频上传重试必须沿用同一个值 |

上传专用凭据位于工作区 `.cache/daydreamer-deploy/mobile-upload.json`。将其配置在手机端负责发送视频的程序中，不嵌入观看网页。它与网页连接码、SSH 密码及百炼 API Key 不同。

单文件上限 250MiB，内容最长 30 分钟。视频必须已拍摄完毕并可读取确定的文件大小；接口不接受未知长度的分块上传。上传完成前保持请求连接，收到成功响应后即可断开，后台会继续生成。

```sh
curl --fail-with-body 'https://your-server.example.com/api/jobs' \
  -H "Authorization: Bearer $DAYDREAMER_UPLOAD_TOKEN" \
  -H 'Content-Type: video/mp4' \
  -H "Idempotency-Key: $RECORDING_ID" \
  --data-binary '@recording.mp4'
```

首次成功返回 HTTP 202；同一个 Idempotency-Key 已接收成功时返回 HTTP 200，并返回同一个任务编号，不重复生成。

响应示例（编号仅作说明）：

```json
{
  "id": "0123456789abcdef0123456789abcdef",
  "state": "queued",
  "created": 1790150000,
  "updated": 1790150000,
  "message": "上传完成，等待分析视频",
  "played": 0,
  "video_url": null,
  "player_url": "/?job=0123456789abcdef0123456789abcdef"
}
```

## 查询状态（可选）

`GET https://your-server.example.com/api/jobs/{id}`，建议间隔至少 5 秒查询。`ready` 时提供 `video_url`。按当前免登录观看要求，已知任务状态和成片读取不再要求浏览器绑定。

状态：`queued` 等待处理，`processing` 正在处理，`ready` 可播放，`failed` 失败，`paused` 暂停。失败时保留已完成的事件卡和生成进度；不要换一个上传编号重复创建付费任务，先让管理员恢复原任务。

错误：401 凭据缺失/无效，403 凭据无此接口权限，409 同一编号仍在上传，413 大小超限，415 媒体类型不支持，429 队列已满，507 磁盘空间不足。连接中断时使用原 Idempotency-Key 重试。

## 观看网页

`https://your-server.example.com/`

沿用“交互网页”任务中的线条小人和不透明云朵。无需连接码或登录，进入后点击“开始”，云朵呼吸等待；新成片准备好后展开播放，结束后缩回。没有成片时等待手机程序自动上传，不再引导手动选择文件。

手机上传程序仍需上传专用凭据。观看相关接口（下一条片段、已知任务查询、视频读取、已播放标记）不校验登录，知道地址的人可以观看。完整任务列表和恢复付费任务等管理操作仍受保护。旧连接页直接跳转到观看页。
