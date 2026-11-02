'use strict';

const KEY = new URLSearchParams(location.hash.slice(1)).get('k') || '';
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const ELEMENTS = {
  text: { name: 'Text', icon: 'text', desc: 'Your message frames' },
  time: { name: 'Time', icon: 'clock', desc: 'Current local time' },
  song: { name: 'Song', icon: 'music', desc: 'What is playing' },
  cpu: { name: 'CPU', icon: 'cpu', desc: 'Usage and model' },
  ram: { name: 'RAM', icon: 'ram', desc: 'Memory usage' },
  gpu: { name: 'GPU', icon: 'gpu', desc: 'Usage, VRAM, model' },
  hr: { name: 'Heart rate', icon: 'heart', desc: 'Pulsoid or HypeRate' },
  mute: { name: 'Mic mute', icon: 'mic', desc: 'Needs OSC listen' },
  playtime: { name: 'Play time', icon: 'hourglass', desc: 'Time in VRChat' },
  div: { name: 'Divider', icon: 'divider', desc: 'Horizontal line' },
  timer: { name: 'Timer', icon: 'timer', desc: 'Countdown' },
};
const TITLES = { chatbox: 'Chatbox', elements: 'Elements', sending: 'Sending', osc: 'OSC', settings: 'Settings', log: 'Log', help: 'Help' };

let cfg = {};
let logSince = 0;

const icon = name => `<svg class="i"><use href="#i-${name}"/></svg>`;

async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'X-Studio-Key': KEY, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

let toastTimer;
function toast(text) {
  const el = $('#toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 1600);
}

async function save(changes, quiet) {
  Object.assign(cfg, changes);
  refreshDerived();
  try {
    await api('/api/config', changes);
    if (!quiet) toast('Saved');
  } catch (e) {
    toast('Not saved: ' + e.message);
  }
}

// ---------------------------------------------------------------- navigation

function showPage(name) {
  if (!TITLES[name]) name = 'chatbox';
  $$('.page').forEach(p => p.classList.toggle('active', p.id === 'page-' + name));
  $$('.nav button').forEach(b => {
    if (b.dataset.page === name) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
  $('#page-title').textContent = TITLES[name];
  try { localStorage.setItem('studio-page', name); } catch (e) { /* storage unavailable */ }
}

// ---------------------------------------------------------------- generic field binding

function readField(el) {
  if (el.type === 'checkbox') return el.checked;
  if (el.type === 'number') return el.value === '' ? null : Number(el.value);
  return el.value;
}

function fillFields() {
  $$('[data-key]').forEach(el => {
    const value = cfg[el.dataset.key];
    if (el.type === 'checkbox') el.checked = !!value;
    else if (document.activeElement !== el) el.value = value ?? '';
  });
}

function bindFields() {
  $$('[data-key]').forEach(el => {
    // text saves on blur/enter (change), so half-typed text never reaches the chatbox
    el.addEventListener('change', () => {
      const value = readField(el);
      if (value === null) return;
      save({ [el.dataset.key]: value });
    });
    if (el.dataset.vars) addVarChips(el);
  });
  $$('[data-set]').forEach(btn => btn.addEventListener('click', () => save(JSON.parse(btn.dataset.set))));
  $$('[data-open]').forEach(btn => btn.addEventListener('click', e => {
    e.preventDefault();
    api('/api/action/open', { link: btn.dataset.open });
  }));
}

function addVarChips(input) {
  const box = document.createElement('div');
  box.className = 'vars';
  for (const name of input.dataset.vars.split(',')) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = `{${name}}`;
    b.title = 'Insert';
    b.addEventListener('click', () => {
      const pos = input.selectionStart ?? input.value.length;
      input.value = input.value.slice(0, pos) + b.textContent + input.value.slice(input.selectionEnd ?? pos);
      input.focus();
      input.setSelectionRange(pos + b.textContent.length, pos + b.textContent.length);
      input.dispatchEvent(new Event('change'));
    });
    box.append(b);
  }
  input.closest('label').append(box);
}

// segmented buttons, conditional blocks, theme, badges: everything derived from cfg
function refreshDerived() {
  $$('[data-set]').forEach(btn => {
    const set = JSON.parse(btn.dataset.set);
    btn.setAttribute('aria-pressed', Object.entries(set).every(([k, v]) => cfg[k] === v));
  });
  $$('[data-show-if]').forEach(el => {
    const expr = el.dataset.showIf;
    const neg = expr.startsWith('!');
    el.hidden = neg ? !!cfg[expr.slice(1)] : !cfg[expr];
  });
  if (cfg.theme === 'auto') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.dataset.theme = cfg.theme;
  const used = new Set((cfg.layout || []).map(e => e.t));
  for (const t of Object.keys(ELEMENTS)) {
    const badge = $(`#el-${t} .badge`);
    if (badge) badge.hidden = !used.has(t);
  }
  $$('[data-bind]').forEach(b => { b.textContent = cfg[b.dataset.bind] || 'Unbound'; });
}

// ---------------------------------------------------------------- layout editor

function renderPalette() {
  const box = $('#palette');
  box.innerHTML = '';
  for (const [t, meta] of Object.entries(ELEMENTS)) {
    const b = document.createElement('button');
    b.innerHTML = `${icon(meta.icon)}<span><b></b><small></small></span>`;
    b.querySelector('b').textContent = meta.name;
    b.querySelector('small').textContent = meta.desc;
    b.addEventListener('click', () => {
      saveLayout([...cfg.layout, { t, div: false, nl: t !== 'div' }]);
    });
    box.append(b);
  }
}

function saveLayout(layout) {
  cfg.layout = layout;
  renderArrange();
  save({ layout }, true);
}

let dragFrom = null;

function renderArrange() {
  const list = $('#arrange');
  list.innerHTML = '';
  if (!cfg.layout.length) {
    list.innerHTML = '<li class="empty">Nothing here yet — add elements below.</li>';
    return;
  }
  cfg.layout.forEach((el, i) => {
    const meta = ELEMENTS[el.t];
    const li = document.createElement('li');
    li.draggable = true;
    li.innerHTML = `
      <span class="grip" title="Drag to reorder">${icon('grip')}</span>
      <span class="name">${icon(meta.icon)}<button type="button" title="Edit settings"></button></span>
      <button class="flag" data-flag="div" title="Vertical divider after this element">┋<span>Divider</span></button>
      <button class="flag" data-flag="nl" title="Start a new line after this element">↵<span>New line</span></button>
      <button class="btn icon" data-move="-1" title="Move up" ${i === 0 ? 'disabled' : ''}>${icon('up')}</button>
      <button class="btn icon" data-move="1" title="Move down" ${i === cfg.layout.length - 1 ? 'disabled' : ''}>${icon('down')}</button>
      <button class="btn icon danger" data-del title="Remove">${icon('x')}</button>`;
    li.querySelector('.name button').textContent = meta.name;
    li.querySelector('.name button').addEventListener('click', () => openElement(el.t));
    li.querySelectorAll('.flag').forEach(f => {
      f.setAttribute('aria-pressed', !!el[f.dataset.flag]);
      f.addEventListener('click', () => {
        const next = cfg.layout.map((e, j) => j === i ? { ...e, [f.dataset.flag]: !e[f.dataset.flag] } : e);
        saveLayout(next);
      });
    });
    li.querySelectorAll('[data-move]').forEach(b => b.addEventListener('click', () => move(i, i + Number(b.dataset.move))));
    li.querySelector('[data-del]').addEventListener('click', () => saveLayout(cfg.layout.filter((_, j) => j !== i)));
    li.addEventListener('dragstart', () => { dragFrom = i; li.classList.add('dragging'); });
    li.addEventListener('dragend', () => { li.classList.remove('dragging'); $$('#arrange li').forEach(x => x.classList.remove('over')); });
    li.addEventListener('dragover', e => { e.preventDefault(); li.classList.add('over'); });
    li.addEventListener('dragleave', () => li.classList.remove('over'));
    li.addEventListener('drop', e => { e.preventDefault(); if (dragFrom !== null) move(dragFrom, i); dragFrom = null; });
    list.append(li);
  });
}

function move(from, to) {
  if (to < 0 || to >= cfg.layout.length || from === to) return;
  const next = [...cfg.layout];
  next.splice(to, 0, next.splice(from, 1)[0]);
  saveLayout(next);
}

function openElement(t) {
  showPage('elements');
  const d = $('#el-' + t);
  d.open = true;
  d.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ---------------------------------------------------------------- actions

function bindActions() {
  $$('.nav button').forEach(b => b.addEventListener('click', () => showPage(b.dataset.page)));
  $('#btn-run').addEventListener('click', () => api('/api/action/run', {}).then(poll));
  $('#btn-afk').addEventListener('click', () => api('/api/action/afk', {}).then(poll));

  $('#spotify-link').addEventListener('click', () => {
    const linked = (lastState.spotify || '').startsWith('Linked');
    if (linked && !confirm('Unlink Spotify?')) return;
    api('/api/action/' + (linked ? 'spotifyUnlink' : 'spotifyLink'), {}).then(load);
  });

  $('#timer-add').addEventListener('click', () => {
    const seconds = Number($('#timer-amount').value) * Number($('#timer-unit').value);
    if (seconds > 0) api('/api/action/timerAdd', { seconds }).then(load);
  });
  $('#timer-reset').addEventListener('click', () => api('/api/action/timerReset', {}).then(load));

  $('#dbg-send').addEventListener('click', () =>
    api('/api/action/debugSend', { path: $('#dbg-path').value, type: $('#dbg-type').value, value: $('#dbg-value').value })
      .then(() => toast('Sent'), e => toast(e.message)));

  $$('[data-bind]').forEach(btn => btn.addEventListener('click', async () => {
    btn.textContent = 'Press a key…';
    btn.disabled = true;
    try {
      const { key } = await api('/api/action/bindKey', {});
      await save({ [btn.dataset.bind]: key });
    } finally {
      btn.disabled = false;
      refreshDerived();
    }
  }));

  $('#pick-file').addEventListener('click', async () => {
    const { path } = await api('/api/action/pickFile', {});
    if (path) save({ FileToRead: path }).then(fillFields);
  });
  $('#open-folder').addEventListener('click', () => api('/api/action/openFolder', {}));
  $('#reset').addEventListener('click', async () => {
    if (!confirm('Reset every setting to its default? Your Spotify link is kept.')) return;
    await api('/api/action/reset', {});
    load();
  });
  $('#check-update').addEventListener('click', () => api('/api/action/checkUpdate', {}).then(poll));
  $('#log-copy').addEventListener('click', () => navigator.clipboard.writeText($('#log').innerText).then(() => toast('Copied')));
}

// ---------------------------------------------------------------- live state

let lastState = {};

function setText(sel, text) {
  const el = $(sel);
  if (el.textContent !== text) el.textContent = text;
}

function renderState(s) {
  lastState = s;
  const run = $('#btn-run');
  run.setAttribute('aria-pressed', s.run);
  run.innerHTML = s.run ? `${icon('play')}<span>Running</span>` : `${icon('pause')}<span>Paused</span>`;
  $('#btn-afk').setAttribute('aria-pressed', s.afk);
  $('#chip-vrchat').classList.toggle('on', s.vrchat);

  const chipHr = $('#chip-hr');
  chipHr.hidden = !s.hr;
  chipHr.classList.toggle('on', !!s.hr);
  chipHr.lastElementChild.textContent = s.hr ? `${s.hr} bpm` : '';

  // chatbox preview
  const text = (s.preview || '').replace(/\v/g, '\n');
  setText('#bubble', text || ' ');
  $('#bubble').classList.toggle('paused', !s.run);
  setText('#bubble-state', s.run ? (s.afk ? 'AFK' : 'Live') : 'Paused');
  const count = $('#bubble-count');
  count.textContent = `${[...(s.preview || '')].length}/144`;
  count.classList.toggle('over', [...(s.preview || '')].length > 144);
  setText('#next-in', s.run && s.nextIn != null ? `next in ${s.nextIn.toFixed(1)} s` : '');

  // now playing
  const m = s.media || {};
  $('#np-card').hidden = !(cfg.showSongInfo && m.title);
  setText('#np-title', (m.playing ? '' : '⏸ ') + (m.title || ''));
  setText('#np-artist', m.artist || '');

  // stats
  setText('#st-cpu', s.cpu.percent !== '' ? `${Math.round(s.cpu.percent)}%` : '–');
  setText('#st-cpu-name', s.cpu.name || '');
  setText('#st-gpu', s.gpu.gpu_name ? `${s.gpu.gpu_percent}%` : '–');
  setText('#st-gpu-name', s.gpu.gpu_name ? `${s.gpu.gpu_name} · VRAM ${s.gpu.vram_percent}%` : '');
  setText('#st-ram', s.ram.ram_percent !== undefined ? `${s.ram.ram_percent}%` : '–');
  setText('#st-ram-detail', s.ram.ram_total ? `${s.ram.ram_used} / ${s.ram.ram_total} GB` : '');
  setText('#st-hr', s.hr ? String(s.hr) : '–');
  setText('#st-hr-src', s.hr ? (cfg.useHypeRate ? 'HypeRate' : 'Pulsoid') : 'not connected');

  // spotify
  const st = $('#spotify-status');
  st.textContent = s.spotify;
  st.className = 'status ' + (s.spotify.startsWith('Linked') ? 'ok' : 'warn');
  $('#spotify-link span').textContent = s.spotify.startsWith('Linked') ? 'Unlink Spotify' : 'Link Spotify';

  // gpu choices (once they're known)
  const sel = $('#gpu-choice');
  if (s.gpus.length && sel.options.length === 1) {
    for (const g of s.gpus) {
      const o = document.createElement('option');
      o.value = g.luid;
      const vram = g.vram >= 1073741824 ? `${Math.round(g.vram / 1073741824)} GB` : `${Math.round(g.vram / 1048576)} MB`;
      o.textContent = `${g.name} — ${vram}${g.display ? ', drives a monitor' : ''}`;
      sel.append(o);
    }
    sel.value = cfg.gpuChoice;
  }

  // timer
  const t = s.timerLeft;
  setText('#timer-left', [Math.floor(t / 3600), Math.floor(t % 3600 / 60), t % 60].map(n => String(n).padStart(2, '0')).join(':'));

  // about
  setText('#about', `Version ${s.version}` + (s.latest ? ` · latest release ${s.latest}` : ''));

  // log
  if (s.log.length) {
    const box = $('#log');
    const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
    for (const [id, stamp, line] of s.log) {
      const row = document.createElement('div');
      row.innerHTML = '<span></span> ';
      row.firstChild.textContent = stamp;
      row.append(line);
      box.append(row);
      logSince = id;
    }
    while (box.childElementCount > 500) box.firstChild.remove();
    if (stick) box.scrollTop = box.scrollHeight;
  }
}

async function poll() {
  try {
    renderState(await api('/api/state?log=' + logSince));
  } catch (e) { /* backend restarting or closed */ }
}

async function load() {
  const data = await api('/api/config');
  cfg = data.config;
  $('#version').textContent = 'v' + data.version;
  fillFields();
  refreshDerived();
  renderArrange();
}

// the sidebar folds into the page on narrow windows
function placeSide() {
  const narrow = matchMedia('(max-width: 1180px)').matches;
  const side = $('#side');
  const slot = $('.side-inline');
  if (narrow && side.parentElement !== slot) {
    slot.append(...side.children);
  } else if (!narrow && slot.childElementCount) {
    side.append(...slot.children);
  }
}

(async function init() {
  bindFields();
  bindActions();
  renderPalette();
  let page = 'chatbox';
  try { page = localStorage.getItem('studio-page') || page; } catch (e) { /* storage unavailable */ }
  showPage(page);
  placeSide();
  matchMedia('(max-width: 1180px)').addEventListener('change', placeSide);
  await load();
  poll();
  setInterval(poll, 500);
})();
