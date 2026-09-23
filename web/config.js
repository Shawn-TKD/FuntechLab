(() => {
  let currentJob;
  const requestedJob = new URLSearchParams(location.search).get('job');
  function sleep(signal) {
    return new Promise((resolve, reject) => {
      if (signal.aborted) return reject(new DOMException('Cancelled', 'AbortError'));
      const stop = () => { clearTimeout(timer); reject(new DOMException('Cancelled', 'AbortError')); };
      const timer = setTimeout(() => { signal.removeEventListener('abort', stop); resolve(); }, 4000);
      signal.addEventListener('abort', stop, { once: true });
    });
  }
  window.DEMO_CONFIG = {
    loadingDuration: 1000, expandDuration: 2400, collapseDuration: 480,
    respectReducedMotion: false, videoFit: 'cover',
    async resolveVideo({ signal }) {
      while (true) {
        const response = await fetch(requestedJob ? '/api/jobs/' + encodeURIComponent(requestedJob) : '/api/videos/next', { signal });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || '暂时无法获取片段');
        const job = requestedJob ? data : data.job;
        if (!job) {
          document.querySelector('#status').textContent = '正在等待手机拍摄的新片段…';
          await sleep(signal);
          continue;
        }
        currentJob = job.id;
        if (job.state === 'ready') { document.querySelector('#status').textContent = ''; return job.video_url; }
        if (['failed', 'paused'].includes(job.state)) throw new Error(job.message);
        document.querySelector('#status').textContent = job.message || '幻想片段正在准备中';
        await sleep(signal);
      }
    },
    async onVideoEnded() {
      if (currentJob) await fetch('/api/jobs/' + currentJob + '/played', { method: 'POST' });
    }
  };
})();
