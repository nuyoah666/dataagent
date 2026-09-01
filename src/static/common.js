// 嵌入 /app 工作台 iframe 时：子页隐藏自带品牌头部（外壳顶栏已有品牌），避免重复
if (window.self !== window.top) document.documentElement.classList.add('embedded');
// 三页共享：DOM 简写、鉴权头、API 封装、HTML 转义、主题切换
// localStorage 键位统一：dataagent_api_token / dataagent_operator / dataagent_theme
const $ = id => document.getElementById(id);
const TOKEN_KEY = 'dataagent_api_token';
const OPERATOR_KEY = 'dataagent_operator';
const THEME_KEY = 'dataagent_theme';

function token() {
  const el = $('apiToken');  // wizard 页无此输入框，需判空
  return (el && el.value) || localStorage.getItem(TOKEN_KEY) || '';
}
function operator() {
  const el = $('operatorName');
  return ((el && el.value) || localStorage.getItem(OPERATOR_KEY) || 'local-user')
    .trim().slice(0, 50) || 'local-user';
}
function headers() {
  const h = { 'X-Operator': operator() };
  const t = token();
  if (t) h['X-API-Token'] = t;
  return h;
}
function escapeHtml(s) {
  return (s == null ? '' : String(s)).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
async function api(url, opts) {
  const r = await fetch(url, Object.assign({ headers: headers() }, opts || {}));
  if (r.status === 401) { alert('未授权：请在右上角填入 API_TOKEN'); throw new Error('unauthorized'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
  return data;
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

// 全站统一中文标签（数据源类型 / 任务类型），各页面共享同一份真源
function dbTypeLabel(t) {
  return { mysql:'MySQL', mongodb:'MongoDB', elasticsearch:'Elasticsearch', starrocks:'StarRocks' }[t] || t || '';
}
function taskTypeLabel(t) {
  return { data_integration:'数据集成', etl_development:'ETL', data_ops:'运维', data_analysis:'问数' }[t] || t || '—';
}

// 页面间导航：若处于 /app 单页外壳的 iframe 中，通知外壳切 tab（保活不整页跳转）；否则直接跳转
function gotoPage(path) {
  try {
    if (window.parent && window.parent !== window && typeof window.parent.shellGoto === 'function') {
      window.parent.shellGoto(path);
      return;
    }
  } catch (e) { /* 跨 frame 访问受限时退化为整页跳转 */ }
  window.location.href = path;
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const btn = $('themeToggle');
  if (btn) btn.textContent = theme === 'dark' ? '☀️' : '🌙';
}
function initTheme() {
  const saved = localStorage.getItem(THEME_KEY);
  const theme = saved || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  applyTheme(theme);
}
function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  localStorage.setItem(THEME_KEY, next);
}
// 其他同源页面（如 /app 外壳与其 iframe）切换主题后，本页实时跟随
window.addEventListener('storage', e => {
  if (e.key === THEME_KEY) applyTheme(e.newValue || 'dark');
});
