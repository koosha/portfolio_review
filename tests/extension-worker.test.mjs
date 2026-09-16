import test from 'node:test';
import assert from 'node:assert/strict';

test('worker pairs, discovers and delivers a holdings table without using downloads', async () => {
  const listeners = {};
  const hook = name => ({addListener: fn => { listeners[name]=fn; }});
  const local = {server:'http://127.0.0.1:8765',key:'secret-pairing-key'}, session = {};
  const storage = state => ({
    get: async keys => Object.fromEntries((typeof keys==='string'?[keys]:keys).map(key=>[key,state[key]])),
    set: async values => Object.assign(state,structuredClone(values))
  });
  const overview='https://finance.yahoo.com/portfolios/', portfolio='https://finance.yahoo.com/portfolio/p_1';
  let currentTab={id:1,status:'complete',url:overview};
  globalThis.chrome = {
    storage:{local:storage(local),session:storage(session)},
    alarms:{create:async()=>{},onAlarm:hook('alarm')},
    runtime:{id:'a'.repeat(32),onInstalled:hook('install'),onStartup:hook('startup'),onMessage:hook('message')},
    tabs:{
      create:async options => (currentTab={...currentTab,...options}),
      update:async (id,options) => (currentTab={...currentTab,...options}),
      get:async()=>({...currentTab})
    },
    scripting:{executeScript:async ({args}) => {
      if(args[0]==='discover') return [{result:{links:[{href:portfolio,text:'IRA'}]}}];
      assert.equal(args[0],portfolio);
      return [{result:{url:portfolio+'/view',table:{method:'yahoo-holdings-table-v1',headers:['Symbol','Shares'],rows:[['DEMO','1.25']],page_count:1,expected_count:1,completeness:'count-verified'}}}];
    }}
  };
  const queue=[
    {id:'a'.repeat(32),action:'connect',url:overview,name:'Yahoo Finance'},
    {id:'b'.repeat(32),action:'discover',url:overview,name:'Yahoo Finance'},
    {id:'c'.repeat(32),action:'refresh',url:portfolio,name:'IRA',source_id:1,collection_method:'holdings-table-v1'}
  ];
  const results=[];
  globalThis.fetch=async (url, options) => {
    assert.equal(options.headers['X-Companion-Key'],local.key);
    if(url.endsWith('/job')) return {ok:true,json:async()=>({job:queue.shift()||null})};
    results.push(JSON.parse(options.body));
    return {ok:true,json:async()=>({ok:true})};
  };
  await import('../chrome-extension/worker.js');
  await new Promise(resolve => listeners.message({action:'check'},{id:chrome.runtime.id},resolve));
  assert.deepEqual(results.map(r=>r.id),['a'.repeat(32),'b'.repeat(32),'c'.repeat(32)]);
  assert.equal(results[1].links[0].href,portfolio);
  assert.equal(results[2].url,portfolio+'/view');
  assert.deepEqual(results[2].table.rows,[['DEMO','1.25']]);
  assert.equal(results[2].filename,undefined);
  assert.equal(session.active,null);
});

test('a restarted app cannot leave an old extension result stuck forever', async () => {
  const listeners={};
  const hook=name=>({addListener:fn=>listeners[name]=fn});
  const local={server:'http://127.0.0.1:8765',key:'new-key'};
  const session={active:{job:{id:'d'.repeat(32)},started:Date.now(),result:{ok:true}}};
  const storage=state=>({get:async keys=>Object.fromEntries((typeof keys==='string'?[keys]:keys).map(key=>[key,state[key]])),set:async values=>Object.assign(state,structuredClone(values))});
  globalThis.chrome={
    storage:{local:storage(local),session:storage(session)},
    alarms:{create:async()=>{},onAlarm:hook('alarm')},
    runtime:{id:'a'.repeat(32),onInstalled:hook('install'),onStartup:hook('startup'),onMessage:hook('message')},
  };
  let deliveries=0;
  globalThis.fetch=async url=>url.endsWith('/result')
    ? (deliveries++,{ok:false,status:400,json:async()=>({error:'Unknown or expired Chrome job.'})})
    : {ok:true,json:async()=>({job:null})};
  await import('../chrome-extension/worker.js?restart-test');
  await new Promise(resolve=>listeners.message({action:'check'},{id:chrome.runtime.id},resolve));
  assert.equal(deliveries,1);
  assert.equal(session.active,null);
});

async function captureWorkerCase(scenario, injectionResults) {
  const listeners={},local={server:'http://127.0.0.1:8765',key:'test-key'},session={};
  const storage=state=>({get:async keys=>Object.fromEntries((typeof keys==='string'?[keys]:keys).map(k=>[k,state[k]])),set:async values=>Object.assign(state,structuredClone(values))});
  const hook=name=>({addListener:fn=>listeners[name]=fn});
  const portfolio='https://finance.yahoo.com/portfolio/p_fixture';
  let tab={id:1,url:portfolio+'/view',status:'complete'},calls=0;
  globalThis.chrome={
    storage:{local:storage(local),session:storage(session)},
    alarms:{create:async()=>{},onAlarm:hook('alarm')},
    runtime:{id:'a'.repeat(32),onInstalled:hook('install'),onStartup:hook('startup'),onMessage:hook('message')},
    tabs:{create:async options=>(tab={...tab,...options}),update:async(id,options)=>(tab={...tab,...options}),get:async()=>tab},
    scripting:{executeScript:async()=>[injectionResults[Math.min(calls++,injectionResults.length-1)]]}
  };
  const queue=[{id:'f'.repeat(32),action:'refresh',url:portfolio,source_id:1,name:'Fixture',collection_method:'holdings-table-v1'}],results=[],versions=[];
  globalThis.fetch=async(url,options)=>{
    versions.push(options.headers['X-Companion-Version']);
    if(url.endsWith('/job')) return {ok:true,json:async()=>({job:queue.shift() || null})};
    results.push(JSON.parse(options.body));return {ok:true,json:async()=>({ok:true})};
  };
  await import('../chrome-extension/worker.js?'+scenario);
  await new Promise(resolve=>listeners.message({action:'check'},{id:chrome.runtime.id},resolve));
  return {results,calls,versions};
}

test('an empty injected result is retried once and never reported as an account mismatch',async()=>{
  const {results,calls}=await captureWorkerCase('empty-capture',[{result:null}]);
  assert.equal(calls,2);
  assert.equal(results[0].ok,false);
  assert.match(results[0].error,/no holdings capture result after two attempts/);
  assert.equal(results[0].table,undefined);
});

test('page collection errors survive the Chrome boundary without a false account mismatch',async()=>{
  const {results,calls}=await captureWorkerCase('page-error',[{result:{ok:false,url:'https://finance.yahoo.com/portfolio/p_fixture/view',error:'reading table rows: A row has 8 cells for 13 columns.'}}]);
  assert.equal(calls,1);
  assert.equal(results[0].ok,false);
  assert.match(results[0].error,/A row has 8 cells for 13 columns/);
  assert.doesNotMatch(results[0].error,/account did not match/);
});

test('a transient missing result recovers, while a genuinely different account is rejected',async()=>{
  const success={result:{ok:true,url:'https://finance.yahoo.com/portfolio/p_fixture/view',table:{method:'yahoo-holdings-table-v1',rows:[['DEMO','1']]}}};
  const recovered=await captureWorkerCase('recovered-capture',[{result:null},success]);
  assert.equal(recovered.calls,2);
  assert.equal(recovered.results[0].ok,true);
  const rejected=await captureWorkerCase('wrong-captured-account',[{result:{...success.result,url:'https://finance.yahoo.com/portfolio/p_other/view'}}]);
  assert.equal(rejected.results[0].ok,false);
  assert.match(rejected.results[0].error,/Captured account did not match/);
});

test('worker announces extension 1.2.0 and delivers a version 2 capture unchanged',async()=>{
  const table={method:'yahoo-holdings-table-v1',headers:['Symbol','Shares','Yahoo quote symbol'],rows:[['RY','1','RY.TO']],
    page_count:1,expected_count:1,completeness:'count-verified',capture_version:2};
  const {results,versions}=await captureWorkerCase('listing-capture',[{result:{ok:true,url:'https://finance.yahoo.com/portfolio/p_fixture/view',table}}]);
  assert.ok(versions.length>=2);
  assert.ok(versions.every(version=>version==='1.2.0'));
  assert.equal(results[0].ok,true);
  assert.deepEqual(results[0].table,table);
});
