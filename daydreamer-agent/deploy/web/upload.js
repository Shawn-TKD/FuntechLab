const form = document.querySelector('#upload'), message = document.querySelector('#message'), submit = document.querySelector('#submit');
let key, selection;
const states = { uploading: '上传中', queued: '等待处理', processing: '正在生成', ready: '已完成', failed: '处理未完成', paused: '等待恢复' };
document.querySelector('#file').addEventListener('change', () => { key = null; selection = null; });
async function refresh() {
  try {
    const response = await fetch('/api/jobs');
    if (response.status === 401) { location.replace('/connect'); return; }
    if (!response.ok) return;
    const { jobs } = await response.json(); const container = document.querySelector('#jobs'); container.replaceChildren();
    if (!jobs.length) { container.textContent = '上传第一段生活视频后，会在这里显示进度。'; return; }
    for (const job of jobs) {
      const item = document.createElement('div'); item.className = 'task';
      const title = document.createElement('strong'); title.textContent = states[job.state] || job.state;
      const detail = document.createElement('p'); detail.textContent = job.message;
      const date = document.createElement('p'); date.className = 'small'; date.textContent = new Date(job.created * 1000).toLocaleString();
      item.append(title, detail, date);
      if (job.state === 'ready') { const link = document.createElement('a'); link.textContent = '在云朵中播放'; link.href = job.player_url; item.append(link); }
      if (['failed', 'paused'].includes(job.state)) {
        const retry = document.createElement('button'); retry.textContent = '恢复原任务';
        retry.onclick = async () => { retry.disabled = true; try { const r = await fetch('/api/jobs/' + job.id + '/resume', { method: 'POST' }); if (!r.ok) message.textContent = (await r.json()).error; await refresh(); } catch { message.textContent = '网络中断，请稍后重试'; } finally { retry.disabled = false; } }; item.append(retry);
      }
      container.append(item);
    }
  } catch { /* Keep the last known status during a temporary disconnection. */ }
}
form.addEventListener('submit', event => {
  event.preventDefault(); const file = document.querySelector('#file').files[0];
  if (!file) return;
  if (file.size <= 0 || file.size > 250 * 1024 * 1024) { message.textContent = '请选择不超过 250MB 的视频'; return; }
  if (selection !== file || !key) { key = Array.from(crypto.getRandomValues(new Uint8Array(16)), x => x.toString(16).padStart(2, '0')).join(''); selection = file; }
  const types = { mp4: 'video/mp4', mov: 'video/quicktime', webm: 'video/webm', mkv: 'video/x-matroska', '3gp': 'video/3gpp' };
  const type = types[file.name.split('.').pop().toLowerCase()] || file.type;
  const xhr = new XMLHttpRequest(), progress = document.querySelector('#progress');
  submit.disabled = true; progress.hidden = false; message.textContent = '上传中，请保持页面打开…';
  xhr.open('POST', '/api/jobs'); xhr.setRequestHeader('Content-Type', type); xhr.setRequestHeader('Idempotency-Key', key); xhr.timeout = 20 * 60 * 1000;
  xhr.upload.onprogress = event => { if (event.lengthComputable) progress.value = event.loaded / event.total * 100; };
  xhr.onload = () => {
    submit.disabled = false;
    if (xhr.status === 401) { location.replace('/connect'); return; }
    try { const data = JSON.parse(xhr.responseText); if (xhr.status >= 400) throw new Error(data.error);
      message.textContent = '上传完成！服务器会继续处理，可以稍后回来查看。'; progress.value = 100; refresh();
    } catch (error) { message.textContent = error.message || '上传失败，请重试'; }
  };
  xhr.onerror = xhr.ontimeout = () => { submit.disabled = false; message.textContent = '连接中断，请点击重试；同一上传请求不会重复创建生成任务。'; };
  xhr.send(file);
});
refresh(); setInterval(refresh, 5000);
