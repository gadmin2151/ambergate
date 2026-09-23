'use strict';
let agentsData = null, agentsLoading = false, agentsMutating = false, agentWizard = null;
const agentStatus = status => ({online:at('Подключён','Connected'),waiting:at('Ожидает подключения','Waiting'),offline:at('Не в сети','Offline'),error:at('Ошибка Docker / labels','Docker / label error'),disabled:at('Отключён','Disabled'),reconnecting:at('Восстановление связи','Reconnecting')})[status] || status;
function agentStats() {
  const rows=agentsData?.agents || [];
  return `<div class="stats-grid">${[[at('Агенты','Agents'),rows.length,'server'],[at('Туннели онлайн','Tunnels online'),rows.filter(a=>a.tunnel).length,'bolt'],[t('Контейнеры'),rows.reduce((n,a)=>n+a.containers,0),'docker'],[at('Цели из labels','Label targets'),rows.reduce((n,a)=>n+a.routes,0),'routes']].map(([label,count,symbol])=>`<div class="stat-card"><div class="stat-head"><span>${label}</span>${icon(symbol)}</div><strong>${count}</strong></div>`).join('')}</div>`;
}
function agentList() {
  if(!agentsData) return `<p>${t('Подключаемся…')}</p>`;
  if(!agentsData.agents.length) return empty(at('Подключите первую машину','Connect your first machine'),at('Один агент обнаруживает Docker-контейнеры и передаёт трафик через исходящий туннель.','One agent discovers Docker containers and carries traffic over an outbound tunnel.'),btn('agent-create',at('Добавить агента','Add agent'),'plus','primary'));
  return agentsData.agents.map(a=>`<article class="agent-card"><div class="agent-heading"><span class="docker-symbol">${icon('server')}</span><div><h3>${esc(a.name)}</h3><p>${a.last_seen ? at('Последний отчёт: ','Last report: ')+dockerDate(a.last_seen) : at('Запустите агент с выданным токеном','Start the agent with its token')}</p></div>${badge(agentStatus(a.status),a.status==='online'?'green':a.status==='error'||a.status==='offline'?'red':'gray')}</div><div class="agent-facts"><span>${icon('docker')}${a.running} / ${a.containers} ${at('контейнеров запущено','containers running')}</span><span>${icon('routes')}${a.routes} ${at('целей','targets')}</span><span>${icon('clock')}${a.timeout}s ${at('таймаут','timeout')}</span><span>${icon('lock')}${a.tunnel?at('Туннель подключён','Tunnel connected'):at('Туннель отключён','Tunnel disconnected')}</span></div>${a.error?`<p class="error agent-error">${esc(translateError(a.error))}</p>`:''}<div class="agent-actions">${btn('agent-inventory',t('Контейнеры'),'docker','small',`data-id="${a.id}"`)}${btn('agent-edit',at('Настроить','Settings'),'sliders','small',`data-id="${a.id}"`)}${btn('agent-rotate',at('Новый токен','New token'),'lock','ghost small',`data-id="${a.id}"`)}${btn('agent-delete',t('Удалить'),'trash','ghost small',`data-id="${a.id}"`)}</div></article>`).join('');
}
function agentCenterAddress() {
  const saved=generalData?.settings;
  return `<div class="agent-center-bar"><div>${saved?.public_url?`${badge(saved.allow_http?'HTTP':'HTTPS',saved.allow_http?'amber':'green')} <code>${esc(saved.public_url)}</code>`:at('Сначала укажите адрес AmberGate для агентов','First, set the AmberGate address for agents')}</div>${btn('navigate',t('Общие настройки'),'sliders','small','data-page="general"')}</div>`;
}
function agentsPage() {
  return `<div class="agents-page"><div class="agent-intro"><div><span class="eyebrow">OUTBOUND TUNNELS</span><h2>${at('Ваши приложения. На любых машинах.','Your applications. On any machine.')}</h2><p>${at('Агент работает в Docker-сетях приложений. Публиковать их порты и открывать входящий порт агента не нужно.','The agent joins your application networks. No published application ports or inbound agent port required.')}</p></div>${btn('agent-create',at('Добавить агента','Add agent'),'plus','primary')}</div><div id="agent-center-address">${agentCenterAddress()}</div><div id="agent-stats">${agentStats()}</div><div id="agent-list">${agentList()}</div><section class="panel agent-how"><h2>${at('Как это работает','How it works')}</h2><div class="agent-flow"><span>AmberGate + Nginx</span>${icon('routes')}<span id="agent-transport-label">${generalData?.settings.allow_http?'HTTP / WS':'HTTPS / WSS'}</span>${icon('routes')}<span>Agent + docker.sock</span>${icon('routes')}<span>${at('Docker-сети','Docker networks')}</span></div><p>${at('Создайте агента и скопируйте готовую команду Docker run или скачайте Compose. Добавьте labels на контейнеры и выберите режим автоматизации ниже. Одинаковый host + path или group объединяет цели в балансировку.','Create an agent and copy its ready-to-run Docker command or download Compose. Add labels to containers and choose an automation mode below. Matching host + path or group combines targets into one load balancer.')}</p><pre class="label-example">ambergate.route: "host=example.com; path=/api; port=3000"</pre></section>${dockerLabelsPanel()}</div>`;
}
function updateAgents() {
  if(page!=='agents' || !csrf)return;
  const list=$('#agent-list'), stats=$('#agent-stats');
  const focused=list?.contains(document.activeElement) ? {action:document.activeElement.dataset.action,id:document.activeElement.dataset.id} : null;
  if(list)list.innerHTML=agentList(); if(stats)stats.innerHTML=agentStats();
  if(focused)list.querySelector(`[data-action="${focused.action}"][data-id="${focused.id}"]`)?.focus({preventScroll:true});
}
function receiveAgents(data) {
  if(data.agents && !agentsMutating){agentsData=data.agents;updateAgents();agentWizard?.refresh();}
}
async function loadAgentsPage() {
  if(agentsLoading)return;
  agentsLoading=true;const session=csrf;
  try {
    const [data,labels,general]=await Promise.all([api('agents'),api('docker/labels'),api('settings')]);
    if(session!==csrf)return;
    agentsData=data;dockerLabelsData=labels;receiveGeneral({general});updateAgents();updateDockerLabels();
  } catch(error){if(session===csrf)notify(error.message,true);}
  finally{agentsLoading=false;}
}
async function mutateAgent(value) {
  const session=csrf; agentsMutating=true;
  try {
    const data=await api('agents',{revision:agentsData.revision,...value});
    if(session!==csrf)throw new Error(at('Сессия завершена','Session expired'));
    agentsData={revision:data.revision,agents:data.agents};updateAgents();return data;
  } finally{agentsMutating=false;}
}
async function agentForm(id) {
  if(!agentsData)return;
  const session=csrf;
  const general=await api('settings');if(session!==csrf)return;
  receiveGeneral({general});
  if(!id && !general.settings.public_url){page='general';render();notify(at('Укажите адрес AmberGate, затем добавьте агента','Set the AmberGate address, then add an agent'));return;}
  const agent=agentsData?.agents.find(a=>a.id===id), revision=agentsData.revision;
  setModal(agent?at('Настройки агента','Agent settings'):at('Новый агент','New agent'),at('Отдельный токен и туннель для каждой машины.','A separate token and tunnel for each machine.'),`<div class="fields">${field(at('Название машины','Machine name'),'name',agent?.name || '', 'text','required maxlength="80" placeholder="production-eu-1"')}${field(at('Таймаут отключения, секунд','Disconnect timeout, seconds'),'timeout',agent?.timeout || 30,'number','required min="15" max="300"')}${agent?check(at('Агент включён','Agent enabled'),'enabled',agent.enabled):''}</div><p class="hint">${at('После таймаута цели агента исключаются из маршрутов при следующей синхронизации labels (до 5 секунд). Ручные серверы сохраняются.','After this timeout, agent targets are removed on the next label sync (up to 5 seconds). Manual servers are preserved.')}</p>`,async fd=>{
    const result=await mutateAgent({action:agent?'update':'create',revision,id,name:fd.get('name'),timeout:Number(fd.get('timeout')),enabled:agent?fd.has('enabled'):true});
    if(result.token)$('#modal').addEventListener('close',()=>queueMicrotask(()=>agentSetup(result.token,result.agent_id,fd.get('name'),general.settings)),{once:true});
  },agent?t('Сохранить'):at('Создать агента','Create agent'));
  $('.form-actions .hint',$('#modal')).textContent=at('Настройки действуют сразу','Settings take effect immediately');
}
function checkAgentInstall(origin, network, allowHttp) {
  if(!/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$/.test(network))throw new Error(at('Укажите существующую Docker-сеть приложения','Enter an existing application Docker network'));
  const url=new URL(origin);
  if(url.username||url.password||url.pathname!=='/'||url.search||url.hash||!(url.protocol==='https:' || (url.protocol==='http:' && allowHttp===true)))throw new Error(at('Сначала сохраните адрес в общих настройках','Save the address in General settings first'));
}
function agentCompose(token, origin, network, allowHttp=false) {
  checkAgentInstall(origin,network,allowHttp);
  return `services:\n  ambergate-agent:\n    image: ghcr.io/gadmin2151/ambergate-agent:latest\n    restart: unless-stopped\n    init: true\n    read_only: true\n    tmpfs:\n      - /tmp:size=16m,mode=1777\n    environment:\n      AMBERGATE_SERVER_URL: ${JSON.stringify(origin)}\n      AMBERGATE_AGENT_TOKEN: ${JSON.stringify(token)}\n${origin.startsWith('http://')?'      AMBERGATE_AGENT_ALLOW_HTTP: "true"\n':''}    volumes:\n      - type: bind\n        source: /var/run/docker.sock\n        target: /var/run/docker.sock\n        read_only: true\n        bind:\n          create_host_path: false\n    networks:\n      - applications\n    logging:\n      driver: json-file\n      options:\n        max-size: 10m\n        max-file: "3"\nnetworks:\n  applications:\n    external: true\n    name: ${JSON.stringify(network)}\n`;
}
const shellQuote = value => "'" + String(value).replaceAll("'", "'\"'\"'") + "'";
function agentDockerRun(token, origin, network, allowHttp=false) {
  checkAgentInstall(origin,network,allowHttp);
  return `docker run -d --name ambergate-agent --restart unless-stopped --init --read-only --tmpfs /tmp:size=16m,mode=1777 --network ${shellQuote(network)} --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock,readonly -e ${shellQuote('AMBERGATE_SERVER_URL='+origin)} -e ${shellQuote('AMBERGATE_AGENT_TOKEN='+token)}${origin.startsWith('http://')?' -e AMBERGATE_AGENT_ALLOW_HTTP=true':''} --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 ghcr.io/gadmin2151/ambergate-agent:latest`;
}
async function copyAgentCommand(text, source) {
  try {if(navigator.clipboard && window.isSecureContext){await navigator.clipboard.writeText(text);return true;}} catch { /* Fall back on private HTTP panels too. */ }
  const area=document.createElement('textarea');area.value=text;area.className='clipboard-buffer';area.setAttribute('aria-hidden','true');
  $('#modal').appendChild(area);area.select();
  try {if(document.execCommand('copy'))return true;} catch { /* Offer manual selection. */ }
  finally{area.remove();}
  const selection=window.getSelection(), range=document.createRange();range.selectNodeContents(source);selection.removeAllRanges();selection.addRange(range);
  return false;
}
function agentSetup(token, identity, name, connection, rotating=false) {
  const modal=$('#modal');modal.classList.remove('route-modal');modal.classList.add('agent-setup-modal');modal.setAttribute('aria-labelledby','agent-setup-title');
  const origin=connection.public_url, insecure=connection.allow_http===true;
  let format='run';
  modal.innerHTML=`<div class="modal-header"><div><span class="eyebrow">AMBERGATE AGENT</span><h2 id="agent-setup-title">${at('Подключите машину','Connect your machine')}</h2><p class="modal-subtitle">${esc(name)}</p></div>${btn('close','','close','ghost icon',`aria-label="${t('Закрыть')}"`)}</div><div class="modal-body"><ol class="agent-steps"><li class="complete"><span>1</span>${at('Агент создан','Agent created')}</li><li class="active"><span>2</span>${at('Запустите команду','Run the command')}</li><li id="agent-connect-step"><span>3</span>${at('Подключение','Connected')}</li></ol><div class="connection-address ${insecure?'insecure':''}">${icon(insecure?'info':'lock')}<div><strong>${esc(origin)}</strong><p>${insecure?at('HTTP разрешён в общих настройках. Токен и трафик не зашифрованы.','HTTP was allowed in General settings. Token and traffic are unencrypted.'):at('Адрес из общих настроек · HTTPS с проверкой сертификата','From General settings · HTTPS with certificate verification')}</p></div></div><div class="agent-install-network">${field(at('Docker-сеть приложений','Application Docker network'),'network','ambergate-apps','text','id="agent-install-network" required maxlength="128" spellcheck="false"',at('Существующая сеть на машине агента, например my-app_default. Порты публиковать не нужно.','An existing network on the agent machine, for example my-app_default. No ports need publishing.'))}</div>${rotating?`<p class="docker-info">${at('Старый токен отключён. Пересоздайте существующий агент с новой командой или замените Compose-файл и выполните docker compose up -d.','The old token is revoked. Recreate your existing agent with the new command, or replace the Compose file and run docker compose up -d.')}</p>`:''}<div class="agent-install-tabs" role="tablist" aria-label="${at('Способ запуска','Installation method')}"><button type="button" id="agent-tab-run" role="tab" aria-controls="agent-install-panel" aria-selected="true" data-install-format="run">${icon('code')}Docker run</button><button type="button" id="agent-tab-compose" role="tab" aria-controls="agent-install-panel" aria-selected="false" tabindex="-1" data-install-format="compose">${icon('docker')}Docker Compose</button></div><div id="agent-install-panel" role="tabpanel" aria-labelledby="agent-tab-run"><div class="agent-command-head"><span id="agent-install-caption"></span><button type="button" id="agent-copy-command" class="small">${icon('copy')}<span>${at('Копировать команду','Copy command')}</span></button></div><pre id="agent-install-code" tabindex="0"></pre><div id="agent-compose-launch" hidden><p>${at('Скачайте файл и выполните в его каталоге:','Download the file, then run in its directory:')}</p><code>docker compose -f compose.agent.yaml up -d</code><button type="button" id="agent-download-compose" class="primary">${icon('download')}${at('Скачать Compose','Download Compose')}</button></div></div><div id="agent-setup-error" class="error" role="alert"></div><p class="hint">${at('Команда и файл содержат персональный токен. Он доступен только в этом окне — сохраните нужный вариант перед закрытием.','The command and file contain a personal token. It is available only in this window — save your preferred option before closing.')}</p><div id="agent-install-status" class="agent-install-status" role="status" aria-live="polite"></div></div><div class="form-actions"><span>${at('Подключение появится автоматически','Connection appears automatically')}</span>${btn('close',at('Готово','Done'),'check','primary')}</div>`;
  const renderCode=()=>{
    const input=$('#agent-install-network'), code=$('#agent-install-code');
    let value='';
    try {
      value=format==='run'?agentDockerRun(token,origin,input.value.trim(),insecure):agentCompose(token,origin,input.value.trim(),insecure);
      $('#agent-setup-error').textContent='';
    } catch(error){$('#agent-setup-error').textContent=error.message;}
    code.textContent=value;$('#agent-copy-command').disabled=!value;$('#agent-download-compose').disabled=!value;
    $('#agent-copy-command span').textContent=format==='run'?at('Копировать команду','Copy command'):at('Копировать YAML','Copy YAML');
    $('#agent-install-caption').textContent=format==='run'?at('Одна команда на машине агента','One command on the agent machine'):'compose.agent.yaml';
    $('#agent-compose-launch').hidden=format!=='compose';
  };
  const selectFormat=value=>{
    format=value;
    modal.querySelectorAll('[data-install-format]').forEach(button=>{button.setAttribute('aria-selected',String(button.dataset.installFormat===format));button.tabIndex=button.dataset.installFormat===format?0:-1;});
    $('#agent-install-panel').setAttribute('aria-labelledby','agent-tab-'+format);renderCode();
  };
  modal.querySelectorAll('[data-install-format]').forEach(button=>{
    button.addEventListener('click',()=>selectFormat(button.dataset.installFormat));
    button.addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();selectFormat(event.key==='Home'?'run':event.key==='End'?'compose':format==='run'?'compose':'run');$('#agent-tab-'+format).focus();}});
  });
  $('#agent-install-network').addEventListener('input',renderCode);
  $('#agent-copy-command').addEventListener('click',async()=>{
    const node=$('#agent-install-code'), copied=await copyAgentCommand(node.textContent,node);
    if(modal.open && $('#agent-copy-command span'))$('#agent-copy-command span').textContent=copied?at('Скопировано','Copied'):at('Выделено — нажмите Ctrl+C','Selected — press Ctrl+C');
  });
  $('#agent-download-compose').addEventListener('click',()=>{
    download('compose.agent.yaml',agentCompose(token,origin,$('#agent-install-network').value.trim(),insecure),'application/yaml');
  });
  agentWizard={identity,refresh:()=>{
    if(!modal.open)return;
    const a=agentsData?.agents.find(a=>a.id===identity), connected=a?.status==='online';
    const target=$('#agent-install-status');if(!target)return;
    target.classList.toggle('connected',connected);
    const title=connected?at('Агент подключён','Agent connected'):a?.error?at('Туннель подключён, проверьте настройки','Tunnel connected — check settings'):at('Ожидаем подключения агента…','Waiting for your agent…');
    const detail=connected?`${a.containers} ${at('контейнеров обнаружено. Данные обновляются через SSE.','containers discovered. Updates arrive through SSE.')}`:a?.error?translateError(a.error):at('Запустите команду на машине с Docker. Статус обновится сам.','Run the command on your Docker machine. This status updates automatically.');
    const html=`${icon(connected?'check':'clock')}<div><strong>${title}</strong><p>${esc(detail)}</p></div>`;
    if(target.innerHTML!==html)target.innerHTML=html;
    $('#agent-connect-step').classList.toggle('complete',connected);
  }};
  modal.addEventListener('close',()=>{token='';agentWizard=null;modal.classList.remove('agent-setup-modal');modal.innerHTML='';},{once:true});
  renderCode();modal.showModal();agentWizard.refresh();
}
async function agentInventory(id) {
  const session=csrf, data=await api('agents/'+id);
  if(session!==csrf)return;
  const a=data.agents[0];
  setModal(a.name,at('Контейнеры и маршрутные labels последнего отчёта','Containers and route labels from the latest report'),`<div class="agent-inventory">${a.inventory.length?a.inventory.map(c=>`<article><h3>${esc(c.name)} ${badge(c.state,c.state==='running'?'green':'gray')}</h3><p>${esc(c.image)}</p><p>${at('Общие сети: ','Shared networks: ')}${esc(c.shared_networks.join(', ')||'—')}</p>${Object.entries(c.route_labels).map(([key,value])=>`<pre>${esc(key)}: ${esc(value)}</pre>`).join('')}</article>`).join(''):`<p>${at('Отчётов пока нет','No reports yet')}</p>`}</div>`,async()=>{},t('Закрыть'));
  $('.form-actions .hint',$('#modal')).textContent=at('Только чтение','Read only');
}
async function agentAction(action,id) {
  if(agentsMutating)return;
  if(action==='agent-create')return agentForm();
  if(action==='agent-edit')return agentForm(id);
  if(action==='agent-inventory')return agentInventory(id);
  const a=agentsData?.agents.find(a=>a.id===id);if(!a)return;
  const deleting=action==='agent-delete', revision=agentsData.revision;
  let connection;
  if(!deleting){const session=csrf, general=await api('settings');if(session!==csrf)return;receiveGeneral({general});connection=general.settings;if(!connection.public_url){page='general';render();notify(at('Сначала сохраните адрес AmberGate','Save the AmberGate address first'));return;}}
  // Reuse the accessible confirmation dialog, with immediate-action copy.
  const confirmation=confirmAction(deleting?at('Удалить агента?','Delete agent?'):at('Заменить токен агента?','Replace agent token?'),at('Туннель будет отключён сразу. Автоматизация labels обновит маршруты при следующей синхронизации.','The tunnel disconnects immediately. Label automation updates routes on the next sync.'),a.name,deleting?t('Удалить'):at('Заменить токен','Replace token'),deleting);
  $('.confirm-hint',$('#confirm-modal')).textContent=deleting?at('Для повторного подключения создайте нового агента.','Create a new agent to reconnect this machine.'):at('Старый токен перестанет работать. Обновите Compose на машине агента.','The old token stops working. Update Compose on the agent machine.');
  if(await confirmation){const result=await mutateAgent({action:deleting?'delete':'rotate',id,revision});if(result.token)agentSetup(result.token,result.agent_id,a.name,connection,true);}
}
