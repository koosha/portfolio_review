import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {portfolioURL, validServer, yahooPageAction, validateJob, waitForYahooTab} from '../chrome-extension/helpers.js';

test('extension has no Google, cookies, debugger, or general browsing permissions', async () => {
  const manifest=JSON.parse(await readFile(new URL('../chrome-extension/manifest.json',import.meta.url)));
  assert.equal(manifest.manifest_version,3);
  assert.deepEqual(manifest.host_permissions,['https://finance.yahoo.com/*','http://127.0.0.1/*','http://localhost/*']);
  for (const permission of ['cookies','debugger','tabs','webRequest','downloads']) assert.ok(!manifest.permissions.includes(permission));
});
test('pairing cannot send its key to a remote server', () => {
  assert.equal(validServer('http://127.0.0.1:8765/'),'http://127.0.0.1:8765');
  for (const value of ['https://evil.example','http://localhost.evil.example','http://user@localhost:8765','http://localhost:8765/evil'])
    assert.throws(()=>validServer(value));
});
test('portfolio identity strips view state but rejects external and overview pages', () => {
  assert.equal(portfolioURL('https://finance.yahoo.com/portfolio/p_fixture_1/view/v1'),'https://finance.yahoo.com/portfolio/p_fixture_1');
  assert.equal(portfolioURL('https://finance.yahoo.com/portfolios/'),null);
  assert.equal(portfolioURL('https://evil.example/portfolio/p_fixture_1'),null);
});
test('linked brokerage IDs survive discovery and refresh validation', () => {
  const url='https://finance.yahoo.com/portfolio/yodlee%7C11111111-2222-3333-4444-555555555555';
  assert.equal(portfolioURL(url+'/view'),url);
  assert.equal(portfolioURL(url.replace('%7C','%7c')+'/view'),url);
  assert.equal(portfolioURL(url.replace('%7C','|')+'/view'),url);
  const job={id:'f'.repeat(32),action:'refresh',url};
  assert.equal(validateJob(job),job);
  for (const id of ['yodlee%7C..%2Fprivate','yodlee%257Caccount','yodlee%7Caccount%3Fother','yodlee%7C'])
    assert.equal(portfolioURL('https://finance.yahoo.com/portfolio/'+id),null);
});
test('jobs cannot open unrelated sites or use invalid identifiers', () => {
  const job={id:'a'.repeat(32),action:'refresh',url:'https://finance.yahoo.com/portfolio/p_fixture_1'};
  assert.equal(validateJob(job),job);
  assert.throws(()=>validateJob({...job,id:'../../private'}));
  assert.throws(()=>validateJob({...job,url:'https://accounts.google.com/'}));
  assert.throws(()=>validateJob({...job,action:'eval'}));
});
test('page helper never runs on Google sign-in', async () => {
  globalThis.location={origin:'https://accounts.google.com'};
  await assert.rejects(yahooPageAction('discover'),/Finish Yahoo sign-in/);
});
async function discoveryFixture(delayedLinks) {
  const originalNow=Date.now, originalTimeout=globalThis.setTimeout;
  let clock=0;
  const entries=Array.from({length:13},(_,i)=>({
    innerText:i<7 ? `Manual ${i}` : `Linked ${i}`,
    href:i<7 ? `https://finance.yahoo.com/portfolio/p_fixture_${i}` : `https://finance.yahoo.com/portfolio/yodlee%7Caccount-${i}/view`,
    getAttribute(){return this.href;}
  }));
  const table={querySelectorAll:selector=>selector.startsWith('th')
    ? [{innerText:''},{innerText:'Portfolio Name'}]
    : entries.map(entry=>({querySelectorAll:()=>[{innerText:''},{innerText:entry.innerText}]}))};
  globalThis.location={origin:'https://finance.yahoo.com',href:'https://finance.yahoo.com/portfolios/'};
  globalThis.document={querySelectorAll:selector=>selector==='a[href]'
    ? entries.slice(0,delayedLinks(clock) ? 13 : 7) : [table]};
  Date.now=()=>clock;
  globalThis.setTimeout=(callback,ms)=>{clock+=ms;queueMicrotask(callback);return 0;};
  try { return {result:await yahooPageAction('discover'),elapsed:clock}; }
  finally { Date.now=originalNow;globalThis.setTimeout=originalTimeout; }
}
test('discovery waits for linked accounts rendered after the first seven portfolios', async () => {
  const {result,elapsed}=await discoveryFixture(clock=>clock>=800);
  assert.equal(result.links.length,13);
  assert.equal(result.portfolio_names.length,13);
  assert.ok(elapsed>=2000);
});
test('incomplete discovery retains table names so missing accounts can be reported', async () => {
  const {result,elapsed}=await discoveryFixture(()=>false);
  assert.equal(result.links.length,7);
  assert.equal(result.portfolio_names.length,13);
  assert.equal(elapsed,20000);
});

test('navigation waits through a stale complete page and loading state',async()=>{
  const target='https://finance.yahoo.com/portfolio/p_fixture_9/view/v1';
  const states=[
    {url:'https://finance.yahoo.com/portfolios/',status:'complete'},
    {url:target,status:'complete',pendingUrl:target},
    {url:target,status:'loading'},
    {url:target,status:'complete'}
  ];
  let calls=0;
  const result=await waitForYahooTab(async()=>states[Math.min(calls++,states.length-1)],target,{pause:async()=>{},settle:0});
  assert.equal(calls,4);
  assert.equal(result.url,target);
});

test('navigation to a different account never passes readiness',async()=>{
  await assert.rejects(waitForYahooTab(async()=>({url:'https://finance.yahoo.com/portfolio/p_wrong/view',status:'complete'}),
    'https://finance.yahoo.com/portfolio/p_fixture_9/view',{pause:async()=>{},timeout:5,settle:0}),/previous holdings were kept/);
  assert.throws(()=>validateJob({id:'a'.repeat(32),action:'refresh',url:'https://finance.yahoo.com/portfolio/p_fixture_9',
    navigation_url:'https://finance.yahoo.com/portfolio/p_other/view'}),/does not match/);
});
test('manifest version matches the version the worker and popup announce', async () => {
  const manifest=JSON.parse(await readFile(new URL('../chrome-extension/manifest.json',import.meta.url)));
  const worker=await readFile(new URL('../chrome-extension/worker.js',import.meta.url),'utf8');
  const popup=await readFile(new URL('../chrome-extension/popup.html',import.meta.url),'utf8');
  assert.equal(manifest.version,'1.2.0');
  assert.match(worker,/'X-Companion-Version':\s*'1\.2\.0'/);
  assert.match(popup,new RegExp(`Version ${manifest.version.replace(/\./g,'\\.')}\\.`));
});
