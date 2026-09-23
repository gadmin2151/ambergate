'use strict';
let agentsData = null, agentsLoading = false, agentsMutating = false;
const at = (ru, en) => language === 'ru' ? ru : en;
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
function agentsPage() {
  return `<div class="agents-page"><div class="agent-intro"><div><span class="eyebrow">OUTBOUND TUNNELS</span><h2>${at('Ваши приложения. На любых машинах.','Your applications. On any machine.')}</h2><p>${at('Агент работает в Docker-сетях приложений. Публиковать их порты и открывать входящий порт агента не нужно.','The agent joins your application networks. No published application ports or inbound agent port required.')}</p></div>${btn('agent-create',at('Добавить агента','Add agent'),'plus','primary')}</div><div id="agent-stats">${agentStats()}</div><div id="agent-list">${agentList()}</div><section class="panel agent-how"><h2>${at('Как это работает','How it works')}</h2><div class="agent-flow"><span>AmberGate + Nginx</span>${icon('routes')}<span>HTTPS / WSS</span>${icon('routes')}<span>Agent + docker.sock</span>${icon('routes')}<span>${at('Docker-сети','Docker networks')}</span></div><p>${at('Создайте агента, скачайте Compose и подключите его к сети приложений. Добавьте labels на контейнеры и выберите режим автоматизации ниже. Одинаковый host + path или group объединяет цели в балансировку.','Create an agent, download its Compose file and join your application network. Add labels to containers and choose an automation mode below. Matching host + path or group combines targets into one load balancer.')}</p><pre class="label-example">ambergate.route: "host=example.com; path=/api; port=3000"</pre></section>${dockerLabelsPanel()}</div>`;
}
function updateAgents() {
  if(page!=='agents' || !csrf)return;
  const list=$('#agent-list'), stats=$('#agent-stats');
  const focused=list?.contains(document.activeElement) ? {action:document.activeElement.dataset.action,id:document.activeElement.dataset.id} : null;
  if(list)list.innerHTML=agentList(); if(stats)stats.innerHTML=agentStats();
  if(focused)list.querySelector(`[data-action="${focused.action}"][data-id="${focused.id}"]`)?.focus({preventScroll:true});
}
function receiveAgents(data) {
  if(data.agents && !agentsMutating){agentsData=data.agents;updateAgents();}
}
async function loadAgentsPage() {
  if(agentsLoading)return;
  agentsLoading=true;const session=csrf;
  try {
    const [data,labels]=await Promise.all([api('agents'),api('docker/labels')]);
    if(session!==csrf)return;
    agentsData=data;dockerLabelsData=labels;updateAgents();updateDockerLabels();
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
function agentForm(id) {
  if(!agentsData)return;
  const agent=agentsData?.agents.find(a=>a.id===id), revision=agentsData.revision;
  setModal(agent?at('Настройки агента','Agent settings'):at('Новый агент','New agent'),at('Отдельный токен и туннель для каждой машины.','A separate token and tunnel for each machine.'),`<div class="fields">${field(at('Название машины','Machine name'),'name',agent?.name || '', 'text','required maxlength="80" placeholder="production-eu-1"')}${field(at('Таймаут отключения, секунд','Disconnect timeout, seconds'),'timeout',agent?.timeout || 30,'number','required min="15" max="300"')}${agent?check(at('Агент включён','Agent enabled'),'enabled',agent.enabled):''}</div><p class="hint">${at('После таймаута цели агента исключаются из маршрутов при следующей синхронизации labels (до 5 секунд). Ручные серверы сохраняются.','After this timeout, agent targets are removed on the next label sync (up to 5 seconds). Manual servers are preserved.')}</p>`,async fd=>{
    const result=await mutateAgent({action:agent?'update':'create',revision,id,name:fd.get('name'),timeout:Number(fd.get('timeout')),enabled:agent?fd.has('enabled'):true});
    if(result.token)$('#modal').addEventListener('close',()=>queueMicrotask(()=>agentSetup(result.token)),{once:true});
  },agent?t('Сохранить'):at('Создать агента','Create agent'));
  $('.form-actions .hint',$('#modal')).textContent=at('Настройки действуют сразу','Settings take effect immediately');
}
function agentCompose(token, origin, network) {
  return `services:\n  ambergate-agent:\n    image: ghcr.io/gadmin2151/ambergate-agent:latest\n    restart: unless-stopped\n    init: true\n    read_only: true\n    tmpfs:\n      - /tmp:size=16m,mode=1777\n    environment:\n      AMBERGATE_SERVER_URL: ${JSON.stringify(origin)}\n      AMBERGATE_AGENT_TOKEN: ${JSON.stringify(token)}\n    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock:ro\n    networks:\n      - applications\n    logging:\n      driver: json-file\n      options:\n        max-size: 10m\n        max-file: "3"\nnetworks:\n  applications:\n    external: true\n    name: ${JSON.stringify(network)}\n`;
}
function agentSetup(token) {
  const modal=$('#modal');modal.classList.remove('route-modal');modal.setAttribute('aria-labelledby','agent-setup-title');
  const origin=location.protocol==='https:' ? location.origin : '';
  modal.innerHTML=`<form id="agent-setup"><div class="modal-header"><div><span class="eyebrow">AMBERGATE AGENT</span><h2 id="agent-setup-title">${at('Подключите Docker-машину','Connect a Docker machine')}</h2></div>${btn('close','','close','ghost icon',`aria-label="${t('Закрыть')}"`)}</div><div class="modal-body"><p>${at('Токен показывается только сейчас. Скачайте Compose и храните его как секрет.','The token is shown only now. Download the Compose file and keep it secret.')}</p>${field(at('HTTPS-адрес центрального AmberGate','Central AmberGate HTTPS URL'),'origin',origin,'url','required placeholder="https://gateway.example.com"')}${field(at('Существующая Docker-сеть приложений','Existing application Docker network'),'network','ambergate-apps','text','required maxlength="128" pattern="[a-zA-Z0-9][a-zA-Z0-9_.-]*"')}<label>${at('Токен агента','Agent token')}<input readonly class="agent-token" value="${esc(token)}" aria-label="${at('Токен агента','Agent token')}" autocomplete="off"></label><p class="hint">${at('HTTPS и WebSocket обслуживает ваш внешний TLS-прокси. На агенте порты не публикуются.','Your external TLS proxy handles HTTPS and WebSocket. The agent publishes no ports.')}</p><pre>chmod 600 compose.agent.yaml\ndocker compose -f compose.agent.yaml up -d</pre><div id="agent-setup-error" class="error" role="alert"></div></div><div class="form-actions">${btn('close',t('Закрыть'),'','ghost')}<button type="submit" class="primary">${icon('download')}${at('Скачать Compose','Download Compose')}</button></div></form>`;
  modal.addEventListener('close',()=>{token='';modal.innerHTML='';},{once:true});
  $('#agent-setup').addEventListener('submit',event=>{
    event.preventDefault();const fd=new FormData(event.target);
    try {
      const url=new URL(fd.get('origin'));
      if(url.protocol!=='https:'||url.username||url.password||url.pathname!=='/'||url.search||url.hash)throw new Error(at('Нужен HTTPS-адрес без пути и пароля','Enter an HTTPS origin without a path or credentials'));
      download('compose.agent.yaml',agentCompose(token,url.origin,fd.get('network')),'application/yaml');
    } catch(error){$('#agent-setup-error').textContent=error.message;}
  });
  modal.showModal();
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
  // Reuse the accessible confirmation dialog, with immediate-action copy.
  const confirmation=confirmAction(deleting?at('Удалить агента?','Delete agent?'):at('Заменить токен агента?','Replace agent token?'),at('Туннель будет отключён сразу. Автоматизация labels обновит маршруты при следующей синхронизации.','The tunnel disconnects immediately. Label automation updates routes on the next sync.'),a.name,deleting?t('Удалить'):at('Заменить токен','Replace token'),deleting);
  $('.confirm-hint',$('#confirm-modal')).textContent=deleting?at('Для повторного подключения создайте нового агента.','Create a new agent to reconnect this machine.'):at('Старый токен перестанет работать. Обновите Compose на машине агента.','The old token stops working. Update Compose on the agent machine.');
  if(await confirmation){const result=await mutateAgent({action:deleting?'delete':'rotate',id,revision});if(result.token)agentSetup(result.token);}
}
