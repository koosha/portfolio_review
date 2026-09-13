import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

async function uiFixture(handleRequest) {
  const html=await readFile(new URL('../static/index.html',import.meta.url),'utf8');
  const code=await readFile(new URL('../static/app.js',import.meta.url),'utf8');
  const elements=new Map();
  class Element {
    constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.hidden=false;this.disabled=false;}
    set id(value){this._id=value;elements.set(value,this);}
    get id(){return this._id;}
    setAttribute(key,value){this[key]=value;}
    removeAttribute(key){delete this[key];}
    append(...children){this.children.push(...children);}
    replaceChildren(...children){this.children=children;}
    scrollIntoView(){this.scrolled=true;}
  }
  for(const [,id] of html.matchAll(/id="([^"]+)"/g)){const el=new Element('element');el.id=id;}
  const sources=Array.from({length:13},(_,i)=>({id:i+1,name:`Portfolio ${i+1}`,url:`https://finance.yahoo.com/portfolio/p_${i}`,selected:i>=4?1:0,position_count:0}));
  sources[5].last_error='Export unavailable';
  sources[4].snapshot_id=1; sources[4].holding_summary={groups:[{currency:null,formatted_total:'107.26'}],cash_row_count:1,missing_market_values:0,completeness_verified:false};
  const response={token:'local-token',sources,browser:{connector:'chrome',browser_open:true,busy:false,progress:{total:0,completed:0}}};
  const requests=[];
  const document={getElementById:id=>elements.get(id),createElement:tag=>new Element(tag),
    querySelectorAll:()=>[...elements.values()].filter(el=>el.dataset.selection),createDocumentFragment:()=>new Element('fragment')};
  const context=vm.createContext({document,Date,JSON,console,setTimeout:()=>{},fetch:async(path,options)=>{
    const handled=handleRequest?.(path,options,response);
    if(handled !== undefined) return {ok:true,json:async()=>structuredClone(await handled)};
    if(path!='/api/state') {
      requests.push({path,options});
      const match=path.match(/sources\/(\d+)\/selection/);
      if(match) sources[Number(match[1])-1].selected=JSON.parse(options.body).selected?1:0;
    }
    return {ok:true,json:async()=>structuredClone(path==='/api/state'?response:{saved:true})};
  }});
  vm.runInContext(code,context);
  await new Promise(resolve=>setImmediate(resolve));
  return {elements,sources,requests,context,html,response};
}

test('checkboxes render all portfolios, save a change, and pull without labels',async()=>{
  const {elements,sources,requests,html}=await uiFixture();
  assert.equal(elements.get('sources').children.length,13);
  assert.equal(elements.get('source-count').textContent,'9 / 13 selected');
  assert.deepEqual(sources.map(s=>elements.get(`portfolio-${s.id}`).checked),[false,false,false,false,...Array(9).fill(true)]);
  const total=elements.get('sources').children[4].children[1].children[1];
  assert.match(total.children[0].textContent,/107.26/);
  assert.equal(total.children[0].textContent,'Market value: 107.26');
  assert.equal(total.children.length,1);
  assert.match(elements.get('sources').children[4].children[1].children[2].textContent,/^Last pull:/);
  const checkbox=elements.get('portfolio-5');
  checkbox.checked=false;
  await checkbox.onchange();
  assert.equal(requests[0].path,'/api/sources/5/selection');
  assert.equal(requests[0].options.headers['X-Local-Token'],'local-token');
  assert.equal(elements.get('source-count').textContent,'8 / 13 selected');
  await elements.get('refresh').onclick();
  assert.equal(requests[1].path,'/api/pull');
  assert.equal(requests[1].options.body,'{}');
  const retry=elements.get('sources').children[5].children[2].children.find(el=>el.textContent==='Retry');
  assert.ok(retry);
  await retry.onclick();
  assert.equal(requests[2].path,'/api/browser');
  assert.deepEqual(JSON.parse(requests[2].options.body),{action:'refresh',source_ids:[6]});
  assert.ok(!elements.has('add-form'));
  assert.ok(!html.includes('Choose account label'));
});

function deferred() {
  let resolve;
  const promise=new Promise(done=>{resolve=done;});
  return {promise,resolve};
}

test('an older state poll cannot restore selections after a successful save',async()=>{
  const stalePoll=deferred();
  let holdNextPoll=false;
  const {elements,context,response}=await uiFixture(path=>{
    if(path==='/api/state' && holdNextPoll) {
      holdNextPoll=false;
      return stalePoll.promise;
    }
  });
  const oldState=structuredClone(response);
  holdNextPoll=true;
  const poll=vm.runInContext('reload()',context);
  const checkbox=elements.get('portfolio-5');
  checkbox.checked=false;
  await checkbox.onchange();
  assert.equal(elements.get('source-count').textContent,'8 / 13 selected');
  stalePoll.resolve(oldState);
  await poll;
  assert.equal(elements.get('portfolio-5').checked,false);
  assert.equal(elements.get('source-count').textContent,'8 / 13 selected');
});

const snapshot=id=>({captured_at:'2026-01-01T00:00:00Z',rows:[{symbol:`FIXTURE${id}`}],
  holding_summary:{groups:[],completeness_verified:true},warnings:[]});

test('a slower earlier portfolio response cannot replace the latest chosen holdings',async()=>{
  const first=deferred();
  const {elements,context}=await uiFixture(path=>{
    if(path==='/api/sources/1/snapshots') return first.promise;
    if(path==='/api/sources/2/snapshots') return [{id:20,captured_at:'2026-01-01'}];
    if(path.startsWith('/api/snapshots/')) return snapshot(path.split('/').pop());
  });
  const earlier=vm.runInContext('showSource({id:1,name:"Fixture A"})',context);
  await vm.runInContext('showSource({id:2,name:"Fixture B"})',context);
  assert.equal(elements.get('snapshot-title').textContent,'Fixture B');
  first.resolve([{id:10,captured_at:'2026-01-01'}]);
  await earlier;
  assert.equal(elements.get('snapshot-title').textContent,'Fixture B');
  assert.equal(elements.get('snapshot-select').value,'20');
  assert.equal(elements.get('raw-download').href,'/api/snapshots/20/csv');
});

test('closing holdings invalidates pending portfolio and snapshot responses',async()=>{
  const list=deferred(),detail=deferred();
  const {elements,context}=await uiFixture(path=>{
    if(path==='/api/sources/1/snapshots') return list.promise;
    if(path==='/api/sources/2/snapshots') return [{id:20,captured_at:'2026-01-01'}];
    if(path==='/api/snapshots/20') return detail.promise;
  });
  const pendingList=vm.runInContext('showSource({id:1,name:"Fixture A"})',context);
  elements.get('close-snapshot').onclick();
  list.resolve([{id:10,captured_at:'2026-01-01'}]);
  await pendingList;
  assert.equal(elements.get('snapshot-panel').hidden,true);
  const pendingDetail=vm.runInContext('showSource({id:2,name:"Fixture B"})',context);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(elements.get('snapshot-panel').hidden,false);
  elements.get('close-snapshot').onclick();
  detail.resolve(snapshot(20));
  await pendingDetail;
  assert.equal(elements.get('snapshot-panel').hidden,true);
  assert.equal(elements.get('raw-download').href,undefined);
});
