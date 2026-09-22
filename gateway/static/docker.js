'use strict';

let dockerData = null, dockerDraft = null, dockerLoading = false, dockerError = '';
let dockerQuery = '', dockerNetwork = '', dockerOnlyReady = false;
let dockerPickerData = null, dockerPickerToken = 0, dockerPickerMode = 'auto', dockerHostDraft = null;
const dockerAddress = (address, port) => `${address.includes(':') ? '[' + address + ']' : address}${port ? ':' + port : ''}`;
const dockerDate = timestamp => new Date(timestamp * 1000).toLocaleTimeString(locale());
const dockerState = state => ({running:t('Запущен'), exited:t('Остановлен'), paused:t('На паузе'), restarting:t('Перезапуск'), created:t('Создан'), dead:t('Остановлен')})[state] || state;

function dockerPage() {
  const cfg = dockerDraft || dockerData?.settings || {socket_path:'/var/run/docker.sock',gateway_container:''};
  return ui`<div class="docker-page"><section class="panel docker-connect"><div class="docker-connect-title"><span class="docker-symbol">${icon('docker')}</span><div><h2>Docker Engine</h2><p>Контейнеры вашего сервера — прямо в редакторе маршрутов.</p></div><div id="docker-status">${dockerStatus()}</div></div><form id="docker-settings"><div class="docker-settings-grid">${field(t('Путь к socket внутри Gateway'),'socket_path',cfg.socket_path,'text','required maxlength="103" placeholder="/var/run/docker.sock"')}${field(t('Имя или ID контейнера gateway'),'gateway_container',cfg.gateway_container,'text',t('maxlength="128" placeholder="Автоматически"'),t('Оставьте пустым для стандартного Docker hostname.'))}${field(t('IP Docker-хоста'),'host_address',cfg.host_address || '', 'text','maxlength="45" placeholder="10.0.0.10"',t('Для опубликованных портов контейнеров из другой сети. Без схемы и порта.'))}</div><div class="docker-connect-actions"><p>Настройки подключения сохраняются локально. Рабочие маршруты меняются после «Применить».</p><span id="docker-connection-actions">${dockerConnectionActions()}</span></div><div id="docker-connection-error" class="error" role="status">${esc(translateError(dockerError))}</div></form><p class="docker-access-note">${icon('lock')}Gateway читает Docker API. Сам socket даёт привилегированный доступ к Docker-хосту.</p></section><div id="docker-inventory">${dockerInventory()}</div><details class="docker-setup"><summary>${icon('code')} Как подключить docker.sock</summary><div><p>Запустите gateway с дополнительным Compose-файлом:</p><pre>docker compose -f compose.ghcr.yaml -f compose.docker.yaml up -d</pre><p>Для локальной сборки замените <code>compose.ghcr.yaml</code> на <code>compose.yaml</code>. Затем нажмите «Подключить Docker» выше.</p><p>Используйте общую сеть, например <code>nginx-gateway</code>, или укажите IP Docker-хоста и выберите опубликованные TCP-порты. Список контейнеров сам по себе не меняет настройки сетей.</p><div class="docker-socket-note">${icon('lock')}<span>Docker socket даёт привилегированный доступ к хосту. Gateway использует только чтение Docker API; монтирование <code>:ro</code> само по себе не ограничивает операции API.</span></div></div></details></div>`;
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
  return ui`<div class="docker-summary"><div>${icon('docker')}<span>Engine <strong>${esc(d.engine_version)}</strong></span></div><div>${icon('server')}<span><strong>${d.containers.length}</strong> контейнеров · <strong>${d.containers.filter(c=>c.selectable).length}</strong> для выбора</span></div><div>${icon('routes')}<span>${d.mode === 'container' ? t('Сети gateway: ') + esc(d.gateway_networks.join(', ') || t('не определены')) : t('Локальный режим · опубликованные порты')}</span></div></div><div class="docker-inventory-heading"><div><h2>Контейнеры</h2><p>${esc(translateError(d.message))} · обновлено ${dockerDate(d.checked_at)}</p></div>${btn('docker-refresh',t('Обновить'),'history','small',dockerLoading ? 'disabled' : '')}</div>${d.truncated ? t('<p class="docker-info">Показаны последние 500 контейнеров Docker.</p>') : ''}<div class="docker-filters"><label class="search-field">${icon('search')}<input id="docker-search" type="search" placeholder="Контейнер, образ или Compose-сервис…" aria-label="Поиск Docker-контейнеров" value="${esc(dockerQuery)}"></label><label class="docker-filter-network"><select id="docker-network" aria-label="Сеть Docker"><option value="">Все сети</option>${d.networks.map(n=>`<option value="${esc(n)}" ${dockerNetwork===n?'selected':''}>${esc(n)}</option>`).join('')}</select></label>${check(t('Можно выбрать'),'docker-ready',dockerOnlyReady)}</div><div id="docker-container-list">${dockerContainerList()}</div><div class="docker-route-tip">${icon('routes')}<div><strong>Выберите контейнеры в настройках маршрута</strong><p>Домены и маршруты → нужный маршрут → Серверы → Выбрать из Docker.</p></div>${btn('navigate',t('К маршрутам'),'arrow','small','data-page="routes"')}</div>`;
}
function dockerContainerList() {
  const rows = dockerFiltered(dockerData,dockerQuery,dockerNetwork,dockerOnlyReady);
  if (!rows.length) return ui`<div class="docker-empty compact"><h3>Контейнеров не найдено</h3><p>${dockerData.containers.length ? t('Измените поиск или фильтр сети.') : t('Запустите приложения в Docker и обновите список.')}</p></div>`;
  return ui`<div class="docker-table-wrap"><table class="docker-table"><thead><tr><th>Контейнер / образ</th><th>Состояние</th><th>TCP-порты</th><th>Сети</th><th>Маршрутизация</th></tr></thead><tbody>${rows.map(c=>`<tr><td><strong>${esc(c.name)}</strong><small>${esc(c.image)}</small>${c.project ? `<span class="docker-compose-tag">${esc(c.project)} / ${esc(c.service)}</span>` : ''}</td><td>${badge(dockerState(c.state),c.state==='running'?'green':'gray')}<small>${esc(c.status)}</small></td><td><code>${c.ports.join(', ') || t('Не объявлены')}</code></td><td>${c.networks.map(n=>`<span class="docker-network-tag ${c.shared_networks.includes(n)?'shared':''}">${esc(n)}</span>`).join('') || '—'}</td><td>${c.selectable ? `<span class="docker-eligible">${icon('check')}${c.endpoints[0].kind==='dns'?t('Имя в Docker DNS'):c.endpoints[0].kind==='published'?t('Порт хоста'):t('IP в общей сети')}</span><code>${esc(dockerAddress(c.endpoints[0].address,c.endpoints[0].port))}</code>` : `<span class="docker-unavailable">${esc(translateError(c.reason))}</span>`}</td></tr>`).join('')}</tbody></table></div>`;
}
async function loadDockerPage(force=false) {
  if (!csrf || page!=='docker' || dockerLoading) return;
  if (!force && dockerData && Date.now()/1000-dockerData.checked_at<3) return;
  dockerLoading=true;
  try {
    const data=await api('docker'+(force?'?refresh=1':''),undefined,{signal:AbortSignal.timeout(10000)});
    if (!csrf) return;
    const first=!dockerData; dockerData=data; dockerError='';
    if (!dockerDraft) dockerDraft={...data.settings};
    if (page==='docker') {
      if (first) $('#page-content').innerHTML=dockerPage();
      else updateDockerPage();
    }
  } catch(error) {if(csrf){dockerError=error instanceof TypeError?t('Не удалось связаться с Gateway'):error.message;}}
  finally {dockerLoading=false; if(csrf && page==='docker') updateDockerPage();}
}
function updateDockerPage() {
  const status=$('#docker-status'); if(!status)return;
  status.innerHTML=dockerStatus();
  $('#docker-connection-actions').innerHTML=dockerConnectionActions();
  $('#docker-connection-error').textContent=translateError(dockerError);
  $('#docker-inventory').innerHTML=dockerInventory();
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
  if(dockerPickerMode==='auto')return dockerPickerData.containers;
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
  if(refresh && dockerPickerData) for(const check of dialog.querySelectorAll('[name=docker-container]:checked')) {
    const row=check.closest('[data-docker-row]'), c=dockerPickerRows()[Number(check.value)];
    const endpoint=c.endpoints[Number($('[data-docker-endpoint]',row).value)];
    selection.set(c.id,{address:endpoint.address,endpointPort:endpoint.port,port:$('[data-docker-port]',row)?.value,custom:$('[data-docker-custom]',row)?.value});
  }
  if (!refresh) {
    dialog.innerHTML=ui`<div class="modal-header"><div><span class="eyebrow">${t('DOCKER DISCOVERY')}</span><h2 id="docker-picker-title">Контейнеры для маршрута</h2><p class="modal-subtitle">Выберите приложения и TCP-порты. Несколько целей распределят нагрузку.</p></div>${btn('docker-picker-close','','close','ghost icon',t('aria-label="Закрыть выбор контейнеров"'))}</div><div id="docker-picker-body" class="modal-body"><p class="docker-loading">Подключаемся к Docker…</p></div>`;
    dialog.showModal();
  } else $('#docker-picker-body').innerHTML=t('<p class="docker-loading">Обновляем контейнеры…</p>');
  try {
    const data=await api('docker?refresh=1',undefined,{signal:AbortSignal.timeout(10000)});
    if(!dialog.open || token!==dockerPickerToken || !csrf)return;
    dockerData=data; dockerPickerData=data;
    if(!data.connected) {
      $('#docker-picker-body').innerHTML=ui`<div class="docker-empty compact">${icon('docker')}<h3>${data.settings.enabled ? t('Нет подключения к Docker') : t('Docker не подключён')}</h3><p>${esc(translateError(data.message))}</p><p>Подключите socket в разделе Docker. Вы можете закрыть выбор и продолжить редактирование вручную.</p>${btn('docker-picker-close',t('Вернуться к маршруту'),'arrow','primary')}</div>`;
      return;
    }
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
    if(dialog.open && token===dockerPickerToken && csrf)$('#docker-picker-body').innerHTML=`<div class="error">${esc(error instanceof TypeError ? t('Не удалось связаться с Gateway') : translateError(error.message))}</div>${btn('docker-picker-refresh',t('Повторить'),'history','small')}`;
  }
}
function renderDockerPicker() {
  const d=dockerPickerData, rows=dockerPickerRows();
  const previousReplace=$('[name=docker-replace]', $('#docker-picker'))?.checked;
  const previousSearch=$('#docker-picker-search')?.value || '';
  const host=location.hostname.replace(/^\[|\]$/g,'');
  const suggested=host!=='::' && host!=='::1' && host!=='0.0.0.0' && !host.startsWith('127.') && !host.startsWith('fe80:') && (/^(\d{1,3}\.){3}\d{1,3}$/.test(host) || host.includes(':')) ? host : '';
  const address=dockerHostDraft ?? (d.settings.host_address || suggested);
  $('#docker-picker-body').innerHTML=ui`<form id="docker-picker-form"><div class="docker-picker-toolbar"><label class="search-field">${icon('search')}<input type="search" id="docker-picker-search" placeholder="Найти контейнер…" aria-label="Найти контейнер"></label>${btn('docker-picker-refresh',t('Обновить'),'history','small')}</div><div class="docker-access-controls"><label>Способ подключения<select id="docker-access-mode"><option value="auto" ${dockerPickerMode==='auto'?'selected':''}>Автоматически</option><option value="host" ${dockerPickerMode==='host'?'selected':''}>Через IP хоста</option></select></label><label>IP Docker-хоста<input id="docker-host-address" type="text" value="${esc(address)}" maxlength="45" placeholder="10.0.0.10" autocomplete="off"></label>${btn('docker-host-save',t('Сохранить IP'),'check','small')}</div><p class="docker-picker-context">${dockerPickerMode==='host' ? t('Используются опубликованные TCP-порты. Общая сеть не требуется.') : t('Сначала общая сеть Docker, иначе — опубликованный порт хоста.')} ${d.settings.host_address ? t('Сохранённый IP: ')+esc(d.settings.host_address)+'.' : t('Сохраните IP хоста для портов, опубликованных на всех интерфейсах.')}${d.truncated?t(' Показаны последние 500 контейнеров.'):''}</p><div class="docker-choices">${rows.length ? rows.map((c,i)=>dockerChoice(c,i)).join('') : t('<div class="docker-empty compact"><h3>Контейнеров пока нет</h3><p>Запустите приложения в Docker и обновите список.</p></div>')}</div><div class="docker-picker-options">${check(t('Заменить текущий список серверов'),'docker-replace',$('#modal').dataset.replaceDockerTargets==='true')}<p>Порты обнаружены по настройкам Docker. Укажите вручную, если приложение не объявило EXPOSE. Убедитесь, что порт обслуживает HTTP.</p>${d.mode==='container' && !config.settings.resolvers.includes('127.0.0.11') ? t('<p class="docker-info">Для имён контейнеров настройте DNS 127.0.0.11 в разделе «Защита и лимиты».</p>') : ''}</div><div class="error" id="docker-picker-error" role="alert"></div><div class="form-actions"><span class="hint" id="docker-selection-count">Выберите контейнеры</span>${btn('docker-picker-close',t('Отмена'),'','ghost')}<button type="submit" class="primary" id="docker-add-selected" disabled>${icon('plus')}Добавить выбранные</button></div></form>`;
  $('#docker-picker-form').addEventListener('submit',addDockerTargets);
  if(previousReplace!==undefined)$('[name=docker-replace]', $('#docker-picker')).checked=previousReplace;
  $('#docker-picker-search').value=previousSearch; filterDockerPicker(previousSearch);
}
function dockerChoice(c,i) {
  const options=c.endpoints.map((e,j)=>`<option value="${j}">${esc(e.kind==='published' ? `${e.container_port}/tcp → ${dockerAddress(e.address,e.port)}` : e.address + (e.network?' · '+e.network:''))}</option>`).join('');
  return `<div class="docker-choice ${c.selectable?'':'unavailable'}" data-docker-row="${i}" data-docker-name="${esc([c.name,c.image,c.project,c.service].join(' ').toLowerCase())}"><label class="check docker-choice-label"><input type="checkbox" name="docker-container" value="${i}" ${c.selectable?'':'disabled'}><span><strong>${esc(c.name)}</strong><small>${esc(c.image)}</small></span></label><span class="docker-choice-state">${badge(dockerState(c.state),c.state==='running'?'green':'gray')}</span>${c.selectable ? `<div class="docker-choice-fields"><label>${c.endpoints[0].kind==='published'?t('Опубликованный порт'):t('Адрес цели')}<select data-docker-endpoint disabled>${options}</select></label>${c.endpoints[0].kind==='published' ? '' : ui`<label>Порт приложения<select data-docker-port disabled>${c.ports.map(p=>`<option value="${p}">${p}/tcp</option>`).join('')}<option value="custom" ${c.ports.length?'':'selected'}>Указать вручную</option></select></label><label data-docker-custom-wrap ${c.ports.length?'hidden':''}>TCP-порт<input type="number" min="1" max="65535" data-docker-custom placeholder="8080" disabled></label>`}</div><p class="docker-choice-note">${c.endpoints[0].kind==='dns' ? 'Docker DNS · ' + esc(c.shared_networks.join(', ')) : c.endpoints[0].kind==='ip' ? t('IP-адрес может измениться при пересоздании. Тогда обновите цель маршрута.') : t('Подключение через порт Docker-хоста.')}</p>` : `<p class="docker-choice-note">${esc(translateError(c.reason))}${c.reason==='Нет общей сети с gateway' ? t('. Укажите IP хоста и опубликуйте TCP-порт, либо подключите общую сеть.') : ''}</p>`}</div>`;
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
function addDockerTargets(event) {
  event.preventDefault();
  const editor=$('#target-editor'); if(!editor || !$('#modal').open)return;
  const result=$('[name=docker-replace]',$('#docker-picker')).checked?[]:collectTargets();
  for(const check of $('#docker-picker').querySelectorAll('[name=docker-container]:checked')) {
    const row=check.closest('[data-docker-row]'), c=dockerPickerRows()[Number(check.value)];
    const endpoint=c.endpoints[Number($('[data-docker-endpoint]',row).value)];
    if(endpoint.kind==='published' && dockerHostDraft!==null && $('#docker-host-address').value.trim()!==dockerPickerData.settings.host_address){$('#docker-picker-error').textContent=t('Сначала сохраните IP Docker-хоста, затем выберите контейнеры.');return;}
    const portField=$('[data-docker-port]',row), port=endpoint.port || Number(portField.value==='custom'?$('[data-docker-custom]',row).value:portField.value);
    if(!Number.isInteger(port)||port<1||port>65535){$('#docker-picker-error').textContent=t('Укажите TCP-порт от 1 до 65535.');return;}
    if(!result.some(t=>t.address.toLowerCase()===endpoint.address.toLowerCase()&&t.port===port))result.push({address:endpoint.address,port,weight:1,backup:false});
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
  if(event.target.id==='docker-access-mode'){dockerPickerMode=event.target.value;renderDockerPicker();}
  if(event.target.id==='docker-network'){dockerNetwork=event.target.value;$('#docker-container-list').innerHTML=dockerContainerList();}
  if(event.target.name==='docker-ready'){dockerOnlyReady=event.target.checked;$('#docker-container-list').innerHTML=dockerContainerList();}
  const row=event.target.closest('[data-docker-row]');if(row)updateDockerSelection(row);
});
document.addEventListener('submit',event=>{if(event.target.id==='docker-settings'){event.preventDefault();saveDocker(true);}});
document.addEventListener('click',event=>{
  const button=event.target.closest('[data-action]');if(!button||button.disabled)return;
  const action=button.dataset.action;
  if(action==='docker-connect')saveDocker(true);
  if(action==='docker-disconnect')saveDocker(false);
  if(action==='docker-refresh')loadDockerPage(true);
  if(action==='docker-picker')openDockerPicker();
  if(action==='docker-picker-close'){$('#docker-picker').close();dockerPickerToken++;}
  if(action==='docker-picker-refresh')openDockerPicker(true);
  if(action==='docker-host-save')saveDockerHost();
});
