const form = document.querySelector('#connect'), message = document.querySelector('#message');
const code = new URLSearchParams(location.hash.slice(1)).get('code');
if (code) { document.querySelector('#code').value = code; history.replaceState(null, '', '/connect'); }
fetch('/api/session').then(r => { if (r.ok) location.replace('/'); }).catch(() => {});
form.addEventListener('submit', async event => {
  event.preventDefault(); const button = form.querySelector('button'); button.disabled = true; message.textContent = '正在连接…';
  try {
    const response = await fetch('/api/session', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: document.querySelector('#code').value.trim() }) });
    const data = await response.json(); if (!response.ok) throw new Error(data.error);
    location.replace('/');
  } catch (error) { message.textContent = error.message || '无法连接服务器，请重试'; button.disabled = false; }
});
