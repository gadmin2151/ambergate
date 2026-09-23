'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const i18n = fs.readFileSync('ambergate/static/i18n.js','utf8');
function fixture(saved='en', blocked=false) {
  const storage = new Map(saved ? [['ambergate.language',saved]] : []);
  const context = vm.createContext({Intl, Date, navigator:{language:'en-US'},
    localStorage:{getItem:key=>{if(blocked)throw new Error('blocked');return storage.get(key);}, setItem:(key,value)=>{if(blocked)throw new Error('blocked');storage.set(key,value);}},
    document:{documentElement:{}, addEventListener(){}, querySelector(){return null;}},window:{addEventListener(){}},
    crypto:require('node:crypto').webcrypto, structuredClone, setTimeout, clearTimeout});
  vm.runInContext(i18n,context);
  return {context,storage,run:code=>vm.runInContext(code,context)};
}
test('locale is restored, validated and persisted without requiring storage',()=>{
  const f=fixture('ru');assert.equal(f.run('locale()'),'ru-RU');
  f.run("setLanguage('en')");assert.equal(f.storage.get('ambergate.language'),'en');assert.equal(f.context.document.documentElement.lang,'en');
  f.run("setLanguage('<script>')");assert.equal(f.run('language'),'en');
  assert.equal(fixture('invalid').run('language'),'en');
  const blocked=fixture(null,true);assert.doesNotThrow(()=>blocked.run("setLanguage('en')"));assert.equal(blocked.run("t('Применить')"),'Apply');
});
test('authored template text is translated while user data and escaping stay intact',()=>{
  const f=fixture();
  assert.equal(f.run('ui`<label title="Удалить маршрут">Маршрут «${"Маршруты <api>"}» будет удалён.</label>`'),'<label title="Delete route">Route “Маршруты <api>” will be deleted.</label>');
  assert.equal(f.run("t('  Сохранить IP  ')"),'  Save IP  ');
  assert.equal(f.run("translateError('hosts[0]: ожидается целое число 1–32')"),'hosts[0]: expected an integer in range 1–32');
  assert.equal(f.run("translateError('Неверный пароль')"),'Incorrect password');
  assert.equal(f.run("translateError('nginx: upstream example.com:8000')"),'nginx: upstream example.com:8000');
  f.run("setLanguage('ru')");
  assert.equal(f.run("translateError('backend-1 / ambergate.route: port is required')"),'backend-1 / ambergate.route: Параметр port обязателен');
});
function panelFixture() {
  const f=fixture();
  for(const name of ['dashboard','docker','app']){
    let code=fs.readFileSync(`ambergate/static/${name}.js`,'utf8');
    if(name==='app')code=code.slice(0,code.lastIndexOf('(async()=>'));
    f.run(code);
  }
  f.run(`config={settings:{rate_rps:20,rate_burst:40,connections_per_ip:50,body_mb:100,cache_mb:1024,trusted_proxies:[],resolvers:['127.0.0.11'],block_dotfiles:true,security_headers:true},hosts:[]};state={config,history:[],pending:false};csrf='test';`);
  return f;
}
test('pages, route settings and Docker diagnostics render in English',()=>{
  const f=panelFixture();
  for(const expression of ['routesPage()','settingsPage()','cachePage()','historyPage()','dockerPage()','dashboardContent()'])assert.doesNotMatch(f.run(expression),/[А-Яа-яЁё]/,expression);
  f.run("config.hosts.push({id:'host',domain:'example.com',enabled:true,routes:[newRoute('/api','API','backend',8000)]});");
  for(const expression of ['routesPage()','cachePage()'])assert.doesNotMatch(f.run(expression),/[А-Яа-яЁё]/,expression);
  f.context.document.querySelector=()=>({dataset:{},addEventListener(){}});
  f.run("setModal=(...args)=>globalThis.modalText=args.slice(0,3).join(' '); updatePathPreview=()=>{}; editRoute('host',config.hosts[0].routes[0].id);");
  assert.doesNotMatch(f.run('modalText'),/[А-Яа-яЁё]/);
  assert.doesNotThrow(()=>f.run('editHost()'));
  assert.doesNotMatch(f.run('modalText'),/[А-Яа-яЁё]/);
  f.run("dockerLabelsData={mode:'preview',errors:[],warnings:[],changes:[{host:'example.com',path:'/api',action:'create',targets:[]}],checked_at:1,token:'preview'};");
  assert.doesNotMatch(f.run('dockerLabelsResults()'),/[А-Яа-яЁё]/);
  f.run("dockerData={settings:{enabled:true},connected:false,message:'Socket не найден. Смонтируйте docker.sock и проверьте путь.'};");
  assert.doesNotMatch(f.run('dockerInventory()'),/[А-Яа-яЁё]/);

});
test('English plural and number formatting differ from Russian',()=>{
  const f=panelFixture();
  assert.equal(f.run("plural(21,t('маршрут'),t('маршрута'),t('маршрутов'))"),'routes');
  assert.equal(f.run('dashNumber(1234.5)'),'1,234.5');
  f.run("setLanguage('ru')");assert.equal(f.run("plural(21,t('маршрут'),t('маршрута'),t('маршрутов'))"),'маршрут');
  assert.match(f.run('dashNumber(1234.5)'),/234,5$/);
});

test('old language preference survives the AmberGate rename',()=>{
  const f=fixture(null); f.storage.set('gateway.language','ru');
  const context=vm.createContext({document:{documentElement:{}},navigator:{language:'en-US'},localStorage:{getItem:key=>f.storage.get(key),setItem:(key,value)=>f.storage.set(key,value)}});
  vm.runInContext(i18n,context);
  assert.equal(vm.runInContext('language',context),'ru');
  vm.runInContext("setLanguage('en')",context);
  assert.equal(f.storage.get('ambergate.language'),'en');
});
