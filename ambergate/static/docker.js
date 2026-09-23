'use strict';

let dockerLabelsData = null, dockerLabelMode = null, dockerLabelsBusy = false, dockerConfigSync = false;
let dockerData = null, dockerDraft = null, dockerLoading = false, dockerError = '';
let dockerQuery = '', dockerNetwork = '', dockerOnlyReady = false;
let dockerPickerSource = 'local';
let dockerPickerData = null, dockerPickerToken = 0, dockerPickerMode = 'auto', dockerHostDraft = null;
const dockerAddress = (address, port) => `${address.includes(':') ? '[' + address + ']' : address}${port ? ':' + port : ''}`;
const dockerDate = timestamp => new Date(timestamp * 1000).toLocaleTimeString(locale());
const dockerState = state => ({running:t('Запущен'), exited:t('Остановлен'), paused:t('На паузе'), restarting:t('Перезапуск'), created:t('Создан'), dead:t('Остановлен')})[state] || state;

function dockerLabelsPanel() {
  const mode=dockerLabelMode ?? dockerLabelsData?.mode ?? 'off';
  return ui`<section class="panel docker-labels"><div class="docker-connect-title"><span class="docker-symbol">${icon('routes')}</span><div><h2>Маршруты из Docker labels</h2><p>Одна строка в Compose — домен, маршрут и балансировщик.</p></div>${badge('5s · SSE','amber')}</div><pre class="label-example">ambergate.route: "host=example.com; path=/api; port=3000"</pre><div class="label-controls"><label>Режим автоматизации<select id="docker-label-mode">${[['off',t('Выключено')],['preview',t('Предлагать изменения')],['auto',t('Применять автоматически')]].map(([key,label])=>`<option value="${key}" ${mode===key?'selected':''}>${label}</option>`).join('')}</select></label>${btn('labels-settings',t('Сохранить режим'),'save','small')}</div><p class="hint">Проверка каждые 5 секунд. Перед reload выполняется nginx -t. Ручные настройки и черновики сохраняются отдельно.</p><div id="docker-label-results" aria-live="polite">${dockerLabelsResults()}</div></section>`;
}
function dockerLabelsResults() {
  const d=dockerLabelsData;
  if(!d)return `<p>${t('Загружаем настройки Docker…')}</p>`;
  const disabled=dockerLabelsBusy || d.mode==='off';
  return `<div class="label-status">${badge(({off:t('Выключено'),preview:t('Предлагать изменения'),auto:t('Применять автоматически')})[d.mode],d.errors.length?'amber':'green')}<span>${d.checked_at ? dockerDate(d.checked_at) : '—'}</span></div>
    ${d.errors.map(error=>`<p class="error">${esc(translateError(error))}</p>`).join('')}${d.warnings.map(warning=>`<p class="docker-info">${esc(translateError(warning))}</p>`).join('')}
    ${d.changes.length ? `<div class="label-changes">${d.changes.map(change=>`<div><strong>${esc(change.host)}<code>${esc(change.path)}</code></strong>${badge(change.action==='create'?t('Новый маршрут'):t('Обновить'))}<p>${change.targets.map(target=>esc(dockerAddress(target.address,target.port))).join(', ') || t('Нет серверов · HTTP 503')}</p></div>`).join('')}</div>` : `<p class="hint">${d.mode==='off' ? t('Labels выключены. Уже настроенные маршруты сохраняются.') : d.errors.length ? t('Изменения не применены. Исправьте ошибки labels.') : t('Нет ожидающих изменений')}</p>`}
    <div class="actions">${btn('labels-scan',t('Проверить labels'),'history','small',disabled?'disabled':'')}${btn('labels-apply',t('Применить найденное'),'check','primary small',disabled || !d.token || !d.changes.length || d.errors.length ? 'disabled':'')}</div>`;
}
function updateDockerLabels() {
  const result=$('#docker-label-results');
  if(result){
    const focused=result.contains(document.activeElement) ? document.activeElement.dataset.action : null;
    result.innerHTML=dockerLabelsResults();
    if(focused)result.querySelector(`[data-action="${focused}"]`)?.focus({preventScroll:true});
  }
  const save=$('[data-action=labels-settings]');if(save)save.disabled=dockerLabelsBusy || !dockerLabelsData;
}
async function runDockerLabels(action) {
  if(dockerLabelsBusy || !dockerLabelsData)return;
  dockerLabelsBusy=true;updateDockerLabels();
  const session=csrf;
  try {
    const value={action};
    if(action==='settings'){value.mode=$('#docker-label-mode').value;value.revision=dockerLabelsData.revision;}
    if(action==='apply')value.token=dockerLabelsData.token;
    const data=await api('docker/labels',value,{signal:AbortSignal.timeout(30000)});
    if(csrf!==session)return;
    dockerLabelsData=data;
    if(action==='settings') {dockerLabelMode=data.mode;notify(t('Режим labels сохранён'));}
    if(data.errors.length)notify(t('Изменения не применены. Исправьте ошибки labels.'),true);
    else if(action==='apply')notify(t('Конфигурация проверена и применена'));
  } catch(error){if(csrf===session)notify(error.message,true);}
  finally {dockerLabelsBusy=false;if(csrf===session)updateDockerLabels();}
}
function receiveDockerLabels(data) {
  if(data.labels && !dockerLabelsBusy) {
    const changed=JSON.stringify(dockerLabelsData)!==JSON.stringify(data.labels);
    dockerLabelsData=data.labels;
    if(changed)updateDockerLabels();
  }
  // Refresh read-only pages on revision changes. Never replace an open editor
  // or unsaved form, even if it is opened while the fetch is in flight.
  const safe=()=>csrf && state && config && !dirty && !busy && !document.querySelector('dialog[open]') && ['routes','history','dashboard'].includes(page);
  if(data.configuration.revision && data.configuration.revision!==state?.revision && safe() && !dockerConfigSync) {
    dockerConfigSync=true;
    const session=csrf, revision=state.revision;
    api('config').then(snapshot=>{
      if(session===csrf && safe() && state.revision===revision){accept(snapshot);render();}
    }).catch(()=>{}).finally(()=>{dockerConfigSync=false;});
  }
}

function dockerPage() {
  const cfg = dockerDraft || dockerData?.settings || {socket_path:'/var/run/docker.sock',gateway_container:''};
  return ui`<div class="docker-page"><section class="panel docker-connect"><div class="docker-connect-title"><span class="docker-symbol">${icon('docker')}</span><div><h2>Docker Engine</h2><p>Контейнеры вашего сервера — прямо в редакторе маршрутов.</p></div><div id="docker-status">${dockerStatus()}</div></div><form id="docker-settings"><div class="docker-settings-grid">${field(t('Путь к socket внутри AmberGate'),'socket_path',cfg.socket_path,'text','required maxlength="103" placeholder="/var/run/docker.sock"')}${field(t('Имя или ID контейнера AmberGate'),'gateway_container',cfg.gateway_container,'text',t('maxlength="128" placeholder="Автоматически"'),t('Оставьте пустым для стандартного Docker hostname.'))}${field(t('IP Docker-хоста'),'host_address',cfg.host_address || '', 'text','maxlength="45" placeholder="10.0.0.10"',t('Для опубликованных портов контейнеров из другой сети. Без схемы и порта.'))}</div><div class="docker-connect-actions"><p>Настройки подключения сохраняются локально. Ручные маршруты меняются после «Применить». Автоматизация labels настраивается ниже.</p><span id="docker-connection-actions">${dockerConnectionActions()}</span></div><div id="docker-connection-error" class="error" role="status">${esc(translateError(dockerError))}</div></form><p class="docker-access-note">${icon('lock')}AmberGate читает Docker API. Сам socket даёт привилегированный доступ к Docker-хосту.</p></section>${dockerLabelsPanel()}<div id="docker-inventory">${dockerInventory()}</div><details class="docker-setup"><summary>${icon('code')} Как подключить docker.sock</summary><div><p>Запустите AmberGate с дополнительным Compose-файлом:</p><pre>docker compose -f compose.ghcr.yaml -f compose.docker.yaml up -d</pre><p>Для локальной сборки замените <code>compose.ghcr.yaml</code> на <code>compose.yaml</code>. Затем нажмите «Подключить Docker» выше.</p><p>Используйте общую сеть, например <code>ambergate</code>, или укажите IP Docker-хоста и выберите опубликованные TCP-порты. Список контейнеров сам по себе не меняет настройки сетей.</p><div class="docker-socket-note">${icon('lock')}<span>Docker socket даёт привилегированный доступ к хосту. AmberGate использует только чтение Docker API; монтирование <code>:ro</code> само по себе не ограничивает операции API.</span></div></div></details></div>`;
}
function dockerStatus() {
  if (!dockerData) return badge(t('Проверяем подключение'),'gray');
  return badge(dockerData.connected ? t('Подключён') : dockerData.settings.enabled ? t('Нет подключения') : t('Выключен'), dockerData.connected ? 'green' : dockerData.settings.enabled ? 'amber' : 'gray');
}
function dockerConnectionActions() {
  return `${dockerData?.settings.enabled ? btn('docker-disconnect',t('Отключить'),'','ghost small',dockerLoading ? 'disabled' : '') : ''}${btn('docker-connect',dockerLoading ? t('Подключаемся…') : dockerData?.settings.enabled ? t('Сохранить и проверить') : t('Подключить Docker'),'docker','primary',dockerLoading || !dockerData ? 'disabled' : '')}`;
}
function dockerFiltered(data, query, network, ready) {
  const q = query.trim().toLowerCase();
  return data.containers.filter(c => (!ready || c.selectable) && (!network || c.networks.includes(network)) && (!q || [c.name,c.image,c.project,c.service].some(t=>t.toLowerCase().includes(q))));
}
function dockerInventory() {
  if (!dockerData) return ui`<div class="docker-empty">${icon('docker')}<h3>Загружаем настройки Docker…</h3></div>`;
  const d = dockerData;
  if (!d.connected) return ui`<div class="docker-empty">${icon('docker')}<h3>${d.settings.enabled ? t('Docker пока недоступен') : t('Подключите ваши контейнеры')}</h3><p>${esc(translateError(d.message))}</p><p>После подключения здесь появятся контейнеры, сети и TCP-порты.</p></div>`;
  return ui`<div class="docker-summary"><div>${icon('docker')}<span>Engine <strong>${esc(d.engine_version)}</strong></span></div><div>${icon('server')}<span><strong>${d.containers.length}</strong> контейнеров · <strong>${d.containers.filter(c=>c.selectable).length}</strong> для выбора</span></div><div>${icon('routes')}<span>${d.mode === 'host-network' ? at('Сеть хоста · локальные bridge-сети','Host network · local bridge networks') : d.mode === 'container' ? t('Сети AmberGate: ') + esc(d.gateway_networks.join(', ') || t('не определены')) : t('Локальный режим · опубликованные порты')}</span></div></div><div class="docker-inventory-heading"><div><h2>Контейнеры</h2><p>${esc(translateError(d.message))} · обновлено ${dockerDate(d.checked_at)}</p></div>${btn('docker-refresh',t('Обновить'),'history','small',dockerLoading ? 'disabled' : '')}</div>${d.truncated ? t('<p class="docker-info">Показаны последние 500 контейнеров Docker.</p>') : ''}<div class="docker-filters"><label class="search-field">${icon('search')}<input id="docker-search" type="search" placeholder="Контейнер, образ или Compose-сервис…" aria-label="Поиск Docker-контейнеров" value="${esc(dockerQuery)}"></label><label class="docker-filter-network"><select id="docker-network" aria-label="Сеть Docker"><option value="">Все сети</option>${d.networks.map(n=>`<option value="${esc(n)}" ${dockerNetwork===n?'selected':''}>${esc(n)}</option>`).join('')}</select></label>${check(t('Можно выбрать'),'docker-ready',dockerOnlyReady)}</div><div id="docker-container-list">${dockerContainerList()}</div><div class="docker-route-tip">${icon('routes')}<div><strong>Выберите контейнеры в настройках маршрута</strong><p>Домены и маршруты → нужный маршрут → Серверы → Выбрать из Docker.</p></div>${btn('navigate',t('К маршрутам'),'arrow','small','data-page="routes"')}</div>`;
}
function dockerContainerList() {
  const rows = dockerFiltered(dockerData,dockerQuery,dockerNetwork,dockerOnlyReady);
  if (!rows.length) return ui`<div class="docker-empty compact"><h3>Контейнеров не найдено</h3><p>${dockerData.containers.length ? t('Измените поиск или фильтр сети.') : t('Запустите приложения в Docker и обновите список.')}</p></div>`;
  return ui`<div class="docker-table-wrap"><table class="docker-table"><thead><tr><th>Контейнер / образ</th><th>Состояние</th><th>TCP-порты</th><th>Сети</th><th>Маршрутизация</th></tr></thead><tbody>${rows.map(c=>`<tr><td><strong>${esc(c.name)}</strong><small>${esc(c.image)}</small>${c.project ? `<span class="docker-compose-tag">${esc(c.project)} / ${esc(c.service)}</span>` : ''}</td><td>${badge(dockerState(c.state),c.state==='running'?'green':'gray')}<small>${esc(c.status)}</small></td><td><code>${c.ports.join(', ') || t('Не объявлены')}</code></td><td>${c.networks.map(n=>`<span class="docker-network-tag ${(c.reachable_networks || c.shared_networks).includes(n)?'shared':''}">${esc(n)}</span>`).join('') || '—'}</td><td>${c.selectable ? `<span class="docker-eligible">${icon('check')}${c.endpoints[0].kind==='dns'?t('Имя в Docker DNS'):c.endpoints[0].kind==='published'?t('Порт хоста'):t('IP в общей сети')}</span><code>${esc(dockerAddress(c.endpoints[0].address,c.endpoints[0].port))}</code>` : `<span class="docker-unavailable">${esc(translateError(c.reason))}</span>`}</td></tr>`).join('')}</tbody></table></div>`;
}
async function loadDockerPage(force=false) {
  if (!csrf || page!=='docker' || dockerLoading) return;
  if (!force && dockerData && Date.now()/1000-dockerData.checked_at<3) return;
  dockerLoading=true;
  try {
    const [data,labels]=await Promise.all([api('docker'+(force?'?refresh=1':''),undefined,{signal:AbortSignal.timeout(10000)}),api('docker/labels')]);
    dockerLabelsData=labels;
    if (!csrf) return;
    const first=!dockerData; dockerData=data; dockerError='';
    if (!dockerDraft) dockerDraft={...data.settings};
    if (page==='docker') {
      if (first) $('#page-content').innerHTML=dockerPage();
      else updateDockerPage();
    }
  } catch(error) {if(csrf){dockerError=error instanceof TypeError?t('Не удалось связаться с AmberGate'):error.message;}}
  finally {dockerLoading=false; if(csrf && page==='docker') updateDockerPage();}
}
function updateDockerPage() {
  const status=$('#docker-status'); if(!status)return;
  status.innerHTML=dockerStatus();
  $('#docker-connection-actions').innerHTML=dockerConnectionActions();
  $('#docker-connection-error').textContent=translateError(dockerError);
  $('#docker-inventory').innerHTML=dockerInventory();
  updateDockerLabels();
}
function dockerSettingsFromForm() {
  return {enabled:dockerData?.settings.enabled || false, socket_path:$('#docker-settings [name=socket_path]').value.trim(), gateway_container:$('#docker-settings [name=gateway_container]').value.trim(), host_address:$('#docker-settings [name=host_address]').value.trim()};
}
async function saveDocker(enabled) {
  if(!dockerData || dockerLoading || enabled && !$('#docker-settings').reportValidity())return;
  dockerDraft=enabled?dockerSettingsFromForm():{...dockerData.settings};dockerDraft.enabled=enabled;dockerLoading=true;updateDockerPage();
  const session=csrf;
  try {
    const data=await api('docker',{settings:dockerDraft,revision:dockerData.revision},{signal:AbortSignal.timeout(10000)});
    if(csrf!==session)return;
    dockerData=data;
    dockerDraft={...dockerData.settings};dockerHostDraft=dockerData.settings.host_address;dockerError='';
    const form=$('#docker-settings');if(form){$('[name=socket_path]',form).value=dockerDraft.socket_path;$('[name=gateway_container]',form).value=dockerDraft.gateway_container;$('[name=host_address]',form).value=dockerDraft.host_address || '';}
    notify(!enabled ? t('Обнаружение Docker выключено. Маршруты сохранены.') : dockerData.connected ? t('Docker подключён. Контейнеры доступны в редакторе.') : t('Настройки сохранены. Проверьте подключение к socket.'),enabled && !dockerData.connected);
  } catch(error) {if(csrf===session)dockerError=error.message;}
  finally {dockerLoading=false;if(csrf && page==='docker')updateDockerPage();}
}

function dockerPickerRows() {
  if(dockerPickerData.agent || dockerPickerMode==='auto')return dockerPickerData.containers;
  return dockerPickerData.containers.map(c=>({...c,endpoints:c.host_endpoints || [],
    selectable:!!c.host_endpoints?.length,reason:c.host_reason || c.reason}));
}
function filterDockerPicker(value) {
  const query=value.trim().toLowerCase();
  $('#docker-picker').querySelectorAll('[data-docker-row]').forEach(row=>row.hidden=!row.dataset.dockerName.includes(query));
}
async function saveDockerHost() {
  const button=$('[data-action=docker-host-save]'), token=dockerPickerToken, session=csrf;
  const host_address=$('#docker-host-address').value.trim();
  button.disabled=true;
  try {
    const data=await api('docker',{revision:dockerPickerData.revision,
      settings:{...dockerPickerData.settings,host_address}},{signal:AbortSignal.timeout(10000)});
    if(csrf!==session)return;
    if(dockerDraft && dockerDraft.host_address===dockerData?.settings.host_address)dockerDraft.host_address=data.settings.host_address;
    dockerData=data;dockerHostDraft=data.settings.host_address;
    if($('#docker-picker').open && token===dockerPickerToken)await openDockerPicker(true);
  } catch(error) {
    if(csrf===session && token===dockerPickerToken && $('#docker-picker-error'))$('#docker-picker-error').textContent=translateError(error.message);
  } finally {button.disabled=false;}
}
async function openDockerPicker(refresh=false) {
  const dialog=$('#docker-picker'), token=++dockerPickerToken;
  const selection = new Map(), search = refresh ? $('#docker-picker-search')?.value || '' : '';
  const replace = refresh ? $('[name=docker-replace]',dialog)?.checked : undefined;
  if(refresh && dockerPickerData && dockerPickerData.source===dockerPickerSource) for(const check of dialog.querySelectorAll('[name=docker-container]:checked')) {
    const row=check.closest('[data-docker-row]'), c=dockerPickerRows()[Number(check.value)];
    const endpoint=c.endpoints[Number($('[data-docker-endpoint]',row).value)];
    selection.set(c.id,{address:endpoint.address,endpointPort:endpoint.port,port:$('[data-docker-port]',row)?.value,custom:$('[data-docker-custom]',row)?.value});
  }
  if (!refresh) {
    dialog.innerHTML=ui`<div class="modal-header"><div><span class="eyebrow">${t('DOCKER DISCOVERY')}</span><h2 id="docker-picker-title">Контейнеры для маршрута</h2><p class="modal-subtitle">Выберите приложения и TCP-порты. Несколько целей распределят нагрузку.</p></div>${btn('docker-picker-close','','close','ghost icon',t('aria-label="Закрыть выбор контейнеров"'))}</div><div id="docker-picker-body" class="modal-body"><p class="docker-loading">Подключаемся к Docker…</p></div>`;
    dialog.showModal();
  } else $('#docker-picker-body').innerHTML=t('<p class="docker-loading">Обновляем контейнеры…</p>');
  try {
    const session=csrf;
    const summary=await api('agents');
    if(!dialog.open || token!==dockerPickerToken || session!==csrf)return;
    agentsData=summary;
    if(dockerPickerSource!=='local' && !summary.agents.some(a=>a.id===dockerPickerSource))dockerPickerSource='local';
    let data;
    if(dockerPickerSource==='local') {
      data=await api('docker?refresh=1',undefined,{signal:AbortSignal.timeout(10000)});dockerData=data;
    } else {
      const detail=await api('agents/'+dockerPickerSource,undefined,{signal:AbortSignal.timeout(10000)}), agent=detail.agents[0];
      const ready=agent.enabled && agent.tunnel && agent.docker_connected && !agent.truncated && ['online','error'].includes(agent.status);
      data={agent,settings:{enabled:true,host_address:''},mode:'agent',connected:ready,containers:agent.inventory.map(c=>({
        ...c,endpoints:[{address:c.name,kind:'agent'}],
        selectable:ready && agent.manual_routing && c.state==='running' && !c.is_gateway && c.endpoints.length>0 && c.shared_networks.length>0,
        reason:!agent.manual_routing?at('Обновите образ агента для ручного выбора контейнеров.','Update the agent image to select containers manually.'):
          !ready?at('Агент недоступен','Agent unavailable'):c.is_gateway?at('Это сам агент','This is the agent itself'):
          c.state!=='running'?at('Контейнер не запущен','Container is not running'):at('Используйте host на Linux или подключите агент к сети контейнера','Use host networking on Linux or connect the agent to the container network')
      }))};
    }
    if(!dialog.open || token!==dockerPickerToken || session!==csrf)return;
    dockerPickerData={...data,source:dockerPickerSource};
    renderDockerPicker();
    $('#docker-picker-search').value=search;filterDockerPicker(search);
    if(replace!==undefined)$('[name=docker-replace]',dialog).checked=replace;
    for(const check of dialog.querySelectorAll('[name=docker-container]')) {
      const c=dockerPickerRows()[Number(check.value)], old=selection.get(c.id);
      if(!old || !c.selectable)continue;
      const index=c.endpoints.findIndex(e=>e.address===old.address&&e.port===old.endpointPort);
      if(index<0)continue;
      const row=check.closest('[data-docker-row]');check.checked=true;
      $('[data-docker-endpoint]',row).value=String(index);
      const port=$('[data-docker-port]',row), custom=$('[data-docker-custom]',row);
      if(port && [...port.options].some(o=>o.value===old.port))port.value=old.port;
      if(custom)custom.value=old.custom || '';
      updateDockerSelection(row);
    }
  } catch(error) {
    if(dialog.open && token===dockerPickerToken && csrf)$('#docker-picker-body').innerHTML=`<div class="error">${esc(error instanceof TypeError ? t('Не удалось связаться с AmberGate') : translateError(error.message))}</div>${btn('docker-picker-refresh',t('Повторить'),'history','small')}`;
  }
}
function renderDockerPicker() {
  const d=dockerPickerData, rows=dockerPickerRows(), remote=!!d.agent;
  const previousReplace=$('[name=docker-replace]', $('#docker-picker'))?.checked;
  const previousSearch=$('#docker-picker-search')?.value || '';
  const host=location.hostname.replace(/^\[|\]$/g,'');
  const suggested=host!=='::' && host!=='::1' && host!=='0.0.0.0' && !host.startsWith('127.') && !host.startsWith('fe80:') && (/^(\d{1,3}\.){3}\d{1,3}$/.test(host) || host.includes(':')) ? host : '';
  const address=dockerHostDraft ?? (d.settings.host_address || suggested);
  $('#docker-picker-body').innerHTML=ui`<form id="docker-picker-form"><label class="docker-source-control">${at('Источник контейнеров','Container source')}<select id="docker-picker-source"><option value="local" ${dockerPickerSource==='local'?'selected':''}>${at('Локальный Docker · AmberGate','Local Docker · AmberGate')}</option>${(agentsData?.agents||[]).map(a=>`<option value="${a.id}" ${dockerPickerSource===a.id?'selected':''}>${esc(a.name)} · ${esc(agentStatus(a.status))}</option>`).join('')}</select></label><div class="docker-picker-toolbar"><label class="search-field">${icon('search')}<input type="search" id="docker-picker-search" placeholder="Найти контейнер…" aria-label="Найти контейнер"></label>${btn('docker-picker-refresh',t('Обновить'),'history','small')}</div>${remote?`<div class="docker-picker-context agent-picker-context">${badge(agentStatus(d.agent.status),d.connected?'green':'gray')}<strong>${esc(d.agent.name)}</strong><p>${at('Трафик идёт через туннель агента. Labels и публикация портов не нужны.','Traffic uses the agent tunnel. No labels or published ports required.')}</p>${!d.agent.manual_routing?`<p class="error">${at('Обновите контейнер агента до последнего образа ambergate-agent:latest и пересоздайте его.','Update the agent container to the latest ambergate-agent:latest image and recreate it.')}</p>`:''}</div>`:ui`<div class="docker-access-controls"><label>Способ подключения<select id="docker-access-mode"><option value="auto" ${dockerPickerMode==='auto'?'selected':''}>Автоматически</option><option value="host" ${dockerPickerMode==='host'?'selected':''}>Через IP хоста</option></select></label><label>IP Docker-хоста<input id="docker-host-address" type="text" value="${esc(address)}" maxlength="45" placeholder="10.0.0.10" autocomplete="off"></label>${btn('docker-host-save',t('Сохранить IP'),'check','small')}</div><p class="docker-picker-context">${dockerPickerMode==='host' ? t('Используются опубликованные TCP-порты. Общая сеть не требуется.') : t('Сначала общая сеть Docker, иначе — опубликованный порт хоста.')} ${d.settings.host_address ? t('Сохранённый IP: ')+esc(d.settings.host_address)+'.' : t('Сохраните IP хоста для портов, опубликованных на всех интерфейсах.')}${d.truncated?t(' Показаны последние 500 контейнеров.'):''}</p>`}<div class="docker-choices">${!d.connected&&!remote ? `<div class="docker-empty compact"><h3>${t('Docker не подключён')}</h3><p>${esc(translateError(d.message))}</p><p>${at('Выберите подключённого агента выше или настройте локальный Docker.','Select a connected agent above, or configure local Docker.')}</p></div>` : rows.length ? rows.map((c,i)=>dockerChoice(c,i)).join('') : t('<div class="docker-empty compact"><h3>Контейнеров пока нет</h3><p>Запустите приложения в Docker и обновите список.</p></div>')}</div><div class="docker-picker-options">${check(t('Заменить текущий список серверов'),'docker-replace',$('#modal').dataset.replaceDockerTargets==='true')}<p>Порты обнаружены по настройкам Docker. Укажите вручную, если приложение не объявило EXPOSE. Убедитесь, что порт обслуживает HTTP.</p>${d.mode==='container' && !config.settings.resolvers.includes('127.0.0.11') ? t('<p class="docker-info">Для имён контейнеров настройте DNS 127.0.0.11 в разделе «Защита и лимиты».</p>') : ''}</div><div class="error" id="docker-picker-error" role="alert"></div><div class="form-actions"><span class="hint" id="docker-selection-count">Выберите контейнеры</span>${btn('docker-picker-close',t('Отмена'),'','ghost')}<button type="submit" class="primary" id="docker-add-selected" disabled>${icon('plus')}Добавить выбранные</button></div></form>`;
  $('#docker-picker-form').addEventListener('submit',addDockerTargets);
  if(previousReplace!==undefined)$('[name=docker-replace]', $('#docker-picker')).checked=previousReplace;
  $('#docker-picker-search').value=previousSearch; filterDockerPicker(previousSearch);
}
function dockerChoice(c,i) {
  const options=c.endpoints.map((e,j)=>`<option value="${j}">${esc(e.kind==='published' ? `${e.container_port}/tcp → ${dockerAddress(e.address,e.port)}` : e.address + (e.network?' · '+e.network:''))}</option>`).join('');
  return `<div class="docker-choice ${c.selectable?'':'unavailable'}" data-docker-row="${i}" data-docker-name="${esc([c.name,c.image,c.project,c.service].join(' ').toLowerCase())}"><label class="check docker-choice-label"><input type="checkbox" name="docker-container" value="${i}" ${c.selectable?'':'disabled'}><span><strong>${esc(c.name)}</strong><small>${esc(c.image)}</small></span></label><span class="docker-choice-state">${badge(dockerState(c.state),c.state==='running'?'green':'gray')}</span>${c.selectable ? `<div class="docker-choice-fields"><label>${c.endpoints[0].kind==='published'?t('Опубликованный порт'):t('Адрес цели')}<select data-docker-endpoint disabled>${options}</select></label>${c.endpoints[0].kind==='published' ? '' : ui`<label>Порт приложения<select data-docker-port disabled>${c.ports.map(p=>`<option value="${p}">${p}/tcp</option>`).join('')}<option value="custom" ${c.ports.length?'':'selected'}>Указать вручную</option></select></label><label data-docker-custom-wrap ${c.ports.length?'hidden':''}>TCP-порт<input type="number" min="1" max="65535" data-docker-custom placeholder="8080" disabled></label>`}</div><p class="docker-choice-note">${c.endpoints[0].kind==='dns' ? 'Docker DNS · ' + esc(c.shared_networks.join(', ')) : c.endpoints[0].kind==='agent' ? at('Через агента · ','Via agent · ')+esc(dockerPickerData.agent.name)+' · '+esc(c.shared_networks.join(', ')) : c.endpoints[0].kind==='ip' ? t('IP-адрес может измениться при пересоздании. Тогда обновите цель маршрута.') : t('Подключение через порт Docker-хоста.')}</p>` : `<p class="docker-choice-note">${esc(translateError(c.reason))}${c.reason==='Нет общей сети с AmberGate' ? t('. Укажите IP хоста и опубликуйте TCP-порт, либо подключите общую сеть.') : ''}</p>`}</div>`;
}
function updateDockerSelection(row) {
  const chosen=$('[name=docker-container]',row).checked;
  row.classList.toggle('chosen',chosen);
  row.querySelectorAll('select,input[type=number]').forEach(el=>el.disabled=!chosen);
  const port=$('[data-docker-port]',row), custom=$('[data-docker-custom]',row);
  if(custom){const manual=port.value==='custom';custom.required=chosen&&manual;custom.disabled=!chosen||!manual;$('[data-docker-custom-wrap]',row).hidden=!manual;}
  const count=$('#docker-picker').querySelectorAll('[name=docker-container]:checked').length;
  $('#docker-selection-count').textContent=ui`Выбрано контейнеров: ${count}`;
  $('#docker-add-selected').disabled=!count;
}
async function addDockerTargets(event) {
  event.preventDefault();
  const editor=$('#target-editor'); if(!editor || !$('#modal').open)return;
  const result=$('[name=docker-replace]',$('#docker-picker')).checked?[]:collectTargets();
  const remote=[], token=dockerPickerToken, session=csrf;
  for(const check of $('#docker-picker').querySelectorAll('[name=docker-container]:checked')) {
    const row=check.closest('[data-docker-row]'), c=dockerPickerRows()[Number(check.value)];
    const endpoint=c.endpoints[Number($('[data-docker-endpoint]',row).value)];
    if(endpoint.kind==='published' && dockerHostDraft!==null && $('#docker-host-address').value.trim()!==dockerPickerData.settings.host_address){$('#docker-picker-error').textContent=t('Сначала сохраните IP Docker-хоста, затем выберите контейнеры.');return;}
    const portField=$('[data-docker-port]',row), port=endpoint.port || Number(portField.value==='custom'?$('[data-docker-custom]',row).value:portField.value);
    if(!Number.isInteger(port)||port<1||port>65535){$('#docker-picker-error').textContent=t('Укажите TCP-порт от 1 до 65535.');return;}
    if(endpoint.kind==='agent'){remote.push({container_id:c.id,port});continue;}
    if(!result.some(t=>t.address.toLowerCase()===endpoint.address.toLowerCase()&&t.port===port))result.push({address:endpoint.address,port,weight:1,backup:false});
  }
  if(remote.length){
    const button=$('#docker-add-selected');button.disabled=true;
    try{
      const data=await api('agents/targets',{agent_id:dockerPickerData.agent.id,targets:remote});
      if(session!==csrf || token!==dockerPickerToken || !$('#docker-picker').open || editor!==$('#target-editor'))return;
      for(const target of data.targets)if(!result.some(t=>t.address===target.address&&t.port===target.port))result.push(target);
    }catch(error){if(session===csrf&&token===dockerPickerToken&&$('#docker-picker-error'))$('#docker-picker-error').textContent=translateError(error.message);return;}
    finally{if(button.isConnected)button.disabled=false;}
  }
  if(!result.length||result.length>32){$('#docker-picker-error').textContent=t('В маршруте должно быть от 1 до 32 серверов. При необходимости замените текущий список.');return;}
  editor.innerHTML=result.map(targetFields).join('');
  $('#modal').dataset.replaceDockerTargets='false';
  $('#docker-picker').close();
  notify(t('Контейнеры добавлены в редактор. Сохраните маршрут, затем примените конфигурацию.'));
}
document.addEventListener('input',event=>{
  if(event.target.closest('#docker-settings'))dockerDraft=dockerSettingsFromForm();
  if(event.target.id==='docker-search'){dockerQuery=event.target.value;$('#docker-container-list').innerHTML=dockerContainerList();}
  if(event.target.id==='docker-picker-search')filterDockerPicker(event.target.value);
  if(event.target.id==='docker-host-address')dockerHostDraft=event.target.value;
});
document.addEventListener('change',event=>{
  if(event.target.id==='docker-label-mode')dockerLabelMode=event.target.value;
  if(event.target.id==='docker-picker-source'){dockerPickerSource=event.target.value;openDockerPicker(true);}
  if(event.target.id==='docker-access-mode'){dockerPickerMode=event.target.value;renderDockerPicker();}
  if(event.target.id==='docker-network'){dockerNetwork=event.target.value;$('#docker-container-list').innerHTML=dockerContainerList();}
  if(event.target.name==='docker-ready'){dockerOnlyReady=event.target.checked;$('#docker-container-list').innerHTML=dockerContainerList();}
  const row=event.target.closest('[data-docker-row]');if(row)updateDockerSelection(row);
});
document.addEventListener('submit',event=>{if(event.target.id==='docker-settings'){event.preventDefault();saveDocker(true);}});
document.addEventListener('click',event=>{
  const button=event.target.closest('[data-action]');if(!button||button.disabled)return;
  const action=button.dataset.action;
  if(action==='labels-settings')runDockerLabels('settings');
  if(action==='labels-scan')runDockerLabels('scan');
  if(action==='labels-apply')runDockerLabels('apply');
  if(action==='docker-connect')saveDocker(true);
  if(action==='docker-disconnect')saveDocker(false);
  if(action==='docker-refresh')loadDockerPage(true);
  if(action==='docker-picker')openDockerPicker();
  if(action==='docker-picker-close'){$('#docker-picker').close();dockerPickerToken++;}
  if(action==='docker-picker-refresh')openDockerPicker(true);
  if(action==='docker-host-save')saveDockerHost();
});
