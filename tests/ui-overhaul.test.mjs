import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {JSDOM} from 'jsdom';
import * as stateFunctions from '../static/research-state.js';

const config={mandate:{base_currency:'USD',benchmark_id:'VTI'},allocation:{horizon_months:12,shared_state:{market_returns:{adverse:-.2,central:.07,favorable:.18}}}};
const eps={security_id:'SEC-ABC',starting_price:100,currency:'USD',horizon_months:12,eps_convention:'forward',pe_convention:'forward',scenarios:[{label:'adverse',eps:4,pe:18,distributions_per_starting_share:1},{label:'central',eps:5,pe:24,distributions_per_starting_share:1},{label:'favorable',eps:6,pe:28,distributions_per_starting_share:1}]};
function sample(action='Buy') {
  return {run_id:'f38904d81e5af5b4',metadata:{as_of:'2026-09-30',valuation_date:'2026-09-30',mode:'offline'},
    summary:{currency:'USD',accounts:[{account_id:'account_abc098ade1',account_name:'Schwab Brokerage'}]},
    accounts:[{account_id:'account_abc098ade1',display_name:'Schwab Brokerage'}],account_labels:{account_abc098ade1:'Schwab Brokerage'},
    holdings:[{security_id:'SEC-ABC',ticker:'ABC',name:'Example Company',account_id:'account_abc098ade1',price:100,currency:'USD',quantity:5,market_value:500}],
    holding_analysis:[{security_id:'SEC-ABC',ticker:'ABC',name:'Example Company',action,reason:'Scenario return exceeds the benchmark.',status:'ready',model:'eps_multiple',inputs:{starting_price:100,currency:'USD'},valuation_input:eps,
      scenarios:eps.scenarios.map(row=>({...row,return_value:action==='Buy'?.2:-.2,horizon_price:row.eps*row.pe})),excess_return:action==='Buy'?.12:-.25,
      evidence:{statements:{used:true,status:'available'},estimates:{used:false,status:'unavailable'},macro:{used:false,status:'available',note:'Economic data is context; the EPS valuation does not automatically use it.'}}}],
    macro:[{series_id:'FEDFUNDS',latest_value:4}],signals:[],research:{},company_research:{'SEC-ABC':{eps:{currency:'USD',scenarios:eps.scenarios.map(row=>({...row,horizon_price:row.eps*row.pe,total_return:.2,status:'ready'}))}}}};
}
async function fixture(t) {
  const [html,source]=await Promise.all([readFile(new URL('../static/index.html',import.meta.url),'utf8'),readFile(new URL('../static/research.js',import.meta.url),'utf8')]);
  const dom=new JSDOM(html,{url:'http://localhost',runScripts:'outside-only',pretendToBeVisual:true});t.after(()=>dom.window.close());
  dom.window.__stateFunctions=stateFunctions;dom.window.scrollTo=()=>{};dom.window.HTMLElement.prototype.scrollIntoView=()=>{};
  const code=source.replace(/import\s*\{([\s\S]*?)\}\s*from\s*['"]\.\/research-state\.js['"];?/,'const {$1}=window.__stateFunctions;').replace(/initialize\(\);\s*$/,'');
  dom.window.eval(`(()=>{${code}\nwindow.uiTest={load(value,context={}){result=value;view='saved';service={capabilities:{holding_analysis:1},config:${JSON.stringify(config)},runs:[{run_id:value.run_id,as_of:value.metadata.as_of}],mode:'offline',...context};draft=newDraft(value.run_id,service.config,{});saved={result:value,config:service.config};renderAll();},draft(){return clone(draft);},table,renderCurrent(value){current=value;renderCurrent();},renderExceptionForms(value){exceptionState=value;renderExceptionForms();}};})();`);
  return {window:dom.window,$:id=>dom.window.document.getElementById(id),ui:dom.window.uiTest};
}
test('research opens with three populated scenario inputs and per-holding action',async t=>{
  const {window,$,ui}=await fixture(t);ui.load(sample());
  assert.equal($('holding-analysis').querySelector('.action-badge').textContent,'Buy');
  assert.match($('holding-analysis').textContent,/Adverse.*Central.*Favorable/s);
  assert.equal($('eps-form').querySelectorAll('tbody tr').length,3);
  assert.deepEqual([...$('eps-form').querySelectorAll('tbody input')].map(input=>input.value),['4','18','1','5','24','1','6','28','1']);
  assert.match($('company-data-basis').textContent,/Statements: Used/);
  assert.match($('company-data-basis').textContent,/Economic data: Context only/);
  const edited=$('eps-form').querySelector('tbody input');edited.value='3';edited.dispatchEvent(new window.Event('input',{bubbles:true}));
  assert.equal(ui.draft().workspace.valuations['SEC-ABC'].eps.scenarios[0].eps,3);
  assert.equal(ui.draft().workspace.valuations['SEC-ABC'].eps.scenarios[1].eps,5,'editing one prefilled value retains the other cases');
  assert.match($('holding-analysis-state').textContent,/Recalculate/);
  assert.ok($('holding-analysis').classList.contains('stale-result'));
  ui.load(sample('Sell'));
  assert.equal($('holding-analysis').querySelector('.action-badge').textContent,'Sell');
  assert.ok(!$('holding-analysis').classList.contains('stale-result'));
});
test('routine controls show names and dates with identifiers reachable in details',async t=>{
  const {$,ui}=await fixture(t);ui.load(sample());
  assert.equal($('account-scope').options[1].textContent,'Schwab Brokerage');
  assert.doesNotMatch($('research-run').selectedOptions[0].textContent,/f38904d8/);
  assert.match($('technical-context').textContent,/f38904d81e5af5b4/);
  assert.match($('account-reconciliation').textContent,/Schwab Brokerage/);
  assert.doesNotMatch($('account-reconciliation').textContent,/account_abc098ade1/);
  assert.equal($('cutoff-context').closest('details').open,false);
});
test('large result tables page rows while sorting across the complete set',async t=>{
  const {ui}=await fixture(t);
  const grid=ui.table(Array.from({length:1000},(_,i)=>({value:999-i})),[{key:'value'}]);
  assert.equal(grid.querySelectorAll('tbody tr').length,100);
  assert.match(grid.querySelector('.table-pagination').textContent,/1–100 of 1000/);
  grid.querySelector('th button').click();
  assert.equal(grid.querySelector('tbody td').textContent,'0','sorting uses the full thousand rows');
  [...grid.querySelectorAll('.table-pagination button')].find(button=>button.textContent==='Next').click();
  assert.equal(grid.querySelector('tbody td').textContent,'100');
});
test('empty attention cards withdraw and currency assumptions stay concise',async t=>{
  const {$,ui}=await fixture(t);
  ui.renderExceptionForms({exceptions:[]});assert.ok($('exception-panel').hidden);
  ui.renderCurrent({supported:true,current:{accounts:[],positions:[],dates:{collection_received_at:'2026-09-30T16:00:00Z'},totals:{by_currency:[],usd:{covered_total:100,covered_position_count:1,unconverted_position_count:0,value_currency:{basis:'presentation',label:'Values read as reported in USD: no currency supplied by source.'}}}}});
  assert.equal($('current-value-currency').querySelector('summary').textContent,'Currency: USD · assumed');
  assert.equal($('current-value-currency').querySelector('details').open,false);
  assert.match($('current-value-currency').textContent,/no currency supplied/,'the full basis remains accessible');
  assert.doesNotMatch($('current-dates').textContent,/Generated|Source valuation/);
});
test('a legacy saved review missing the analysis contract requests an update, never a placeholder action',async t=>{
  const {$,ui}=await fixture(t);const legacy=sample();delete legacy.holding_analysis;
  ui.load(legacy);
  assert.equal($('holding-analysis').querySelector('.action-badge').textContent,'Update analysis');
  assert.match($('holding-analysis-message').textContent,/saved review has no holding analysis/);
  assert.match($('holding-analysis').textContent,/Why \/ next step/);
  assert.match($('holding-analysis').textContent,/Update & analyze/);
  assert.match($('company-data-basis').textContent,/saved review has no holding analysis/);
  assert.equal($('company-data-basis').querySelectorAll('.evidence-chip').length,0,'an absent contract does not claim evidence is missing');
  assert.doesNotMatch($('holding-analysis').textContent,/Review|Needs data/);
});
test('new static assets on an old running app explicitly require restart',async t=>{
  const {$,ui}=await fixture(t);const legacy=sample();delete legacy.holding_analysis;
  ui.load(legacy,{capabilities:{},backend_version:'old'});
  assert.equal($('holding-analysis').querySelector('.action-badge').textContent,'Restart app');
  assert.match($('holding-analysis-message').textContent,/running app does not support holding analysis/);
  assert.match($('company-data-basis').textContent,/Restart Portfolio Review, then reload/);
  assert.doesNotMatch($('holding-analysis').textContent,/Needs data|Update & analyze/);
});
test('real input gaps show the concrete reason and a visible next step',async t=>{
  const {window,$,ui}=await fixture(t);const missing=sample();
  Object.assign(missing.holding_analysis[0],{action:'Review',model:'unavailable',status:'unavailable',reason:'No comparable statement EPS or currency-matched consensus is available.',valuation_input:null,scenarios:[],next_step:{label:'Check data',section:'data'}});
  ui.load(missing);
  assert.equal($('holding-analysis').querySelector('.action-badge').textContent,'Review');
  assert.ok($('holding-analysis-message').hidden,'an input gap does not become an engine compatibility error');
  assert.match($('holding-analysis').querySelector('.analysis-reason').textContent,/No comparable statement EPS/);
  assert.match($('company-data-basis').textContent,/No comparable statement EPS/);
  $('holding-analysis').querySelector('.analysis-reason button').click();
  assert.equal(window.document.querySelector('[aria-current="page"]').dataset.page,'data');
  assert.doesNotMatch($('holding-analysis').textContent,/Restart app/);
});
test('read-only analysis computed from legacy saved inputs is identified with model details',async t=>{
  const {$,ui}=await fixture(t);const derived=sample();
  derived.metadata.computed_views={holding_analysis:{version:1,method_version:'holding-scenarios-1',status:'computed',source:'frozen_saved_inputs',base_run_id:derived.run_id,archive_unchanged:true}};
  ui.load(derived);
  assert.equal($('holding-analysis').querySelector('.action-badge').textContent,'Buy');
  assert.match($('holding-analysis-message').textContent,/Research calculated from saved inputs/);
  assert.equal($('holding-analysis-message').querySelector('details').open,false);
  assert.match($('company-data-basis').textContent,/holding-scenarios-1/);
  assert.match($('company-data-basis').textContent,/frozen_saved_inputs/);
  derived.metadata.computed_views.holding_analysis={...derived.metadata.computed_views.holding_analysis,status:'unavailable',reason:'The saved review has no retained source bundle; run Update & analyze.'};
  ui.load(derived);
  assert.equal($('holding-analysis').querySelector('.action-badge').textContent,'Update analysis','an incompatible archive does not retain a misleading action');
  assert.match($('holding-analysis-message').textContent,/no retained source bundle/);
});
