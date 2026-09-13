import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readYahooHoldings} from '../chrome-extension/holdings.js';
const url='https://finance.yahoo.com/portfolio/yodlee%7Cfixture/view';
const headers=['Symbol','Shares','Last Price','AC/Share','Total Cost ($)','Market Value ($)'];
const row=symbol=>[symbol,'12.345','20.10','18.25','225.29625','248.1345'];

async function fixture({pages=[[row('DEMO')]],ranges=false,total,loadMore=false,virtual=false,tab=false,wrongURL=false,deepPagination=false,nextLabel='Next page',rawResult=false,unrelatedNext=false,columnHeaders=headers,hiddenColumns=[]}={}) {
  const originalNow=Date.now,originalTimeout=globalThis.setTimeout;
  let clock=0,index=0,scrolled=false,selected=!tab,clicks=0;
  const base={getClientRects:()=>[{}],getAttribute:()=>null,hasAttribute:()=>false};
  const textElement=value=>({...base,innerText:value,textContent:value,querySelector:()=>null});
  const next={...base,innerText:nextLabel,get disabled(){return index===pages.length-1;},click:()=>{index++;clicks++;}};
  const unrelated={...base,innerText:'Next',disabled:false,click:()=>{clicks++;}};
  const holdings={...base,innerText:'Holdings',click:()=>{selected=true;clicks++;}};
  const data=()=>loadMore && !scrolled ? pages[index].slice(0,1) : virtual && scrolled ? [row('REPLACED')] : pages[index];
  const body={...base,parentElement:null,querySelectorAll:()=>unrelatedNext?[unrelated]:(ranges?[next]:[]),get innerText(){
    const first=pages.slice(0,index).reduce((n,p)=>n+p.filter(r=>r[0]!=='Total Cash').length,0)+1;
    return ranges?`${first}-${first+data().filter(r=>r[0]!=='Total Cash').length-1} of ${total ?? pages.flat().filter(r=>r[0]!=='Total Cash').length}`:'';
  }};
  const table={...base,parentElement:body,querySelectorAll:selector=>selector.startsWith('thead')
    ? columnHeaders.map((h,i)=>({...textElement(h),getClientRects:()=>hiddenColumns.includes(i)?[]:[{}]}))
    : data().map(r=>({...base,querySelectorAll:()=>r.map((v,i)=>({...textElement(v),getClientRects:()=>hiddenColumns.includes(i)?[]:[{}]}))}))};
  if(deepPagination) {let parent=body;for(let i=0;i<6;i++) parent={...base,parentElement:parent,innerText:'',querySelectorAll:()=>[]};table.parentElement=parent;}
  globalThis.location={origin:'https://finance.yahoo.com',href:wrongURL?url.replace('fixture','wrong'):url};
  globalThis.getComputedStyle=()=>({visibility:'visible',overflowY:'visible'});
  globalThis.document={body,documentElement:{scrollHeight:2000},querySelectorAll:selector=>
    selector.startsWith('table,')?(selected?[table]:[]):selector.startsWith('th,')?columnHeaders.map((h,i)=>({...textElement(h),getClientRects:()=>hiddenColumns.includes(i)?[]:[{}]})):[holdings]};
  globalThis.window={scrollTo:()=>{scrolled=true;}};
  Date.now=()=>clock;
  globalThis.setTimeout=(callback,ms)=>{clock+=ms;queueMicrotask(callback);return 0;};
  try {
    // Chrome serializes the function: run its body without module closures.
    const injected=vm.runInNewContext('('+readYahooHoldings.toString()+')',{
      document,window,location,getComputedStyle,Date,setTimeout,URL
    });
    const result=JSON.parse(JSON.stringify(await injected(url)));
    if(result.ok===false && !rawResult) throw new Error(result.error);
    return {result,clicks};
  }
  finally {Date.now=originalNow;globalThis.setTimeout=originalTimeout;}
}

test('reads representative Holdings columns directly, preserving displayed numbers',async()=>{
  const {result,clicks}=await fixture();
  assert.equal(clicks,0);
  assert.deepEqual(result.table.headers,headers);
  assert.deepEqual(result.table.rows,[row('DEMO')]);
  assert.equal(result.table.completeness,'end-observed');
  assert.equal(result.table.expected_count,null);
});
test('selects Holdings when a different view is open',async()=>{
  const {result,clicks}=await fixture({tab:true});
  assert.equal(clicks,1);
  assert.equal(result.table.rows.length,1);
});
test('scrolls until additional rows stop appearing',async()=>{
  const {result}=await fixture({pages:[[row('FIRST'),row('SECOND'),row('THIRD')]],loadMore:true});
  assert.equal(result.table.rows.length,3);
});
test('reads every page and verifies the total row count',async()=>{
  const {result,clicks}=await fixture({pages:[[row('FIRST'),row('SECOND')],[row('THIRD')]],ranges:true});
  assert.equal(clicks,1);
  assert.equal(result.table.page_count,2);
  assert.equal(result.table.expected_count,3);
  assert.equal(result.table.completeness,'count-verified');
  assert.deepEqual(result.table.rows.map(r=>r[0]),['FIRST','SECOND','THIRD']);
});
test('rejects a missing final page rather than saving a partial portfolio',async()=>{
  await assert.rejects(fixture({ranges:true,total:64}),/Read 1 of 64 holdings/);
});
test('rejects virtualized rows replacing earlier holdings',async()=>{
  await assert.rejects(fixture({virtual:true}),/replaced holdings rows/);
});
test('never reads a different account',async()=>{
  await assert.rejects(fixture({wrongURL:true}),/different account/);
});

test('finds pagination outside deep table wrappers with Goto next page labels',async()=>{
  const {result}=await fixture({pages:[[row('FIRST')],[row('SECOND')]],ranges:true,deepPagination:true,nextLabel:'Goto next page'});
  assert.equal(result.table.rows.length,2);
  assert.equal(result.table.completeness,'count-verified');
});
test('retains a repeated Total Cash footer only once across pages',async()=>{
  const cash=['Total Cash','','7.25','','',''];
  const {result}=await fixture({pages:[[row('FIRST'),cash],[row('SECOND'),cash]],ranges:true});
  assert.equal(result.table.rows.length,3);
  assert.equal(result.table.rows.filter(r=>r[0]==='Total Cash').length,1);
  assert.equal(result.table.expected_count,3);
});

test('injected reader returns a serializable error and page URL instead of rejecting',async()=>{
  const {result}=await fixture({ranges:true,total:64,rawResult:true});
  assert.equal(result.ok,false);
  assert.equal(result.url,url);
  assert.match(result.error,/Read 1 of 64 holdings/);
  assert.equal(result.table,undefined);
});

test('does not click a page-wide Next button with no holdings pagination evidence',async()=>{
  const {result,clicks}=await fixture({unrelatedNext:true});
  assert.equal(clicks,0);
  assert.equal(result.table.rows.length,1);
});


test('preserves cell alignment with repeated blank selection and action headers',async()=>{
  const {result}=await fixture({columnHeaders:['',...headers,''],pages:[[['',...row('DEMO'),'Edit']]]});
  assert.deepEqual(result.table.headers,['Unlabeled column 1',...headers,'Unlabeled column 8']);
  assert.deepEqual(result.table.rows,[['',...row('DEMO'),'Edit']]);
});
test('ignores hidden columns consistently in headers and rows',async()=>{
  const {result}=await fixture({columnHeaders:[...headers,'Shares'],pages:[[ [...row('DEMO'),'999'] ]],hiddenColumns:[6]});
  assert.deepEqual(result.table.headers,headers);
  assert.deepEqual(result.table.rows,[row('DEMO')]);
});
test('still rejects duplicate named financial columns with useful header details',async()=>{
  await assert.rejects(fixture({columnHeaders:[...headers,'Shares'],pages:[[ [...row('DEMO'),'999'] ]]}),/ambiguous columns:.*Shares/);
});

test('reports the ticker and exact rejected Shares text without guessing a quantity',async()=>{
  await assert.rejects(fixture({pages:[[['DEMO','150 2 lots',...row('DEMO').slice(2)]]]}),/Shares for DEMO contains unrecognized text "150 2 lots"/);
});

test('retains Add entries without inventing quantities and preserves pagination counts',async()=>{
  const unheld=['UNHELD','Add','50','--','--','--'];
  const {result}=await fixture({pages:[[row('HELD')],[unheld]],ranges:true});
  assert.equal(result.table.expected_count,2);
  assert.equal(result.table.completeness,'count-verified');
  assert.deepEqual(result.table.headers,[...headers,'Yahoo Shares display']);
  assert.deepEqual(result.table.rows,[[...row('HELD'),'12.345'],['UNHELD','','50','--','--','--','Add']]);
});
test('does not treat Add followed by a number as an empty quantity',async()=>{
  await assert.rejects(fixture({pages:[[['DEMO','Add 10',...row('DEMO').slice(2)]]]}),/unrecognized text "Add 10"/);
});
