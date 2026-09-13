import {portfolioURL, validServer, yahooPageAction, validateJob, waitForYahooTab} from './helpers.js';
import {readYahooHoldings} from './holdings.js';

let active = null, checking = false, finishing = false;

async function settings() {
  const values = await chrome.storage.local.get(['server', 'key']);
  if (!values.server || !values.key) throw new Error('Paste the pairing key from your local portfolio app.');
  return {server: validServer(values.server), key: values.key};
}
async function request(path, body) {
  const config = await settings();
  const response = await fetch(config.server + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {'X-Companion-Key': config.key, 'X-Companion-Version':'1.1.6', ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
    body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(12000)
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || 'Cannot reach the local app.');
    error.status = response.status;
    throw error;
  }
  return result;
}
async function status(message, error = false) {
  await chrome.storage.local.set({status:message, error, checkedAt:Date.now()});
}
async function saveActive() { await chrome.storage.session.set({active}); }

async function yahooTab(url) {
  const saved = await chrome.storage.session.get('yahooTab');
  let tab;
  if (saved.yahooTab) {
    try { tab = await chrome.tabs.update(saved.yahooTab, {url, active:true}); } catch {}
  }
  if (!tab) tab = await chrome.tabs.create({url, active:true});
  await chrome.storage.session.set({yahooTab:tab.id});
  return waitForYahooTab(()=>chrome.tabs.get(tab.id),url);
}

async function finish(payload) {
  if (!active || finishing) return;
  finishing = true;
  try {
    // Persist delivery before sending so a stopped service worker can retry safely.
    active.result = payload; await saveActive();
    const result = await request('/api/companion/result', {id:active.job.id, ...payload});
    await status(result.ok ? 'Completed. Your local app has the result.' : result.error, !result.ok);
    active = null; await saveActive();
  } catch (error) {
    if (error.status === 400) {
      active = null; await saveActive();
      await status('The previous request expired or the app restarted. Retry the operation in the local app.', true);
    } else await status(error.message + ' Open the local app and click Check now to retry delivery.', true);
  }
  finally { finishing = false; }
}

async function perform(job) {
  active = {job, started:Date.now(), tabId:null}; await saveActive();
  try {
    await status('Opening ' + job.name + ' in Chrome…');
    const tab = await yahooTab(job.navigation_url || (job.action==='refresh' ? job.url+'/view' : job.url));
    active.tabId = tab.id; await saveActive();
    if (job.action === 'connect') return await finish({ok:true});
    if (!tab.url?.startsWith('https://finance.yahoo.com/')) throw new Error('Finish Yahoo sign-in in Chrome, then retry.');
    if (job.action === 'refresh' && portfolioURL(tab.url) !== job.url) throw new Error('Yahoo did not open the requested portfolio. Previous data was kept.');
    if(job.action==='discover') {
      const [answer]=await chrome.scripting.executeScript({target:{tabId:tab.id},func:yahooPageAction,args:['discover']});
      return await finish({ok:true,links:answer.result.links,portfolio_names:answer.result.portfolio_names || []});
    }
    if(job.collection_method!=='holdings-table-v1') throw new Error('Restart the local app to enable direct Holdings table collection, then retry.');
    await status('Reading holdings from '+job.name+'…');
    let answer;
    for(let attempt=0;attempt<2;attempt++) {
      [answer]=await chrome.scripting.executeScript({target:{tabId:tab.id},func:readYahooHoldings,args:[job.url]});
      if(answer?.result && typeof answer.result==='object') break;
      if(answer?.error) throw new Error('Chrome could not execute the holdings reader: '+String(answer.error.message || answer.error));
      if(attempt===0) {
        await status('Yahoo returned no capture result. Waiting for the selected account and trying once more…');
        await waitForYahooTab(()=>chrome.tabs.get(tab.id),job.url,{timeout:10000,settle:500});
      }
    }
    if(!answer?.result || typeof answer.result!=='object')
      throw new Error('Chrome returned no holdings capture result after two attempts. The page may have navigated or interrupted the reader; this is not an account mismatch. Previous holdings were kept.');
    if(answer.result.ok===false)
      throw new Error('Holdings collection failed: '+String(answer.result.error || 'The page reader could not finish.'));
    if(typeof answer.result.url!=='string' || !answer.result.url)
      throw new Error('The holdings reader returned a result without a page URL. Previous holdings were kept.');
    const describe=value=>{try{return new URL(value).pathname;}catch{return 'unavailable';}};
    if(portfolioURL(answer?.result?.url)!==job.url)
      throw new Error(`Captured account did not match: expected ${describe(job.url)}, received ${describe(answer?.result?.url)}. Previous holdings were kept.`);
    // Holdings tab navigation can finish in the document before tabs.get reflects it.
    try {await waitForYahooTab(()=>chrome.tabs.get(tab.id),job.url,{timeout:10000,settle:300});}
    catch {
      const current=await chrome.tabs.get(tab.id).catch(()=>null);
      throw new Error(`Yahoo account navigation did not settle: expected ${describe(job.url)}, browser shows ${describe(current?.url)}. Previous holdings were kept.`);
    }
    if(!answer.result.table) throw new Error('No holdings table was returned. Reload Local Portfolio in chrome://extensions.');
    await finish({ok:true,url:answer.result.url,table:answer.result.table});
  } catch (error) { await finish({ok:false,error:error.message}); }
}

async function poll() {
  if (checking) return;
  checking = true;
  try {
    active = active || (await chrome.storage.session.get('active')).active;
    if (active) {
      if (active.result) await finish(active.result);
      else if (Date.now() - active.started > 240000) await finish({ok:false,error:'The Chrome request timed out. Retry from the local app.'});
      else await finish({ok:false,error:'Chrome interrupted this operation. Retry from the local app.'});
      if (active) return;
    }
    // Drain a requested group of accounts; idle checks never initiate new imports.
    for (let i=0; i<200; i++) {
      const {job} = await request('/api/companion/job');
      if (!job) { await status('Connected. Sign in to Yahoo normally; request imports from the local app.'); break; }
      await perform(validateJob(job));
      if (active) break;
    }
  } catch (error) { await status(error.message, true); }
  finally { checking = false; }
}

chrome.runtime.onInstalled.addListener(() => chrome.alarms.create('portfolio-check', {periodInMinutes:0.5}));
chrome.runtime.onStartup.addListener(() => { chrome.alarms.create('portfolio-check', {periodInMinutes:0.5}); void poll(); });
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === 'portfolio-check') void poll(); });
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (message.action !== 'check' || sender.id !== chrome.runtime.id) return;
  void poll().then(() => reply({ok:true})).catch(error => reply({ok:false,error:error.message}));
  return true;
});
