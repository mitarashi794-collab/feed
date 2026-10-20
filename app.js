'use strict';

/* ===== 定数 ===== */
const KEYS = {
  read: 'feed.read',
  fav: 'feed.fav',
  userSources: 'feed.userSources',
  hidden: 'feed.hiddenSources',
  prefs: 'feed.prefs'
};
const CATEGORY_LABELS = { news: 'ニュース', blog: 'ブログ', video: '動画', sns: 'SNS' };
const CATEGORY_ICONS = { news: '📰', blog: '✍️', video: '▶️', sns: '💬' };
const TYPE_LABELS = { rss: 'RSS/Atom', youtube: 'YouTube', misskey: 'Misskey', html: 'HTML抽出' };
const STALE_MS = 3 * 60 * 60 * 1000;      // 3時間以上更新がなければ警告
const RELOAD_MS = 10 * 60 * 1000;         // 復帰時の再読込間隔
const PAGE_SIZE = 40;
const MAX_READ_KEEP = 3000;

/* ===== ユーティリティ ===== */
const $ = (id) => document.getElementById(id);

function loadJSON(key, fallback) {
  try {
    const v = localStorage.getItem(key);
    return v ? JSON.parse(v) : fallback;
  } catch (e) {
    return fallback;
  }
}

function saveJSON(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch (e) {
    /* 容量超過やプライベートモードでは保存を諦める */
  }
}

function asArray(v) {
  return Array.isArray(v) ? v : [];
}

function str(v, max) {
  return typeof v === 'string' ? v.slice(0, max) : '';
}

/* http/https のみ許可。それ以外（javascript: など）は空文字を返す */
function safeUrl(u) {
  if (typeof u !== 'string' || u.length > 2000) return '';
  try {
    const parsed = new URL(u);
    if (parsed.protocol === 'https:' || parsed.protocol === 'http:') return parsed.href;
  } catch (e) { /* 不正なURL */ }
  return '';
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function keyOf(item) {
  return item.id || item.url;
}

function toTime(iso) {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? 0 : t;
}

function fmtDateTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '不明';
  return d.toLocaleString('ja-JP', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function relTime(iso) {
  const t = toTime(iso);
  if (!t) return '';
  const diff = Date.now() - t;
  if (diff < 60 * 1000) return 'たった今';
  if (diff < 3600 * 1000) return Math.floor(diff / 60000) + '分前';
  if (diff < 86400 * 1000) return Math.floor(diff / 3600000) + '時間前';
  if (diff < 7 * 86400 * 1000) return Math.floor(diff / 86400000) + '日前';
  return new Date(t).toLocaleDateString('ja-JP', { year: 'numeric', month: 'numeric', day: 'numeric' });
}

/* ===== 状態 ===== */
const state = {
  items: [],
  generatedAt: null,
  errors: [],
  repoSources: [],
  userSources: asArray(loadJSON(KEYS.userSources, [])).filter(isValidSource),
  hidden: new Set(asArray(loadJSON(KEYS.hidden, []))),
  read: new Set(asArray(loadJSON(KEYS.read, []))),
  fav: new Set(asArray(loadJSON(KEYS.fav, []))),
  prefs: Object.assign(
    { category: 'all', sort: 'desc', unreadOnly: false, favOnly: false },
    loadJSON(KEYS.prefs, {})
  ),
  query: '',
  limit: PAGE_SIZE,
  loadedAt: 0,
  loadFailed: false
};

function isValidItem(it) {
  return it && typeof it === 'object' && typeof it.title === 'string' && safeUrl(it.url);
}

function isValidSource(s) {
  return s && typeof s === 'object' && typeof s.id === 'string' && typeof s.name === 'string' && safeUrl(s.url);
}

function saveRead() {
  while (state.read.size > MAX_READ_KEEP) {
    state.read.delete(state.read.values().next().value);
  }
  saveJSON(KEYS.read, Array.from(state.read));
}
function saveFav() { saveJSON(KEYS.fav, Array.from(state.fav)); }
function saveHidden() { saveJSON(KEYS.hidden, Array.from(state.hidden)); }
function saveUserSources() { saveJSON(KEYS.userSources, state.userSources); }
function savePrefs() { saveJSON(KEYS.prefs, state.prefs); }

/* ===== データ読み込み ===== */
async function fetchJSON(path) {
  const res = await fetch(path, { cache: 'no-cache' });
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return res.json();
}

async function loadData() {
  const [feedRes, srcRes] = await Promise.allSettled([
    fetchJSON('data/feed.json'),
    fetchJSON('sources.json')
  ]);

  if (feedRes.status === 'fulfilled') {
    const f = feedRes.value || {};
    state.items = asArray(f.items).filter(isValidItem);
    state.generatedAt = typeof f.generated_at === 'string' ? f.generated_at : null;
    state.errors = asArray(f.errors);
    state.loadFailed = false;
  } else {
    state.loadFailed = true;
  }

  if (srcRes.status === 'fulfilled' && srcRes.value) {
    state.repoSources = asArray(srcRes.value.sources).filter(isValidSource);
    /* sources.json に載った情報源は、スマホ内の「自分で追加」から外す */
    const repoUrls = new Set(state.repoSources.map((s) => s.url));
    const before = state.userSources.length;
    state.userSources = state.userSources.filter((s) => !repoUrls.has(s.url));
    if (state.userSources.length !== before) saveUserSources();
  }

  state.loadedAt = Date.now();
  render();
  renderSettings();
}

/* ===== 絞り込み・並べ替え ===== */
function getVisibleItems() {
  const terms = state.query.toLowerCase().split(/\s+/).filter(Boolean);
  const list = state.items.filter((it) => {
    if (it.source_id && state.hidden.has(it.source_id)) return false;
    if (state.prefs.category !== 'all' && it.category !== state.prefs.category) return false;
    const id = keyOf(it);
    if (state.prefs.unreadOnly && state.read.has(id)) return false;
    if (state.prefs.favOnly && !state.fav.has(id)) return false;
    if (terms.length) {
      const hay = (str(it.title, 300) + ' ' + str(it.summary, 600) + ' ' + str(it.source, 100)).toLowerCase();
      if (!terms.every((t) => hay.includes(t))) return false;
    }
    return true;
  });
  const dir = state.prefs.sort === 'asc' ? 1 : -1;
  list.sort((a, b) => dir * (toTime(a.published) - toTime(b.published)));
  return list;
}

/* ===== 描画 ===== */
function render() {
  renderStatus();

  const timeline = $('timeline');
  timeline.replaceChildren();

  const all = getVisibleItems();
  const shown = all.slice(0, state.limit);
  shown.forEach((it) => timeline.append(renderCard(it)));

  $('empty').hidden = all.length > 0;
  $('moreBtn').hidden = all.length <= state.limit;

  document.querySelectorAll('.chip').forEach((chip) => {
    chip.classList.toggle('active', chip.dataset.cat === state.prefs.category);
  });
  $('sortSelect').value = state.prefs.sort;
  $('unreadToggle').setAttribute('aria-pressed', String(state.prefs.unreadOnly));
  $('favToggle').setAttribute('aria-pressed', String(state.prefs.favOnly));
}

function renderStatus() {
  const bar = $('statusBar');
  let text = '';
  let warn = false;

  if (state.generatedAt) {
    text = '最終更新 ' + fmtDateTime(state.generatedAt);
    if (Date.now() - toTime(state.generatedAt) > STALE_MS) {
      text += '（更新が遅れています）';
      warn = true;
    }
  } else {
    text = '最終更新 不明';
  }
  if (state.errors.length) {
    text += ' ・ 取得エラー ' + state.errors.length + '件';
    warn = true;
  }
  if (state.loadFailed) {
    text += ' ・ データを読み込めませんでした';
    warn = true;
  }
  bar.textContent = text;
  bar.classList.toggle('warn', warn);
}

function renderCard(item) {
  const id = keyOf(item);
  const category = CATEGORY_LABELS[item.category] ? item.category : 'news';
  const card = el('article', 'card');
  if (state.read.has(id)) card.classList.add('is-read');

  const thumb = safeUrl(item.thumbnail);
  if (thumb && thumb.startsWith('https:')) {
    const img = document.createElement('img');
    img.className = 'thumb';
    img.alt = '';
    img.loading = 'lazy';
    img.referrerPolicy = 'no-referrer';
    img.src = thumb;
    img.addEventListener('error', () => {
      img.replaceWith(el('div', 'thumb-ph', CATEGORY_ICONS[category]));
    });
    card.append(img);
  } else {
    card.append(el('div', 'thumb-ph', CATEGORY_ICONS[category]));
  }

  const body = el('div', 'card-body');

  const meta = el('div', 'meta');
  meta.append(el('span', 'badge', CATEGORY_LABELS[category]));
  meta.append(el('span', '', str(item.source, 60) || '不明な情報源'));
  const time = el('span', '', '・ ' + relTime(item.published));
  time.title = fmtDateTime(item.published);
  meta.append(time);
  body.append(meta);

  const title = el('a', 'card-title', str(item.title, 300));
  title.href = safeUrl(item.url);
  title.target = '_blank';
  title.rel = 'noopener noreferrer';
  title.addEventListener('click', () => setRead(id, card, true));
  body.append(title);

  const summary = str(item.summary, 600);
  if (summary) body.append(el('p', 'card-summary', summary));

  card.append(body);

  const actions = el('div', 'card-actions');

  const readBtn = el('button', 'act-btn');
  readBtn.type = 'button';
  readBtn.addEventListener('click', () => setRead(id, card, !state.read.has(id)));
  readBtn.dataset.role = 'read';

  const favBtn = el('button', 'act-btn');
  favBtn.type = 'button';
  favBtn.dataset.role = 'fav';
  favBtn.addEventListener('click', () => {
    if (state.fav.has(id)) state.fav.delete(id);
    else state.fav.add(id);
    saveFav();
    updateCardButtons(card, id);
  });

  actions.append(readBtn, favBtn);
  card.append(actions);
  updateCardButtons(card, id);
  return card;
}

function setRead(id, card, value) {
  if (value) state.read.add(id);
  else state.read.delete(id);
  saveRead();
  updateCardButtons(card, id);
}

function updateCardButtons(card, id) {
  const isRead = state.read.has(id);
  const isFav = state.fav.has(id);
  card.classList.toggle('is-read', isRead);

  const readBtn = card.querySelector('[data-role="read"]');
  readBtn.textContent = isRead ? '未読に戻す' : '既読にする';

  const favBtn = card.querySelector('[data-role="fav"]');
  favBtn.textContent = isFav ? '★' : '☆';
  favBtn.classList.toggle('fav-on', isFav);
  favBtn.setAttribute('aria-label', isFav ? 'お気に入り解除' : 'お気に入りに追加');
  favBtn.setAttribute('aria-pressed', String(isFav));
}

/* ===== 設定ダイアログ ===== */
function renderSettings() {
  renderErrors();
  renderSources();
}

function renderErrors() {
  const info = $('metaInfo');
  info.textContent = state.generatedAt
    ? '最終更新：' + fmtDateTime(state.generatedAt)
    : '最終更新：不明';

  const list = $('errorList');
  list.replaceChildren();
  if (!state.errors.length) {
    const li = el('li');
    li.append(el('span', 'muted', '取得エラーはありません。'));
    list.append(li);
    return;
  }
  state.errors.forEach((e) => {
    const li = el('li');
    const box = el('div', 'src-info');
    box.append(el('span', 'src-name', str(e && e.source, 80) || '不明な情報源'));
    box.append(el('span', 'err-item', str(e && e.message, 200)));
    if (e && e.at) box.append(el('span', 'src-sub', fmtDateTime(e.at)));
    li.append(box);
    list.append(li);
  });
}

function renderSources() {
  const list = $('sourceList');
  list.replaceChildren();

  const rows = state.repoSources.map((s) => ({ s, origin: 'repo' }))
    .concat(state.userSources.map((s) => ({ s, origin: 'user' })));

  if (!rows.length) {
    const li = el('li');
    li.append(el('span', 'muted', '情報源がありません。'));
    list.append(li);
    return;
  }

  rows.forEach(({ s, origin }) => {
    const hidden = state.hidden.has(s.id);
    const li = el('li');
    const info = el('div', 'src-info' + (hidden || s.enabled === false ? ' src-off' : ''));
    info.append(el('div', 'src-name', str(s.name, 60)));

    const tags = [
      TYPE_LABELS[s.type] || s.type,
      CATEGORY_LABELS[s.category] || s.category,
      origin === 'user' ? '自分で追加' : 'sources.json'
    ];
    if (s.enabled === false) tags.push('収集無効');
    if (hidden) tags.push('非表示中');
    info.append(el('div', 'src-sub', tags.join(' ・ ')));
    info.append(el('div', 'src-sub', str(s.url, 200)));
    li.append(info);

    const btn = el('button', hidden ? 'restore-btn' : 'danger-btn', hidden ? '復元' : '削除');
    btn.type = 'button';
    btn.addEventListener('click', () => {
      if (origin === 'user') {
        state.userSources = state.userSources.filter((x) => x.id !== s.id);
        saveUserSources();
      } else if (hidden) {
        state.hidden.delete(s.id);
        saveHidden();
      } else {
        state.hidden.add(s.id);
        saveHidden();
      }
      renderSources();
      render();
    });
    li.append(btn);
    list.append(li);
  });
}

function addSource(event) {
  event.preventDefault();
  const msg = $('formMsg');
  const name = $('srcName').value.trim().slice(0, 40);
  const url = safeUrl($('srcUrl').value.trim());
  const type = $('srcType').value;
  const category = $('srcCategory').value;

  if (!name) { msg.textContent = '名前を入力してください。'; return; }
  if (!url) { msg.textContent = 'URLは http:// か https:// で始まる形式にしてください。'; return; }
  if (!TYPE_LABELS[type] || !CATEGORY_LABELS[category]) { msg.textContent = '種類またはカテゴリが不正です。'; return; }

  const exists = state.repoSources.concat(state.userSources).some((s) => s.url === url);
  if (exists) { msg.textContent = '同じURLの情報源が既にあります。'; return; }

  state.userSources.push({
    id: 'user-' + Date.now().toString(36),
    name, type, category, url,
    enabled: true,
    max_items: 20,
    interval_minutes: 60
  });
  saveUserSources();
  $('addSourceForm').reset();
  msg.textContent = '追加しました。';
  renderSources();
}

/* GitHub Pages のURL（https://ユーザー名.github.io/リポジトリ名/）から、sources.json の編集画面のURLを作る */
function githubEditUrl() {
  const m = location.hostname.match(/^([^.]+)\.github\.io$/i);
  const repo = location.pathname.split('/').filter(Boolean)[0];
  if (!m || !repo) return '';
  return 'https://github.com/' + encodeURIComponent(m[1]) + '/' + encodeURIComponent(repo) + '/edit/main/sources.json';
}

async function copyUserSources() {
  const msg = $('formMsg');
  if (!state.userSources.length) { msg.textContent = '先に「追加」で情報源を登録してください。'; return; }

  /* "sources": [ の次の行にそのまま貼れるよう、各項目の後ろにカンマを付ける */
  const text = state.userSources.map((s) => JSON.stringify(s, null, 2)).join(',\n') + ',';
  let copied = false;
  try {
    await navigator.clipboard.writeText(text);
    copied = true;
  } catch (e) { /* コピーできない環境 */ }

  if (!copied) { msg.textContent = 'コピーできませんでした。'; return; }

  const url = githubEditUrl();
  if (url) {
    msg.textContent = 'コピーしました。GitHubの編集画面を開きます。"sources": [ の次の行に貼り付けてください。';
    window.open(url, '_blank', 'noopener');
  } else {
    msg.textContent = 'コピーしました。GitHubで sources.json を開き、"sources": [ の次の行に貼り付けてください。';
  }
}

/* ===== イベント ===== */
function bindEvents() {
  $('searchInput').addEventListener('input', (e) => {
    state.query = e.target.value;
    state.limit = PAGE_SIZE;
    render();
  });

  $('categoryChips').addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    state.prefs.category = chip.dataset.cat;
    state.limit = PAGE_SIZE;
    savePrefs();
    render();
  });

  $('sortSelect').addEventListener('change', (e) => {
    state.prefs.sort = e.target.value === 'asc' ? 'asc' : 'desc';
    savePrefs();
    render();
  });

  $('unreadToggle').addEventListener('click', () => {
    state.prefs.unreadOnly = !state.prefs.unreadOnly;
    state.limit = PAGE_SIZE;
    savePrefs();
    render();
  });

  $('favToggle').addEventListener('click', () => {
    state.prefs.favOnly = !state.prefs.favOnly;
    state.limit = PAGE_SIZE;
    savePrefs();
    render();
  });

  $('moreBtn').addEventListener('click', () => {
    state.limit += PAGE_SIZE;
    render();
  });

  $('refreshBtn').addEventListener('click', async () => {
    const btn = $('refreshBtn');
    btn.disabled = true;
    await loadData();
    btn.disabled = false;
  });

  const dialog = $('settingsDialog');
  const openSettings = () => { renderSettings(); dialog.showModal(); };
  $('settingsBtn').addEventListener('click', openSettings);
  $('statusBar').addEventListener('click', openSettings);
  $('closeSettings').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', (e) => { if (e.target === dialog) dialog.close(); });

  $('addSourceForm').addEventListener('submit', addSource);
  $('copySourcesBtn').addEventListener('click', copyUserSources);

  $('resetReadBtn').addEventListener('click', () => {
    if (!confirm('既読の記録をすべて消します。よろしいですか？')) return;
    state.read.clear();
    saveRead();
    render();
  });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && Date.now() - state.loadedAt > RELOAD_MS) {
      loadData();
    }
  });
}

/* ===== 起動 ===== */
function init() {
  bindEvents();
  loadData();
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('service-worker.js').catch(() => { /* 登録失敗でも画面は動く */ });
    });
  }
}

init();
