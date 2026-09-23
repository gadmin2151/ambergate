'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const code = fs.readFileSync('ambergate/static/i18n.js', 'utf8') + '\n' + fs.readFileSync('ambergate/static/dashboard.js', 'utf8');
function fixture() {
  const sources = [], timers = new Map(), listeners = {};
  let timerId = 0;
  class EventSource {
    static CLOSED = 2;
    constructor(url) { this.url = url; this.handlers = {}; this.readyState = 0; sources.push(this); }
    addEventListener(name, handler) { this.handlers[name] = handler; }
    close() { this.readyState = 2; }
    emit(name, data) { this.handlers[name]?.({data:JSON.stringify(data)}); }
  }
  const context = vm.createContext({EventSource, AbortSignal, Date, Intl,
    document:{documentElement:{}, hidden:false, addEventListener:(name, fn) => listeners[name] = fn},
    window:{addEventListener:(name, fn) => listeners[name] = fn},
    setTimeout:(fn, delay) => { timers.set(++timerId, {fn, delay}); return timerId; },
    clearTimeout:id => timers.delete(id),
  });
  vm.runInContext("let csrf='session', page='dashboard', health=null; const $=()=>null; let logins=0, requests=0; async function api(){requests++;} function showLogin(){stopDashboardStream(); logins++;}", context);
  vm.runInContext(code, context);
  return {sources, timers, listeners, run:js => vm.runInContext(js, context), context,
    fire(delay) { const item=[...timers].find(([,t])=>t.delay===delay); assert.ok(item); timers.delete(item[0]); return item[1].fn(); }};
}
const snapshot = count => ({nginx:{healthy:true}, summary:{requests:count}, configuration:{}});

test('one stream updates data across navigation, without HTTP polling', () => {
  const f=fixture(); f.run('startDashboardStream(); startDashboardStream();');
  assert.equal(f.sources.length,1);
  assert.equal(f.sources[0].url,'/api/events');
  f.sources[0].emit('dashboard',snapshot(4));
  assert.equal(f.run('dashboardData.summary.requests'),4);
  f.run("page='routes'; startDashboardStream();");
  f.sources[0].emit('dashboard',snapshot(5));
  assert.equal(f.run('health.healthy'),true);
  assert.equal(f.run('dashboardData.summary.requests'),5);
  assert.equal(f.sources.length,1);
  assert.equal(f.run('requests'),0);
});
test('disconnect retries; data from an obsolete connection is ignored', async () => {
  const f=fixture(); f.run('startDashboardStream()');
  const old=f.sources[0]; old.emit('dashboard',snapshot(1));
  await old.onerror();
  assert.equal(old.readyState,2);
  assert.equal(f.run('dashboardStreamState'),'reconnecting');
  f.fire(1000);
  f.sources[1].emit('dashboard',snapshot(2)); old.emit('dashboard',snapshot(999));
  assert.equal(f.run('dashboardData.summary.requests'),2);
  assert.equal(f.run('dashboardStreamState'),'live');
});
test('stalled stream is detected and reconnected', async () => {
  const f=fixture(); f.run('startDashboardStream()');
  f.sources[0].emit('dashboard',snapshot(1));
  await f.fire(15000);
  assert.equal(f.sources[0].readyState,2);
  assert.equal(f.run('dashboardStreamState'),'reconnecting');
  f.fire(1000); assert.equal(f.sources.length,2);
});
test('hidden pages, logout and invalid sessions release the connection', () => {
  const f=fixture(); f.run('startDashboardStream()');
  f.context.document.hidden=true; f.listeners.visibilitychange();
  assert.equal(f.sources[0].readyState,2); assert.equal(f.timers.size,0);
  f.context.document.hidden=false; f.listeners.visibilitychange();
  assert.equal(f.sources.length,2);
  f.sources[1].emit('auth_required',{});
  assert.equal(f.run('csrf'),''); assert.equal(f.run('logins'),1);
  assert.equal(f.sources[1].readyState,2); assert.equal(f.timers.size,0);
});
test('closed HTTP stream checks session; expired login cancels retries', async () => {
  const f=fixture(); f.run("api=async()=>{csrf='';showLogin();throw new Error('expired');}; startDashboardStream();");
  f.sources[0].readyState=2;
  await f.sources[0].onerror();
  assert.equal(f.run('logins'),1); assert.equal(f.timers.size,0);
});
