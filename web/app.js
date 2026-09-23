(() => {
  'use strict';
  const config = window.DEMO_CONFIG;
  const app = document.querySelector('#app');
  const scene = document.querySelector('#scene');
  const start = document.querySelector('#start');
  const status = document.querySelector('#status');
  const video = document.querySelector('#video');
  const layer = document.querySelector('#video-layer');
  const outline = document.querySelector('#cloud-outline');
  const path = document.querySelector('#cloud-path');
  const tools = document.querySelector('.playback-tools');
  const resume = document.querySelector('#resume');
  const sound = document.querySelector('#sound');
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const shouldReduceMotion = () => config.respectReducedMotion === true && reduceMotion.matches;
  let state = 'idle', progress = 0, frame = 0, generation = 0, controller;
  let breathingFrame = 0, breathingScale = 1, loadingStarted = 0;
  const cloudRadii = [];
  let cloudReady;
  const setState = next => {
    if (state === 'loading' && next !== 'loading') {
      cancelAnimationFrame(breathingFrame);
      app.dataset.lastLoadingMs = String(Math.round(performance.now() - loadingStarted));
    }
    state = next;
    app.dataset.state = next;
    start.disabled = next !== 'idle';
    start.firstElementChild.textContent = next === 'loading' ? '加载中…' : '开始';
    tools.inert = next !== 'playing';
    layer.setAttribute('aria-hidden', String(next !== 'playing'));
    if (next === 'loading') startBreathing();
  };
  function startBreathing() {
    cancelAnimationFrame(breathingFrame);
    loadingStarted = performance.now();
    breathingScale = 1;
    function tick(now) {
      if (state !== 'loading') return;
      // 每秒一次完整呼吸：原大小 → 放大 12% → 原大小。
      const phase = (now - loadingStarted) / 1000 * Math.PI * 2;
      breathingScale = shouldReduceMotion() ? 1 : 1 + .06 * (1 - Math.cos(phase));
      render();
      breathingFrame = requestAnimationFrame(tick);
    }
    breathingFrame = requestAnimationFrame(tick);
  }
  // 使用预计算的原图轮廓，兼容 Chrome 直接打开本地网页的场景。
  async function prepareCloud() {
    const shape = window.CLOUD_SHAPE;
    if (!shape?.radii?.length || !shape.path) throw new Error('云朵素材未加载，请刷新页面');
    cloudRadii.push(...shape.radii);
    path.setAttribute('d', shape.path);
    const img = new Image();
    img.src = '素材/云朵.png';
    await img.decode();
    render();
  }
  const mix = (a, b, p) => a + (b - a) * p;
  const smooth = p => p * p * (3 - 2 * p);
  // 只有首尾各 12% 轻微缓动，中间保持均匀速度。
  const steady = p => {
    const ramp = .12;
    if (p < ramp) return p * p / (2 * ramp * (1 - ramp));
    if (p > 1 - ramp) return 1 - (1 - p) ** 2 / (2 * ramp * (1 - ramp));
    return (p - ramp / 2) / (1 - ramp);
  };
  function render() {
    const rect = scene.getBoundingClientRect();
    const host = app.getBoundingClientRect();
    const unit = rect.width / 402;
    const x = mix(rect.left - host.left + 240 * unit, host.width / 2, progress);
    const y = mix(rect.top - host.top + 324 * unit, host.height / 2, progress);
    const small = 161 * unit / 1280;
    // 按实际轮廓覆盖屏幕边缘，避免云朵提前铺满后仍在屏幕外放大。
    let large = small;
    for (let i = 0; i < cloudRadii.length; i++) {
      const angle = i * Math.PI / 360;
      const edge = Math.min(
        host.width / 2 / Math.max(1e-6, Math.abs(Math.cos(angle))),
        host.height / 2 / Math.max(1e-6, Math.abs(Math.sin(angle)))
      );
      large = Math.max(large, edge / Math.max(1, cloudRadii[i]));
    }
    const scale = mix(small * breathingScale, large * 1.005, progress);
    const transform = `translate(${x} ${y}) scale(${scale})`;
    path.setAttribute('transform', transform);
    outline.setAttribute('transform', transform);
    outline.style.opacity = String(1 - smooth(Math.min(1, progress / .72)));
    // 白色云朵底始终不透明，只有视频画面渐入，避免透出后方的小人。
    video.style.opacity = String(progress);
    scene.style.opacity = String(1 - Math.min(1, progress * 2.4));
  }
  function animate(target, duration) {
    cancelAnimationFrame(frame);
    const from = progress;
    const fromBreathingScale = breathingScale;
    const begin = performance.now();
    duration = shouldReduceMotion() ? 1 : duration;
    app.dataset.animationDuration = String(duration);
    return new Promise(resolve => {
      function tick(now) {
        const t = Math.min(1, (now - begin) / duration);
        progress = mix(from, target, target === 1 ? steady(t) : smooth(t));
        // 从呼吸的当前大小接续展开或收回，避免就绪时突然跳回原大小。
        breathingScale = mix(fromBreathingScale, 1, smooth(t));
        render();
        if (t < 1) frame = requestAnimationFrame(tick);
        else {
          app.dataset.lastAnimationMs = String(Math.round(now - begin));
          if (target === 1) app.dataset.lastExpandMs = app.dataset.lastAnimationMs;
          resolve();
        }
      }
      frame = requestAnimationFrame(tick);
    });
  }
  function waitForVideo(signal) {
    return new Promise((resolve, reject) => {
      let timer;
      const cleanup = () => {
        clearTimeout(timer);
        video.removeEventListener('loadeddata', ready);
        video.removeEventListener('seeked', ready);
        video.removeEventListener('canplay', ready);
        video.removeEventListener('error', fail);
        signal.removeEventListener('abort', abort);
      };
      const ready = () => { if (video.readyState >= 2) { cleanup(); resolve(); } };
      const fail = () => { cleanup(); reject(new Error('视频暂时无法打开，请再试一次')); };
      const abort = () => { cleanup(); reject(new DOMException('Cancelled', 'AbortError')); };
      video.addEventListener('loadeddata', ready);
      video.addEventListener('seeked', ready);
      video.addEventListener('canplay', ready);
      video.addEventListener('error', fail);
      signal.addEventListener('abort', abort, { once: true });
      timer = setTimeout(() => { cleanup(); reject(new Error('视频加载超时，请检查连接后重试')); }, 20000);
      if (signal.aborted) abort(); else if (video.error) fail(); else ready();
    });
  }
  async function begin() {
    if (state !== 'idle') return;
    const id = ++generation;
    controller = new AbortController();
    setState('loading');
    status.textContent = '';
    const delay = new Promise(resolve => setTimeout(resolve, Math.max(1000, Number(config.loadingDuration) || 1000)));
    try {
      const source = config.resolveVideo
        ? await config.resolveVideo({ signal: controller.signal }) : config.videoUrl;
      if (id !== generation) return;
      if (video.getAttribute('src') !== source || video.error) { video.src = source; video.load(); }
      video.currentTime = 0;
      await Promise.all([cloudReady, delay, waitForVideo(controller.signal)]);
      if (id !== generation) return;
      setState('expanding');
      await animate(1, config.expandDuration);
      if (id !== generation) return;
      setState('playing');
      if (document.hidden) { resume.textContent = '继续播放'; resume.hidden = false; return; }
      try { await video.play(); }
      catch { resume.hidden = false; }
    } catch (error) {
      await delay;
      if (id !== generation || error.name === 'AbortError') return;
      await finish(error.message || '暂时无法播放，请重试');
    }
  }
  async function finish(message = '') {
    if (state === 'collapsing' || state === 'idle') return;
    ++generation;
    controller?.abort();
    video.pause();
    resume.hidden = true;
    setState('collapsing');
    await animate(0, config.collapseDuration);
    video.currentTime = 0;
    status.textContent = message;
    setState('idle');
    start.focus({ preventScroll: true });
  }
  start.addEventListener('click', begin);
  document.querySelector('#close').addEventListener('click', () => finish());
  video.addEventListener('ended', () => {
    Promise.resolve(config.onVideoEnded?.()).catch(() => {});
    finish();
  });
  video.addEventListener('error', () => {
    if (state === 'playing' || state === 'expanding') finish('视频暂时无法播放，请重试');
  });
  resume.addEventListener('click', async () => {
    try { await video.play(); resume.hidden = true; }
    catch { resume.textContent = '点按重试'; }
  });
  sound.addEventListener('click', () => {
    video.muted = !video.muted;
    sound.classList.toggle('unmuted', !video.muted);
    sound.setAttribute('aria-label', video.muted ? '打开声音' : '关闭声音');
    sound.title = video.muted ? '打开声音' : '关闭声音';
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && state === 'playing') {
      video.pause(); resume.textContent = '继续播放'; resume.hidden = false;
    }
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') finish();
  });
  new ResizeObserver(render).observe(app);
  video.style.objectFit = config.videoFit || 'contain';
  if (config.videoUrl) video.src = config.videoUrl;
  cloudReady = prepareCloud();
  cloudReady.catch(() => { status.textContent = '图片加载失败，请刷新页面'; });
  render();
})();
