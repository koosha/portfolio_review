import {clone, merge, numberOrNull, newDraft, editDraft, changeHorizon, previewReady,
  acceptPreview, draftKey, restoreDraft, RequestGate, differences, validateConfigurationPatch, validateCompanyInputs, reviseAssessmentInputs,
  applyProposal, keepValuationEdit, adoptOrphanDraft, decisionRecord, selectRun, CURRENT_VIEW} from './research-state.js';

const $ = id => document.getElementById(id);
const labels = ['adverse', 'central', 'favorable'];
const pages = ['overview','holdings','research','scenarios','review','data','settings'];
const gate = new RequestGate();
let service = {}, result = null, saved = null, draft = newDraft(null), token = '', busy = false;
// 'loading' until the first load settles, then 'ready' or 'failed'. A failed load never
// assigns a token, so every control it gates stays disabled for the life of the page;
// the owner is owed the reason rather than a tooltip that says it is still loading.
let bootstrap = 'loading';
let page = 'overview', securityId = '', detailId = '', selectedCandidate = '', account = '';
let view = CURRENT_VIEW, previousRun = null;
let supplemental = null, supplementalSupported = false, decisionRecords = [], latestEvaluation = null;
let fieldCounter = 0;
let current = null, currentRequest = 0;
let exceptionState = null, exceptionRequest = 0;
const columnVisibility = new Set(['security_id','account_id','quantity','price','market_value','calculated_weight','currency']);
const text = value => value === null || value === undefined || value === '' ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value);
const friendly = value => String(value || '').replaceAll('_',' ').replace(/\b\w/g, letter => letter.toUpperCase());
const numeric = value => { try { return numberOrNull(value); } catch { return null; } };
const num = (value, digits=2) => numeric(value) === null ? '—' : numeric(value).toLocaleString(undefined,{maximumFractionDigits:typeof digits==='number'?digits:2});
const pct = value => numeric(value) === null ? '—' : `${num(numeric(value)*100,1)}%`;
const money = (value, currency=result?.summary?.currency) => numeric(value) === null ? '—' : `${num(value)}${currency ? ` ${currency}` : ''}`;
function when(value) {
  if (!value) return 'Never';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}
function el(tag, content, className) {
  const node = document.createElement(tag);
  if (content !== undefined && content !== null) node.textContent = String(content);
  if (className) node.className = className;
  return node;
}
function replace(id, ...children) { $(id).replaceChildren(...children.filter(Boolean)); }
function empty(message='Run Update & analyze to calculate this view.', heading='No calculated result') {
  const node=el('div',null,'empty-state'); node.append(el('strong',heading),el('span',message)); return node;
}
function badge(value) { return el('span',friendly(value || 'unavailable'),`badge ${value || ''}`); }
function error(message='') { $('research-error').textContent=message; $('research-error').hidden=!message; }
function announce(message) { $('research-status').textContent=message; }
function persist() {
  try { sessionStorage.setItem(draftKey(draft.baseRunId),JSON.stringify(draft)); }
  catch { error('This browser could not retain the draft. Keep this tab open until you save a run.'); }
}
function dirty(changes={}) { draft=editDraft(draft,changes); persist(); renderState(true); renderDiff(); }
function patch(values) { validateConfigurationPatch(values);dirty({patch:merge(draft.patch,values)}); }
function workspaceEdit(mutator) { const next=clone(draft.workspace); mutator(next); dirty({workspace:next}); }
const resolved = () => merge(draft.baseConfig,draft.patch);
const horizon = () => resolved().allocation?.horizon_months || 12;
function button(label, action, className='secondary') {
  const node=el('button',label,className); node.type='button';
  node.addEventListener('click', async () => {
    node.disabled=true;
    try { error(); await action(); } catch (failure) { error(failure.message); }
    finally { node.disabled=false; renderState(); }
  }); return node;
}
async function api(path, body) {
  const options=body===undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify(body)};
  const response=await fetch(path,options);
  let payload; try { payload=await response.json(); } catch { throw new Error('The local research service returned an unreadable response.'); }
  if (!response.ok) throw new Error(payload.error || 'Research request failed.');
  return payload;
}
function listIssues(items, fallback='No issues reported for this calculation.') {
  if (!items?.length) return el('p',fallback,'muted');
  const node=el('ul',null,'issue-list');
  for (const issue of items) node.append(el('li',typeof issue==='string' ? issue : issue.message || issue.reason || text(issue),issue.severity || ''));
  return node;
}
function kv(record) {
  const list=el('dl',null,'key-value');
  for (const [key,value] of Object.entries(record || {})) list.append(el('dt',friendly(key)),el('dd',text(value)));
  return list;
}
function disclosure(title,...content) {
  const node=el('details');node.append(el('summary',title),...content.filter(Boolean));return node;
}
function metric(label,value,note='') {
  const node=el('div',null,'metric'); node.append(el('div',label,'metric-label'),el('div',value,'metric-value'),el('div',note,'metric-note')); return node;
}
// Every chart states what it measures, as of when, over which scope and how much of that
// scope this run actually covered. A chart missing those four is a picture rather than a
// measurement, so the caption is assembled from the run instead of typed per chart.
function captionText({units,date,scope,coverage}) {
  return [units || 'Units unstated',date || 'Date unavailable',scope || 'Scope unstated',coverage || 'coverage unstated'].join(' · ');
}
function chartCaption(parts) { return el('p',captionText(parts),'chart-note chart-caption'); }
const observationDate = () => result?.metadata?.market_observation_date || result?.metadata?.as_of || null;
// What the named capabilities answered this run, read from the coverage report the fetch
// wrote; a run without one says so rather than implying complete coverage.
function coverageSummary(...names) {
  const capabilities=result?.coverage?.capabilities;
  if(!capabilities)return 'coverage unstated';
  const stated=names.map(name=>{
    const row=capabilities[name];
    return row?`${friendly(name).toLowerCase()} ${numeric(row.available) ?? 0}/${numeric(row.requested) ?? 0}`:null;
  }).filter(Boolean);
  return stated.length?`coverage ${stated.join(', ')}`:'coverage unstated';
}
// What one published output of this run has and what it still needs.
function readinessNote(output) {
  const row=(result?.readiness || []).find(item=>item.output===output);
  if(!row)return 'coverage unstated';
  return `coverage ${row.status}${row.missing?.length?` · missing ${row.missing.join(', ')}`:''}`;
}
function insight({metric,comparison,source_run,rationale}) {
  const node=el('div',null,'insight');
  node.append(el('strong',text(metric),'insight-metric'),el('span',text(comparison),'insight-comparison'),
    el('small',[source_run,rationale].filter(Boolean).join(' · '),'insight-source'));
  return node;
}
// Current holdings and a saved analysis are dated separately and are never read as one
// view: the analysed sections withdraw rather than sit unexplained beside a newer
// collection, and the caption says which of the two is on screen.
function applyView() {
  const analysed=view==='saved' && !!result;
  for(const node of document.querySelectorAll('[data-analysis]'))node.hidden=!analysed;
  const caption=analysed
    ?`Last completed analysis · ${friendly(result.metadata?.review_kind || 'historical')} · market date ${text(result.metadata?.as_of)} · generated ${result.metadata?.computed_at?when(result.metadata.computed_at):'time unavailable'} · run ${String(result.run_id || '').slice(0,12)}${analysisOperationNote()}`
    :result?'No completed analysis shown · current holdings above are the newest collection. Choose a saved run to see its analysis.'
    :'No completed analysis yet · Current holdings above are shown from the newest collection.';
  $('analysis-caption').textContent=caption;
  // Every analysed page repeats the same statement, so no page can display a month-old
  // run while the one selector above them says no analysis is shown.
  for(const node of document.querySelectorAll('[data-analysis-note]'))node.textContent=caption;
}
// The selector, the view and the sections on screen are one statement. A load that did
// not happen restores all three together rather than leaving them disagreeing.
function restoreRunSelection() {
  view=draft.baseRunId?'saved':CURRENT_VIEW;
  $('research-run').value=draft.baseRunId || CURRENT_VIEW;
  applyView();
}
function field(label,value,onChange,{type='text',options=null,percent=false,help='',wide=false}={}) {
  const wrapper=el('label',null,`field${wide?' wide':''}${type==='checkbox'?' check-field':''}`);
  const id=`research-field-${++fieldCounter}`; wrapper.htmlFor=id;
  let input;
  if (options) {
    input=el('select');
    for (const option of options) { const [key,name]=Array.isArray(option)?option:[option,friendly(option)]; const item=el('option',name); item.value=key; input.append(item); }
    input.value=value ?? '';
  } else if (type==='textarea') { input=el('textarea'); input.rows=3; input.value=value ?? ''; }
  else { input=el('input'); input.type=type; if(type==='checkbox') input.checked=value===true;
    else input.value=value===null || value===undefined ? '' : percent ? numeric(value)*100 : value;
    if(type==='number') input.step='any'; }
  input.id=id;
  if (type==='checkbox') wrapper.append(input,el('span',label)); else wrapper.append(el('span',label),input);
  if(help) wrapper.append(el('small',help,'field-help'));
  input.addEventListener(options || type==='checkbox' ? 'change' : 'input', () => {
    try {
      let next=type==='checkbox' ? input.checked : input.value;
      if(type==='number') { next=numberOrNull(next); if(percent && next!==null) next/=100; }
      onChange(next); input.removeAttribute('aria-invalid');
    } catch (failure) { input.setAttribute('aria-invalid','true'); error(failure.message); }
  }); return wrapper;
}
function form(fields) { const node=el('div',null,'form-grid'); node.append(...fields); return node; }
function table(rows, columns, caption='') {
  if (!rows?.length) return empty('No rows are available for this scope.','No data');
  const wrap=el('div',null,'table-wrap'), grid=el('table'), body=el('tbody'), head=el('thead'), heading=el('tr');
  let order=[...rows], sortedKey='', ascending=true;
  if(caption) grid.append(el('caption',caption));
  const draw=()=>{
    body.replaceChildren();
    for(const row of order) {
      const tr=el('tr');
      for(const column of columns) {
        const td=el('td',null,column.wrap?'wrap':'');
        const value=column.render ? column.render(row[column.key],row) : text(row[column.key]);
        if(value instanceof Node) td.append(value); else td.textContent=value;
        tr.append(td);
      } body.append(tr);
    }
    if(body.querySelector('input, select, textarea'))for(const control of heading.querySelectorAll('button'))control.disabled=true;
  };
  for(const column of columns) {
    const th=el('th'); th.scope='col';
    const control=button(column.label || friendly(column.key),()=>{
      ascending=sortedKey===column.key ? !ascending : true; sortedKey=column.key;
      order.sort((a,b)=>{
        const x=a[column.key],y=b[column.key];
        if(x===null || x===undefined) return y===null || y===undefined?0:1;
        if(y===null || y===undefined) return -1;
        const comparison=typeof x==='number' && typeof y==='number' ? x-y : String(x).localeCompare(String(y));
        return ascending?comparison:-comparison;
      });
      for(const cell of heading.children) cell.removeAttribute('aria-sort');
      th.setAttribute('aria-sort',ascending?'ascending':'descending'); draw();
    },'sort-button'); th.append(control);heading.append(th);
  }
  head.append(heading);grid.append(head,body);wrap.append(grid);draw();return wrap;
}
function autoTable(rows, preferred=null) {
  if(!Array.isArray(rows)) return kv(rows);
  const keys=preferred || [...new Set(rows.flatMap(row=>Object.keys(row || {})))];
  return table(rows,keys.map(key=>({key,wrap:typeof rows[0]?.[key]==='object' || /reason|issue/.test(key)})));
}
function barChart(rows,key,labelKey,caption) {
  const usable=(rows || []).filter(row=>numeric(row[key])!==null);
  if(!usable.length) return empty('No complete exposure values are available.');
  const node=el('div',null,'bar-chart'), maximum=Math.max(...usable.map(row=>Math.abs(numeric(row[key]))),.01);
  for(const row of usable.slice(0,10)) {
    const item=el('div',null,'bar-row'), track=el('div',null,'bar-track'), fill=el('span',null,'bar-fill');
    fill.style.width=`${Math.abs(numeric(row[key]))/maximum*100}%`; track.append(fill);
    const label=el('span',text(row[labelKey]),'bar-label'); label.title=text(row[labelKey]);
    item.append(label,track,el('span',pct(row[key]),'bar-value'));node.append(item);
  }
  if(caption)node.append(caption instanceof Node?caption:el('p',caption,'chart-note'));
  return node;
}
function lineChart(points,caption) {
  const valid=(points || []).filter(point=>numeric(point.value)!==null);
  if(valid.length<2) return empty('At least two valid dated observations are required.','History unavailable');
  const container=el('div'), svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox','0 0 600 160');svg.setAttribute('class','line-chart');svg.setAttribute('role','img');svg.setAttribute('aria-label',caption);
  const title=document.createElementNS(svg.namespaceURI,'title');title.textContent=caption;svg.append(title);
  const values=valid.map(point=>numeric(point.value)), low=Math.min(...values), high=Math.max(...values), span=high-low || 1;
  for(const y of [20,70,120]) { const line=document.createElementNS(svg.namespaceURI,'line');for(const [key,value] of Object.entries({x1:0,x2:600,y1:y,y2:y,stroke:'#e6ece0','stroke-width':1}))line.setAttribute(key,value);svg.append(line); }
  const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',values.map((value,index)=>`${index?'L':'M'}${index/(values.length-1)*600},${135-(value-low)/span*115}`).join(' '));
  path.setAttribute('fill','none');path.setAttribute('stroke','#5d8550');path.setAttribute('stroke-width','2.5');svg.append(path);
  const axis=el('div',null,'chart-axis');axis.append(el('span',valid[0].date),el('span',`Observed range ${num(low,3)}–${num(high,3)}`),el('span',valid.at(-1).date));container.append(svg,axis);return container;
}
function jsonEditor(id,label,value,onApply,key=id) {
  const node=el('div',null,'json-editor'), labelNode=el('label',label), textarea=el('textarea');
  textarea.id=`json-${key.replace(/[^a-z0-9-]/gi,'-')}`;labelNode.htmlFor=textarea.id;
  textarea.value=draft.rawEditors[key] ?? JSON.stringify(value,null,2);textarea.spellcheck=false;
  textarea.addEventListener('input',()=>dirty({rawEditors:{...draft.rawEditors,[key]:textarea.value}}));
  node.append(labelNode,textarea,button('Apply JSON to draft',()=>{
    const parsed=JSON.parse(textarea.value);
    if(parsed===null || typeof parsed!=='object') throw new Error('Enter a JSON object or array.');
    onApply(parsed);
    const raw={...draft.rawEditors};delete raw[key];draft={...draft,rawEditors:raw};persist();renderAll();announce('JSON applied. Inspect resolved changes, then recalculate.');
  }),button('Discard JSON edit',()=>{
    const raw={...draft.rawEditors};delete raw[key];draft={...draft,rawEditors:raw};persist();renderAll();
  },'quiet'));
  replace(id,node);
}
function navigate(next) {
  if(!pages.includes(next)) return;
  const moved=page!==next;
  page=next;
  for(const name of pages) $(`page-${name}`).hidden=name!==next;
  document.querySelectorAll('[data-page]').forEach(node=>{if(node.dataset.page===next)node.setAttribute('aria-current','page');else node.removeAttribute('aria-current');});
  $('page-title').textContent=friendly(next);history.replaceState(null,'',`#${next}`);
  // A view swapped in under a kept scroll offset opens mid-page — on the long views that
  // is past the heading and the controls entirely. Moving focus to the workspace both
  // puts the new view at its top and announces the change to a screen reader; main
  // already carries tabindex="-1" for the skip link, so no markup is added for it.
  if(!moved) return;
  window.scrollTo({top:0});
  $('workspace-main').focus({preventScroll:true});
}
function renderState(writeStatus=false) {
  const unapplied=Object.keys(draft.rawEditors || {}).length;
  const state=busy?'calculating':unapplied?'dirty':previewReady(draft)?'preview':draft.dirty?'dirty':draft.baseRunId?'saved':'empty';
  $('draft-state').textContent=friendly(state);$('draft-state').className=`badge ${state}`;
  $('recalculate').disabled=busy || !draft.baseRunId || !!unapplied;
  $('recalculate').title=!draft.baseRunId?'Run Update & analyze to retain base inputs.':unapplied?'Apply or discard advanced JSON edits first.':'';
  $('save-run').disabled=busy || !previewReady(draft) || !!unapplied;
  $('save-run').title=previewReady(draft)?'Save this calculation as an immutable child run.':'Recalculate the current draft before saving.';
  // An operation is authorized with the local token, so nothing that starts one may be
  // offered before the page holds it: a click made first is refused by the service, not
  // queued, and the owner is told to reload a page that was merely still loading.
  // A load that never finished and a load that failed are different states: the first
  // ends on its own, the second never will, so the tooltip may not claim a load is in
  // progress under a control that will stay dead until the page is reloaded.
  const waiting=!token?(bootstrap==='failed'?'The local research service did not load. Reload this page to try again.':'Loading the local research service…'):'';
  $('update-analyze').disabled=!token || busy || workflowBusy || !!unapplied;
  $('update-analyze').title=waiting || (unapplied?'Apply or discard advanced JSON edits first.':workflowBusy?'This operation is already running.':'Collect the newest holdings, fetch provider data and save one review.');
  $('cancel-workflow').disabled=!workflowRecord || !liveWorkflow(workflowRecord.status) || !!workflowRecord.cancel_requested;
  $('monthly-review').disabled=!token || busy || workflowBusy || !!unapplied;
  $('refresh-research').disabled=!token || busy || workflowBusy || !!unapplied;
  $('monthly-review').title=waiting;$('refresh-research').title=waiting;
  // Every remaining control that writes through the local token is settled here too. The
  // service refuses a token-less write with "Reload the local app before making changes."
  // rather than queueing it, so offering the click at all only produces that refusal.
  for(const id of ['save-settings','save-supplemental','import-csv','evaluate-run']) {
    $(id).disabled=!token || busy;
    $(id).title=waiting;
  }
  // Saving settings writes the resolved draft, so an unapplied JSON edit blocks it the
  // way it blocks a calculation: what would be written is not what the page is showing.
  $('save-settings').disabled=!token || busy || !!unapplied;
  $('reset-draft').disabled=busy || !draft.dirty;
  // Recording a decision needs a loaded run and a finished calculation, so it is settled
  // here with the other controls: a run loaded while busy re-enables it when busy clears.
  $('save-decision').disabled=busy || !draft.baseRunId;
  if(!busy && writeStatus) announce(unapplied?'Advanced JSON has unapplied edits. Apply or discard them before calculating.':draft.dirty && !previewReady(draft)?'Draft changed · displayed results are out of date until recalculated.':previewReady(draft)?'Preview calculated · save a new run to retain this result.':draft.baseRunId?'Saved inputs · display filters leave the analytical scope unchanged.':'Run Update & analyze to create the first retained input set.');
}
function renderDiff() {
  const changes=differences(draft.baseConfig,resolved());
  replace('resolved-diff',changes.length?table(changes,[{key:'field'},{key:'before',wrap:true},{key:'after',wrap:true}]):el('p','No configuration changes in this draft.','muted'));
}
// Current holdings: the newest collection, dated by server receipt time. This view never
// depends on a saved review or reconciles NAV; a holding is quoted in the currency its
// listed exchange states, its own account says what its values are reported in, and USD
// amounts appear only where that and a dated FX observation are both known, beside the
// captured amounts.
const collectionLabels={published:'Published',legacy_partial:'Legacy partial',none:'None'};
const collectionClasses={published:'complete',legacy_partial:'warning',none:''};
const completenessLabels={'count-verified':['Count verified','complete'],'end-observed':['End observed',''],unverified:['Unverified','warning']};
const listOf = value => Array.isArray(value) ? value : [];
const objectRows = value => listOf(value).filter(row => row && typeof row === 'object');
const resolvedIdentity = new Set(['mapped','resolved','resolved_from_display','resolved_by_search']);
// Keyed records carry a resolution and are answered in the Exceptions panel, not listed twice.
const resolvable = record => typeof record?.key === 'string' && !!record.key && !!record?.resolution;
const belongsTo = (record, row) => record?.account_id ? record.account_id === row?.account_id : record?.source_id != null && record.source_id === row?.source_id;
function collectionBadge(label,className='') {
  const node=$('current-collection-state');node.textContent=label;node.className=`badge ${className}`.trim();
}
function completenessBadge(value) {
  const [label,className]=completenessLabels[value] || [value ? friendly(value) : 'Completeness unknown',value ? '' : 'warning'];
  return el('span',label,`badge ${className}`.trim());
}
// How a holding's value currency was established, said in one word beside the currency so
// an assumption never reads like a label. This is not a conversion: a holding converted at
// a dated rate names its FX pair and observation date in its own column.
const valueBasisWords={row:'labelled by source',attested:'stated by you',account:'account currency',presentation:'read as reported'};
// The covered-USD total on this panel rests on how each value currency was established,
// so the issues that qualify it are shown ahead of the cap rather than behind it: a
// warning about the basis of a number belongs on the same screen as the number.
const currencyBasisCodes=new Set(['VALUE_ARITHMETIC_MISMATCH','VALUE_CURRENCY_UNCHECKED','INFERRED_ACCOUNT_CURRENCY','INFERRED_CASH_CURRENCY']);
// How many of the remaining collection issues are listed outright; the rest stay on the
// page inside a disclosure, because a count of discarded warnings is not a report.
const CURRENT_ISSUE_LIMIT=8;
function valueCurrencyCell(row) {
  const currency=row?.reported_currency ?? row?.value_currency;
  if(currency===null || currency===undefined || currency==='')return '\u2014';
  const word=valueBasisWords[row?.value_currency_basis];
  return word?`${currency} \u00b7 ${word}`:String(currency);
}
// The standing statement of that basis for a whole account or collection. It replaces the
// error the collector used to raise about an unlabeled row, which named nothing an owner
// could answer; an account that labels no currency is ordinary, not broken.
function valueCurrencyLine(summary,className='account-meta') {
  const label=typeof summary?.label==='string'?summary.label:'';
  if(!label)return null;
  const node=el('div',label,`${className}${summary?.basis==='presentation'?' assumed':''}`.trim());
  node.title='How this value currency was established. Amounts converted at a dated FX observation name their pair and date separately.';
  return node;
}
function subtotalLine(line) {
  const missing=numeric(line?.missing_value_count) || 0;
  const parts=[`Holdings ${num(line?.holdings)}`,`Cash ${num(line?.cash)}`,line?.currency || 'unlabeled'];
  if(missing)parts.push(`${missing} without a value`);
  const node=el('div',parts.join(' · '));
  // 'unlabeled' means the source page carried no currency column, not that the currency is
  // unknown: each holding is still valued in the currency its listed exchange quotes.
  node.title='Captured exactly as collected, grouped by the currency the source labelled; no FX conversion.';
  return node;
}
// A covered subtotal is only shown when something was actually converted: an account
// whose holdings all stayed unconverted is not worth "USD 0".
function usdCoverage(row,held) {
  const covered=numeric(row?.usd?.covered_position_count);
  const unconverted=numeric(row?.usd?.unconverted_position_count) ?? held.filter(position=>position?.market_value_usd==null).length;
  const cash=row?.usd?.cash ?? row?.cash_usd;
  const converted=covered===null?held.filter(position=>position?.market_value_usd!=null).length:covered;
  return {converted,unconverted,hasCash:numeric(cash)!==null,complete:row?.usd?.fully_covered!==false};
}
function accountUsdLine(row,held) {
  const {converted,unconverted,hasCash,complete}=usdCoverage(row,held);
  const reported=[row?.usd?.total,row?.usd?.covered_total,row?.usd_total,row?.total_value_usd].map(numeric).find(value=>value!==null);
  const total=reported!==undefined?reported:(() => {
    const values=held.map(position=>numeric(position?.market_value_usd)).filter(value=>value!==null);
    return values.length?values.reduce((sum,value)=>sum+value,0)+(numeric(row?.cash_usd) ?? 0):null;
  })();
  if(total===null || (!converted && !hasCash))return el('div','USD unavailable · nothing converted','account-usd muted');
  const note=complete?'':` · ${unconverted || 'some'} unconverted`;
  return el('div',`USD ${num(total)}${note}`,`account-usd${complete?'':' partial'}`);
}
function identityLine(row,held,records) {
  const counts=row?.identity && typeof row.identity==='object'?row.identity:null;
  const resolved=numeric(counts?.resolved) ?? held.filter(position=>resolvedIdentity.has(position?.resolution_status)).length;
  const open=numeric(counts?.open) ?? records.filter(record=>resolvable(record) && belongsTo(record,row)).length;
  return el('div',`identity: ${resolved} resolved / ${open} open`,`account-meta${open?' has-exceptions':''}`);
}
function accountCard(row, context={}) {
  const node=el('article',null,'account-card'), heading=el('div',null,'account-card-heading');
  heading.append(el('strong',row?.name || row?.account_id || 'Account'),completenessBadge(row?.completeness));
  const positions=numeric(row?.position_count);
  const captured=`Captured ${when(row?.captured_at)}${positions===null?'':` · ${positions} ${positions===1?'position':'positions'}`}`;
  node.append(heading,el('div',captured,'account-meta'));
  const subtotals=el('div',null,'account-subtotals'), lines=listOf(row?.subtotals);
  for(const line of lines)subtotals.append(subtotalLine(line));
  if(!lines.length)subtotals.append(el('div','No captured subtotal','muted'));
  const held=objectRows(context.positions).filter(position=>position.account_id===row?.account_id);
  const usdLine=accountUsdLine(row,held);if(usdLine)subtotals.append(usdLine);
  node.append(subtotals);
  const valueLine=valueCurrencyLine(row?.value_currency);if(valueLine)node.append(valueLine);
  if(context.identity)node.append(identityLine(row,held,objectRows(context.exceptions)));
  if(row?.value_basis==='attested')node.append(el('div',`Attested valuation ${row.valuation_date || 'date unavailable'}`,'account-meta'));
  const exceptions=listOf(row?.exceptions).length;
  node.append(el('div',exceptions?`${exceptions} ${exceptions===1?'exception':'exceptions'}`:'No exceptions',`account-meta${exceptions?' has-exceptions':''}`));
  return node;
}
function renderAccountCards() {
  const snapshot=current?.current || {}, accounts=objectRows(snapshot.accounts);
  // Open exceptions come with the snapshot and from the exceptions list, whichever loaded.
  const open=new Map([...objectRows(snapshot.exceptions),...objectRows(exceptionState?.exceptions)].filter(resolvable).map(record=>[record.key,record]));
  const context={positions:snapshot.positions,exceptions:[...open.values()],identity:!!snapshot.identity && typeof snapshot.identity==='object'};
  replace('current-accounts',...(accounts.length?accounts.map(row=>accountCard(row,context)):[empty('Pull holdings to capture the newest collection.','No captured accounts')]));
}
function renderCurrent() {
  const containers=['current-value-currency','current-totals','current-accounts','current-positions','current-exceptions'];
  if(!current){collectionBadge('Loading…');$('current-dates').textContent='Loading the newest collection…';for(const id of containers)replace(id);return;}
  $('exception-panel').hidden=!current.supported;
  if(!current.supported){collectionBadge('Unavailable');$('current-dates').textContent=current.reason || 'Current holdings are unavailable for this source.';for(const id of containers)replace(id);return;}
  const snapshot=current.current || {}, dates=snapshot.dates || {}, collection=snapshot.collection || {};
  const accounts=objectRows(snapshot.accounts), positions=objectRows(snapshot.positions);
  const status=collection.status || 'none';
  collectionBadge(collectionLabels[status] || friendly(status),collectionClasses[status] ?? '');
  $('current-dates').textContent=`Collected ${when(dates.collection_received_at)} · Source valuation time ${dates.source_valuation_time || 'unknown — captured values shown as observed'} · Market observation date ${dates.market_observation_date || 'unavailable'} · Generated ${when(dates.generated_at)}`;
  const totals=objectRows(snapshot.totals?.by_currency), usd=snapshot.totals?.usd && typeof snapshot.totals.usd==='object'?snapshot.totals.usd:null;
  const covered=usd?metric('Covered value (USD)',num(usd.covered_total),`${numeric(usd.covered_position_count) ?? 0} positions · ${numeric(usd.unconverted_position_count) ?? 0} unconverted · not reconciled NAV`):null;
  if(covered)covered.title=typeof usd.label==='string'?usd.label:'USD presentation of covered amounts using dated FX observations; not reconciled NAV';
  replace('current-value-currency',valueCurrencyLine(usd?.value_currency,'muted value-currency-basis'));
  replace('current-totals',covered,...(totals.length?totals.map(row=>metric(`Captured subtotal (${row?.currency || 'unlabeled'})`,num(row?.total),`${row?.account_count ?? 0} ${row?.account_count===1?'account':'accounts'} · not reconciled NAV`)):[el('p','No captured subtotals in the newest collection.','muted')]));
  renderAccountCards();
  const names=new Map(accounts.map(row=>[row?.account_id,row?.name || row?.account_id]));
  replace('current-positions',table(positions,[
    {key:'symbol'},{key:'name',wrap:true},{key:'quantity',render:value=>num(value,4)},{key:'price',render:value=>num(value)},
    {key:'market_value',label:'Market value',render:value=>num(value)},{key:'currency'},
    {key:'quote_currency',label:'Quote currency'},{key:'reported_currency',label:'Value currency',render:(_value,row)=>valueCurrencyCell(row)},
    {key:'market_value_usd',label:'USD value',render:value=>num(value)},
    {key:'fx_pair',label:'FX (pair · date)',render:(value,row)=>value?`${value} · ${row?.fx_observation_date || 'undated'}`:'—'},
    {key:'account_id',label:'Account',render:value=>names.get(value) || text(value)},{key:'observed_at',label:'Observed',render:value=>when(value)},
  ],`${positions.length} captured positions · values as observed at capture · USD only with a dated FX observation`));
  const issues=listOf(snapshot.exceptions).filter(issue=>['error','warning'].includes(issue?.severity) && !resolvable(issue));
  const qualifying=issues.filter(issue=>currencyBasisCodes.has(issue?.code));
  const rest=issues.filter(issue=>!currencyBasisCodes.has(issue?.code));
  const shown=[...qualifying,...rest.slice(0,CURRENT_ISSUE_LIMIT)];
  const overflow=rest.slice(CURRENT_ISSUE_LIMIT);
  replace('current-exceptions',
    collection.newer_collection_in_progress?el('p','A newer collection is still in progress; the newest complete collection is shown until it publishes.','current-progress'):null,
    collection.newer_collection_failed?el('p','A newer collection did not complete; the newest complete collection is shown.','current-alert'):null,
    listIssues(shown,'No collection warnings or errors.'),
    overflow.length?disclosure(`+${overflow.length} more`,listIssues(overflow)):null);
}
async function loadCurrent() {
  const request=++currentRequest;
  let next;
  try { next=await api('/api/research/current'); }
  catch(failure){ next={supported:false,reason:`Current holdings could not be loaded. ${failure?.message || ''}`.trim()}; }
  if(request!==currentRequest)return;
  current=next && typeof next==='object'?next:{supported:false,reason:'The current holdings response was unreadable.'};
  try { renderCurrent(); }
  catch(failure){ current={supported:false,reason:`Current holdings could not be displayed: ${failure?.message || failure}`}; try { renderCurrent(); } catch { /* the panel keeps its last state */ } }
}
// Exceptions: only the questions listing identity and FX normalization could not answer.
// A holding's currency is never one of them: the exchange its listing trades on states it.
// Each saved answer becomes dated supplemental evidence; holdings and the open list then
// reload from the server, which alone decides whether the exception is closed.
const exceptionLabels={AMBIGUOUS_LISTING:'Ambiguous listing',UNRESOLVED_LISTING:'Listing not found',STALE_FX:'Stale FX observation',MISSING_FX:'Missing FX observation'};
const noOpenExceptions='No open exceptions. Listing identity and currencies were established from the source, its exchange and provider metadata.';
function exactAmount(value,label) {
  const trimmed=String(value ?? '').trim().replaceAll(',','');
  if(!trimmed)return null;
  if(!/^\d+(\.\d+)?$/.test(trimmed))throw new Error(`${label} must be a non-negative amount such as 1250.40, or blank when unknown.`);
  return trimmed;
}
function resolutionFields(record) { return listOf(record?.resolution?.fields).filter(key=>typeof key==='string' && key); }
function allowedValues(record,values) {
  const fields=resolutionFields(record);
  return fields.length?Object.fromEntries(Object.entries(values).filter(([key])=>fields.includes(key))):values;
}
function exceptionTitle(record) {
  const accounts=objectRows(current?.current?.accounts), account=accounts.find(row=>belongsTo(record,row));
  const label=exceptionLabels[record?.code] || friendly(String(record?.code || 'exception').toLowerCase());
  return [label,record?.raw_symbol || record?.pair,account?.name].filter(Boolean).join(' · ');
}
function candidateLabel(candidate) { return [candidate.symbol,candidate.name,candidate.exchange].filter(Boolean).join(' · '); }
function listingControls(record) {
  const candidates=objectRows(record?.candidates).filter(row=>typeof row.symbol==='string' && row.symbol);
  let chosen=null, exact='';
  const controls=[];
  if(candidates.length){
    const group=el('fieldset',null,'candidate-list wide'), name=`listing-choice-${++fieldCounter}`;
    group.append(el('legend','Matching listings'));
    for(const candidate of candidates){
      const option=el('label',null,'candidate-option'), input=el('input');
      input.type='radio';input.name=name;input.value=candidate.symbol;input.id=`${name}-${group.children.length}`;option.htmlFor=input.id;
      input.addEventListener('change',()=>{if(input.checked)chosen=candidate;});
      option.append(input,el('span',candidateLabel(candidate)));group.append(option);
    }
    controls.push(group);
  }
  controls.push(field(candidates.length?'Or the exact quote symbol':'Exact quote symbol','',value=>{exact=value;},{help:'The Yahoo quote symbol, for example RY.TO.'}));
  return {controls,collect:()=>{
    const typed=exact.trim().toUpperCase();
    if(typed){
      if(!/^[A-Z0-9][A-Z0-9.\-=^]{0,19}$/.test(typed))throw new Error('Enter an exact quote symbol such as RY.TO.');
      return {security_id:typed,ticker:typed};
    }
    if(!chosen)throw new Error('Choose one of the matching listings or enter the exact quote symbol.');
    return {security_id:chosen.symbol,ticker:chosen.symbol,...(chosen.exchange?{exchange:chosen.exchange}:{}),...(chosen.instrument_type?{instrument_type:chosen.instrument_type}:{})};
  }};
}
function factsControls(record) {
  const proposed=record?.proposed && typeof record.proposed==='object'?record.proposed:{};
  let facts={valuation_date:proposed.valuation_date ?? '',cash:proposed.cash ?? '',total_value:proposed.total_value ?? '',complete:proposed.complete===true};
  const update=key=>value=>{facts={...facts,[key]:value};};
  return {controls:[
    field('Valuation date',facts.valuation_date,update('valuation_date'),{type:'date'}),
    field('Cash',facts.cash,update('cash'),{help:'Exact amount in the account currency; blank when unknown.'}),
    field('Account NAV',facts.total_value,update('total_value'),{help:'Exact total value in the account currency; blank when unknown.'}),
    field('Holdings list is complete',facts.complete,update('complete'),{type:'checkbox'}),
  ],collect:()=>{
    const date=String(facts.valuation_date || '').trim();
    if(date && !/^\d{4}-\d{2}-\d{2}$/.test(date))throw new Error('Enter the valuation date as YYYY-MM-DD.');
    return {valuation_date:date || null,cash:exactAmount(facts.cash,'Cash'),total_value:exactAmount(facts.total_value,'Account NAV'),complete:facts.complete===true};
  }};
}
const exceptionControls={security_listing:listingControls,account_facts:factsControls};
async function saveResolution(record,values) {
  let response;
  try {
    response=await api('/api/research/resolutions',{key:record.key,kind:record.resolution?.kind,source_id:record.source_id ?? null,snapshot_id:record.snapshot_id ?? null,values});
  } finally {
    // A refused answer usually means the list moved on; reload it either way.
    await loadCurrent();await loadExceptions();
  }
  const remaining=numeric(response?.remaining);
  announce(`Answer saved as supplemental evidence.${remaining===null?'':` ${remaining} ${remaining===1?'exception remains':'exceptions remain'} open.`}`);
}
function exceptionCard(record) {
  const kind=record.resolution?.kind || '', node=el('form',null,'exception-form'), heading=el('div',null,'exception-heading');
  node.noValidate=true;node.dataset.kind=kind;
  node.addEventListener('submit',event=>event.preventDefault());
  const [flag,flagClass]=kind==='fx_manual'?['Refresh needed','warning']:record.severity==='warning'?['Warning','warning']:['Needs an answer','blocked'];
  heading.append(el('strong',exceptionTitle(record)),el('span',flag,`badge ${flagClass}`));
  node.append(heading,el('p',record.message || 'This exception needs an explicit answer.','exception-message'));
  const builder=exceptionControls[kind];
  if(!builder){
    node.append(el('p',kind==='fx_manual'?'Refresh market data to load a dated FX observation.':'This exception is answered through supplemental inputs on the Data page.','exception-note'));
    return node;
  }
  const {controls,collect}=builder(record), save=button('Save',()=>saveResolution(record,allowedValues(record,collect())),'');
  save.type='submit';
  node.append(form([...controls,save]));
  return node;
}
function renderExceptionForms() {
  const count=$('exception-count');
  const status=(label,className='')=>{count.textContent=label;count.className=`badge ${className}`.trim();};
  if(!exceptionState){status('Loading…');replace('current-exception-forms',el('p','Loading open exceptions…','muted'));return;}
  if(exceptionState.unavailable){status('Unavailable','warning');replace('current-exception-forms',el('p',exceptionState.unavailable,'muted'));return;}
  const records=objectRows(exceptionState.exceptions).filter(resolvable);
  status(records.length?`${records.length} open`:'None open',records.length?'warning':'complete');
  replace('current-exception-forms',...(records.length?records.map(exceptionCard):[el('p',noOpenExceptions,'exception-empty')]));
}
async function loadExceptions() {
  const request=++exceptionRequest;
  let next;
  if(current && !current.supported)next={exceptions:[]};
  else {
    try {
      const response=await api('/api/research/exceptions');
      next=response?.supported===false?{unavailable:response.reason || 'Exceptions are unavailable for this source.'}
        :Array.isArray(response?.exceptions)?response:{unavailable:'The open exceptions response was unreadable.'};
    } catch(failure){ next={unavailable:`Open exceptions could not be loaded. ${failure?.message || ''}`.trim()}; }
  }
  if(request!==exceptionRequest)return;
  exceptionState=next;
  try { renderExceptionForms(); if(current?.supported)renderAccountCards(); }
  catch(failure){ exceptionState={unavailable:`Open exceptions could not be displayed: ${failure?.message || failure}`}; try { renderExceptionForms(); } catch { /* the panel keeps its last state */ } }
}
function renderOverview() {
  const summary=result?.summary || {}, risk=result?.risk || {}, currency=summary.currency;
  replace('overview-metrics',metric('Portfolio NAV',money(summary.total_value,currency),'Declared account NAV'),
    metric('Explicit cash',summary.unknown_cash_accounts?.length?'Incomplete':money(summary.known_cash,currency),summary.unknown_cash_accounts?.length?`${summary.unknown_cash_accounts.length} accounts need cash evidence`:'Cash supplied with the snapshot'),
    metric('Coverage',pct(summary.coverage),summary.complete?'Accounts reconciled':'Unresolved amounts remain visible'),
    metric('Unclassified',money(summary.unclassified_value,currency),'Not assumed to be cash'));
  $('overview-caption').textContent=result?`${text(result.metadata?.valuation_date)} · ${summary.position_count ?? '—'} positions · ${friendly(result.metadata?.scope)}`:'Run a review to reconcile completed holdings and available research inputs.';
  $('exposure-state').replaceChildren(badge(result?.exposure_status));
  replace('issuer-chart',barChart(result?.issuer_exposure,'weight','issuer_id',null),
    chartCaption({units:'Share of portfolio NAV (%)',date:observationDate(),scope:'Top issuers; direct holdings and fund look-through',coverage:readinessNote('exposure')}));
  replace('sector-chart',barChart(result?.sector_exposure,'weight','sector',null),
    chartCaption({units:'Share of portfolio NAV (%)',date:observationDate(),scope:'Additive sector weights; unknown exposure retained',coverage:readinessNote('exposure')}));
  renderWhatChanged();renderPortfolioRisks();renderPriorityLists();renderOverlap();
  replace('risk-metrics',metric('Volatility',pct(risk.annualized_volatility),'Annualized'),metric('Beta',num(risk.beta),'Approved benchmark'),metric('Tracking error',pct(risk.tracking_error),'Annualized'));
  replace('risk-history',lineChart(risk.history,risk.history_label || 'Hypothetical historical risk illustration'),
    chartCaption({units:'Rebased weekly index (hypothetical)',date:observationDate(),scope:risk.history_label || 'Constant-weight weekly replay',coverage:readinessNote('risk')}));
  $('risk-caption').textContent=`${risk.history_label || 'History unavailable'} · ${risk.observations ?? 0} common observations · ${friendly(risk.status)}`;
  replace('risk-detail',kv({status:risk.status,observations:risk.observations,max_drawdown:pct(risk.max_drawdown),volatility_interval:risk.volatility_interval}),
    autoTable(risk.risk_contributions || []),autoTable(risk.stresses || []),listIssues(risk.issues),
    disclosure('Five-year risk sensitivity',kv(risk.five_year_sensitivity || {status:'Unavailable; five-year history coverage is required.'})),
    disclosure('Benchmark history illustration',lineChart((risk.history || []).map(row=>({date:row.date,value:row.benchmark})),'Benchmark total-return history illustration'),
      chartCaption({units:'Rebased weekly benchmark index (hypothetical)',date:observationDate(),
        scope:`${resolved().mandate?.benchmark_id || 'Approved benchmark'} · constant-weight weekly replay`,
        coverage:readinessNote('risk')})));
  replace('review-priorities',result?listIssues(result.issues?.slice(0,8),'No blocking data issues reported. Review the assumptions before considering changes.'):empty());
  const macros=(result?.macro || []).map(row=>{
    const node=el('article',null,'macro-card');node.append(metric(row.series_id,num(row.latest_value),`${row.units || 'Units unavailable'} · ${row.observation_date || 'Undated'}`),lineChart(row.history,`${row.series_id} observed history`),
      chartCaption({units:row.units || 'Units unavailable',date:row.observation_date || observationDate(),scope:`Vintage ${row.vintage_date || 'unknown'} · context only`,coverage:`coverage ${(row.history || []).length} observations`}));return node;
  });replace('macro-panels',...(macros.length?macros:[empty('Import dated macro vintages or configure a provider.','Macro observations unavailable')]));
  applyView();
}
// What changed since the comparable previous review: the decision that was recorded
// then, the few balances this run can compare, and each security's own dated brief. A
// balance is never restated as a return, because flows between reviews are unknown here.
function decisionInsight() {
  const record=service.last_decision;
  if(!record || typeof record!=='object')return [];
  const alternative=record.selected_alternative || record.candidate_id;
  return [{metric:'Last recorded decision',comparison:`${friendly(record.action)}${alternative?` · ${alternative}`:' · no alternative chosen'}`,
    source_run:`run ${String(record.run_id || '').slice(0,12)} · ${record.as_of || 'undated'}`,rationale:record.rationale}];
}
function summaryComparison() {
  const now=result?.summary || {}, earlier=previousRun?.result?.summary;
  if(!earlier)return [];
  const label=`run ${String(previousRun.run_id).slice(0,12)} · ${previousRun.result?.metadata?.as_of || 'undated'}`;
  return [
    {metric:'Portfolio NAV',comparison:`${money(earlier.total_value,earlier.currency)} → ${money(now.total_value,now.currency)}`,source_run:label,
      rationale:'Balance change only: contributions, withdrawals and other flows between the two reviews are unknown, so this is not a return.'},
    {metric:'Reconciled coverage',comparison:`${pct(earlier.coverage)} → ${pct(now.coverage)}`,source_run:label,rationale:'Share of account value each review reconciled.'},
    {metric:'Captured positions',comparison:`${text(earlier.position_count)} → ${text(now.position_count)}`,source_run:label,rationale:'Positions captured in each review.'},
  ];
}
function briefChanges() {
  const rows=[];
  for(const [sid,record] of Object.entries(result?.research || {})) {
    const brief=record?.brief;
    if(!brief)continue;
    for(const bullet of brief.changes_since_previous_review || [])
      rows.push({metric:brief.name || sid,comparison:bullet?.text || text(bullet),
        source_run:`as of ${brief.as_of || 'undated'}`,rationale:(bullet?.source_ids || []).join(', ')});
  }
  return rows;
}
function renderWhatChanged() {
  const rows=result?[...decisionInsight(),...summaryComparison(),...briefChanges()]:[];
  const shown=rows.slice(0,12);
  $('what-changed-state').textContent=rows.length?`${rows.length} stated`:'Nothing stated';
  replace('what-changed',
    ...(shown.length?shown.map(insight):[result?empty('No recorded decision, comparable earlier review or dated brief change.','Nothing this review can evidence'):empty()]),
    rows.length>shown.length?el('p',`+${rows.length-shown.length} more`,'muted'):null,
    result?el('p',previousRun
      ?`Compared with the previous ${friendly(result.metadata?.review_kind || 'review').toLowerCase()} review, run ${String(previousRun.run_id).slice(0,12)}.`
      :'No earlier review of this kind is retained; only this review’s own dated changes are listed.','muted'):null);
}
// Concentration is stated against the cap the owner confirmed, never against a default.
function concentrationRows() {
  const cap=numeric(resolved().mandate?.issuer_cap);
  return (result?.issuer_exposure || []).slice(0,12).map(row=>({issuer_id:row.issuer_id,weight:row.weight,
    issuer_cap:cap,over_cap:cap!==null && numeric(row.weight)!==null?numeric(row.weight)-cap:null}));
}
function renderPortfolioRisks() {
  const risk=result?.risk || {};
  $('risk-state').replaceChildren(badge(risk.status));
  replace('risk-contribution-chart',barChart(risk.risk_contributions,'contribution','security_id',null),
    chartCaption({units:'Contribution to annualized volatility (%)',date:observationDate(),
      scope:numeric(risk.denominator)!==null?`Covered holdings · denominator ${money(risk.denominator)}`:risk.scope || 'Reconciled NAV weights',
      coverage:readinessNote('risk')}));
  replace('stress-table',table(risk.stresses || [],[{key:'scenario',render:value=>friendly(value)},
    {key:'return',label:'Portfolio return',render:pct},{key:'status',render:badge},{key:'basis',wrap:true}],
    'Mechanical shocks the owner stated; not probabilities and not forecasts.'));
  replace('concentration-table',table(concentrationRows(),[{key:'issuer_id',label:'Issuer'},
    {key:'weight',label:'Household weight',render:pct},{key:'issuer_cap',label:'Confirmed cap',render:pct},
    {key:'over_cap',label:'Over the cap',render:value=>numeric(value)===null?'—':numeric(value)>0?pct(value):'—'}],
    resolved().mandate?.issuer_cap?'Household issuer weights against the confirmed issuer cap.':'No issuer cap is confirmed; weights are shown without a limit.'));
}
// This month's reading list: each row says which rules named the security and opens the
// evidence behind them. Nothing here is a recommendation.
function priorityRow(row) {
  const node=button('',()=>showSecurity(row.security_id),'priority-row');
  const reasons=(row.reasons || []).map(reason=>friendly(reason.rule)).join(' · ');
  node.append(el('strong',`${row.security_id}${row.name?` · ${row.name}`:''}`,'priority-name'),
    el('span',reasons || 'No rule stated','priority-reasons'),
    el('small',[row.evidence?.sector,numeric(row.current_weight)!==null?`weight ${pct(row.current_weight)}`:null,
      numeric(row.issuer_weight)!==null?`issuer ${pct(row.issuer_weight)}`:null].filter(Boolean).join(' · '),'priority-note'));
  return node;
}
function renderPriorityLists() {
  const priorities=result?.priorities || {};
  for(const [id,key,fallback] of [['holdings-review','holdings_to_review','No holding collected a stated reason this review.'],
    ['new-candidates','new_candidates','No unowned company collected a stated reason this review.']]) {
    const rows=(priorities[key] || []).slice(0,10);
    replace(id,...(rows.length?rows.map(priorityRow):[result?empty(fallback,'Nothing to review here'):empty()]),
      result?el('p',priorities.rules?`${(priorities[key] || []).length} listed · ordered by how many independent reasons each collected`:'Automatic research did not run for this review; this list is empty rather than guessed.','muted'):null);
  }
}
// Where the same issuer is held twice: directly and inside a fund. The residual a fund
// did not disclose stays visible as unclassified rather than being spread silently.
function overlapRows() {
  const rows=(result?.issuer_exposure || []).filter(row=>numeric(row.indirect_value)>0 || (row.sources || []).length>1)
    .slice(0,12).map(row=>({issuer_id:row.issuer_id,sector:row.sector,direct_value:row.direct_value,
      indirect_value:row.indirect_value,funds:(row.sources || []).join(', ')}));
  const residual=numeric(result?.summary?.unclassified_value);
  if(residual)rows.push({issuer_id:'Unclassified residual',sector:'Unknown',direct_value:null,
    indirect_value:residual,funds:'Exposure no fund disclosed; never spread across the known issuers'});
  return rows;
}
function fundSectorBars() {
  const rows=(result?.fund_sectors || []).filter(row=>row && typeof row==='object');
  const funds=[...new Set(rows.map(row=>row.fund_id))];
  return funds.map(fund=>{
    const node=el('section',null,'fund-sectors');
    node.append(el('h3',`${fund} disclosed sectors`),
      barChart(rows.filter(row=>row.fund_id===fund),'weight','sector',null));
    return node;
  });
}
function renderOverlap() {
  const unclassified=numeric(result?.summary?.unclassified_value);
  const overlap=table(overlapRows(),[{key:'issuer_id',label:'Issuer'},{key:'sector'},
      {key:'direct_value',label:'Held directly',render:value=>money(value)},
      {key:'indirect_value',label:'Held through funds',render:value=>money(value)},
      {key:'funds',label:'Contributing funds',wrap:true}],
      'Issuers reached twice: directly and through a fund’s disclosed holdings.');
  overlap.classList?.add('overlap-table');
  replace('overlap-view',overlap,
    ...fundSectorBars(),
    el('p',unclassified?`Unclassified residual ${money(unclassified)} · exposure a fund did not disclose stays unclassified.`:'No unclassified residual in this review.','muted'),
    chartCaption({units:`Value in ${result?.summary?.currency || 'the base currency'}`,date:observationDate(),
      scope:'Direct holdings and disclosed fund holdings',coverage:coverageSummary('fund_disclosures','prices')}));
}
function securityRow(id) { return (result?.holdings || []).find(row=>row.security_id===id) || (result?.signals || []).find(row=>row.security_id===id) || {}; }
function securityButton(id) { return button(id || 'Unresolved',()=>showSecurity(id),'text-button'); }
// A published locator is data, not markup: only an https address the service already
// vetted becomes a link, and everything else stays readable text.
function sourceLink(source) {
  const locator=typeof source?.locator==='string' && /^https:\/\//.test(source.locator)?source.locator:null;
  const label=[source?.id,source?.received_at?`received ${source.received_at}`:null,source?.published_at?`published ${source.published_at}`:null].filter(Boolean).join(' · ');
  if(!locator)return el('li',label || text(source));
  const item=el('li'), link=el('a',label || locator);
  link.href=locator;link.rel='noreferrer noopener';link.target='_blank';item.append(link);return item;
}
function bulletList(items) {
  const rows=(items || []).filter(Boolean);
  if(!rows.length)return el('p','Nothing stated in this section.','muted');
  const list=el('ul',null,'issue-list');
  for(const row of rows) {
    const item=el('li',typeof row==='string'?row:row.text || text(row));
    const sources=(row?.source_ids || []).join(', ');
    if(sources)item.append(el('small',` · ${sources}`,'insight-source'));
    list.append(item);
  }
  return list;
}
const BRIEF_SECTIONS=[['changes_since_previous_review','Changes since the previous review'],
  ['key_financial_developments','Key financial developments'],['market_expectations','Market expectations'],
  ['thesis','Thesis'],['counter_thesis','Counter-thesis'],['catalysts','Dated catalysts'],
  ['invalidation_conditions','Invalidation conditions']];
function detailEvidence(id) {
  const brief=result?.research?.[id]?.brief;
  if(!brief)return [empty('This review retained no dated brief for this security.','No brief')];
  const sources=el('ul',null,'issue-list');
  for(const source of brief.sources || [])sources.append(sourceLink(source));
  return [el('p',`${brief.name || id} · as of ${text(brief.as_of)} · ${brief.horizon_months ?? '—'}-month horizon · ${text(brief.method_version)}`,'muted'),
    ...BRIEF_SECTIONS.flatMap(([key,label])=>[el('h3',label),bulletList(brief[key])]),
    el('h3','Sources'),(brief.sources || []).length?sources:el('p','No source with a known receipt was retained.','muted')];
}
function detailCalculation(id) {
  const company=result?.company_research?.[id], proposals=result?.research?.[id]?.proposals;
  const nodes=[];
  if(company?.eps)nodes.push(el('h3','EPS and multiple'),table(company.eps.scenarios,[{key:'label',label:'Outcome'},
    {key:'horizon_price',label:'Horizon price',render:value=>money(value,company.eps.currency)},
    {key:'total_return',label:'Total return',render:pct},{key:'status',render:badge}],'Reviewed assumptions calculated by this run.'));
  if(company?.dcf)nodes.push(el('h3','Discounted cash flow'),kv({status:company.dcf.status,
    intrinsic_value_per_share:money(company.dcf.value_per_share,company.dcf.currency),
    terminal_reinvestment_rate:pct(company.dcf.terminal_reinvestment_rate)}));
  if(proposals?.eps)nodes.push(el('h3','Proposed EPS inputs'),table(proposals.eps.scenarios,[{key:'label',label:'Outcome'},
    {key:'eps',label:'Horizon EPS',render:value=>num(value,2)},{key:'pe',label:'Matching P/E',render:value=>num(value,2)},
    {key:'distributions_per_starting_share',label:'Cash / starting share',render:value=>num(value,2)}],
    `Proposed from ${text(proposals.eps.proposal_meta?.estimate_period)} consensus; not adopted until kept.`));
  if(proposals?.dcf)nodes.push(el('h3','Proposed DCF inputs'),kv({discount_rate:pct(proposals.dcf.discount_rate),
    terminal_growth_rate:pct(proposals.dcf.terminal_growth_rate),terminal_roic:pct(proposals.dcf.terminal_roic),
    diluted_shares:num(proposals.dcf.diluted_shares,0)}));
  if(!nodes.length)nodes.push(empty('No valuation was calculated and none was proposed for this security.','No calculation'));
  return nodes;
}
function detailAssumptions(id) {
  const proposals=result?.research?.[id]?.proposals, rows=[];
  for(const kind of ['eps','dcf']) {
    const meta=proposals?.[kind]?.proposal_meta;
    if(!meta)continue;
    for(const name of meta.assumptions_visible || [])
      rows.push({model:kind.toUpperCase(),assumption:friendly(name),evidence:meta.evidence?.[name] || 'Stated by the model, not observed',
        basis:meta.basis || 'proposed'});
  }
  const kept=draft.workspace.valuations?.[id];
  return [table(rows,[{key:'model'},{key:'assumption'},{key:'evidence',wrap:true},{key:'basis'}],
    'The assumptions each proposal makes and the evidence behind them.'),
    kept?kv({origin:kept.origin,source:kept.source,version:kept.version,horizon_months:kept.horizon_months}):el('p','No proposal has been kept for this security.','muted'),
    ...(proposals?.reasons || []).length?[el('h3','Why a proposal was refused'),listIssues((proposals.reasons || []).map(row=>({message:row.detail || text(row),severity:row.severity})))]:[]];
}
function detailLimitations(id) {
  const coverage=result?.coverage?.by_security?.[id];
  const brief=result?.research?.[id]?.brief;
  const rows=(result?.readiness || []).map(row=>({output:friendly(row.output),status:row.status,missing:(row.missing || []).join(', ')}));
  return [el('h3','Provider coverage for this security'),
    coverage?kv(coverage):el('p','This review recorded no per-security coverage; nothing is claimed about it.','muted'),
    el('h3','What each output of this run needs'),table(rows,[{key:'output'},{key:'status',render:badge},{key:'missing',wrap:true}]),
    el('h3','Stated limitations'),listIssues(brief?.limitations || result?.coverage?.limitations,'No limitation was recorded for this security.')];
}
function showSecurity(id) {
  detailId=id;const holdings=(result?.holdings || []).filter(row=>row.security_id===id), score=(result?.signals || []).find(row=>row.security_id===id);
  $('security-dialog-title').textContent=id || 'Unresolved security';replace('security-dialog-content',autoTable(holdings),score?kv(score):empty('This instrument has no company score.','Company score unavailable'));
  replace('detail-evidence',...detailEvidence(id));replace('detail-calculation',...detailCalculation(id));
  replace('detail-assumptions',...detailAssumptions(id));replace('detail-limitations',...detailLimitations(id));
  $('security-dialog').showModal();
}
function renderHoldings() {
  const search=$('holdings-search').value.toLowerCase(), rows=(result?.holdings || []).filter(row=>(!account || row.account_id===account) && JSON.stringify(row).toLowerCase().includes(search));
  const fields=[['security_id','Security'],['account_id','Account'],['quantity','Quantity'],['price','Price'],['market_value','Market value'],['calculated_weight','Account weight'],['currency','Currency'],['issuer_id','Issuer'],['valuation_date','Valuation date'],['reported_weight','Reported weight']];
  replace('holdings-columns',...fields.map(([key,label])=>field(label,columnVisibility.has(key),checked=>{if(checked)columnVisibility.add(key);else columnVisibility.delete(key);renderHoldings();},{type:'checkbox'})));
  replace('research-holdings',table(rows,fields.filter(([key])=>columnVisibility.has(key)).map(([key,label])=>({key,label,render:key==='security_id'?value=>securityButton(value):key.includes('weight')?pct:['quantity','price','market_value'].includes(key)?value=>num(value):undefined})),`${rows.length} visible positions · account display filter only`));
  replace('account-reconciliation',table((result?.summary?.accounts || []).filter(row=>!account || row.account_id===account),[
    {key:'account_id',label:'Account'},{key:'total_value',label:'NAV',render:num},{key:'cash',label:'Explicit cash',render:num},
    {key:'unclassified_value',label:'Unclassified',render:num},{key:'coverage',render:pct},{key:'currency'},
    {key:'complete',render:value=>badge(value?'complete':'incomplete')},
  ]));
}
function renderScreen() {
  const search=$('screen-search').value.toLowerCase(), rows=(result?.signals || []).filter(row=>JSON.stringify(row).toLowerCase().includes(search));
  $('screen-scope').textContent=`${result?.signals?.length || 0} supplied securities · not a market-wide claim`;
  replace('company-screen',table(rows,[{key:'security_id',label:'Security',render:securityButton},{key:'sector'},
    {key:'quality',render:pct},{key:'value',render:pct},{key:'momentum',render:pct},{key:'score',label:'Composite rank',render:pct},
    {key:'eligible',render:value=>badge(value?'eligible':'ineligible')},{key:'reasons',label:'Coverage / eligibility',wrap:true}],
  'Ranks are relative research scores, not return forecasts. Open a security for raw inputs and peer coverage.'));
}
// A proposal is this review's reviewable starting point, never an adopted assumption:
// keeping one copies it into the workspace as a prefill that still has to be
// recalculated, reviewed and saved.
const proposalFor = kind => result?.research?.[securityId]?.proposals?.[kind] || null;
function keepProposal(kind) {
  const proposal=proposalFor(kind);
  if(!proposal)throw new Error(`This review proposed no ${kind.toUpperCase()} model for ${securityId || 'this security'}.`);
  if(!draft.baseRunId)throw new Error('Load a saved run before keeping a proposal.');
  draft=applyProposal(draft,securityId,kind,proposal,horizon());persist();renderAll();
  announce(`Proposed ${kind.toUpperCase()} assumptions copied for review. Recalculate to value them, then save to retain them.`);
}
function assumptionTags(kind,proposal) {
  const meta=proposal?.proposal_meta;
  if(!meta)return [el('span',`${kind.toUpperCase()} · no proposal this review`,'assumption-tag muted')];
  const tags=(meta.assumptions_visible || []).map(name=>el('span',`${kind.toUpperCase()} · ${friendly(name)}`,'assumption-tag'));
  for(const [name,source] of Object.entries(meta.evidence || {}))tags.push(el('span',`${friendly(name)} · ${source}`,'assumption-tag evidence'));
  return tags;
}
function renderProposals() {
  const actions=[], tags=[];
  for(const kind of ['eps','dcf']) {
    const proposal=proposalFor(kind);
    const action=button(`Use proposed ${kind.toUpperCase()} assumptions`,()=>keepProposal(kind),proposal?'secondary':'quiet');
    action.disabled=!proposal || !draft.baseRunId;
    action.title=proposal?`Copy this review's proposed ${kind.toUpperCase()} inputs into the workspace for review.`
      :`This review proposed no ${kind.toUpperCase()} model for ${securityId || 'this security'}.`;
    actions.push(action);tags.push(...assumptionTags(kind,proposal));
  }
  replace('proposal-actions',...actions);
  replace('assumption-strip',...tags,el('p','A proposal is evidence-backed input, not an established assumption. Review each value before recalculating.','muted'));
}
function epsDefaults() {
  const security=securityRow(securityId);
  return {starting_price:numeric(security.price),currency:security.currency || resolved().mandate?.base_currency || null,
    horizon_months:horizon(),eps_convention:'forward',pe_convention:'forward',scenarios:labels.map(label=>({label,eps:null,pe:null,distributions_per_starting_share:null}))};
}
function valuationValue(kind) { return draft.workspace.valuations?.[securityId]?.[kind]; }
function updateValuation(kind, mutate) {
  const model=clone(draft.workspace.valuations?.[securityId]?.[kind] || (kind==='eps'?epsDefaults():dcfDefaults()));
  mutate(model);
  draft=keepValuationEdit(draft,securityId,kind,model,
    {origin:'manual',source:'User scenario input',version:1,horizon_months:horizon()});
  persist();renderState(true);renderDiff();
}
// The grid is shaded by where each cell sits between the grid's own lowest and highest
// value, so the eye finds the drivers; the number and its contrast stay unchanged, and a
// cell without a value is left unshaded rather than shaded as a zero.
function heatLegend(low,high,currency) {
  const node=el('div',null,'heat-legend');
  node.append(el('span',money(low,currency),'heat-end'),el('span',null,'heat-bar'),el('span',money(high,currency),'heat-end'),
    el('small','Shading compares cells within this grid only.','muted'));
  return node;
}
function sensitivity(id,grid,type) {
  if(!grid){replace(id,empty('Supply complete inputs and recalculate to compare drivers.','Sensitivity unavailable'));return;}
  const x=type==='eps'?grid.pe_values:grid.terminal_growth_rates, y=type==='eps'?grid.eps_values:grid.discount_rates;
  const rows=(grid.cells || []).map((cells,index)=>({axis:y[index],...Object.fromEntries(cells.map((cell,j)=>[`c${j}`,cell]))}));
  const amount=cell=>numeric(type==='eps'?cell?.horizon_price:cell?.value_per_share);
  const values=(grid.cells || []).flat().map(amount).filter(value=>value!==null);
  const low=values.length?Math.min(...values):null, high=values.length?Math.max(...values):null, span=(high-low) || 1;
  const node=table(rows,[{key:'axis',label:type==='eps'?'EPS / P/E':'Discount / growth',render:type==='eps'?num:pct},...x.map((value,index)=>({key:`c${index}`,label:type==='eps'?num(value):pct(value),render:cell=>{
    const cellValue=amount(cell);
    const item=el('span',money(type==='eps'?cell.horizon_price:cell.value_per_share,grid.currency),cellValue===null?'':'heat');
    if(cellValue!==null)item.style.setProperty('--heat',((cellValue-low)/span).toFixed(3));
    item.title=cell.issues?.join('; ') || (type==='eps'?`Horizon return ${pct(cell.total_return)}`:'Conditional intrinsic value per share');return item;
  }}))],type==='eps'?'Horizon price per share; hover a cell for total return or missing reason.':'Conditional intrinsic value per share; invalid terminal combinations remain unavailable.');
  node.classList.add('sensitivity');
  replace(id,node,values.length?heatLegend(low,high,grid.currency):null,
    chartCaption({units:type==='eps'?`Horizon price per share (${grid.currency || 'currency unstated'})`
      :`Intrinsic value per share (${grid.currency || 'currency unstated'})`,
      date:observationDate(),scope:`${securityId || 'this security'} · grid around the central case`,
      coverage:readinessNote('company_valuation')}));
}
function renderEPS() {
  const data=valuationValue('eps') || epsDefaults();
  const fields=form([
    field('Starting price / share',data.starting_price,value=>updateValuation('eps',model=>model.starting_price=value),{type:'number'}),
    field('Currency',data.currency,value=>updateValuation('eps',model=>model.currency=value.toUpperCase() || null)),
    field('EPS and matching P/E basis',data.eps_convention,value=>updateValuation('eps',model=>{model.eps_convention=value;model.pe_convention=value;}),{options:['forward','trailing']}),
    field('Model horizon',data.horizon_months,value=>{updateValuation('eps',model=>{if(model.horizon_months!==Number(value))model.scenarios=model.scenarios.map(row=>({...row,eps:null,pe:null,distributions_per_starting_share:null}));model.horizon_months=Number(value);});renderCompany();},{options:[[6,'6 months'],[12,'12 months'],[18,'18 months']]}),
  ]);
  const rows=table(labels.map(label=>data.scenarios.find(row=>row.label===label) || {label}),[{key:'label',label:'Outcome'},...['eps','pe','distributions_per_starting_share'].map(key=>({key,label:{eps:'Horizon EPS',pe:'Matching P/E',distributions_per_starting_share:'Cash / starting share'}[key],render:(value,row)=>field(`${row.label} ${friendly(key)}`,value,next=>updateValuation('eps',model=>{
    let item=model.scenarios.find(item=>item.label===row.label);if(!item){item={label:row.label};model.scenarios.push(item);}item[key]=next;
  }),{type:'number'})}))]);
  replace('eps-form',fields,rows,el('p','Cash distributions exclude buybacks. Buybacks and dilution enter EPS/share assumptions only. Returns are over the declared horizon, without reinvestment.','muted'));
  const output=result?.company_research?.[securityId]?.eps;
  replace('eps-output',output?table(output.scenarios,[{key:'label',label:'Outcome'},{key:'horizon_price',label:'Horizon price',render:value=>money(value,output.currency)},{key:'total_return',label:'Total return',render:pct},{key:'status',render:badge},{key:'issues',wrap:true}]):empty('Enter assumptions and recalculate.','No EPS valuation yet'));
  sensitivity('eps-grid',result?.company_research?.[securityId]?.eps_sensitivity,'eps');
  const rationale=field('Rationale for linking reviewed EPS scenarios','',()=>{}, {type:'textarea'});
  const action=button('Use reviewed EPS scenarios',async()=>{
    const value=rationale.querySelector('textarea').value.trim();if(!value)throw new Error('Explain why these reviewed scenarios should feed portfolio comparison.');
    const response=await api('/api/research/valuation-link',{base_run_id:draft.baseRunId,security_id:securityId,workspace:draft.workspace,reviewed:true,rationale:value});
    patch(response.patch);renderScenarios();navigate('scenarios');announce('Reviewed EPS returns linked as subjective scenarios. Recalculate the portfolio.');
  });
  action.disabled=!draft.baseRunId;replace('valuation-link',rationale,action,el('p','Linking is an explicit review action. It does not create a calibrated forecast.','muted'));
}
function dcfDefaults() {
  return {currency:securityRow(securityId).currency || resolved().mandate?.base_currency || null,monetary_unit:'units',
    company_type:null,sbc_treatment:null,discount_rate:null,terminal_growth_rate:null,terminal_roic:null,
    debt:null,preferred:null,nci:null,excess_cash:null,nonoperating_assets:null,diluted_shares:null,
    projections:[{year:1,revenue:null,ebit:null,tax_rate:null,depreciation:null,capex:null,change_working_capital:null,invested_capital:null,stock_compensation:null}]};
}
function renderDCF() {
  const data=valuationValue('dcf') || dcfDefaults();
  const fields=[field('Applicable company type',data.company_type,value=>updateValuation('dcf',model=>model.company_type=value || null),{options:[['','Unconfirmed'],['nonfinancial','Nonfinancial operating company']]}),
    field('Currency',data.currency,value=>updateValuation('dcf',model=>model.currency=value.toUpperCase() || null)),
    field('Operating / bridge amount units',data.monetary_unit,value=>updateValuation('dcf',model=>model.monetary_unit=value),{options:['units','thousands','millions','billions']}),
    field('Stock compensation treatment',data.sbc_treatment,value=>updateValuation('dcf',model=>model.sbc_treatment=value || null),{options:[['','Choose treatment'],['expensed_in_ebit','Already expensed in EBIT'],['deduct_from_ebit','Deduct SBC from supplied EBIT']]}),
    ...[['discount_rate','Discount rate (%)'],['terminal_growth_rate','Terminal growth (%)'],['terminal_roic','Terminal ROIC (%)']].map(([key,label])=>field(label,data[key],value=>updateValuation('dcf',model=>model[key]=value),{type:'number',percent:true}))];
  const projections=table(data.projections,[{key:'year'},...['revenue','ebit','tax_rate','depreciation','capex','change_working_capital','invested_capital','stock_compensation'].map(key=>({key,label:key==='invested_capital'?'Beginning invested capital':key==='tax_rate'?'Tax rate (%)':friendly(key),render:(value,row)=>field(`Year ${row.year} ${friendly(key)}`,value,next=>updateValuation('dcf',model=>model.projections[row.year-1][key]=next),{type:'number',percent:key==='tax_rate'})}))]);
  const bridge=form(['debt','preferred','nci','excess_cash','nonoperating_assets','diluted_shares'].map(key=>field(key==='diluted_shares'?'Diluted shares (actual count)':friendly(key),data[key],value=>updateValuation('dcf',model=>model[key]=value),{type:'number'})));
  replace('dcf-form',form(fields),el('h3','Annual operating projections'),projections,button('Add projection year',()=>{
    updateValuation('dcf',model=>model.projections.push({...dcfDefaults().projections[0],year:model.projections.length+1}));renderCompany();
  }),button('Remove last year',()=>{if(data.projections.length>1){updateValuation('dcf',model=>model.projections.pop());renderCompany();}},'quiet'),el('h3','Enterprise value to common equity'),bridge,
  el('p','End-of-year FCFF discounting. Capital rolls forward from reinvestment; terminal reinvestment is growth / ROIC. Every bridge amount needs evidence, including explicit zeros. SBC stays an operating expense; diluted shares account for existing awards.','muted'));
  const output=result?.company_research?.[securityId]?.dcf;
  const amount=value=>numeric(value)===null?'—':`${num(value)} ${output.monetary_unit} ${output.currency}`;
  replace('dcf-output',output?kv({status:output.status,enterprise_value:amount(output.enterprise_value),common_equity_value:amount(output.common_equity_value),intrinsic_value_per_share:money(output.value_per_share,output.currency),explicit_cash_flow_present_value:amount(output.explicit_present_value),terminal_value:amount(output.terminal_value),terminal_present_value:amount(output.terminal_present_value),terminal_reinvestment_rate:pct(output.terminal_reinvestment_rate),horizon_return:output.return_reason}):empty('Enter explicit projections and bridge inputs, then recalculate.','No DCF valuation yet'),output?autoTable(output.projections,['year','nopat','reinvestment','roic','fcff','present_value']):null,output?listIssues(output.issues):null);
  sensitivity('dcf-grid',result?.company_research?.[securityId]?.dcf_sensitivity,'dcf');
}
function assessmentDefaults() {
  return {version:1,security_id:securityId,author:'',horizon_months:horizon(),decision_cutoff:result?.timeline?.decision_cutoff || null,
    generated_at:new Date().toISOString(),review_status:'draft',reviewed_by:'',reviewed_at:null,
    market_expectations:'',thesis:'',counter_thesis:'',catalysts:'',balance_sheet_risks:'',invalidation_conditions:'',sources:[],facts:[],operating_assumptions:[]};
}
function updateAssessment(mutate,reviewAction=false) {
  workspaceEdit(next=>{const value=clone(next.assessments[securityId] || assessmentDefaults());mutate(value);value.version=(value.version || 0)+1;
    if(!reviewAction){value.review_status='needs_review';value.reviewed_at=null;}
    next.assessments[securityId]=value;
  });
}
function renderEvidence() {
  const data=draft.workspace.assessments?.[securityId] || assessmentDefaults();
  const narrative=form([field('Assessment author',data.author,value=>updateAssessment(model=>model.author=value)),
    field('Reviewer',data.reviewed_by,value=>updateAssessment(model=>model.reviewed_by=value)),
    field('Assessment reviewed',data.review_status==='reviewed',value=>updateAssessment(model=>{model.review_status=value?'reviewed':'draft';model.reviewed_at=value?new Date().toISOString():null;},true),{type:'checkbox'}),
    ...['market_expectations','thesis','counter_thesis','catalysts','balance_sheet_risks','invalidation_conditions'].map(key=>field(friendly(key),typeof data[key]==='string'?data[key]:JSON.stringify(data[key],null,2),value=>updateAssessment(model=>model[key]=value),{type:'textarea',wide:true,help:key==='catalysts'?'Describe horizon catalysts or explicitly state none identified. Dated event objects can be entered in advanced JSON.':''}))]);
  const sourceRows=table(data.sources || [],[{key:'id'},...['locator','published_at','received_at','version','content_hash'].map(key=>({key,label:friendly(key),render:(value,row)=>field(`${row.id} ${friendly(key)}`,value,next=>updateAssessment(model=>{
    model.sources.find(item=>item.id===row.id)[key]=next || null;
    for(const fact of model.facts || [])if(fact.source_id===row.id){fact.review_status='draft';fact.reviewed_at=null;}
  }))}))]);
  const factRows=table(data.facts || [],[{key:'id'},...['field','value','units','source_id','reviewed_by'].map(key=>({key,render:(value,row)=>field(`${row.id} ${friendly(key)}`,value,next=>updateAssessment(model=>{
    const item=model.facts.find(item=>item.id===row.id);item[key]=next;item.review_status='draft';item.reviewed_at=null;
  }),key==='value'?{type:'number'}:{})})),{key:'review_status',label:'Reviewed against source',render:(value,row)=>field(`Reviewed fact ${row.id}`,value==='reviewed',checked=>updateAssessment(model=>{
    const item=model.facts.find(item=>item.id===row.id);item.review_status=checked?'reviewed':'draft';item.reviewed_at=checked?new Date().toISOString():null;
  }),{type:'checkbox'})}]);
  const assumptionRows=table(data.operating_assumptions || [],['field','value','units','basis','author'].map(key=>({key,render:(value,row)=>field(`Assumption ${row.field || ''} ${friendly(key)}`,value,next=>updateAssessment(model=>{
    const index=data.operating_assumptions.indexOf(row);model.operating_assumptions[index][key]=next;model.operating_assumptions[index].version=(model.operating_assumptions[index].version || 0)+1;
  }),key==='value'?{type:'number'}:{})})));
  replace('evidence-form',narrative,el('h3','Retained source evidence'),el('p','Publication and receipt require exact ISO timestamps with timezone. Use the retained document’s SHA-256 and original version; period end is not public availability.','muted'),sourceRows,
    button('Add source',()=>{updateAssessment(model=>model.sources.push({id:`source-${model.sources.length+1}`,locator:'',published_at:null,received_at:null,version:'',content_hash:null}));renderCompany();}),
    el('h3','Facts checked against sources'),factRows,button('Add fact',()=>{updateAssessment(model=>model.facts.push({id:`fact-${model.facts.length+1}`,field:'',value:null,units:'',source_id:model.sources[0]?.id || '',origin:'manual',review_status:'draft',reviewed_by:'',reviewed_at:null}));renderCompany();}),
    el('h3','Operating assumptions'),assumptionRows,button('Add operating assumption',()=>{updateAssessment(model=>model.operating_assumptions.push({field:'',value:null,units:'',basis:'',author:model.author,version:1,horizon_months:horizon(),origin:'manual'}));renderCompany();}));
  const output=result?.company_research?.[securityId]?.assessment;
  replace('evidence-output',output?kv({status:output.status,version:output.version,forecast_type:output.forecast_type,timing_policy:output.timing_policy,eligible_facts:output.eligible_facts?.length,assessment_id:output.assessment_id}):empty('Enter source-backed facts and assumptions, then recalculate.','No assessment calculated'),output?listIssues(output.issues):null,output?listIssues(output.calibration_issues,'A reviewed assessment remains subjective; it is not a calibrated forecast.'):null,
    output?disclosure('Fact and source validation results',autoTable(output.facts || []),autoTable(output.sources || [])):null);
}
function renderCompany() {
  const ids=[...new Set([...(result?.holdings || []).map(row=>row.security_id),...(result?.signals || []).map(row=>row.security_id),...Object.keys(draft.workspace.valuations || {}),...Object.keys(draft.workspace.assessments || {})].filter(Boolean))].sort();
  if(securityId && !ids.includes(securityId))ids.unshift(securityId);
  if(!securityId)securityId=ids[0] || '';
  $('research-security').replaceChildren(...(ids.length?ids:['']).map(id=>{const item=el('option',id || 'Enter an exact security ID');item.value=id;return item;}));$('research-security').value=securityId;
  if(!securityId){for(const id of ['eps-form','dcf-form','evidence-form'])replace(id,empty('Choose a retained security or enter its exact identifier.','Select a company'));return;}
  const security=securityRow(securityId);
  const kept=draft.workspace.valuations?.[securityId];
  replace('security-summary',kv({security_id:securityId,issuer:security.issuer_id,currency:security.currency,horizon:`${horizon()} months`,
    valuation_origin:kept?.origin || 'none kept',valuation_source:kept?.source || 'not stated',
    basis:'Subjective assumptions; prices may be unavailable'}),
    disclosure('Retained financial statements and source observations',autoTable((result?.observations?.fundamentals?.rows || []).filter(row=>row.security_id===securityId)),el('p',result?.observations?.fundamentals?.scope || 'Only facts eligible at the saved cutoff are displayed.','muted')));
  renderProposals();renderEPS();renderDCF();renderEvidence();
  jsonEditor('company-json','Company assessment and valuation inputs',{
    assessment:draft.workspace.assessments[securityId] || assessmentDefaults(),
    valuations:draft.workspace.valuations[securityId] || {},
  },value=>{
    validateCompanyInputs(value);
    workspaceEdit(next=>{
      if(value.assessment)next.assessments[securityId]=reviseAssessmentInputs(next.assessments[securityId],merge(assessmentDefaults(),value.assessment));
      if(value.valuations){
        const previous=next.valuations[securityId];next.valuations[securityId]=clone(value.valuations);
        if(differences(previous || {},value.valuations).length){
          next.valuations[securityId].version=Math.max(Number.isInteger(previous?.version)?previous.version:0,Number.isInteger(value.valuations.version)?value.valuations.version:0)+1;
          next.valuations[securityId].origin='manual';
        }
      }
    });
  },`company-${securityId}`);
}
// Grouped bars for two series over the same states, drawn like `lineChart`: one baseline
// at zero, one pair of bars per state, and every value also stated in the axis text so
// the chart is not the only way to read it.
function groupedBarChart(groups,series,caption) {
  const usable=groups.filter(group=>group.values.some(value=>numeric(value)!==null));
  if(!usable.length)return empty('No comparable outcomes are available for these states.','Comparison unavailable');
  const values=usable.flatMap(group=>group.values.map(numeric)).filter(value=>value!==null);
  const high=Math.max(...values,0), low=Math.min(...values,0), span=(high-low) || 1;
  const container=el('div'), svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox','0 0 600 180');svg.setAttribute('class','grouped-chart');svg.setAttribute('role','img');svg.setAttribute('aria-label',caption);
  const title=document.createElementNS(svg.namespaceURI,'title');title.textContent=caption;svg.append(title);
  const place=value=>160-(value-low)/span*140, zero=place(0);
  const axis=document.createElementNS(svg.namespaceURI,'line');
  for(const [key,value] of Object.entries({x1:0,x2:600,y1:zero,y2:zero,stroke:'#c9d4c2','stroke-width':1}))axis.setAttribute(key,value);
  svg.append(axis);
  const width=600/usable.length;
  usable.forEach((group,index)=>{
    group.values.forEach((value,position)=>{
      const amount=numeric(value);
      if(amount===null)return;
      const y=place(amount), rect=document.createElementNS(svg.namespaceURI,'rect');
      rect.setAttribute('x',index*width+width*0.18+position*(width*0.3));
      rect.setAttribute('width',width*0.26);
      rect.setAttribute('y',Math.min(y,zero));
      rect.setAttribute('height',Math.max(1,Math.abs(zero-y)));
      rect.setAttribute('class',`series-${position}`);
      const label=document.createElementNS(svg.namespaceURI,'title');
      label.textContent=`${group.label} · ${series[position]} · ${pct(amount)}`;rect.append(label);
      svg.append(rect);
    });
  });
  const legend=el('div',null,'chart-legend');
  legend.append(...series.map((name,index)=>el('span',name,`legend-key series-${index}`)));
  const readout=el('div',null,'chart-readout');
  readout.append(...usable.map(group=>el('span',`${group.label}: ${group.values.map(pct).join(' vs ')}`)));
  container.append(svg,legend,readout);return container;
}
// The owner's stated view of the world: a few market states, a multiplier per sector and
// a currency state per foreign quote currency. The next full review expands it into the
// per-security joint scenarios the engine validates; a recalculation replays the frozen
// scenarios this run already has.
function sharedState() { return resolved().allocation?.shared_state || {}; }
// One versioned statement of the world: an edit replaces it whole, exactly as the
// service stores it, so half of a previous view can never stay merged underneath.
function patchSharedState(values) { patch({allocation:{shared_state:merge(sharedState(),values)}}); }
function stateProbability(labels,label,value) {
  if(value===null){patchSharedState({probabilities:null});return;}
  const current=sharedState().probabilities;
  const next={...(current || Object.fromEntries(labels.map(name=>[name,null])))};
  next[label]=value;patchSharedState({probabilities:next});
}
function renderSharedState() {
  const state=sharedState(), labels=Object.keys(state.market_returns || {});
  if(!labels.length){replace('shared-state-editor',el('p','This run retained no shared market state; per-security joint forecasts are used as supplied.','muted'));return;}
  const markets=form(labels.map(label=>field(`${friendly(label)} market return (%)`,state.market_returns[label],
    value=>patchSharedState({market_returns:{[label]:value}}),{type:'number',percent:true,
    help:'Total return of the broad equity market in this state over the stated horizon.'})));
  const sectors=table(Object.entries(state.sector_multipliers || {}).map(([sector,multiplier])=>({sector,multiplier})),
    [{key:'sector'},{key:'multiplier',label:'Multiplier on the market state',render:(value,row)=>field(`${row.sector} multiplier`,value,
      next=>patchSharedState({sector_multipliers:{[row.sector]:next}}),{type:'number'})}],
    'Each sector moves with the market state times this multiplier. A multiplier is a stated view, not a measured beta.');
  const fx=table(Object.entries(state.fx_returns || {}).map(([code,states])=>({code,...states})),
    [{key:'code',label:'Currency'},...labels.map(label=>({key:label,label:friendly(label),render:(value,row)=>field(`${row.code} ${friendly(label)} currency return (%)`,value,
      next=>patchSharedState({fx_returns:{[row.code]:{[label]:next}}}),{type:'number',percent:true})}))],
    'Presentation currency per unit of the foreign currency in each state.');
  const probabilities=form(labels.map(label=>field(`${friendly(label)} state probability (%)`,state.probabilities?.[label] ?? null,
    value=>stateProbability(labels,label,value),{type:'number',percent:true,
    help:'Optional. State every probability or leave every one blank; a partial distribution is refused.'})));
  replace('shared-state-editor',el('h3','Stated market view'),
    el('p','These states are expanded into per-security joint scenarios by the next full review (Update & analyze). Recalculating replays the joint scenarios this saved run already froze.','muted'),
    markets,el('h3','Sector multipliers'),sectors,el('h3','Currency states'),fx,
    el('h3','State probabilities (optional)'),probabilities,
    button('Reset to this run’s saved market view',()=>{
      patchSharedState(clone(draft.baseConfig.allocation?.shared_state || {}));renderScenarios();
      announce('The stated market view was restored to this saved run’s assumptions.');
    },'quiet'));
}
function selectedCandidateRow() {
  const candidates=result?.allocation?.candidates || [];
  return candidates.find(row=>row.candidate===selectedCandidate)
    || candidates.find(row=>row.candidate===result?.allocation?.selected_candidate) || candidates[0] || null;
}
function renderScenarioComparison() {
  const outcome=result?.scenarios || {}, candidate=selectedCandidateRow();
  const groups=(outcome.scenarios || []).map(row=>({label:friendly(row.scenario),
    values:[row.return,candidate?.scenarios?.[row.scenario] ?? null]}));
  replace('scenario-comparison',
    groupedBarChart(groups,['Current portfolio',candidate?friendly(candidate.candidate):'No alternative selected'],
      'Current portfolio against the selected alternative, by joint state'),
    chartCaption({units:`${outcome.horizon_months || horizon()}-month total return (%)`,date:observationDate(),
      scope:candidate?`Current portfolio against ${candidate.candidate}, after costs and tax reserve`:'Current portfolio only; no alternative is selected',
      coverage:readinessNote('baskets')}),
    el('p',outcome.basis || 'Conditional on the stated assumptions; not a forecast.','muted'));
}
function scenarioLabels() {
  const observed=(result?.scenarios?.scenarios || []).map(row=>row.scenario);
  return observed.length?observed:[...new Set((result?.forecast_inputs || []).map(row=>row.scenario))].filter(Boolean).length?[...new Set(result.forecast_inputs.map(row=>row.scenario))]:labels;
}
function renderScenarios() {
  const config=resolved(), names=scenarioLabels(), allocation=config.allocation || {};
  $('scenario-horizon').value=allocation.horizon_months || 12;
  replace('scenario-controls',form([
    field('Use joint probabilities',allocation.use_probabilities !== false,value=>{patch({allocation:{use_probabilities:value}});renderScenarios();},{type:'checkbox',help:'Turn off for unweighted exploration; weighted means remain unavailable.'}),
    field(`${horizon()}-month cash total return (%)`,allocation.cash_return,value=>patch({allocation:{cash_return:value}}),{type:'number',percent:true,help:'Explicit horizon assumption, shared across joint states. Blank means unknown; enter 0 only when intended.'}),
  ]));
  replace('probability-editor',el('h3','Joint state probabilities (optional)'),form(names.map(name=>field(`${friendly(name)} probability (%)`,allocation.probability_overrides?.[name] ?? result?.scenarios?.scenarios?.find(row=>row.scenario===name)?.probability,value=>{
    const probabilities={...(resolved().allocation?.probability_overrides || {})};
    // On first edit, retain all observed probabilities explicitly; never normalize their sum.
    if(!Object.keys(probabilities).length)for(const row of result?.scenarios?.scenarios || [])if(row.probability!==null)probabilities[row.scenario]=row.probability;
    if(value===null)delete probabilities[name];else probabilities[name]=value;
    patch({allocation:{probability_overrides:probabilities}});
  },{type:'number',percent:true}))),button('Use source probabilities',()=>{patch({allocation:{probability_overrides:{}}});renderScenarios();},'quiet'));
  const ids=[...new Set([...(result?.holdings || []).map(row=>row.security_id),...(result?.forecast_inputs || []).map(row=>row.security_id),config.mandate?.benchmark_id].filter(Boolean))].filter(id=>id!=='CASH');
  const rows=ids.map(id=>({security_id:id,...Object.fromEntries(names.map(name=>[name,allocation.return_overrides?.[id]?.[name] ?? (result?.forecast_inputs || []).find(row=>row.security_id===id && row.scenario===name && row.horizon_months===horizon())?.return_value]))}));
  replace('return-editor',el('h3','Asset and benchmark horizon returns (%)'),table(rows,[{key:'security_id',label:'Security',render:securityButton},...names.map(name=>({key:name,label:friendly(name),render:(value,row)=>field(`${row.security_id} ${name} return (%)`,value,next=>{
    const map=clone(resolved().allocation?.return_overrides || {});map[row.security_id] ||= {};
    if(!Object.keys(map[row.security_id]).length)for(const stateName of names){const base=rows.find(item=>item.security_id===row.security_id)?.[stateName];if(base!==undefined && base!==null)map[row.security_id][stateName]=base;}
    if(next===null)delete map[row.security_id][name];else map[row.security_id][name]=next;
    patch({allocation:{return_overrides:map}});
  },{type:'number',percent:true})}))],'Manual overrides remain subjective. Use identical joint state labels across assets and benchmark.'));
  renderSharedState();
  replace('stress-editor',form(['stress_equity_shock','stress_growth_shock','stress_rates_shock'].map(key=>field(`${friendly(key)} (%)`,config.risk?.[key],value=>patch({risk:{[key]:value}}),{type:'number',percent:true}))));
  jsonEditor('scenario-json','Allocation and risk assumptions',{allocation:config.allocation,risk:config.risk},value=>patch(value));
  const outcome=result?.scenarios || {};
  replace('scenario-chart',barChart(outcome.scenarios,'return','scenario',null),
    chartCaption({units:`${outcome.horizon_months || horizon()}-month total return (%)`,date:observationDate(),
      scope:'Hypothetical portfolio outcomes; bars show magnitude, labels show sign',coverage:readinessNote('candidates')}));
  renderScenarioComparison();
  replace('scenario-results',table(outcome.scenarios,[{key:'scenario'},{key:'probability',render:pct},{key:'return',label:'Portfolio return',render:pct},{key:'benchmark_return',render:pct},{key:'active_return',label:'Active return',render:pct},{key:'terminal_value',label:'Terminal value',render:value=>money(value)},{key:'net_return',label:'After reserved friction',render:pct},{key:'net_terminal_value',label:'Terminal value after reserved friction',render:value=>money(value)}]),kv({weighted_return:pct(outcome.weighted_return),net_weighted_return:pct(outcome.net_weighted_return),benchmark_weighted_return:pct(outcome.benchmark_weighted_return),basis:outcome.basis,status:outcome.status}));
  const prior=saved?.result?.scenarios;
  const comparison=(outcome.scenarios || []).map(row=>({scenario:row.scenario,saved_return:prior?.horizon_months===outcome.horizon_months?prior.scenarios?.find(item=>item.scenario===row.scenario)?.return:null,displayed_return:row.return}));
  replace('scenario-issues',listIssues(outcome.issues),
    disclosure('Saved versus displayed scenario returns',table(comparison,[{key:'scenario'},{key:'saved_return',render:pct},{key:'displayed_return',render:pct}],prior?.horizon_months===outcome.horizon_months?'Identical horizons; displayed results may be stale until recalculated.':'Saved returns are unavailable for this comparison because the horizons differ.')),
    el('p',outcome.cash_assumption || 'Cash requires an explicit horizon total-return assumption.','muted'),
    el('p',outcome.distribution_convention || 'Asset returns must include distributions under an explicit reinvestment convention; do not add dividend or buyback yields again.','muted'),
    el('p','Results remain conditional on assumptions. Missing probabilities cannot support a weighted mean. Scores and macro indicators do not supply automatic forecasts.','muted'));
}
function renderReview() {
  const allocation=result?.allocation || {}, candidates=allocation.candidates || [];
  if(!candidates.some(row=>row.candidate===selectedCandidate))selectedCandidate=allocation.selected_candidate || candidates[0]?.candidate || '';
  $('allocation-state').replaceChildren(badge(allocation.status));
  replace('candidate-cards',...(candidates.length?candidates.map(candidate=>{
    const node=button('',()=>{selectedCandidate=candidate.candidate;renderReview();},'candidate-card');node.setAttribute('aria-pressed',String(candidate.candidate===selectedCandidate));
    node.append(el('strong',friendly(candidate.candidate)),badge(candidate.status),el('small',candidate.reason || 'Review funding, assumptions and constraints.'));return node;
  }):[empty('Resolve the specific funding, mandate or evidence gaps below.','Candidates are blocked')]));
  replace('candidate-comparison',table(allocation.comparison || candidates,[{key:'candidate',label:'Candidate'},{key:'status',render:badge},{key:'scenario_weighted_return_after_cost_and_tax_reserve',label:'Conditional net return',render:pct},{key:'estimated_terminal_wealth_after_cost_and_tax_reserve',label:'Conditional terminal value',render:value=>money(value)},{key:'execution_cost',render:num},{key:'conditional_tax_reserve',render:num},{key:'improvement_per_redeployed_dollar',label:'Improvement / redeployed dollar',render:pct},{key:'reason',wrap:true}]));
  const candidate=candidates.find(row=>row.candidate===selectedCandidate) || allocation;
  $('basket-title').textContent=selectedCandidate?friendly(selectedCandidate):'Proposed basket';$('basket-state').replaceChildren(badge(candidate.status));
  replace('basket-metrics',metric('Execution cost',money(candidate.execution_cost),'Complete basket'),metric('Conditional tax reserve',money(candidate.conditional_tax_reserve),'Not final liability'),metric('Redeployed principal',money(candidate.redeployed_principal),'Hurdle denominator'));
  renderAllocationChange(candidate);
  // The exact decimal amounts, beside the weights the chart above shows.
  replace('trade-basket',table(candidate.proposals || [],[
    {key:'security_id',label:'Security',render:securityButton},{key:'account_id',label:'Account'},{key:'action'},
    {key:'current_weight',label:'Current weight',render:pct},{key:'target_weight',label:'Target weight',render:pct},
    {key:'decimal_amounts',label:'Exact trade value',render:(value,row)=>`${value?.trade_value ?? num(row.trade_value)} ${row.currency || ''}`.trim()},
    {key:'fractional_quantity_change',label:'Exact quantity change',render:(value,row)=>text(row.decimal_amounts?.quantity_change ?? num(value,8))},
    {key:'price_used',label:'Price used',render:value=>num(value)},
    {key:'estimated_cost',label:'Execution cost',render:value=>num(value)},
    {key:'estimated_tax',label:'Conditional tax reserve',render:value=>num(value)},
    {key:'executable',label:'Executable',render:value=>badge(value?'executable':'not executable')},
    {key:'review_flags',label:'Review flags',wrap:true,
      render:value=>Array.isArray(value)?(value.length?value.map(friendly).join(', '):'—'):text(value)},
    {key:'basis',wrap:true},{key:'reason',wrap:true}],
    'Exact decimal trade values and quantities for the complete basket. Nothing here places an order.'),
    // The reserve above is only as good as the lots behind it, so the plan stays readable.
    disclosure('Tax lot plan behind the conditional reserve',
      autoTable((candidate.proposals || []).flatMap(leg=>(leg?.lot_plan || [])
        .map(lot=>({security_id:leg.security_id,account_id:leg.account_id,...lot})))),
      el('p','Lots the conditional tax reserve was estimated from. An estimate flagged for review is named in the basket table above.','muted')));
  replace('basket-funding',autoTable(candidate.accounts || []));
  replace('basket-reasons',listIssues([...(allocation.issues || []),...(candidate.issues || []),...(candidate.decisions || []).map(row=>({message:`${row.security_id || row.account_id || ''}: ${row.reason || row.action}`}))]),candidate.validation?kv(candidate.validation):null,
    disclosure('Candidate joint outcomes after costs and tax reserve',table(Object.entries(candidate.scenarios || {}).map(([scenario,value])=>({scenario,return:value})),[{key:'scenario'},{key:'return',render:pct}],'Conditional horizon returns for the complete funded basket.')),
    disclosure('Trading-cost sensitivities',autoTable(candidate.cost_sensitivities || [])),
    disclosure('Substitution-hurdle sensitivities',autoTable(candidate.hurdle_sensitivities || [])),
    disclosure('Candidate method and solver diagnostics',kv(allocation.solver || {})));
  renderDecisionHistory();
  const runs=service.runs || [];
  replace('run-history',table(runs,[{key:'run_id',label:'Saved run',render:id=>button(String(id).slice(0,16),()=>loadRun(id),'text-button')},{key:'as_of',label:'Decision date'},{key:'created_at'},{key:'parent_run_id'}]));
  for(const id of ['compare-left','compare-right']){
    const previous=$(id).value;$(id).replaceChildren(...runs.map(run=>{const item=el('option',`${run.as_of || run.created_at || 'Run'} · ${run.run_id.slice(0,12)}`);item.value=run.run_id;return item;}));
    $(id).value=previous && runs.some(run=>run.run_id===previous)?previous:(id==='compare-right'?runs[1]?.run_id:runs[0]?.run_id) || '';
  }
  const savedId=draft.baseRunId;
  for(const [id,extension] of [['export-json','json'],['export-html','html']]){
    if(savedId){$(id).href=`/api/research/export/${encodeURIComponent(savedId)}.${extension}`;$(id).removeAttribute('aria-disabled');}
    else{$(id).removeAttribute('href');$(id).setAttribute('aria-disabled','true');}
  }
  replace('evaluation-results',latestEvaluation?kv(latestEvaluation):empty('Supply actual observations after the endpoint and required latency mature.','No evaluation loaded'));
  renderLedgerEvaluation();
}
// What the selected basket would change, per security, before and after. The exact
// amounts stay in the funding table below; the chart is a shape, not a settlement.
function renderAllocationChange(candidate) {
  const legs=(candidate?.proposals || []).filter(row=>row && typeof row==='object');
  // A basket with many legs is read leg by leg in the table below; the chart shows the
  // largest few so its stated values stay readable rather than overflowing the panel.
  const shown=legs.slice(0,10);
  const groups=shown.map(row=>({label:`${row.security_id}${row.account_id?` · ${row.account_id}`:''}`,
    values:[row.current_weight,row.target_weight]}));
  replace('allocation-before-after',
    groupedBarChart(groups,['Current weight','Target weight'],'Household weight before and after the selected basket'),
    legs.length>shown.length?el('p',`+${legs.length-shown.length} more legs, listed in the basket table below.`,'muted'):null,
    chartCaption({units:'Share of the funding denominator (%)',date:observationDate(),
      scope:candidate?.candidate?`Selected alternative ${candidate.candidate}`:'No alternative is selected',
      coverage:readinessNote('baskets')}),
    el('p','Exact amounts, including the decimal trade values and quantities, are in the basket and funding tables below.','muted'));
}
function renderDecisionHistory() {
  const rows=(decisionRecords || []).map(record=>({...(record.payload || record),
    recorded_at:record.created_at || record.recorded_at}));
  replace('decision-history',table(rows,[{key:'as_of',label:'Decision date'},
    {key:'action',render:value=>friendly(value)},{key:'selected_alternative',label:'Selected alternative'},
    {key:'compared_candidates',label:'Compared against',wrap:true,render:value=>Array.isArray(value)?value.join(', '):text(value)},
    {key:'rationale',wrap:true},{key:'recorded_at',label:'Recorded',render:value=>when(value)}],
    'Decisions recorded against this saved run. Recording a decision never changes a saved forecast.'));
}
// Recording the decision is one explicit step: which alternative, weighed against which
// others, and why. The dialog refuses a decision the saved run cannot support.
// A refused decision is answered inside the dialog the owner is reading: the page's own
// error line sits behind the modal backdrop, where neither eye nor screen reader reaches
// it while the dialog is open.
function decisionError(message='') {
  $('decision-error').textContent=message;$('decision-error').hidden=!message;
  const rationale=$('decision-rationale');
  if(message){rationale.setAttribute('aria-invalid','true');rationale.focus();}
  else rationale.removeAttribute('aria-invalid');
}
// The recorded action and the recorded alternative are one statement, so the two controls
// are kept in step rather than letting the action silently discard the alternative.
let decisionAlternative='';
function syncDecisionAlternative() {
  const select=$('decision-alternative'), noAction=$('decision-action').value==='no_action';
  select.disabled=noAction;
  select.value=noAction?'':(select.value || decisionAlternative);
  select.title=noAction?'A no-change decision records no alternative. Choose another action to name one.':'';
}
function openDecisionDialog() {
  if(!draft.baseRunId)throw new Error('Choose a saved run before recording a decision.');
  if(draft.dirty)throw new Error('Save or reset the current draft so this decision refers to a retained run.');
  const candidates=(result?.allocation?.candidates || []).map(row=>row.candidate).filter(Boolean);
  $('decision-compared').textContent=candidates.length
    ?`Compared against ${candidates.join(', ')} · run ${String(draft.baseRunId).slice(0,12)} · ${text(result?.metadata?.as_of)}`
    :'This run produced no feasible alternative, so only a no-change decision can be recorded.';
  const chosen=selectedCandidateRow()?.candidate || '';
  $('decision-alternative').replaceChildren(...[['','No alternative chosen'],...candidates.map(id=>[id,friendly(id)])].map(([value,label])=>{
    const option=el('option',label);option.value=value;return option;
  }));
  decisionAlternative=candidates.includes(chosen)?chosen:'';
  $('decision-action').value='no_action';$('decision-rationale').value='';
  decisionError();syncDecisionAlternative();
  $('decision-dialog').showModal();
}
async function recordDecision() {
  try {
    decisionError();
    const record=decisionRecord(draft,result,{action:$('decision-action').value,
      candidate_id:$('decision-alternative').value || null,rationale:$('decision-rationale').value});
    await api('/api/research/decision',record);
  } catch(failure){decisionError(failure.message);return;}
  $('decision-dialog').close();$('decision-rationale').value='';decisionError();
  await refreshService();await loadDecisions();renderOverview();
  announce('Decision recorded. No saved forecast was changed.');
}
function configField(group,key,label= friendly(key),options={}) {
  return field(label,resolved()[group]?.[key],value=>patch({[group]:{[key]:value}}),options);
}
function renderSettings() {
  const config=resolved();
  replace('mandate-form',form([
    field('Base currency',config.mandate?.base_currency,value=>patch({mandate:{base_currency:value.toUpperCase() || null}})),
    configField('mandate','benchmark_id','Approved benchmark security ID'),
    ...['issuer_cap','sector_cap','min_cash_weight','max_turnover','max_volatility','max_stress_loss'].map(key=>configField('mandate',key,`${friendly(key)} (%)`,{type:'number',percent:true})),
    configField('mandate','confirmed','I confirm this mandate',{type:'checkbox'}),
    configField('mandate','allow_taxable_proposals','Allow conditional taxable-account proposals',{type:'checkbox'}),
    field('Locked security IDs (comma separated)',(config.mandate?.locked_security_ids || []).join(', '),value=>patch({mandate:{locked_security_ids:value.split(',').map(item=>item.trim()).filter(Boolean)}}),{wide:true}),
  ]));
  const accountNodes=[];
  for(const row of result?.summary?.accounts || []) {
    const id=row.account_id, block=el('section',null,'section-block');block.append(el('h3',id));
    block.append(field('Permitted security IDs (comma separated)',(config.mandate?.account_permissions?.[id] || []).join(', '),value=>{
      const map=clone(resolved().mandate?.account_permissions || {});map[id]=value.split(',').map(item=>item.trim()).filter(Boolean);patch({mandate:{account_permissions:map}});
    },{help:'Blank does not grant trading permission. Include approved residual and benchmark instruments explicitly.'}));
    const holdings=(result?.holdings || []).filter(item=>item.account_id===id);
    block.append(table(holdings,[{key:'security_id'},...['dealing_allowed','fractional_shares','quantity_increment'].map(key=>({key,label:friendly(key),render:(_,position)=>{
      const rule=config.mandate?.dealing_rules?.[id]?.[position.security_id];
      return field(`${position.security_id} ${friendly(key)}`,rule?.[key],value=>{
        const map=clone(resolved().mandate?.dealing_rules || {});map[id] ||= {};map[id][position.security_id] ||= {dealing_allowed:false,fractional_shares:false,quantity_increment:1};map[id][position.security_id][key]=value;patch({mandate:{dealing_rules:map}});
      },{type:key==='quantity_increment'?'number':'checkbox',help:rule?'':'Unconfirmed'});
    }}))]));accountNodes.push(block);
  }
  replace('permissions-form',form([
    configField('allocation','active_sleeve_weight','Systematic sleeve budget (%)',{type:'number',percent:true}),
    field('Sleeve budget denominator',config.allocation?.sleeve_budget_basis,value=>patch({allocation:{sleeve_budget_basis:value || null}}),{options:[['','Unconfirmed'],['account_nav','Each account NAV']]}),
    configField('allocation','residual_security_id','Eligible residual security ID'),
    configField('allocation','max_names','Maximum sleeve names',{type:'number'}),
    field('New-flow treatment',config.allocation?.flow_policy,value=>patch({allocation:{flow_policy:value || null}}),{options:[['','Unconfirmed'],['approved_benchmark','Approved benchmark']]}),
  ]),...(accountNodes.length?accountNodes:[empty('Load a reconciled snapshot to see exact account identifiers.','Accounts unavailable')]));
  const policies=config.mandate?.account_candidate_policy || {};
  const accounts=(result?.summary?.accounts || []).map(row=>row.account_id).filter(Boolean);
  replace('candidate-policy-form',form([
    ...accounts.map(id=>field(`${id} candidate comparison`,policies[id] || 'none',value=>{
      const map={...(resolved().mandate?.account_candidate_policy || {})};map[id]=value;patch({mandate:{account_candidate_policy:map}});
    },{options:[['none','None: compare held securities only'],['eligible_universe','Eligible universe: compare screened candidates']],
      help:'Unowned companies are compared for this account only where this says so.'})),
    field('Watchlist symbols (comma separated)',(config.signals?.watchlist || []).join(', '),value=>{
      patch({signals:{watchlist:value.split(',').map(item=>item.trim()).filter(Boolean)}});
    },{wide:true,help:'Exact listing symbols researched first when a review has budget. A watchlist is not a second universe.'}),
  ]),...(accounts.length?[]:[el('p','Load a reconciled snapshot to see exact account identifiers.','muted')]));
  jsonEditor('account-settings-json','Explicit account permissions, dealing rules, sleeve members and source-backed flows',{
    mandate:{account_permissions:config.mandate?.account_permissions || {},dealing_rules:config.mandate?.dealing_rules || {}},
    allocation:{sleeve_membership:config.allocation?.sleeve_membership || {},new_flows:config.allocation?.new_flows || {}},
  },value=>patch(value));
  replace('cost-tax-form',form([
    configField('allocation','transaction_cost_bps','Cost per side (basis points)',{type:'number'}),
    configField('allocation','return_hurdle','12-month improvement / dollar redeployed (%)',{type:'number',percent:true}),
    configField('allocation','min_trade_value','Minimum trade amount',{type:'number'}),
    configField('tax','enabled','Enable conditional US tax reserves',{type:'checkbox'}),
    configField('tax','short_term_rate','Short-term rate (%)',{type:'number',percent:true}),
    configField('tax','long_term_rate','Long-term rate (%)',{type:'number',percent:true}),
    configField('tax','wash_sale_window_verified','Wash-sale window has been verified',{type:'checkbox'}),
  ]));
  replace('method-form',form([
    configField('risk','lookback_years','Risk history (years)',{type:'number'}),
    configField('risk','min_weekly_observations','Minimum common weekly observations',{type:'number'}),
    configField('signals','min_sector_size','Minimum sector peers',{type:'number'}),
    configField('allocation','optimize','Enable experimental optimization',{type:'checkbox',help:'Initially off. This is an advanced experiment; it does not establish investment performance.'}),
  ]));
  jsonEditor('settings-json','Resolved editable configuration',config,value=>patch(value));renderDiff();
}
function renderData() {
  renderProviderHealth();
  const status=service.providers || {};
  const providerDraft={...status};
  const providerFields=form([
    field('Research mode',status.mode,value=>providerDraft.mode=value,{options:status.mode==='demo'?['demo','offline','live']:['offline','live']}),
    field('Price provider',status.price_provider,value=>providerDraft.price_provider=value,{options:[['csv','Validated CSV'],['yahoo','Yahoo prices']]}),
    field('SEC fundamentals',status.sec_enabled,value=>providerDraft.sec_enabled=value,{type:'checkbox'}),
    field('FRED macro',status.fred_enabled,value=>providerDraft.fred_enabled=value,{type:'checkbox'}),
    field('SEC contact user agent (stored privately)','',value=>providerDraft.sec_user_agent=value,{help:'Name and contact email for SEC requests; existing private setting is not displayed.'}),
  ]);
  replace('provider-status',providerFields,button('Save provider settings',async()=>{
    await api('/api/research/providers',providerDraft);await refreshService();renderData();await loadProviderHealth();announce('Provider settings saved for the next explicit refresh.');
  }),el('p',`${service.dataset || 'Source unavailable'} · ${service.mode || 'Mode unavailable'} · provider failures never substitute synthetic data.`,'muted'));
  if(!supplementalSupported || !supplemental){replace('supplemental-forms',empty('The configured source supplies canonical account records, or a holdings pull is needed first.','No supplemental template'));$('save-supplemental').disabled=true;}
  else {
    const nodes=[];
    for(const row of supplemental.accounts || []) {
      const block=el('section',null,'section-block');block.append(el('h3',row.name || `Source ${row.source_id}`),el('p',`Source ${row.source_id} · snapshot ${row.snapshot_id ?? 'unavailable'}`,'muted'));
      block.append(form(['account_type','currency','position_currency','valuation_date','cash','total_value','tax_jurisdiction'].map(key=>field(key==='total_value'?'Verified NAV (exact decimal)':key==='cash'?'Explicit cash (exact decimal)':friendly(key),row[key],value=>{row[key]=value || null;},{type:key==='valuation_date'?'date':'text'}))),field('This dated account snapshot is complete',row.complete,value=>row.complete=value,{type:'checkbox'}));nodes.push(block);
    }
    replace('supplemental-forms',...(nodes.length?nodes:[empty('Pull holdings, then reload this page to map the collected accounts.','No account snapshots')]));
    $('save-supplemental').disabled=false;
    jsonEditor('supplemental-json','Snapshot-specific account facts, exact security identities and tax lots',supplemental,value=>{
      if(!Array.isArray(value.accounts) || !Array.isArray(value.securities))throw new Error('Supplemental input needs accounts and securities arrays.');
      supplemental=clone(value);
    });
  }
  replace('source-manifest',autoTable(result?.sources || []),disclosure('Retained fund holdings and look-through source observations',autoTable(result?.observations?.fund_holdings?.rows || []),el('p',result?.observations?.fund_holdings?.scope || 'Missing underlying weights remain unclassified.','muted')));replace('all-issues',result?listIssues(result.issues):empty());
  replace('job-history',autoTable(service.jobs || [],['job_id','kind','status','created_at','error']));
}
let ledgerText='';
function renderLedgerEvaluation() {
  const existing=$('ledger-evaluation');if(existing)return;
  const details=el('details');details.id='ledger-evaluation';details.append(el('summary','Comparator ledgers and advanced evaluation'));
  details.append(el('p','Compare frozen benchmark, feasible policy, no-discretionary-change and actual-decision ledgers using consistent flows and observed prices. Imported coverage and execution assertions must be verified.','muted'));
  const input=el('textarea');input.id='ledger-evaluation-json';input.setAttribute('aria-label','Comparator events and endpoint prices JSON');input.rows=8;
  input.value=ledgerText || JSON.stringify({events_by_arm:{},end_prices:[],end_date:null,coverage_confirmed:false,execution_confirmed:{}},null,2);
  input.addEventListener('input',()=>{ledgerText=input.value;});
  const file=el('input');file.type='file';file.accept='.json,application/json';file.setAttribute('aria-label','Import comparator evaluation JSON');file.addEventListener('change',async()=>{try{if(file.files[0]){input.value=await file.files[0].text();ledgerText=input.value;}}catch(failure){error(failure.message);}});
  const records=el('div');records.id='comparator-records';
  details.append(input,file,button('Evaluate comparator ledgers',async()=>{
    if(!draft.baseRunId)throw new Error('Choose a saved run first.');
    const value=JSON.parse(input.value);await submitJob('evaluate_ledgers',{...value,run_id:draft.baseRunId},true);
  }),button('Load frozen baselines and evaluations',async()=>{
    if(!draft.baseRunId)throw new Error('Choose a saved run first.');
    const base=draft.baseRunId;
    const [baselines,evaluations]=await Promise.all([
      api('/api/research/records?kind=comparator'),
      api(`/api/research/records?kind=evaluation&run_id=${encodeURIComponent(base)}`),
    ]);
    if(base!==draft.baseRunId)return;
    records.replaceChildren(el('h3','Frozen baselines'),el('p','Shared prospective ledgers retain their original run IDs across later reviews.','muted'),autoTable(Array.isArray(baselines)?baselines:baselines.records || []),el('h3','Saved evaluations for the selected run'),autoTable(Array.isArray(evaluations)?evaluations:evaluations.records || []));
  },'quiet'),records);
  $('evaluation-results').parentElement.append(details);
}
function renderContext() {
  const runs=service.runs || [];
  // One selector over two dated views: the newest collection, or one saved analysis.
  const options=[[CURRENT_VIEW,'Current holdings (no analysis)'],
    ...runs.map(run=>[run.run_id,`${run.as_of || run.created_at || 'Saved run'} · ${run.run_id.slice(0,12)}`])];
  $('research-run').replaceChildren(...options.map(([value,label])=>{const option=el('option',label);option.value=value;return option;}));
  $('research-run').value=view==='saved' && draft.baseRunId?draft.baseRunId:CURRENT_VIEW;
  const accounts=result?.summary?.accounts || [];
  if(account && !accounts.some(row=>row.account_id===account))account='';
  $('account-scope').replaceChildren(...[{account_id:'',name:'All accounts'},...accounts].map(row=>{const option=el('option',row.name || row.account_id);option.value=row.account_id;return option;}));$('account-scope').value=account;
  $('valuation-context').textContent=`Valuation ${result?.metadata?.valuation_date || 'date unavailable'}`;
  $('cutoff-context').textContent=`Decision cutoff ${result?.timeline?.decision_cutoff || 'unavailable'}`;
  const mode=result?.metadata?.mode || service.mode;
  $('research-mode').textContent=mode==='demo'?'SYNTHETIC DEMO · example data':`${friendly(mode || 'unavailable')} · ${service.dataset || 'source unavailable'}`;$('research-mode').className=`badge ${mode==='demo'?'demo':''}`;
  if(!$('review-date').value)$('review-date').value=service.default_as_of || '';
  if(!$('evaluation-date').value)$('evaluation-date').value=new Date().toISOString().slice(0,10);
}
function renderAll() {
  renderContext();renderOverview();renderHoldings();renderScreen();renderCompany();renderScenarios();renderReview();renderSettings();renderData();renderState(true);
}
function draftChoice(message) {
  return new Promise(resolve=>{
    const dialog=$('draft-dialog');$('draft-dialog-message').textContent=message;
    const choose=value=>{dialog.close();for(const node of dialog.querySelectorAll('[data-draft-choice]'))node.onclick=null;dialog.oncancel=null;resolve(value);};
    for(const node of dialog.querySelectorAll('[data-draft-choice]'))node.onclick=()=>choose(node.dataset.draftChoice);
    dialog.oncancel=event=>{event.preventDefault();choose('cancel');};dialog.showModal();
  });
}
async function refreshService() { service=await api('/api/research'); }
// What changed is stated against the newest earlier review of the same kind, which the
// service names for each run. A previous run that cannot be read is simply not compared.
async function loadPreviousRun(id,owner) {
  if(owner!==draft.baseRunId)return;  // The run this comparison belongs to is no longer shown.
  previousRun=null;
  if(id){
    try {
      const response=await api(`/api/research/runs/${encodeURIComponent(id)}`);
      if(owner!==draft.baseRunId)return;
      previousRun={run_id:id,result:response.result};
    } catch { previousRun=null; }
  }
  try { renderWhatChanged(); } catch { /* the panel keeps its last state */ }
}
async function refreshPreviousRun(runId) {
  if(!runId){previousRun=null;return;}
  try { await loadPreviousRun((await api(`/api/research/runs/${encodeURIComponent(runId)}`)).previous_run_id,runId); }
  catch { previousRun=null; }
}
async function loadDecisions() {
  if(!draft.baseRunId){decisionRecords=[];renderDecisionHistory();return;}
  const base=draft.baseRunId;
  try{const records=await api(`/api/research/records?kind=decision&run_id=${encodeURIComponent(base)}`);if(base===draft.baseRunId){decisionRecords=Array.isArray(records)?records:records.records || [];renderDecisionHistory();}}
  catch(failure){error(failure.message);}
}
async function loadRun(id,ask=true) {
  if(ask && draft.dirty){const choice=await draftChoice('Retain this run’s draft for later, discard it, or cancel loading another run.');if(choice==='cancel'){restoreRunSelection();return;}if(choice==='discard')sessionStorage.removeItem(draftKey(draft.baseRunId));else persist();}
  const ticket=gate.next();busy=true;renderState();announce('Loading saved inputs and results…');
  try {
    const response=await api(`/api/research/runs/${encodeURIComponent(id)}`);if(!gate.current(ticket))return;
    saved=response;result=response.result;draft=restoreDraft(sessionStorage,id,response.config,response.workspace);selectedCandidate='';latestEvaluation=null;
    view='saved';previousRun=null;
    $('comparator-records')?.replaceChildren();$('run-comparison').replaceChildren();renderAll();await loadDecisions();loadOperationBehindRun();
    loadPreviousRun(response.previous_run_id,id);
  } catch(failure){error(`${failure.message} The previous result remains visible.`);if(gate.current(ticket))restoreRunSelection();}
  finally{if(gate.current(ticket)){busy=false;renderState(true);}}
}
const wait = milliseconds => new Promise(resolve=>setTimeout(resolve,milliseconds));
async function submitJob(kind,payload,evaluationOnly=false) {
  const ticket=gate.next(), revision=draft.revision, base=draft.baseRunId;
  busy=true;renderState();announce(`${friendly(kind)} submitted…`);
  try {
    let job=await api('/api/research/jobs',{kind,request_key:crypto.randomUUID(),...payload});
    while(!['complete','failed'].includes(job.status)){
      if(!gate.current(ticket))return;
      announce(`${friendly(job.kind)} · ${job.status}. Previous results remain available.`);
      await wait(650);job=await api(`/api/research/jobs/${encodeURIComponent(job.job_id)}`);
    }
    if(job.status==='failed')throw new Error(job.error || 'The calculation failed.');
    if(!gate.current(ticket))return;
    await refreshService();
    if(!gate.current(ticket))return;
    // A completed review repopulates the FX and listing caches and can change every
    // snapshot id, so the open exceptions are reloaded with the holdings.
    if(kind==='monthly' || kind==='save')loadCurrent().then(loadExceptions);
    if(evaluationOnly){latestEvaluation=job.output;renderReview();announce('Evaluation saved.');return;}
    if(draft.revision!==revision || draft.baseRunId!==base){announce('An earlier calculation completed. Your newer draft was retained; recalculate it before saving.');return;}
    const response=job.output;result=response.result;
    if(kind==='preview')draft=acceptPreview(draft,revision,job.job_id);
    else {
      saved=response;
      const newId=result.run_id;
      // A monthly review carries the draft's own patch into the run it produces, so a
      // draft keyed to no run is retired here rather than orphaned.
      if(kind==='save' || !base)sessionStorage.removeItem(draftKey(base));
      draft=newDraft(newId,response.config,response.workspace);selectedCandidate='';latestEvaluation=null;view='saved';$('comparator-records')?.replaceChildren();$('run-comparison').replaceChildren();await loadDecisions();await refreshPreviousRun(newId);
    }
    persist();renderAll();
  } catch(failure){error(`${failure.message} The previous usable result and your draft have been retained.`);}
  finally{if(gate.current(ticket)){busy=false;renderState(true);}}
}
async function monthly(refresh=false) {
  if(Object.keys(draft.rawEditors).length)throw new Error('Apply or discard advanced JSON edits first.');
  if(draft.dirty){const choice=await draftChoice('A new input review is separate from your frozen draft. Retain keeps the draft and includes its applied assumptions in the new review; discard uses saved assumptions.');if(choice==='cancel')return;
    if(choice==='discard'){sessionStorage.removeItem(draftKey(draft.baseRunId));draft=newDraft(draft.baseRunId,saved?.config || service.config,saved?.workspace || {});}else persist();
  }
  await submitJob('monthly',{as_of:$('review-date').value || service.default_as_of,refresh,patch:draft.patch,workspace:draft.workspace});
}
// One monthly operation: collect, resolve, fetch, analyze, publish. The page follows the
// durable workflow record rather than its own memory, so a reload during an operation
// resumes the same progress and a duplicate click stays the same operation.
const stageLabels={collecting:'Collecting',resolving:'Resolving',fetching:'Fetching',analyzing:'Analyzing',publishing:'Publishing'};
const stageOrder=Object.keys(stageLabels);
const stageClasses={pending:'',running:'dirty',complete:'complete',skipped:'warning',failed:'failed',cancelled:'warning'};
const workflowHeadlines={queued:'Update & analyze queued…',running:'Update & analyze running…',waiting:'Waiting for a collection that is already running…',complete:'Review saved.',failed:'The review did not complete; the last usable review is unchanged.',cancelled:'Cancelled before publishing; the last usable review is unchanged.'};
const liveWorkflow = status => ['queued','running','waiting'].includes(status);
let workflowRecord=null, workflowTicket=0, workflowBusy=false, workflowRendered=null;
// Operations behind previously saved runs, read once each so an older run still names
// what its own operation could and could not do.
const workflowNotes=new Map();
function workflowHeadline(workflow) {
  const stage=workflow?.stage?` · ${stageLabels[workflow.stage] || friendly(workflow.stage)}`:'';
  return `${workflowHeadlines[workflow?.status] || friendly(workflow?.status)}${liveWorkflow(workflow?.status)?stage:''}`;
}
// Stage and provider states the owner still has to act on; an operation that answered
// everything says so rather than listing five completed stages.
function workflowSummary(workflow) {
  const stages=workflow?.stages || {}, providers=workflow?.providers || {};
  const stalled=stageOrder.filter(name=>['skipped','failed','cancelled'].includes(stages[name]?.status))
    .map(name=>`${stageLabels[name].toLowerCase()} ${stages[name].status}`);
  const partial=Object.entries(providers).filter(([,record])=>['failed','partial'].includes(record?.status))
    .map(([name,record])=>`${friendly(name).toLowerCase()} ${record.status}`);
  return [stalled.length?`stages: ${stalled.join(', ')}`:'',partial.length?`providers: ${partial.join(', ')}`:''].filter(Boolean).join(' · ');
}
function operationBehindRun() {
  const id=result?.metadata?.workflow_id;
  if(!id)return null;
  return workflowRecord?.workflow_id===id?workflowRecord:workflowNotes.get(id) || null;
}
function analysisOperationNote() {
  const operation=operationBehindRun();
  if(!operation)return '';
  const summary=workflowSummary(operation);
  return ` · operation ${friendly(operation.status).toLowerCase()}${summary?` · ${summary}`:' · every stage completed'}`;
}
async function loadOperationBehindRun() {
  const id=result?.metadata?.workflow_id;
  if(!id || operationBehindRun() || workflowNotes.has(id))return;
  // An operation record that cannot be read is simply not described in the caption.
  try { workflowNotes.set(id,await api(`/api/research/workflows/${encodeURIComponent(id)}`)); }
  catch { workflowNotes.set(id,null); }
  try { renderOverview(); } catch { /* the caption keeps its last state */ }
}
function stageChip(name,record) {
  const state=record?.status || 'pending';
  const node=el('div',null,`stage-chip ${state}`);node.dataset.stage=name;node.dataset.status=state;
  node.append(el('span',stageLabels[name],'stage-name'),el('span',friendly(state),`badge ${stageClasses[state] ?? ''}`));
  if(record?.message)node.append(el('span',record.message,'stage-message'));
  return node;
}
// The record is read once a second for the whole operation. Rebuilding an unchanged strip
// would restate the same progress to assistive technology on every poll, so only a real
// change to what the strip says redraws it.
const workflowShape = workflow => JSON.stringify({status:workflow.status,stage:workflow.stage,
  stages:workflow.stages,providers:workflow.providers,error:workflow.error});
function renderWorkflow(workflow) {
  workflowRecord=workflow && typeof workflow==='object'?workflow:null;
  const strip=$('workflow-progress');
  if(!workflowRecord){workflowRendered=null;strip.replaceChildren();strip.hidden=true;$('cancel-workflow').hidden=true;renderState();return;}
  const shape=workflowShape(workflowRecord);
  if(shape!==workflowRendered){
    workflowRendered=shape;
    const stages=workflowRecord.stages || {};
    const chips=el('div',null,'stage-chips');chips.append(...stageOrder.map(name=>stageChip(name,stages[name])));
    const actions=stageOrder.map(name=>stages[name]?.action_needed).filter(Boolean)
      .filter((message,index,all)=>all.indexOf(message)===index).map(message=>el('p',message,'notice'));
    replace('workflow-progress',el('p',workflowHeadline(workflowRecord),'stage-headline'),chips,...actions,
      workflowRecord.error?el('p',workflowRecord.error,'notice'):null);
  }
  strip.hidden=false;$('cancel-workflow').hidden=!liveWorkflow(workflowRecord.status);
  renderState();
}
async function pollWorkflow(workflowId) {
  const ticket=++workflowTicket;
  let workflow;
  while(true){
    workflow=await api(`/api/research/workflows/${encodeURIComponent(workflowId)}`);
    if(ticket!==workflowTicket)return;
    renderWorkflow(workflow);
    if(!liveWorkflow(workflow.status))break;
    // The stage strip carries the progress; the one spoken region is left alone until the
    // operation starts and finishes, so a long collection is not narrated once a second.
    await wait(1000);
    if(ticket!==workflowTicket)return;
  }
  workflowBusy=false;renderState();
  await finishWorkflow(workflow);
}
// A completed operation publishes exactly one run, so the page loads that run instead of
// guessing; a cancelled or failed one leaves the previously loaded review in place.
async function finishWorkflow(workflow) {
  if(workflow.status==='complete' && workflow.run_id){
    await refreshService();
    await loadRun(workflow.run_id,false);
    // The operation used saved settings, so a draft retained before any run existed is
    // carried onto the run just published rather than left under an unreachable key.
    const carried=adoptOrphanDraft(sessionStorage,draft);
    if(carried!==draft){draft=carried;persist();renderAll();
      announce('The retained draft was carried onto the review just saved. Recalculate it before saving.');}
  } else if(workflow.status==='failed'){
    error(`${workflow.error || 'The review did not complete.'} The last usable review and your draft have been retained.`);
  }
  // A completed review repopulates the FX and listing caches and can change every
  // snapshot id, so the open exceptions reload with the holdings.
  await loadCurrent();await loadExceptions();await loadProviderHealth();
  renderWorkflow(workflow);
  const summary=workflowSummary(workflow);
  announce(workflow.status==='complete' && workflow.run_id
    ?`Review saved · run ${String(workflow.run_id).slice(0,12)}${summary?` · ${summary}`:''}`
    :workflowHeadline(workflow));
}
async function startWorkflow() {
  if(workflowBusy)return;  // A second click during an operation is the same operation.
  if(Object.keys(draft.rawEditors).length)throw new Error('Apply or discard advanced JSON edits first.');
  workflowBusy=true;renderState();
  try {
    if(draft.dirty){
      const choice=await draftChoice('Update & analyze collects and analyzes fresh holdings using saved settings, separate from your frozen draft. Retain keeps the draft for later; discard clears it.');
      if(choice==='cancel')return;
      if(choice==='discard'){sessionStorage.removeItem(draftKey(draft.baseRunId));draft=newDraft(draft.baseRunId,saved?.config || service.config,saved?.workspace || {});}
      else persist();
    }
    const workflow=await api('/api/research/workflows',{operation_key:crypto.randomUUID(),review_kind:'current',collect:true});
    renderWorkflow(workflow);announce(workflowHeadline(workflow));
    await pollWorkflow(workflow.workflow_id);
  } finally { workflowBusy=false;renderState(); }
}
// Restart safety: the operation outlives this page, so a reload rejoins the one the
// service reports as active and otherwise shows what the last one did.
function resumeWorkflow() {
  const active=service.active_workflow;
  if(active && liveWorkflow(active.status)){
    workflowBusy=true;renderWorkflow(active);
    pollWorkflow(active.workflow_id).catch(failure=>{
      workflowBusy=false;renderState();error(`${failure.message} The last usable review is unchanged.`);
    });
    return;
  }
  if(service.last_workflow)renderWorkflow(service.last_workflow);
}
// Provider health: what each source has already answered and the next step when it
// cannot. Names of credentials are shown, never their values.
let providerHealth=null;
function providerState(row) {
  if(typeof row.status==='string' && row.status)return row.status;
  if(row.enabled===false)return 'off';
  if(row.action)return 'attention';
  return numeric(row.cached_responses)?'ready':'enabled';
}
function providerFacts(row) {
  const facts=[];
  if(row.version)facts.push(`Extension ${row.version}`);
  if(row.required_version)facts.push(`Requires ${row.required_version}`);
  if(typeof row.dependency_installed==='boolean')facts.push(row.dependency_installed?'Optional package installed':'Optional package not installed');
  if(typeof row.contact_configured==='boolean')facts.push(row.contact_configured?'Contact configured':'Contact not configured');
  if(row.env_var)facts.push(`${row.env_var} ${row.credential_present?'is set':'is not set'}`);
  if(typeof row.cached_responses==='number')facts.push(`${row.cached_responses} archived responses`);
  if(row.last_received_at)facts.push(`Last answer ${when(row.last_received_at)}`);
  return facts;
}
function providerCard(row) {
  const node=el('article',null,'provider-card');node.dataset.provider=row.name || '';
  const heading=el('div',null,'section-heading');
  heading.append(el('h3',friendly(row.name || 'provider')),badge(providerState(row)));
  node.append(heading,el('p',providerFacts(row).join(' · ') || 'No archived responses yet.','muted'));
  if(row.action)node.append(el('p',row.action,'notice'));
  return node;
}
function renderProviderHealth() {
  if(!providerHealth){replace('provider-health',el('p','Loading provider status…','muted'));return;}
  if(providerHealth.unavailable){replace('provider-health',el('p',providerHealth.unavailable,'muted'));return;}
  const cards=objectRows(providerHealth.providers).map(providerCard);
  replace('provider-health',...(cards.length?cards:[empty('No providers are configured for this source.','No providers')]));
}
async function loadProviderHealth() {
  try {
    const response=await api('/api/research/providers/health');
    providerHealth=response && typeof response==='object'?response:{unavailable:'The provider status response was unreadable.'};
  } catch(failure){ providerHealth={unavailable:`Provider status could not be loaded. ${failure?.message || ''}`.trim()}; }
  try { renderProviderHealth(); }
  catch(failure){ providerHealth={unavailable:`Provider status could not be displayed: ${failure?.message || failure}`};
    try { renderProviderHealth(); } catch { /* the panel keeps its last state */ } }
}
for(const node of document.querySelectorAll('[data-page]'))node.addEventListener('click',()=>navigate(node.dataset.page));
for(const node of document.querySelectorAll('[data-go]'))node.addEventListener('click',()=>navigate(node.dataset.go));
document.addEventListener('portfolio:holdings',()=>navigate('holdings'));
document.addEventListener('portfolio:sources-changed',()=>{loadCurrent().then(loadExceptions);});
window.addEventListener('hashchange',()=>navigate(location.hash.slice(1)));
// One tablist behavior for the company panels and for the security detail: a click or an
// arrow key selects, and the selected panel is the only one on screen.
function wireTabs(attribute,names,panelId,onSelect) {
  const tabs=[...document.querySelectorAll(`[${attribute}]`)];
  const select=node=>{
    for(const tab of tabs)tab.setAttribute('aria-selected',String(tab===node));
    const chosen=node.getAttribute(attribute);
    for(const name of names)$(panelId(name)).hidden=name!==chosen;
    if(onSelect)onSelect(chosen);
  };
  for(const node of tabs){
    node.addEventListener('click',()=>select(node));
    node.addEventListener('keydown',event=>{
      if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
      event.preventDefault();const index=tabs.indexOf(node);
      const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
      select(tabs[next]);tabs[next].focus();
    });
  }
}
wireTabs('data-company-tab',['eps','dcf','evidence'],name=>`company-${name}`);
wireTabs('data-detail-tab',['evidence','calculation','assumptions','limitations'],name=>`detail-${name}`);
function wire(id,action) {
  $(id).addEventListener('click',async()=>{
    try{error();await action();}catch(failure){error(failure.message);}finally{renderState();}
  });
}
wire('update-analyze',()=>startWorkflow());
wire('cancel-workflow',async()=>{
  if(!workflowRecord)throw new Error('No operation is running.');
  renderWorkflow(await api(`/api/research/workflows/${encodeURIComponent(workflowRecord.workflow_id)}/cancel`,{}));
  announce('Cancellation requested. The operation stops before publishing and the last usable review is unchanged.');
});
wire('monthly-review',()=>monthly(false));wire('refresh-research',()=>monthly(true));
wire('recalculate',async()=>{
  if(!draft.baseRunId)throw new Error('Run Update & analyze before recalculating frozen inputs.');
  await submitJob('preview',{base_run_id:draft.baseRunId,patch:draft.patch,workspace:draft.workspace});
});
wire('save-run',async()=>{
  if(!previewReady(draft))throw new Error('Recalculate the current draft before saving.');
  await submitJob('save',{preview_job_id:draft.previewJobId});
});
wire('reset-draft',async()=>{
  const choice=await draftChoice('Reset restores this saved run’s assumptions. Retain cancels the reset and keeps your edits; discard performs the reset.');
  if(choice!=='discard')return;
  sessionStorage.removeItem(draftKey(draft.baseRunId));draft=newDraft(draft.baseRunId,saved?.config || service.config,saved?.workspace || {});
  result=saved?.result || result;renderAll();
});
$('research-run').addEventListener('change',()=>{
  const choice=selectRun(service.runs || [],$('research-run').value);
  if(choice.view!=='saved'){
    view=CURRENT_VIEW;applyView();
    announce('Current holdings only. No analysis is displayed; choose a saved run to see one.');return;
  }
  // The view follows the run that actually loaded; a refused or cancelled load leaves
  // the selector, the view and the sections agreeing on the run still on screen.
  if(choice.runId===draft.baseRunId){view='saved';applyView();announce('Showing the analysis of the loaded saved run.');return;}
  loadRun(choice.runId).catch(failure=>{error(failure.message);restoreRunSelection();});
});
$('account-scope').addEventListener('change',()=>{account=$('account-scope').value;renderHoldings();announce('Account display changed. Portfolio calculations and constraints retain their saved scope.');});
$('holdings-search').addEventListener('input',renderHoldings);$('screen-search').addEventListener('input',renderScreen);
$('research-security').addEventListener('change',()=>{securityId=$('research-security').value;renderCompany();});
wire('open-security',()=>{const value=$('manual-security').value.trim();if(!value || ['__proto__','constructor','prototype'].includes(value))throw new Error('Enter a valid exact security identifier.');securityId=value;renderCompany();});
wire('close-security-dialog',()=>$('security-dialog').close());
wire('research-from-detail',()=>{$('security-dialog').close();securityId=detailId;renderCompany();navigate('research');});
$('scenario-horizon').addEventListener('change',async()=>{
  const months=Number($('scenario-horizon').value);if(months===horizon())return;
  const choice=await draftChoice('Changing the horizon clears incompatible return overrides, priors, probabilities, cash returns and EPS outcomes. Retain keeps your other draft edits; discard restores saved assumptions first. Cancel keeps the current horizon.');
  if(choice==='cancel'){$('scenario-horizon').value=horizon();return;}
  if(choice==='retain')persist();
  else draft=newDraft(draft.baseRunId,saved?.config || service.config,saved?.workspace || {});
  draft=changeHorizon(draft,months);persist();renderAll();announce('Horizon changed. Incompatible numerical overrides were cleared; review evidence horizons before recalculating.');
});
wire('save-settings',async()=>{
  if(Object.keys(draft.rawEditors).length)throw new Error('Apply or discard advanced JSON edits before saving settings.');
  await api('/api/research/settings',{patch:draft.patch});await refreshService();announce('Settings saved for future reviews. The current frozen draft and saved runs are unchanged.');
});
wire('save-supplemental',async()=>{
  if(!supplementalSupported || !supplemental)throw new Error('A supplemental template is unavailable.');
  const response=await api('/api/research/inputs',{kind:'supplemental',data:supplemental});announce(response.message || 'Supplemental inputs saved. Start a new review to use them.');
});
wire('import-csv',async()=>{
  const file=$('import-file').files[0];if(!file)throw new Error('Choose a CSV file to validate.');
  if(file.size>10*1024*1024)throw new Error('Use an import file smaller than 10 MB.');
  const response=await api('/api/research/inputs',{kind:$('import-kind').value,csv:await file.text()});announce(response.message || 'Input saved for the next review.');
});
$('decision-action').addEventListener('change',syncDecisionAlternative);
wire('save-decision',()=>openDecisionDialog());
wire('confirm-decision',()=>recordDecision());
wire('close-decision-dialog',()=>{decisionError();$('decision-dialog').close();});
wire('compare-runs',async()=>{
  const left=$('compare-left').value,right=$('compare-right').value;if(!left || !right)throw new Error('Choose two saved runs.');
  const response=await api(`/api/research/compare?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`);
  replace('run-comparison',Array.isArray(response.differences)?autoTable(response.differences):kv(response));
});
wire('evaluate-run',async()=>{
  const file=$('evaluation-file').files[0];if(!draft.baseRunId || !file)throw new Error('Choose a saved run and an observed-prices CSV file.');
  await submitJob('evaluate',{run_id:draft.baseRunId,prices_csv:await file.text(),evaluation_date:$('evaluation-date').value},true);
});
window.addEventListener('beforeunload',event=>{if(draft.dirty){persist();event.preventDefault();event.returnValue='';}});
async function initialize() {
  navigate(pages.includes(location.hash.slice(1))?location.hash.slice(1):'overview');
  try {
    const [local,status]=await Promise.all([api('/api/state'),api('/api/research'),loadCurrent().then(loadExceptions)]);token=local.token;service=status;bootstrap='ready';
    try{const response=await api('/api/research/supplemental');supplementalSupported=response.supported;supplemental=clone(response.saved || response.template || null);}catch(failure){error(`Supplemental input is unavailable: ${failure.message}`);}
    resumeWorkflow();loadProviderHealth();
    if(service.latest_run_id)await loadRun(service.latest_run_id,false);
    else{draft=restoreDraft(sessionStorage,null,service.config,{});renderAll();}
  } catch(failure){bootstrap='failed';error(`Research could not load: ${failure.message} Reload this page to try again. Yahoo collection remains available in Data and Holdings.`);renderAll();}
}
initialize();
