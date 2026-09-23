'use strict';

let dashboardData = null, dashboardError = '', dashboardStreamState = 'connecting';
let dashboardStream = null, dashboardRetry = null, dashboardWatchdog = null, dashboardRetryDelay = 1000;
const dashNumber = value => value == null ? '—' : new Intl.NumberFormat(locale(), {maximumFractionDigits:1}).format(value);
const dashTime = stamp => new Date(stamp * 1000).toLocaleTimeString(locale(), {hour:'2-digit', minute:'2-digit'});
const dashDuration = seconds => seconds < 60 ? ui`${Math.floor(seconds)} сек` : seconds < 3600 ? ui`${Math.floor(seconds / 60)} мин` : seconds < 86400 ? ui`${Math.floor(seconds / 3600)} ч ${Math.floor(seconds % 3600 / 60)} мин` : ui`${Math.floor(seconds / 86400)} д ${Math.floor(seconds % 86400 / 3600)} ч`;
function dashBytes(bytes) {
  const units = [t('Б'), t('КиБ'), t('МиБ'), t('ГиБ'), t('ТиБ')];
  let index = 0;
  while (bytes >= 1024 && index < units.length - 1) { bytes /= 1024; index++; }
  return `${dashNumber(bytes)} ${units[index]}`;
}
const dashHost = host => host === '_' ? t('Неизвестные домены') : host === '__other__' ? t('Другие домены') : host;
const dashLink = (target, label, id) => btn('navigate', label, 'chevron', 'ghost small', `data-page="${target}" id="${id}"`);

function renderDashboardSnapshot() {
  const status = $('#nginx-status'); if (status) status.innerHTML = statusHTML();
  const content = $('#dashboard-live');
  if (!content || page !== 'dashboard' || !csrf) return;
  const focus = content.contains(document.activeElement) ? document.activeElement.id : null;
  const scrolls = [...content.querySelectorAll('[data-preserve-scroll]')].map(el => [el.id, el.scrollTop, el.scrollLeft]);
  content.innerHTML = dashboardContent();
  for (const [id, top, left] of scrolls) { const el = document.getElementById(id); if (el) {el.scrollTop = top; el.scrollLeft = left;} }
  if (focus) document.getElementById(focus)?.focus({preventScroll:true});
}
function stopDashboardStream() {
  clearTimeout(dashboardRetry); clearTimeout(dashboardWatchdog);
  dashboardRetry = null; dashboardWatchdog = null;
  dashboardStream?.close(); dashboardStream = null;
  dashboardStreamState = 'connecting';
}
function startDashboardStream(force = false) {
  if (!csrf || document.hidden) return;
  if (force) stopDashboardStream();
  if (dashboardStream || dashboardRetry) return;
  const session = csrf, source = new EventSource('/api/events');
  dashboardStream = source;
  dashboardStreamState = 'connecting';
  renderDashboardSnapshot();
  const current = () => csrf === session && dashboardStream === source;
  const retry = async (message, checkSession = false) => {
    if (!current()) return;
    source.close(); clearTimeout(dashboardWatchdog);
    dashboardStreamState = 'reconnecting'; dashboardError = message;
    renderDashboardSnapshot();
    // HTTP 401 closes EventSource without exposing the status to JavaScript.
    if (checkSession) {
      try { await api('session', undefined, {signal:AbortSignal.timeout(5000)}); } catch { /* api handles an expired login. */ }
    }
    if (!current()) return;
    dashboardStream = null;
    dashboardRetry = setTimeout(() => {
      dashboardRetry = null; startDashboardStream();
    }, dashboardRetryDelay);
    dashboardRetryDelay = Math.min(dashboardRetryDelay * 2, 15000);
  };
  const watch = () => {
    clearTimeout(dashboardWatchdog);
    dashboardWatchdog = setTimeout(() => retry('Данные не поступают. Переподключаемся автоматически'), 15000);
  };
  source.addEventListener('dashboard', event => {
    if (!current()) return;
    try {
      const data = JSON.parse(event.data);
      if (!data.nginx || !data.summary || !data.configuration) throw new Error('Invalid snapshot');
      dashboardData = data; health = data.nginx; dashboardError = '';
      dashboardStreamState = 'live'; dashboardRetryDelay = 1000;
      watch(); renderDashboardSnapshot();
      if (typeof receiveDockerLabels === 'function') receiveDockerLabels(data);
      if (typeof receiveGeneral === 'function') receiveGeneral(data);
      if (typeof receiveAgents === 'function') receiveAgents(data);
      if (typeof receiveCertificates === 'function') receiveCertificates(data);
    } catch { retry('Не удалось прочитать обновление. Переподключаемся'); }
  });
  source.addEventListener('stream_error', () => {
    if (!current()) return;
    dashboardError = t('Сервер временно не может получить показатели');
    dashboardStreamState = 'reconnecting';
    renderDashboardSnapshot();
  });
  source.addEventListener('auth_required', () => {
    if (!current()) return;
    csrf = ''; showLogin();
  });
  source.onerror = () => retry('Связь с сервером потеряна. Переподключаемся автоматически', source.readyState === EventSource.CLOSED);
  watch();
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopDashboardStream(); else startDashboardStream();
});
window.addEventListener('pagehide', stopDashboardStream);
window.addEventListener('pageshow', () => startDashboardStream());
window.addEventListener('online', () => startDashboardStream(true));

function dashMetric(label, value, unit, note, symbol, tone = '') {
  return `<article class="dash-kpi ${tone}"><div class="dash-kpi-label">${label}${icon(symbol)}</div><div class="dash-kpi-value">${value}<small>${unit}</small></div><p>${note}</p></article>`;
}
function dashboardContent() {
  const stale = dashboardError ? ui`<div class="dash-alert danger" role="alert">${icon('info')}<div><strong>Обновление данных недоступно</strong><p>${esc(t(dashboardError))}.${dashboardData ? ui` Показан последний снимок на ${dashTime(dashboardData.updated_at)}.` : t(' Нажмите «Обновить», чтобы повторить.')}</p></div></div>` : '';
  if (!dashboardData) return stale || ui`<div class="dash-loading" role="status">${icon('dashboard')}<strong>Подключаемся к вашему AmberGate</strong><span>Загружаем состояние Nginx и статистику трафика…</span></div>`;
  const d = dashboardData, s = d.summary, c = d.configuration, hosts = c.hosts.filter(h => h.enabled);
  const routes = hosts.flatMap(h => h.routes), elapsed = Math.max(0, d.updated_at - d.collected_since);
  const statusTone = !d.nginx.healthy ? 'danger' : s.errors ? 'warning' : '';
  const statusTitle = !d.nginx.healthy ? t('Проверьте состояние Nginx') : s.errors ? t('AmberGate работает · есть ошибки 5xx') : t('AmberGate в работе');
  const statusNote = !d.nginx.healthy ? t('Процесс или действующая версия не подтверждены.') : s.errors ? ui`${dashNumber(s.errors)} ответов с ошибкой сервера за доступный период. Подробности — ниже.` : s.requests ? t('Nginx работает. За доступный период ответов 5xx не было.') : t('Nginx работает. Ожидаем первые запросы к приложениям.');
  return ui`${stale}
    ${!d.available ? ui`<div class="dash-alert danger" role="alert">${icon('info')}<div><strong>Сбор статистики остановлен</strong><p>Проверьте логи контейнера. Показатели трафика могут быть неполными.</p></div></div>` : ''}
    <section class="dash-health ${statusTone}"><span class="dash-health-icon">${icon(d.nginx.healthy ? 'bolt' : 'info')}</span><div><h2>${statusTitle}</h2><p>${statusNote}</p></div><div class="dash-live"><span class="dot ${dashboardStreamState !== 'live' || !d.available ? 'bad' : ''}"></span>${dashboardStreamState === 'live' ? 'LIVE' : t('Снимок')}<small>${new Date(d.updated_at * 1000).toLocaleTimeString(locale())} · автоматически</small></div></section>
    ${(c.pending || dirty) ? ui`<div class="dash-draft">${icon('code')}<span>${dirty ? t('В редакторе есть несохранённые изменения.') : t('Есть неприменённый черновик.')} Здесь показана работа действующей конфигурации.</span>${dashLink('code', t('Смотреть конфиг'), 'dash-config-link')}</div>` : ''}
    <div class="dash-kpis">
      ${dashMetric(t('Всего запросов'), dashNumber(s.requests), '', `${d.rps == null ? t('Скорость появится после замера') : ui`<b>${dashNumber(d.rps)}</b> запр/с · последний полный интервал`}`, 'arrow', 'accent')}
      ${dashMetric(t('Ошибки сервера'), dashNumber(s.errors), '5xx', s.requests ? ui`<b>${dashNumber(s.error_percent)}%</b> от всех запросов` : t('Пока нет завершённых запросов'), 'info', s.errors ? 'has-errors' : '')}
      ${dashMetric(t('Время ответа · p95'), dashNumber(s.p95_ms), t('мс'), s.requests ? ui`Среднее <b>${dashNumber(s.avg_ms)} мс</b> · p95 приблизительно` : t('Измеряется при получении трафика'), 'clock')}
      ${dashMetric(t('Попадания в кеш'), dashNumber(s.cache_hit_percent), s.cache_hit_percent == null ? '' : '%', s.cache_lookups ? ui`<b>${dashNumber(s.cache_hits)}</b> HIT из ${dashNumber(s.cache_lookups)} обращений` : t('Ещё нет обращений к кешу'), 'cache')}
    </div>
    <div class="dash-primary-grid">
      <section class="dash-panel dash-traffic"><div class="dash-panel-head"><div><h2>Трафик через AmberGate</h2><p>Завершённые запросы · по 10 с · последний интервал неполный</p></div><span class="dash-transfer">${icon('download')} ${dashBytes(s.bytes)}<small>отдано клиентам</small></span></div>
        <div class="dash-chart-legend"><span><i class="amber-dot"></i>Все запросы</span><span><i class="red-dot"></i>Ошибки 5xx</span><span class="dash-chart-period">${elapsed < 900 ? ui`Собрано за ${dashDuration(elapsed)}` : t('Последние 15 минут')}</span></div>
        ${trafficChart(d)}
        <div class="dash-chart-foot"><span>${icon('info')}Длительность ответа включает передачу клиенту.</span><span>Запросы панели не учитываются</span></div>
      </section>
      ${systemPanel(d, hosts, routes)}
    </div>
    <div class="dash-secondary-grid">${domainsPanel(d)}${responsesPanel(s)}</div>
    ${eventsPanel(d)}
    <div class="dash-footnote"><span>${icon('lock')}Метрики локальны · история в памяти до 15 минут</span><span>Сбор с ${new Date(d.collected_since * 1000).toLocaleString(locale())} · сброс при перезапуске</span></div>`;
}

function trafficChart(d) {
  const points = d.series, w = 760, top = 14, bottom = 185, left = 46, right = 745;
  const max = Math.max(4, ...points.map(p => p.requests)), magnitude = 10 ** Math.floor(Math.log10(max / 4));
  const ceiling = [1, 2, 5, 10].find(n => n * magnitude >= max / 4) * magnitude * 4;
  const start = points[0]?.time ?? d.updated_at, end = Math.max(start + 10, points.at(-1)?.time ?? start);
  const x = time => left + (time - start) / (end - start) * (right - left);
  const y = value => bottom - value / ceiling * (bottom - top);
  const path = key => points.map((p, i) => `${i ? 'L' : 'M'}${x(p.time).toFixed(1)},${y(p[key]).toFixed(1)}`).join(' ');
  const grid = Array.from({length:5}, (_, i) => {const value = ceiling / 4 * i, row = y(value); return `<line x1="${left}" x2="${right}" y1="${row}" y2="${row}" class="dash-gridline"/><text x="${left - 12}" y="${row + 4}" text-anchor="end">${dashNumber(value)}</text>`;}).join('');
  const ticks = [...new Set([0, Math.floor((points.length - 1) / 2), points.length - 1])].filter(i => points[i]);
  const chartTime = stamp => new Date(stamp * 1000).toLocaleTimeString(locale(), {hour:'2-digit', minute:'2-digit', ...(end-start < 120 ? {second:'2-digit'} : {})});
  const labels = ticks.map(i => `<text x="${x(points[i].time)}" y="215" text-anchor="${i === 0 ? 'start' : i === points.length - 1 ? 'end' : 'middle'}">${chartTime(points[i].time)}</text>`).join('');
  const area = points.length ? `${path('requests')} L${x(points.at(-1).time)},${bottom} L${left},${bottom} Z` : '';
  const targets = points.map(p => ui`<rect x="${Math.max(left, x(p.time) - (right-left) / Math.max(points.length, 1) / 2)}" y="${top}" width="${(right-left) / Math.max(points.length, 1)}" height="${bottom-top}" class="dash-chart-target"><title>${chartTime(p.time)} — ${p.requests} запросов, ${p.errors} ошибок 5xx${p.time + 10 > d.updated_at ? t(" · интервал ещё собирается") : t(" (10 с)")}</title></rect>`).join('');
  return ui`<div class="dash-chart ${d.summary.requests ? '' : 'no-traffic'}"><svg viewBox="0 0 ${w} 228" role="img" aria-label="Трафик: ${d.summary.requests} запросов и ${d.summary.errors} ответов 5xx за доступный период"><defs><linearGradient id="traffic-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#ffaf35" stop-opacity=".24"/><stop offset="100%" stop-color="#ffaf35" stop-opacity=".015"/></linearGradient><clipPath id="traffic-clip"><rect x="${left}" y="0" width="${right-left}" height="${bottom+1}"/></clipPath></defs>${grid}${labels}<g clip-path="url(#traffic-clip)"><path d="${area}" fill="url(#traffic-fill)"/><path d="${path('requests')}" class="dash-traffic-line"/><path d="${path('errors')}" class="dash-error-line"/>${points.length === 1 && points[0].requests ? `<circle cx="${left + 2}" cy="${y(points[0].requests)}" r="3" fill="#ffaf35"/>` : ''}${targets}</g></svg>${d.summary.requests ? '' : ui`<div class="dash-chart-empty"><span>${icon('bolt')}</span><strong>Здесь появится ваш трафик</strong><p>Запросы к доменам AmberGate заполнят график автоматически.</p></div>`}</div>`;
}
function systemPanel(d, hosts, routes) {
  const c = d.connections, cfg = d.configuration;
  return ui`<section class="dash-panel dash-system"><div class="dash-panel-head"><div><h2>Состояние Nginx</h2><p>Текущий момент</p></div><span class="dash-small-icon">${icon('server')}</span></div><div class="dash-connection"><strong>${dashNumber(c?.active)}</strong><span>открытых<br>соединений</span><span class="dash-connection-mark">${icon('bolt')}</span></div><div class="dash-connection-parts"><div><strong>${dashNumber(c?.reading)}</strong><span>Чтение</span></div><div><strong>${dashNumber(c?.writing)}</strong><span>Ответ</span></div><div><strong>${dashNumber(c?.waiting)}</strong><span>Keep-alive</span></div></div><dl class="dash-facts"><div><dt>Время работы</dt><dd>${dashDuration(d.nginx.uptime)}</dd></div><div><dt>Действующие домены</dt><dd>${hosts.length}<span> / ${cfg.hosts.length}</span></dd></div><div><dt>Маршруты / цели</dt><dd>${routes.length} / ${routes.reduce((n, r) => n + r.targets.length + (r.docker?.targets.length || 0), 0)}</dd></div><div><dt>Глобальный лимит / IP</dt><dd>${cfg.settings.rate_rps ? cfg.settings.rate_rps + t(' запр/с') : t('Без лимита')}</dd></div></dl><div class="dash-version"><span><i class="dot ${d.nginx.healthy ? '' : 'bad'}"></i> Версия <code>${esc(cfg.generation.slice(0, 8))}</code></span>${dashLink('history', t('История'), 'dash-history-link')}</div>${d.nginx.last_error ? ui`<p class="dash-runtime-error">Последнее применение не удалось. Откройте nginx.conf и проверьте конфигурацию.</p>` : ''}</section>`;
}
function domainsPanel(d) {
  const active = new Map(d.configuration.hosts.map(h => [h.domain.toLowerCase(), h]));
  const names = [...new Set([...active.keys(), ...Object.keys(d.domains)])].sort((a, b) => (d.domains[b]?.requests || 0) - (d.domains[a]?.requests || 0) || a.localeCompare(b));
  const highest = Math.max(1, ...Object.values(d.domains).map(s => s.requests));
  return ui`<section class="dash-panel dash-domains"><div class="dash-panel-head"><div><h2>Трафик по доменам <span class="dash-count">${names.length}</span></h2><p>Наблюдаемый трафик за доступный период</p></div>${dashLink('routes', t('Маршруты'), 'dash-routes-link')}</div>${names.length ? ui`<div class="dash-table-scroll" id="dash-domain-scroll" data-preserve-scroll><table class="dash-table"><thead><tr><th>Домен</th><th>Запросы</th><th>5xx</th><th>Среднее</th></tr></thead><tbody>${names.map(name => {
    const h = active.get(name), s = d.domains[name];
    return `<tr><td><div class="dash-domain-name">${icon('globe')}<span>${esc(dashHost(name))}<small>${h ? h.enabled ? ui`${h.routes.length} маршрутов · включён` : t('Выключен') : name === '_' ? t('Нет совпадения с server_name') : t('Вне текущего списка доменов')}</small></span></div></td><td><strong>${dashNumber(s?.requests || 0)}</strong><svg viewBox="0 0 68 3" aria-hidden="true"><rect width="68" height="3" rx="1.5" fill="#303033"/><rect width="${68 * (s?.requests || 0) / highest}" height="3" rx="1.5" fill="#ffaf35"/></svg></td><td class="${s?.errors ? 'dash-red' : ''}">${dashNumber(s?.errors || 0)}</td><td>${s?.requests ? dashNumber(s.avg_ms) + t(' мс') : '—'}</td></tr>`;
  }).join('')}</tbody></table></div>` : ui`<div class="dash-empty-state">${icon('globe')}<strong>Пока нет действующих доменов</strong><p>Добавьте домен в «Маршрутах» и примените конфигурацию.</p></div>`}</section>`;
}
function responsesPanel(s) {
  const count = s.requests, success = count ? (s.statuses[1] + s.statuses[2]) / count * 100 : null;
  let offset = 0;
  const arcs = s.statuses.map((n, i) => {
    const size = count ? n / count * 100 : 0, arc = `<circle cx="75" cy="75" r="61" pathLength="100" stroke-dasharray="${size} ${100-size}" stroke-dashoffset="${-offset}" class="response-${i+1}"/>`;
    offset += size; return n ? arc : '';
  }).join('');
  return ui`<section class="dash-panel dash-responses"><div class="dash-panel-head"><div><h2>HTTP-ответы</h2><p>Распределение кодов</p></div>${icon('code')}</div><div class="dash-response-content"><div class="dash-donut"><svg viewBox="0 0 150 150" aria-hidden="true"><circle cx="75" cy="75" r="61" class="dash-ring-track"/><g transform="rotate(-90 75 75)">${arcs}</g></svg><div><strong>${success == null ? '—' : dashNumber(success) + '%'}</strong><span>2xx + 3xx</span></div></div><div class="dash-response-legend">${[['1xx',t('Информация')],['2xx',t('Успешные')],['3xx',t('Редиректы')],['4xx',t('Ошибки клиента')],['5xx',t('Ошибки сервера')]].map(([code, label], i) => `<div><i class="response-${i+1}"></i><span><b>${code}</b> ${label}</span><strong>${dashNumber(s.statuses[i])}</strong></div>`).join('')}</div></div><div class="dash-rate-limits"><span>${icon('shield')} Лимиты AmberGate · HTTP 429</span><strong>${dashNumber(s.limited)}</strong></div></section>`;
}
function eventsPanel(d) {
  return ui`<section class="dash-panel dash-events"><div class="dash-panel-head"><div><h2>Последние проблемные ответы <span class="dash-count">${d.events.length}</span></h2><p>До 30 последних ответов 4xx и 5xx за доступный период</p></div><span class="dash-event-label">${icon('info')} По реальным запросам</span></div>${d.events.length ? ui`<div class="dash-table-scroll" id="dash-event-scroll" data-preserve-scroll><table class="dash-table"><thead><tr><th>Время</th><th>Домен / маршрут</th><th>Ответ</th><th>Длительность</th></tr></thead><tbody>${d.events.map(e => ui`<tr><td class="dash-event-time">${new Date(e.time*1000).toLocaleTimeString(locale())}</td><td><strong>${esc(dashHost(e.host))}</strong><code class="dash-event-route">${esc(e.route === '-' ? t('без upstream') : e.route)}</code></td><td><span class="dash-code ${e.status >= 500 ? 'is-server-error' : ''}">${e.status}</span><span class="dash-error-reason">${e.limited ? t('Лимит AmberGate') : ({429:t('Слишком много запросов'),502:t('Ошибка upstream'),503:t('Сервис недоступен'),504:t('Тайм-аут'),404:t('Не найдено'),403:t('Доступ запрещён'),499:t('Клиент закрыл соединение')})[e.status] || (e.status >= 500 ? t('Ошибка сервера') : t('Ошибка запроса'))}</span></td><td>${dashNumber(e.ms)} мс</td></tr>`).join('')}</tbody></table></div>` : `<div class="dash-events-empty"><span>${icon('check')}</span><div><strong>${d.summary.requests ? t('Ответов с ошибками пока нет') : t('Журнал готов к работе')}</strong><p>${d.summary.requests ? t('В собранных запросах не обнаружены ответы 4xx или 5xx.') : t('При появлении ответов 4xx или 5xx здесь будут домен, маршрут и время.')}</p></div></div>`}</section>`;
}
