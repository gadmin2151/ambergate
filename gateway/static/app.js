'use strict';

const $ = (selector, parent = document) => parent.querySelector(selector);
const esc = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const uid = () => 'id_' + (crypto.randomUUID ? crypto.randomUUID().replaceAll('-', '') : Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join(''));
const paths = {
  dashboard:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  docker:'M4 8h4v4H4z M9 8h4v4H9z M14 8h4v4h-4z M9 3h4v4H9z M2 13h17l3-3 M2 13c0 6 4 8 9 8s8-4 8-8',
  routes:'M4 4h6v6H4z M14 14h6v6h-6z M14 4h6v6h-6z M7 10v7h7 M10 7h4',
  shield:'M12 3 4 6v5c0 5 8 10 8 10s8-5 8-10V6z M8 12l3 3 5-6',
  cache:'M20 7c0 2-4 4-8 4S4 9 4 7s4-4 8-4 8 2 8 4z M4 7v10c0 2 4 4 8 4s8-2 8-4V7 M4 12c0 2 4 4 8 4s8-2 8-4',
  code:'m8 6-6 6 6 6 M16 6l6 6-6 6 M14 3l-4 18',
  history:'M3 11a9 9 0 1 1 2 7 M3 4v7h7 M12 7v5l3 2',
  globe:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0 M3 12h18 M12 3c5 5 5 13 0 18 M12 3c-5 5-5 13 0 18',
  server:'M4 3h16v7H4z M4 14h16v7H4z M7 6.5h.01 M7 17.5h.01 M11 6.5h6 M11 17.5h6',
  arrow:'M4 12h16 M15 7l5 5-5 5', plus:'M12 5v14 M5 12h14',
  check:'m5 12 4 4L19 6', close:'m6 6 12 12 M18 6 6 18',
  edit:'m15 4 5 5 M4 20l1-6L16 3l5 5L10 19z',
  save:'M5 3h12l4 4v14H3V3z M7 3v7h10V3 M7 21v-7h10v7',
  play:'m8 4 12 8-12 8z', lock:'M6 10h12v11H6z M8 10V7a4 4 0 0 1 8 0v3',
  exit:'M9 4H4v16h5 M10 12h11 M17 8l4 4-4 4',
  download:'M12 3v12 M7 10l5 5 5-5 M4 16v5h16v-5',
  upload:'M12 16V3 M7 8l5-5 5 5 M4 16v5h16v-5',
  clock:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0 M12 7v5l3 2',
  info:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0 M12 11v6 M12 7h.01',
  trash:'M3 6h18 M9 6V3h6v3 M5 6l1 15h12l1-15 M10 10v7 M14 10v7',
  bolt:'m13 2-9 12h7l-1 8 10-12h-7z',
  search:'M10.5 18a7.5 7.5 0 1 1 0-15 7.5 7.5 0 0 1 0 15 M16 16l5 5',
  chevron:'m9 6 6 6-6 6',
  sliders:'M4 7h5 M15 7h5 M4 17h11 M19 17h1 M9 4v6 M15 14v6',
  copy:'M9 9h12v12H9z M15 9V3H3v12h6',
  user:'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0 M4 21v-2a8 8 0 0 1 16 0v2',
};
const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.routes}"/></svg>`;
const nav = [['dashboard','Обзор системы'],['routes','Маршруты'],['docker','Docker'],['shield','Защита и лимиты'],['cache','Кеширование'],['code','nginx.conf'],['history','История версий']];
const balanceLabels = {round_robin:'Round robin', least_conn:'Least connections', ip_hash:'IP hash'};
let state = null, config = null, csrf = '', page = 'dashboard', dirty = false, busy = false, health = null;
let toastTimer = null;
let routeQuery = '', hostFilter = 'all';

async function api(path, body, options = {}) {
  const response = await fetch('/api/' + path, {method:body === undefined ? 'GET' : 'POST', credentials:'same-origin',
    headers:body === undefined ? {} : {'Content-Type':'application/json','X-CSRF-Token':csrf},
    body:body === undefined ? undefined : JSON.stringify(body), signal:options.signal});
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== 'login') { csrf = ''; showLogin(); }
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}
function notify(message, error = false) {
  clearTimeout(toastTimer);
  const toast = $('#toast');
  toast.innerHTML = `${icon(error ? 'info' : 'check')}<span>${esc(message)}</span>${btn('dismiss-toast', '', 'close', 'ghost icon', 'aria-label="Закрыть уведомление"')}`;
  toast.className = error ? 'error-toast' : ''; toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, error ? 12000 : 4500);
}
async function task(fn) {
  if (busy) return;
  busy = true;
  updateSaveState();
  try { await fn(); } catch (error) { notify(error.message, true); }
  finally { busy = false; if (config && csrf) render(); }
}
function accept(snapshot) {state = snapshot; config = structuredClone(snapshot.config); dirty = false;}
function changed() { dirty = JSON.stringify(config) !== JSON.stringify(state.config); render(); }
function updateSaveState() {
  const dock = $('#save-dock');
  if (dock) {dock.innerHTML = saveDockContent(); dock.hidden = ['dashboard','docker'].includes(page) && !dirty && !state.pending;}
  document.querySelectorAll('[data-action="test"]').forEach(el => el.disabled = busy);
}
function routes() { return config.hosts.flatMap(h => h.routes); }
function btn(action, label, symbol, cls = '', attrs = '') { return `<button type="button" data-action="${action}" class="${cls}" ${attrs}>${symbol ? icon(symbol) : ''}${label}</button>`; }
function badge(text, color='gray') { return `<span class="badge ${color}">${esc(text)}</span>`; }
function empty(title, body, action='') {return `<div class="empty">${icon('routes')}<h2>${title}</h2><p>${body}</p>${action}</div>`;}
function setModal(title, subtitle, content, submit, button='Сохранить') {
  const modal = $('#modal');
  modal.classList.toggle('route-modal', content.includes('route-tabs') || content.includes('target-editor'));
  modal.dataset.replaceDockerTargets = 'false';
  modal.setAttribute('aria-labelledby', 'modal-title');
  modal.innerHTML = `<form id="modal-form"><div class="modal-header"><div><span class="eyebrow">НАСТРОЙКИ GATEWAY</span><h2 id="modal-title">${esc(title)}</h2><p class="modal-subtitle">${subtitle}</p></div>${btn('close','', 'close','ghost icon','aria-label="Закрыть"')}</div><div class="modal-body">${content}<div id="modal-error" class="error" role="alert"></div></div><div class="form-actions"><span class="hint">${button==='Изменить пароль' ? 'Потребуется повторный вход' : 'Изменения попадут в черновик'}</span>${btn('close','Отмена','','ghost')}<button type="submit" class="primary">${icon('check')}${esc(button)}</button></div></form>`;
  $('#modal-form').addEventListener('invalid', event => {
    const panel = event.target.closest('[data-route-panel]');
    if (panel) selectRouteTab(panel.dataset.routePanel);
  }, true);
  $('#modal-form').addEventListener('submit', async event => {
    event.preventDefault(); const button = $('button[type=submit]', modal); button.disabled = true;
    try { await submit(new FormData(event.target)); modal.close(); }
    catch (error) { $('#modal-error').textContent = error.message; }
    finally {button.disabled = false;}
  });
  if (!modal.open) modal.showModal();
}
function confirmAction(title, description, detail, label='Подтвердить', danger=false) {
  const dialog = $('#confirm-modal');
  if (dialog.open) return Promise.resolve(false);
  dialog.returnValue = 'cancel';
  dialog.innerHTML = `<form method="dialog"><div class="confirm-content"><span class="confirm-symbol">${icon(danger ? 'trash' : 'info')}</span><h2 id="confirm-title">${esc(title)}</h2><p id="confirm-description">${esc(description)}</p>${detail ? `<div class="confirm-detail">${esc(detail)}</div>` : ''}<p class="confirm-hint">${danger ? 'После удаления сохраните черновик или примените изменения. ' : ''}Рабочая конфигурация обновится после «Применить».</p></div><div class="form-actions"><button type="submit" value="cancel" autofocus>Отмена</button><button type="submit" value="confirm" class="${danger ? 'confirm-delete' : 'primary'}">${icon(danger ? 'trash' : 'check')}${esc(label)}</button></div></form>`;
  return new Promise(resolve => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), {once:true});
    dialog.showModal();
  });
}
function field(label, name, value, type='text', extra='', hint='') {
  return `<label>${label}<input name="${name}" type="${type}" value="${esc(value)}" ${extra}>${hint ? `<small>${hint}</small>` : ''}</label>`;
}
function check(label, name, value) {return `<label class="check"><input type="checkbox" name="${name}" ${value ? 'checked' : ''}>${label}</label>`;}
function newRoute(path='/', name='Frontend', address='frontend', port=3000) {
  return {id:uid(),name,path,strip_prefix:false,balance:'round_robin',targets:[{address,port,weight:1,backup:false}],
    cache:false,cache_ttl:60,websocket:true,timeout:60,body_mb:0,rate_rps:0,rate_burst:0};
}

function showLogin() {
  stopDashboardStream();
  config = null; health = null;
  dashboardData = null; dashboardError = '';
  dockerData = null; dockerDraft = null; dockerError = '';
  if ($('#docker-picker').open) $('#docker-picker').close();
  if ($('#confirm-modal').open) $('#confirm-modal').close('cancel');
  if ($('#modal').open) $('#modal').close();
  $('#app').innerHTML = `<div class="login-page"><section class="login-art"><div class="brand"><img src="/favicon.svg?v=2" alt=""><div>gateway<span class="brand-period">.</span><small>NGINX SCALE GATEWAY</small></div></div><div><div class="eyebrow">Один вход. Все приложения.</div><h1>Все приложения.<br><span>Один gateway.</span></h1><div class="login-flow"><span>example.com</span><i></i><div><code>/</code><code>/api</code><code>/s3</code></div></div><p>Маршруты, балансировка и защита приложений — в одной панели управления Nginx.</p></div><div class="eyebrow">Self-hosted · Local configuration · HTTP gateway</div></section><section class="login-form-wrap"><form id="login" class="login-form"><div class="mobile-brand"><img src="/favicon.svg?v=2" alt="Gateway"></div><div class="eyebrow">Панель управления</div><br><h2>Добро пожаловать</h2><p>Войдите, чтобы настроить ваш gateway.</p>${field('Пароль администратора','password','','password','required autocomplete="current-password" autofocus')}<div class="error" id="login-error"></div><button type="submit" class="primary">Войти в Gateway ${icon('arrow')}</button><p class="hint">При первом запуске пароль задаётся через GATEWAY_ADMIN_PASSWORD или выводится в логах контейнера.</p></form></section></div>`;
  $('#login').addEventListener('submit', async event => {
    event.preventDefault(); const button = $('button', event.target); button.disabled = true; $('#login-error').textContent = '';
    try {const session = await api('login',{password:new FormData(event.target).get('password')}); csrf = session.csrf; await load();}
    catch(error){const el = $('#login-error'); if(el) el.textContent = error.message;}
    finally{button.disabled = false;}
  });
}
async function load(){accept(await api('config')); render(); startDashboardStream();}
function statusHTML() {
  if (dashboardStreamState !== 'live') return `<span class="dot bad"></span>${dashboardError ? 'Нет live-связи' : 'Подключаемся…'}`;
  return `<span class="dot ${health?.healthy ? '' : 'bad'}"></span>${health?.healthy ? 'Nginx работает' : 'Nginx недоступен'}`;}
function saveDockContent() {
  const pending = dirty || state.pending;
  return `<div class="save-state ${pending ? 'is-pending' : ''}"><span class="save-state-icon">${icon(pending ? 'clock' : 'check')}</span><div><strong>${busy ? 'Проверяем конфигурацию…' : dirty ? 'Есть несохранённые изменения' : state.pending ? 'Черновик готов к применению' : 'Все изменения применены'}</strong><span>${pending ? 'Рабочие маршруты обновятся после применения' : 'Nginx использует последнюю сохранённую версию'}</span></div></div><div class="actions">${btn('save','Сохранить черновик','save','',!dirty || busy ? 'disabled' : '')}${btn('apply',busy ? 'Применяем…' : 'Применить',busy ? '' : 'arrow','primary',(!dirty && !state.pending) || busy ? 'disabled' : '')}</div>`;
}
function render() {
  if (!config || !csrf) return;
  const current = nav.find(n => n[0] === page);
  const descriptions = {
    dashboard:'Трафик, ответы и состояние вашего gateway — в реальном времени.',
    docker:'Подключите Docker и выбирайте контейнеры для маршрутов и балансировки.',
    routes:'Управляйте трафиком всех ваших приложений в одном месте.',
    shield:'Настройте ограничения и правила доступа для ваших приложений.',
    cache:'Ускоряйте публичные ответы и снижайте нагрузку на серверы.',
    code:'Вся конфигурация перед вами. Проверьте её перед применением.',
    history:'Вернитесь к предыдущей конфигурации в несколько кликов.'
  };
  $('#app').innerHTML = `
    <div class="shell ${page === 'dashboard' ? 'dashboard-shell' : ''}">
      <aside class="sidebar">
        <div class="brand"><img src="/favicon.svg?v=2" alt=""><div>gateway<span class="brand-period">.</span><small>NGINX SCALE GATEWAY</small></div></div>
        <div class="workspace-switch"><span class="workspace-symbol">${icon('server')}</span><div><strong>Мой gateway</strong><small>Self-hosted workspace</small></div><span class="workspace-dot"></span></div>
        <div class="nav-caption">ПРОСТРАНСТВО</div>
        <nav aria-label="Главное меню">${nav.map(([key,label]) => btn('navigate',`<span>${label}</span>${key === 'routes' ? `<span class="nav-count">${config.hosts.length}</span>` : ''}`,key,page === key ? 'active' : '',`data-page="${key}" aria-label="${label}" title="${label}" ${page === key ? 'aria-current="page"' : ''}`)).join('')}</nav>
        <div class="side-bottom"><div class="side-card"><div class="side-card-icon">${icon('shield')}</div><strong>Ваш сервер. Ваши данные.</strong><p>Настройки хранятся локально.<br>Всё остаётся под вашим контролем.</p><span class="side-card-caption"><span class="dot"></span> NGINX GATEWAY</span></div>
        <div class="side-account"><span class="avatar">A</span><div><strong>Администратор</strong><small>Локальный аккаунт</small></div>${btn('password','','sliders','ghost icon','aria-label="Сменить пароль" title="Сменить пароль"')}</div></div>
      </aside>
      <div class="workspace">
        <header class="topbar"><div class="crumb">${icon('server')}<span>Мой gateway</span>${icon('chevron')}<strong>${current[1]}</strong></div><div class="top-right"><span class="status" id="nginx-status">${statusHTML()}</span><span class="topbar-divider"></span>${btn('logout','','exit','ghost icon','aria-label="Выйти" title="Выйти"')}</div></header>
        <main><div class="page-head"><div><span class="eyebrow">${page === 'dashboard' ? 'LIVE OVERVIEW' : page === 'routes' ? 'TRAFFIC MANAGEMENT' : 'GATEWAY CONTROL'}</span><h1>${page === 'routes' ? 'Домены и маршруты' : current[1]}</h1><p>${descriptions[page]}</p></div>${page === 'dashboard' ? `<div class="dash-controls"><span>${icon('clock')} Последние 15 минут</span>${btn('refresh-dashboard','Обновить','history','', 'id="refresh-dashboard"')}</div>` : page === 'routes' ? btn('add-host','Добавить домен','plus','primary') : `<span class="page-symbol">${icon(page)}</span>`}</div>
          <div id="page-content">${pageContent()}</div>
          <div class="page-note"><span>${icon('lock')}Локальное хранение конфигурации</span><span>Gateway <span class="footer-version">v1.0</span></span></div>
        </main>
        <div class="save-dock" id="save-dock" aria-live="polite" ${['dashboard','docker'].includes(page) && !dirty && !state.pending ? 'hidden' : ''}>${saveDockContent()}</div>
      </div>
    </div>`;
  if (page === 'shield') bindSettings();
  if (page === 'code') loadPreview();
  if (page === 'routes') bindRouteSearch();
  startDashboardStream();
  if (page === 'docker') loadDockerPage();
}

function pageContent() {
  if (page === 'dashboard') return `<div id="dashboard-live">${dashboardContent()}</div>`;
  if (page === 'docker') return dockerPage();
  if (page === 'routes') return routesPage();
  if (page === 'shield') return settingsPage();
  if (page === 'cache') return cachePage();
  if (page === 'history') return historyPage();
  return `<div class="toolbar"><div class="flex"><h2 id="preview-heading">Сгенерированный конфиг</h2>${badge(dirty ? 'Несохранённый' : state.pending ? 'Черновик' : 'Применён','green')}</div><div class="actions">${btn('active-config','Действующий конфиг','code','small')}${btn('test','Проверить nginx -t','check','small',busy ? 'disabled' : '')}</div></div><div class="code-toolbar"><span>nginx.conf</span>${btn('copy-config','Копировать','','small')}</div><pre id="preview" tabindex="0">Генерация…</pre><div class="notice">${icon('info')}<div>Конфигурация создаётся из настроек панели. При проверке текущие изменения сохраняются в черновик. Рабочая конфигурация меняется только после «Применить».</div></div>`;
}
function routesPage() {
  const rs = routes();
  const metrics = [
    ['globe','Домены',config.hosts.length,`${config.hosts.filter(h=>h.enabled).length} включено`, 'green'],
    ['routes','Маршруты',rs.length,'правил проксирования','blue'],
    ['server','Серверы',rs.reduce((n,r)=>n+r.targets.length,0),'upstream-подключений','violet'],
    ['shield','Лимит на IP',config.settings.rate_rps || '∞','запросов в секунду','amber']
  ];
  return `<div class="metrics">${metrics.map(([symbol,label,value,note,tone])=>`<div class="metric"><div class="metric-top"><span>${label}</span><span class="metric-icon ${tone}">${icon(symbol)}</span></div><div class="metric-value">${value}</div><div class="metric-note">${note}</div></div>`).join('')}</div>
    <div class="section-title"><div><h2>Ваши домены <span class="count">${config.hosts.length}</span></h2><p>Отдельные маршруты и серверы для каждого домена.</p></div><div class="actions">${btn('import','Импорт','upload','ghost small')}${btn('export','Экспорт','download','ghost small')}</div></div>
    <div class="route-toolbar"><label class="search-field">${icon('search')}<input id="route-search" type="search" placeholder="Найти домен, путь или сервер…" aria-label="Поиск доменов и маршрутов" value="${esc(routeQuery)}"><kbd>/</kbd></label><div class="filter-tabs" role="group" aria-label="Фильтр доменов">${[['all','Все'],['enabled','Включены'],['disabled','Выключены']].map(([key,label])=>btn('filter-hosts',label,'',hostFilter===key ? 'selected' : '',`data-filter="${key}" aria-pressed="${hostFilter===key}"`)).join('')}</div></div>
    <div id="host-list">${filteredHosts()}</div>
    <div class="notice"><span class="notice-icon">${icon('bolt')}</span><div><strong>Обновляйте маршруты без остановки приложений</strong><p>Проверим конфигурацию перед применением. Если что-то пойдёт не так, автоматически вернём предыдущую версию.</p></div><span class="notice-tag">SAFE RELOAD</span></div>`;
}
function filteredHosts() {
  if (!config.hosts.length) return empty('Первый домен — начало вашей сети','Добавьте один или несколько доменов. Для каждого настройте собственные frontend, API и хранилище.',`<div class="actions">${btn('add-host','Добавить домен','plus','primary')}${btn('example','Пример example.com','routes')}</div>`);
  const query = routeQuery.trim().toLocaleLowerCase();
  const filtered = config.hosts.filter(host=> {
    if (hostFilter === 'enabled' && !host.enabled || hostFilter === 'disabled' && host.enabled) return false;
    return !query || [host.domain, ...host.routes.flatMap(r=>[r.name,r.path,...r.targets.map(t=>`${t.address}:${t.port}`)])].some(value=>value.toLocaleLowerCase().includes(query));
  });
  return filtered.length ? filtered.map(hostCard).join('') : `<div class="search-empty">${icon('search')}<h3>Ничего не найдено</h3><p>Попробуйте другое название или сбросьте фильтры.</p>${btn('reset-search','Сбросить фильтры','','small')}</div>`;
}
function bindRouteSearch() {
  $('#route-search').addEventListener('input', event=>{routeQuery=event.target.value;$('#host-list').innerHTML=filteredHosts();});
}
function hostCard(host) {
  return `<article class="host-card ${host.enabled ? '' : 'host-disabled'}">
    <div class="host-head"><div class="host-title"><div class="domain-icon">${icon('globe')}</div><div><div class="domain-heading"><h3>${esc(host.domain)}</h3>${badge(host.enabled ? 'Включён' : 'Выключен',host.enabled ? 'green' : 'gray')}</div><p>${host.routes.length} ${plural(host.routes.length,'маршрут','маршрута','маршрутов')}<span>·</span>HTTP</p></div></div><div class="actions">${btn('add-route','Добавить маршрут','plus','small',`data-host="${host.id}"`)}${btn('edit-host','','sliders','ghost icon',`data-host="${host.id}" aria-label="Настройки домена ${esc(host.domain)}" title="Настройки домена"`)}</div></div>
    ${host.routes.length ? '<div class="route-columns"><span>ВХОДЯЩИЙ МАРШРУТ</span><span></span><span>ЦЕЛЕВЫЕ СЕРВЕРЫ</span><span>ПРАВИЛА</span><span></span></div>' : ''}
    <div class="route-list">${host.routes.length ? host.routes.map(route => routeCard(host,route)).join('') : '<div class="host-empty"><strong>У домена пока нет маршрутов</strong><p>Добавьте маршрут, чтобы направлять запросы к приложению. Домен без маршрутов отвечает 404.</p></div>'}</div>
    <div class="host-foot"><span>${icon('shield')}${config.settings.block_dotfiles ? 'Защита скрытых файлов' : 'Скрытые файлы разрешены'}</span><span>${icon('server')}${host.routes.reduce((n,r)=>n+r.targets.length,0)} ${plural(host.routes.reduce((n,r)=>n+r.targets.length,0),'сервер','сервера','серверов')}</span></div>
  </article>`;
}
function plural(n, one, few, many) {return n%10===1 && n%100!==11 ? one : n%10>=2 && n%10<=4 && (n%100<12 || n%100>14) ? few : many;}
function routeCard(host, route) {
  const tone = route.path==='/' ? 'green' : route.path.includes('s3') ? 'violet' : 'blue';
  return `<div class="route-row">
    <div class="route-identity"><span class="route-icon ${tone}">${icon(route.path==='/' ? 'globe' : route.path.includes('s3') ? 'cache' : 'code')}</span><div><div class="route-name">${esc(route.name)}</div><div class="route-path"><code>${esc(route.path)}</code><span>${route.path==='/' ? 'корневой путь' : route.strip_prefix ? 'убрать префикс' : 'сохранить путь'}</span></div></div></div>
    <span class="flow-arrow">${icon('arrow')}</span>
    <div class="upstream-list">${route.targets.map(t=>`<div class="target-line"><span class="target-node">${icon('server')}</span><code>${esc(t.address)}<span class="target-port">:${t.port}</span></code>${t.backup ? badge('backup') : t.weight>1 ? badge('×'+t.weight) : ''}</div>`).join('')}<span class="balance-label">${icon('routes')}${esc(balanceLabels[route.balance])}</span></div>
    <div class="route-rules"><span class="rule ${route.cache ? 'rule-enabled' : ''}">${icon('cache')}${route.cache ? `Кеш ${route.cache_ttl} сек` : 'Кеш выключен'}</span><span class="rule">${icon('shield')}${route.rate_rps ? route.rate_rps+' r/s' : config.settings.rate_rps ? 'Общий лимит' : 'Без лимита'}</span>${route.websocket ? '<span class="protocol-label">WebSocket</span>' : ''}</div>
    ${btn('edit-route','','edit','route-edit icon',`data-host="${host.id}" data-route="${route.id}" aria-label="Изменить маршрут ${esc(route.name)}" title="Изменить маршрут"`)}
  </div>`;
}

function settingsPage() {
  const s = config.settings;
  return `<form id="settings-form" class="stack"><section class="panel"><div class="panel-head"><h2>Ограничения на один IP-адрес</h2><p>Общий лимит действует суммарно на всех включённых доменах и маршрутах. При превышении — HTTP 429.</p></div><div class="grid">${field('Запросов в секунду','rate_rps',s.rate_rps,'number','min="0" max="100000" required','0 — без общего ограничения')}${field('Допустимый всплеск (burst)','rate_burst',s.rate_burst,'number','min="0" max="100000" required','Дополнительные запросы проходят сразу; сверх burst — 429')}${field('Одновременных запросов с IP','connections_per_ip',s.connections_per_ip,'number','min="0" max="65535" required','0 — без ограничения')}${field('Максимальное тело запроса, MiB','body_mb',s.body_mb,'number','min="1" max="102400" required','Можно переопределить для каждого маршрута')}</div></section><section class="panel"><div class="panel-head"><h2>Внешний SSL-прокси и DNS</h2><p>Nginx принимает обычный HTTP. Укажите IP вашего прокси, чтобы использовать реальный адрес клиента и X-Forwarded-Proto.</p></div><div class="grid"><label>Доверенные прокси, IP или CIDR<textarea name="trusted_proxies" rows="4" placeholder="172.20.0.2/32">${esc(s.trusted_proxies.join('\n'))}</textarea><small>По одному в строке. Доверяйте только своему SSL-прокси; он должен корректно передавать X-Forwarded-For.</small></label><label>DNS-серверы<textarea name="resolvers" rows="4" required>${esc(s.resolvers.join('\n'))}</textarea><small>В Docker-сети обычно 127.0.0.11. Имена upstream обновляются автоматически каждые 10 секунд.</small></label></div></section><section class="panel"><div class="panel-head"><h2>Базовая HTTP-защита</h2><p>TRACE и нестандартные методы запрещены. Включены таймауты чтения заголовков и тела запроса; версия Nginx скрыта.</p></div><div class="stack">${check('Запретить скрытые файлы и каталоги (.env, .git); разрешить .well-known','block_dotfiles',s.block_dotfiles)}${check('Добавить X-Content-Type-Options и Referrer-Policy','security_headers',s.security_headers)}</div><div class="notice">${icon('info')}<div>Это базовые ограничения Nginx. Для WAF и защиты от объёмных DDoS нужен отдельный внешний слой.</div></div></section><div class="actions"><button class="primary" type="submit">${icon('save')}Сохранить настройки в черновик</button>${btn('password','Сменить пароль панели','lock')}</div></form>`;
}
function bindSettings() {
  const form = $('#settings-form');
  form.addEventListener('input', () => {dirty = true; updateSaveState();});
  form.addEventListener('submit', event => {event.preventDefault(); if (!form.reportValidity()) return; task(async()=>{collectSettings(); await saveDraft(); notify('Настройки сохранены. Нажмите «Применить».');});});
}
function collectSettings() {
  const form = $('#settings-form'); if (!form) return;
  if (!form.reportValidity()) throw new Error('Проверьте поля настроек');
  const data = new FormData(form), s = config.settings;
  for (const key of ['rate_rps','rate_burst','connections_per_ip','body_mb']) s[key] = Number(data.get(key));
  for (const key of ['trusted_proxies','resolvers']) s[key] = data.get(key).split(/[\s,]+/).filter(Boolean);
  for (const key of ['block_dotfiles','security_headers']) s[key] = data.has(key);
}
function cachePage() {
  const rs = config.hosts.flatMap(h=>h.routes.map(r=>({h,r})));
  return `<section class="panel"><div class="panel-head"><h2>Кеш ответов Nginx</h2><p>Общий диск: ${config.settings.cache_mb} MiB. Отдельный TTL для каждого маршрута. Кеш хранится в /cache.</p><div class="actions">${btn('cache-size','Изменить размер','edit','small')}</div></div><div class="cache-grid">${rs.map(({h,r})=>`<div class="cache-item"><div class="flex between"><h3>${esc(r.name)}</h3>${badge(r.cache ? 'Включён' : 'Выключен',r.cache ? 'green':'gray')}</div><code>${esc(h.domain)}${esc(r.path)}</code><p>${r.cache ? `TTL ${r.cache_ttl} сек · GET / HEAD · статусы 200, 301, 302`:'Ответы передаются напрямую от приложения.'}</p><div class="actions">${btn('edit-route','Настроить','edit','small',`data-host="${h.id}" data-route="${r.id}"`)}</div></div>`).join('')}</div>${rs.length ? '' : empty('Пока нет маршрутов','Добавьте домен и маршрут, затем включите кеширование.')}</section><div class="notice">${icon('shield')}<div><strong>Персональные ответы не кешируются.</strong><br>Authorization, Cookie, Set-Cookie, подписанные S3-параметры и WebSocket исключены. Учитываются Cache-Control и Vary приложения. Включайте кеш только для публичных ответов.</div></div>`;
}
function historyPage() {
  return `<section class="panel"><div class="panel-head"><h2>Применённые конфигурации</h2><p>Храним последние 20 версий. Восстановление загружает версию в черновик; применение выполняется отдельно.</p></div>${state.history.map(h=>`<div class="history-row"><div><div class="flex"><code>${esc(h.id.slice(0,12))}</code>${h.active ? badge('Действующая','green') : ''}</div><p>${new Date(h.applied_at * 1000).toLocaleString('ru-RU')} · Доменов: ${h.hosts}</p></div>${btn('restore','В черновик','history','small',`data-generation="${h.id}"`)}</div>`).join('')}</section>`;
}
async function loadPreview(active=false){
  const el = $('#preview'); if (!el) return;
  try {
    if (active) {const response = await fetch('/api/active.conf'); if (!response.ok) throw new Error('Не удалось прочитать конфигурацию'); el.textContent = await response.text(); const heading = $('#preview-heading'); if (heading) { heading.textContent = 'Действующий конфиг'; heading.nextElementSibling.textContent = 'Применён'; }}
    else {el.textContent = (await api('preview',{config})).nginx;}
  } catch(error) {el.textContent = error.message;}
}
async function saveDraft(){collectSettings(); accept(await api('config',{config,revision:state.revision}));}

function editHost(id) {
  const existing = config.hosts.find(h=>h.id === id);
  setModal(existing ? 'Настройки домена' : 'Новый домен','Каждый домен получает свои маршруты и серверы. Укажите имя без http:// и пути.',
    `<div class="stack">${field('Домен','domain',existing?.domain || '','text','required placeholder="example.com" maxlength="253"')}${check('Домен включён','enabled',existing?.enabled ?? true)}${existing ? `<div>${btn('delete-host','Удалить домен','trash','danger small',`data-host="${id}"`)}</div>` : `<div class="docker-target-heading"><h3>Серверы первого маршрута /</h3>${btn('docker-picker','Выбрать из Docker','docker','small')}</div><div id="target-editor" class="target-editor">${targetFields({address:'frontend',port:3000,weight:1,backup:false})}</div>${btn('add-target','Добавить сервер','plus','add-target-button')}<p class="hint">Создадим маршрут / с балансировкой Round robin. Остальные маршруты и алгоритм можно настроить после.</p>`}</div>`, async data=>{
      const candidate = structuredClone(config);
      const host = existing ? candidate.hosts.find(h=>h.id === id) : {id:uid(),routes:[newRoute('/','Frontend',data.get('address').trim(),Number(data.get('port')))]};
      if (!existing) host.routes[0].targets = collectTargets();
      host.domain = data.get('domain').trim().toLowerCase(); host.enabled = data.has('enabled');
      if (!existing) candidate.hosts.push(host);
      await api('preview',{config:candidate}); config = candidate; routeQuery=''; hostFilter='all'; changed();
    });
  if (!existing) $('#modal').dataset.replaceDockerTargets = 'true';
}
function collectTargets() {
  return [...document.querySelectorAll('#target-editor .target-fields')].map(row=>({address:$('[name=address]',row).value.trim(),port:Number($('[name=port]',row).value),weight:Number($('[name=weight]',row).value),backup:$('[name=backup]',row).checked}));
}
function targetFields(target) {
  return `<div class="target-fields"><span class="target-marker">${icon('server')}</span>${field('Сервер или IP','address',target.address,'text','required placeholder="backend-1"')}${field('Порт','port',target.port,'number','required min="1" max="65535"')}${field('Вес','weight',target.weight,'number','required min="1" max="1000"')}${check('Резерв','backup',target.backup)}${btn('remove-target','','trash','ghost icon','aria-label="Удалить сервер" title="Удалить сервер"')}</div>`;
}
function selectRouteTab(name) {
  document.querySelectorAll('[data-route-panel]').forEach(panel=>panel.hidden=panel.dataset.routePanel!==name);
  document.querySelectorAll('[data-action="route-tab"]').forEach(button=>{const selected=button.dataset.tab===name;button.classList.toggle('selected',selected);button.setAttribute('aria-selected',String(selected));button.tabIndex=selected ? 0 : -1;});
}
function updatePathPreview() {
  const form = $('#modal-form');
  if (!form || !$('#path-preview')) return;
  const path = $('[name=path]',form).value || '/';
  const stripped = $('[name=strip_prefix]',form).checked && path!=='/';
  $('#path-preview').innerHTML = `<span>Пример запроса</span><code>${esc(path==='/' ? '/users' : path+'/users')}</code>${icon('arrow')}<code>${esc(stripped ? '/users' : path==='/' ? '/users' : path+'/users')}</code>`;
}
function editRoute(hostId, routeId) {
  const host = config.hosts.find(h=>h.id === hostId), existing = host.routes.find(r=>r.id === routeId);
  const route = structuredClone(existing || newRoute('/api','API','backend',8000));
  setModal(existing ? route.name : 'Новый маршрут',`Настройки маршрута для <strong>${esc(host.domain)}</strong>`, `
    <div class="route-tabs" role="tablist" aria-label="Настройки маршрута">${[['general','Маршрут','routes'],['servers','Серверы','server'],['advanced','Кеш и лимиты','sliders']].map(([key,label,symbol])=>btn('route-tab',label,symbol,key==='general' ? 'selected' : '',`data-tab="${key}" role="tab" id="tab-${key}" aria-controls="panel-${key}" aria-selected="${key==='general'}" tabindex="${key==='general' ? 0 : -1}"`)).join('')}</div>
    <section data-route-panel="general" id="panel-general" role="tabpanel" aria-labelledby="tab-general">
      <div class="form-intro"><h3>Куда приходит запрос</h3><p>Путь определяет, какие запросы попадут в этот маршрут.</p></div>
      <div class="grid">${field('Название маршрута','name',route.name,'text','required maxlength="80" placeholder="Backend API"')}${field('Путь на домене','path',route.path,'text','required placeholder="/api"')}</div>
      <div class="option-list">${check('<span><strong>Убирать префикс пути</strong><small>Передавать приложению путь без /api или другого префикса.</small></span>','strip_prefix',route.strip_prefix)}${check('<span><strong>Поддержка WebSocket</strong><small>Разрешить постоянные соединения с приложением.</small></span>','websocket',route.websocket)}</div><div class="path-preview" id="path-preview"></div>
      ${existing ? `<div class="danger-zone">${btn('delete-route','Удалить маршрут','trash','danger ghost small',`data-host="${hostId}" data-route="${routeId}"`)}</div>` : ''}
    </section>
    <section data-route-panel="servers" id="panel-servers" role="tabpanel" aria-labelledby="tab-servers" hidden>
      <div class="form-intro docker-target-heading"><div><h3>Серверы приложения</h3><p>Добавьте несколько серверов, чтобы распределять нагрузку между ними.</p></div>${btn('docker-picker','Выбрать из Docker','docker','small')}</div>
      <div class="grid"><label>Алгоритм балансировки<select name="balance">${Object.entries(balanceLabels).map(([k,v])=>`<option value="${k}" ${route.balance===k ? 'selected' : ''}>${v}</option>`).join('')}</select><small>Round robin — по очереди; Least connections — по загрузке.</small></label>${field('Таймаут ответа, сек','timeout',route.timeout,'number','required min="1" max="3600"','Сколько ждать ответ приложения')}</div>
      <div class="form-section"><div class="target-editor" id="target-editor">${route.targets.map(targetFields).join('')}</div>${btn('add-target','Добавить сервер','plus','add-target-button')}</div>
      <div class="inline-info">${icon('info')}<span>Адрес без http:// и пути. Резервный сервер используется, когда основные недоступны; резерв несовместим с IP hash.</span></div>
    </section>
    <section data-route-panel="advanced" id="panel-advanced" role="tabpanel" aria-labelledby="tab-advanced" hidden>
      <div class="form-intro"><h3>Кеширование и ограничения</h3><p>Индивидуальные правила для этого маршрута.</p></div>
      <div class="option-list">${check('<span><strong>Кешировать публичные ответы</strong><small>Только GET/HEAD без cookies и авторизации.</small></span>','cache',route.cache)}</div>
      <div class="grid">${field('TTL кеша, сек','cache_ttl',route.cache_ttl,'number','required min="1" max="604800"')}${field('Макс. тело запроса, MiB','body_mb',route.body_mb,'number','required min="0" max="102400"','0 — использовать общий лимит')}${field('Дополнительный лимит, r/s','rate_rps',route.rate_rps,'number','required min="0" max="100000"','0 — только общий лимит на IP')}${field('Допустимый всплеск (burst)','rate_burst',route.rate_burst,'number','required min="0" max="100000"')}</div>
      ${route.path.includes('s3') ? `<div class="inline-info">${icon('info')}<span>Для S3 подпись зависит от Host и пути. Удаление префикса может нарушить SigV4. Для стандартного S3 API удобнее отдельный домен.</span></div>` : ''}
    </section>`, async data=>{
      const result={...route,name:data.get('name').trim(),path:data.get('path').trim(),balance:data.get('balance')};
      for(const key of ['timeout','cache_ttl','rate_rps','rate_burst','body_mb']) result[key]=Number(data.get(key));
      for(const key of ['strip_prefix','websocket','cache']) result[key]=data.has(key);
      result.targets=collectTargets();
      const candidate=structuredClone(config), dest=candidate.hosts.find(h=>h.id===hostId);
      if(existing) dest.routes[dest.routes.findIndex(r=>r.id===routeId)]=result; else dest.routes.push(result);
      await api('preview',{config:candidate});config=candidate;changed();
    });
  if (!existing) $('#modal').dataset.replaceDockerTargets = 'true';
  $('#modal-form').addEventListener('input',updatePathPreview);
  updatePathPreview();
}

function example() {
  const front = newRoute(); front.targets.push({address:'frontend-2',port:3000,weight:1,backup:false}); front.targets[0].address = 'frontend-1';
  const back = newRoute('/api','Backend API','backend-1',8000); back.balance = 'least_conn'; back.strip_prefix = true;
  back.targets.push({address:'backend-2',port:8000,weight:1,backup:false});
  const s3 = newRoute('/s3','S3 storage','minio',9000); s3.websocket = false; s3.body_mb = 1024; s3.timeout = 300;
  config.hosts.push({id:uid(),domain:'example.com',enabled:true,routes:[front,back,s3]}); changed();
  notify('Пример добавлен. Укажите реальные адреса серверов перед применением.');
}
function download(name, content, type) {
  const url = URL.createObjectURL(new Blob([content],{type}));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = name; anchor.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
}
document.addEventListener('click', async event=>{
  const button = event.target.closest('[data-action]'); if (!button || button.disabled) return;
  const action = button.dataset.action;
  if (action === 'dismiss-toast') { $('#toast').hidden=true; return; }
  if (action === 'close') return $('#modal').close();
  if (action === 'logout') return task(async()=>{await api('logout',{}); csrf=''; dirty=false; showLogin();});
  if (busy) return;
  try {
    if (action === 'navigate') {collectSettings(); page=button.dataset.page; render();}
    if (action === 'refresh-dashboard') startDashboardStream(true);
    if (action === 'filter-hosts') {hostFilter=button.dataset.filter;render();}
    if (action === 'reset-search') {routeQuery='';hostFilter='all';render();}
    if (action === 'route-tab') selectRouteTab(button.dataset.tab);
    if (action === 'add-host') editHost();
    if (action === 'edit-host') editHost(button.dataset.host);
    if (action === 'add-route' || action === 'edit-route') editRoute(button.dataset.host,button.dataset.route);
    if (action === 'example') example();
    if (action === 'add-target') $('#target-editor').insertAdjacentHTML('beforeend',targetFields({address:'',port:8000,weight:1,backup:false}));
    if (action === 'remove-target') {if(document.querySelectorAll('.target-fields').length > 1) button.closest('.target-fields').remove(); else notify('Нужен хотя бы один сервер',true);}
    if (action === 'delete-host') {
      const host = config.hosts.find(h=>h.id === button.dataset.host);
      if (host && await confirmAction('Удалить домен?', 'Будут удалены домен и все его маршруты.', host.domain, 'Удалить домен', true)) {
        config.hosts = config.hosts.filter(h=>h.id !== host.id); $('#modal').close(); changed();
        notify('Домен удалён из редактора. Сохраните черновик или примените изменения.');
      }
    }
    if (action === 'delete-route') {
      const host = config.hosts.find(h=>h.id === button.dataset.host);
      const route = host?.routes.find(r=>r.id === button.dataset.route);
      if (route && await confirmAction('Удалить маршрут?', `Маршрут «${route.name}» будет удалён.`, host.domain + route.path, 'Удалить маршрут', true)) {
        host.routes = host.routes.filter(r=>r.id !== route.id); $('#modal').close(); changed();
        notify('Маршрут удалён из редактора. Сохраните черновик или примените изменения.');
      }
    }
    if (action === 'save') await task(async()=>{await saveDraft(); notify('Черновик сохранён');});
    if (action === 'apply') await task(async()=>{await saveDraft(); accept(await api('apply',{revision:state.revision})); notify('Конфигурация проверена и применена');});
    if (action === 'test') await task(async()=>{await saveDraft(); const result=await api('test',{revision:state.revision}); notify(result.output);});
    if (action === 'active-config') {await loadPreview(true); notify('Показан действующий nginx.conf');}
    if (action === 'copy-config') {const text=$('#preview').textContent; if (navigator.clipboard && window.isSecureContext) {await navigator.clipboard.writeText(text); notify('Скопировано');} else download('nginx.conf',text,'text/plain');}
    if (action === 'restore' && (!dirty || await confirmAction('Загрузить версию?', 'Несохранённые изменения будут заменены выбранной версией.', '', 'Загрузить версию'))) await task(async()=>{accept(await api('restore',{generation:button.dataset.generation,revision:state.revision})); notify('Версия загружена в черновик. Нажмите «Применить».');});
    if (action === 'export') {collectSettings(); download('gateway-config.json',JSON.stringify(config,null,2),'application/json');}
    if (action === 'import') {const input=document.createElement('input'); input.type='file'; input.accept='.json,application/json'; input.addEventListener('change',()=>task(async()=>{const file=input.files[0]; if(!file)return; if(file.size>1048576)throw new Error('Файл больше 1 MiB'); const candidate=JSON.parse(await file.text()); await api('preview',{config:candidate}); if(config.hosts.length && !await confirmAction('Импортировать конфигурацию?', 'Текущие настройки редактора будут заменены конфигурацией из файла.', file.name, 'Импортировать'))return; config=candidate; dirty=true; notify('Импортировано в редактор. Проверьте настройки и примените.');}));input.click();}
    if (action === 'cache-size') setModal('Размер кеша','Общий лимит дискового кеша для всех маршрутов.',field('Размер, MiB','cache_mb',config.settings.cache_mb,'number','required min="1" max="1048576"'),data=>{config.settings.cache_mb=Number(data.get('cache_mb'));changed();});
    if (action === 'password') setModal('Новый пароль','После изменения потребуется войти снова.',`<div class="stack">${field('Текущий пароль','current','','password','required autocomplete="current-password"')}${field('Новый пароль','password','','password','required minlength="12" maxlength="256" autocomplete="new-password"')}</div>`,async data=>{await api('password',{current:data.get('current'),password:data.get('password')});csrf='';showLogin();notify('Пароль изменён. Войдите снова.');},'Изменить пароль');
  } catch(error) {notify(error.message,true);}
});
document.addEventListener('keydown', event=>{
  if ($('#confirm-modal').open || $('#docker-picker').open) return;
  const editable=event.target.closest('input,textarea,select,[contenteditable=true]');
  if(event.target.matches('[data-action="route-tab"]') && ['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) {
    event.preventDefault(); const buttons=[...document.querySelectorAll('[data-action="route-tab"]')];
    const index=event.key==='Home' ? 0 : event.key==='End' ? buttons.length-1 : (buttons.indexOf(event.target)+(event.key==='ArrowRight' ? 1 : -1)+buttons.length)%buttons.length;
    selectRouteTab(buttons[index].dataset.tab);buttons[index].focus();return;
  }
  if(event.key==='/' && !editable && !$('#modal').open && page==='routes' && $('#route-search')) {event.preventDefault();$('#route-search').focus();}
  if((event.ctrlKey || event.metaKey) && event.key.toLowerCase()==='s' && config && !$('#modal').open) {event.preventDefault();if(dirty) task(async()=>{await saveDraft();notify('Черновик сохранён');});}
});
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
(async()=>{try{csrf=(await api('session')).csrf;await load();}catch{showLogin();}})();
