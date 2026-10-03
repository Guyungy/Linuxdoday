// ==UserScript==
// @name         Linux.do 顺序阅读助手
// @namespace    linuxdoday
// @version      1.0.0
// @description  使用当前登录账号实时收集新话题，顺序打开与滚动；支持暂停和断点继续。
// @match        https://linux.do/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

(() => {
  'use strict';
  if (window.top !== window.self) return;
  const KEY = 'linuxdoday.reader.v1';
  const LOCK = `${KEY}.owner`;
  const owner = `${Date.now()}-${Math.random()}`;
  let state;
  try { state = JSON.parse(localStorage.getItem(KEY) || 'null'); } catch (_) {}
  state ||= { running: false, queue: [], done: 0, target: 200, seconds: 20, page: 0, phase: 'collect', message: '就绪' };
  let cancelled = false;
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const save = () => { localStorage.setItem(KEY, JSON.stringify(state)); render(); };
  const canonical = href => {
    try {
      const url = new URL(href, location.origin);
      const match = url.pathname.match(/^\/t\/[^/]+\/(\d+)/);
      return url.origin === location.origin && match ? `/t/topic/${match[1]}` : null;
    } catch (_) { return null; }
  };
  const panel = document.createElement('div');
  panel.style.cssText = 'position:fixed;right:12px;top:90px;z-index:2147483647;width:255px;padding:12px;background:#fff;color:#222;border:1px solid #aaa;border-radius:10px;box-shadow:0 3px 16px #0003;font:14px/1.6 system-ui';
  panel.innerHTML = '<b>Linux.do 顺序阅读</b><div data-status></div><label>主题数 <input data-target type="number" min="1" max="2000" style="width:65px"></label> <label>停留秒 <input data-seconds type="number" min="10" max="120" style="width:55px"></label><div style="margin-top:8px"><button data-start>新一批</button> <button data-resume>继续</button> <button data-pause>暂停</button></div><small>实时新话题 · 当前登录账号<br>验证时暂停；计数以 Connect 为准。</small>';
  document.body.append(panel);
  const status = panel.querySelector('[data-status]');
  const target = panel.querySelector('[data-target]');
  const seconds = panel.querySelector('[data-seconds]');
  target.value = state.target;
  seconds.value = state.seconds;
  function render() {
    status.textContent = `${state.running ? '运行中' : '已暂停'} · ${state.done}/${state.target} · 已选 ${state.queue.length} 条\n${state.message}`;
    status.style.whiteSpace = 'pre-line';
    panel.querySelector('[data-start]').disabled = state.running;
    panel.querySelector('[data-resume]').disabled = state.running || state.phase === 'finished';
  }
  const pause = message => {
    cancelled = true;
    state.running = false;
    state.message = message;
    save();
    if (JSON.parse(localStorage.getItem(LOCK) || 'null')?.owner === owner) localStorage.removeItem(LOCK);
  };
  function claim() {
    let lock;
    try { lock = JSON.parse(localStorage.getItem(LOCK) || 'null'); } catch (_) {}
    if (lock && lock.owner !== owner && Date.now() - lock.time < 12000) {
      state.running = false;
      state.message = '另一个标签正在运行，请先在那里暂停。';
      render();
      return false;
    }
    localStorage.setItem(LOCK, JSON.stringify({ owner, time: Date.now() }));
    return true;
  }
  window.addEventListener('pagehide', () => {
    if (JSON.parse(localStorage.getItem(LOCK) || 'null')?.owner === owner) localStorage.removeItem(LOCK);
  });
  window.addEventListener('storage', event => {
    if (event.key === KEY) {
      const latest = JSON.parse(event.newValue || 'null');
      if (latest && !latest.running) { state = latest; cancelled = true; render(); }
    }
  });
  panel.querySelector('[data-pause]').onclick = () => pause('已手动暂停');
  panel.querySelector('[data-start]').onclick = () => {
    state = { running: true, queue: [], done: 0, target: Math.min(2000, Math.max(1, Number(target.value) || 200)), seconds: Math.min(120, Math.max(10, Number(seconds.value) || 20)), page: 0, phase: 'collect', message: '实时加载新话题' };
    cancelled = false;
    if (!claim()) return;
    save();
    location.assign('/new');
  };
  panel.querySelector('[data-resume]').onclick = () => {
    state.running = true;
    cancelled = false;
    if (!claim()) return;
    save();
    location.assign(state.phase === 'read' ? state.queue[state.done] : `/new?page=${state.page}`);
  };
  render();
  async function run() {
    if (!state.running || !claim()) return;
    const heartbeat = setInterval(() => {
      if (!cancelled) localStorage.setItem(LOCK, JSON.stringify({ owner, time: Date.now() }));
    }, 3000);
    try {
      // 页面初次加载可能需要几秒；不点击或绕过验证。
      let ready = false;
      for (let i = 0; i < 15 && !cancelled; i++) {
        if (document.querySelector('#current-user, #toggle-current-user')) { ready = true; break; }
        await sleep(1000);
      }
      if (cancelled) return;
      if (!ready) { pause('页面未就绪或需要登录/真人验证。手动处理后点“继续”。'); return; }
      if (state.phase === 'collect') {
        await sleep(3000);
        if (cancelled) return;
        const links = [...document.querySelectorAll('a.title.raw-link.raw-topic-link')].map(a => canonical(a.href)).filter(Boolean);
        const before = state.queue.length;
        state.queue = [...new Set([...state.queue, ...links])].slice(0, state.target);
        if (state.queue.length === before) { pause(`新话题列表没有更多可访问主题，已收集 ${before} 条。`); return; }
        if (state.queue.length < state.target) {
          state.page++;
          state.message = `已实时收集 ${state.queue.length} 条，继续下一页`;
          save();
          location.assign(`/new?page=${state.page}`);
        } else {
          state.phase = 'read';
          state.message = '选题完成，开始顺序阅读';
          save();
          location.assign(state.queue[0]);
        }
        return;
      }
      if (canonical(location.href) !== state.queue[state.done]) {
        pause('当前页面与任务不一致。点“继续”返回待阅读主题。'); return;
      }
      const start = Date.now();
      while (!cancelled && Date.now() - start < state.seconds * 1000) {
        state.message = `阅读第 ${state.done + 1} 条：${document.querySelector('h1')?.textContent.trim() || ''}`;
        render();
        await sleep(3000);
        if (cancelled) return;
        window.scrollBy({ top: Math.round(innerHeight * 0.65), behavior: 'smooth' });
      }
      if (cancelled) return;
      state.done++;
      if (state.done === state.target) {
        state.phase = 'finished';
        pause('本批完成，可到 Connect 核对数据；点“新一批”重新选题。');
      } else {
        state.message = '前往下一条主题';
        save();
        location.assign(state.queue[state.done]);
      }
    } catch (error) {
      pause(`运行暂停：${error.message}`);
    } finally {
      clearInterval(heartbeat);
    }
  }
  run();
})();
