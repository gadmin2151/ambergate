'use strict';
let generalData = null, generalDraft = null, generalLoading = false, generalBusy = false;
function generalPage() {
  const value=generalDraft ?? generalData?.settings.public_url ?? '';
  return `<div class="general-page"><section class="panel general-card"><div class="docker-connect-title"><span class="docker-symbol">${icon('globe')}</span><div><h2>${at('Адрес вашего AmberGate','Your AmberGate address')}</h2><p>${at('Укажите один раз. Панель подставит адрес во все команды подключения агентов.','Set it once. The panel adds this address to every agent installation command.')}</p></div></div><form id="general-form"><label>${at('IP-адрес или домен AmberGate','AmberGate IP address or domain')}<input id="general-address" name="public_url" type="text" maxlength="512" autocomplete="off" spellcheck="false" placeholder="ambergate.exemple.com" value="${esc(value)}" ${generalData?'':'disabled'}></label><p class="hint">${at('Домен → HTTPS. Просто IP → HTTP на порту 8083. Можно указать IP:порт или полный адрес.','Domain → HTTPS. A plain IP → HTTP on port 8083. You can also enter IP:port or a full address.')}</p><div id="general-transport">${generalTransport()}</div><div id="general-error" class="error" role="alert"></div><div class="general-actions"><span class="hint">${at('Сохраняется локально и действует для новых команд запуска.','Stored locally and used for new installation commands.')}</span><button type="submit" class="primary" ${!generalData||generalBusy?'disabled':''}>${icon('save')}${at('Сохранить адрес','Save address')}</button></div></form></section><section class="panel general-card"><h2>${at('Подключение агентов','Agent connections')}</h2><div class="general-examples"><div><strong>HTTPS</strong><code>ambergate.exemple.com</code><p>${at('Ваш TLS-прокси направляет запросы и WebSocket на панель AmberGate:8083. Сертификат проверяется агентом.','Your TLS proxy forwards requests and WebSocket to the AmberGate panel on port 8083. The agent verifies its certificate.')}</p></div><div><strong>HTTP · IP</strong><code>10.0.0.10:8083</code><p>${at('Без TLS-прокси. Токены и трафик не зашифрованы. Доступно только после подтверждения риска.','Without a TLS proxy. Tokens and traffic are unencrypted. Available only after explicit risk confirmation.')}</p></div></div><p class="hint">${at('Изменение адреса не перенастраивает уже запущенные агенты. Обновите их команду или Compose на соответствующих машинах.','Changing the address does not reconfigure existing agents. Update their command or Compose file on the relevant machines.')}</p>${btn('navigate',at('К агентам','Go to agents'),'arrow','small','data-page="agents"')}</section></div>`;
}
function generalTransport() {
  const settings=generalData?.settings;
  if(!settings?.public_url)return `<p class="hint">${at('Адрес ещё не настроен.','No address configured yet.')}</p>`;
  return `<div class="connection-address ${settings.allow_http?'insecure':''}">${icon(settings.allow_http?'info':'lock')}<div><strong>${esc(settings.public_url)}</strong><p>${settings.allow_http?at('HTTP разрешён вами. Токены и трафик передаются без шифрования.','You allowed HTTP. Tokens and traffic are sent without encryption.'):at('HTTPS · проверка сертификата включена','HTTPS · certificate verification enabled')}</p></div></div>`;
}
function receiveGeneral(data) {
  if(!data.general || generalBusy)return;
  generalData=data.general;
  if(page==='general'){
    const input=$('#general-address');if(input){input.disabled=false;if(generalDraft===null)input.value=generalData.settings.public_url;}
    const output=$('#general-transport');if(output)output.innerHTML=generalTransport();
    const button=$('#general-form button[type=submit]');if(button)button.disabled=false;
  }
  const address=$('#agent-center-address');if(address)address.innerHTML=agentCenterAddress();
  const transport=$('#agent-transport-label');if(transport)transport.textContent=generalData.settings.allow_http?'HTTP / WS':'HTTPS / WSS';
}
async function loadGeneralPage() {
  if(generalLoading)return;
  generalLoading=true;const session=csrf;
  try {const data=await api('settings');if(csrf===session)receiveGeneral({general:data});}
  catch(error){if(session===csrf)notify(error.message,true);}
  finally{generalLoading=false;}
}
function bindGeneralPage() {
  $('#general-address').addEventListener('input',event=>{generalDraft=event.target.value;});
  $('#general-form').addEventListener('submit',async event=>{
    event.preventDefault();if(generalBusy||!generalData)return;
    const session=csrf, form=event.target, value=$('#general-address').value, revision=generalData.revision;
    generalBusy=true;form.querySelector('button[type=submit]').disabled=true;$('#general-error').textContent='';
    const save=confirm_http=>api('settings',{settings:{public_url:value},revision,confirm_http});
    try {
      let data;
      try {data=await save(false);} catch(error) {
        if(error.code!=='http_confirmation_required')throw error;
        if(csrf!==session)return;
        const confirmation=confirmAction(at('Разрешить небезопасное HTTP-подключение?','Allow an insecure HTTP connection?'),at('Токен агента и данные приложений будут передаваться без шифрования. Их могут прочитать или изменить участники сети.','The agent token and application data will travel without encryption. Others on the network could read or alter them.'),error.details.public_url,at('Понимаю риск. Разрешить HTTP','I understand. Allow HTTP'));
        $('.confirm-hint',$('#confirm-modal')).textContent=at('Разрешение будет сохранено только для этого адреса. Для другого HTTP-адреса потребуется новое подтверждение.','Permission is saved for this address only. A different HTTP address requires another confirmation.');
        if(!await confirmation || csrf!==session)return;
        data=await save(true);
      }
      if(csrf!==session)return;
      generalData=data;generalDraft=null;
      if($('#general-address'))$('#general-address').value=data.settings.public_url;
      notify(at('Адрес AmberGate сохранён','AmberGate address saved'));
    } catch(error){if(csrf===session){const out=$('#general-error');if(out)out.textContent=translateError(error.message);else notify(error.message,true);}}
    finally{generalBusy=false;if(csrf===session)receiveGeneral({general:generalData});}
  });
  loadGeneralPage();
}
