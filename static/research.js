import {clone, merge, numberOrNull, newDraft, editDraft, changeHorizon, previewReady,
  acceptPreview, draftKey, restoreDraft, RequestGate, differences, validateConfigurationPatch, validateCompanyInputs, reviseAssessmentInputs} from './research-state.js';

const $ = id => document.getElementById(id);
const labels = ['adverse', 'central', 'favorable'];
const pages = ['overview','holdings','research','scenarios','review','data','settings'];
const gate = new RequestGate();
let service = {}, result = null, saved = null, draft = newDraft(null), token = '', busy = false;
let page = 'overview', securityId = '', detailId = '', selectedCandidate = '', account = '';
let supplemental = null, supplementalSupported = false, decisionRecords = [], latestEvaluation = null;
let fieldCounter = 0;
const columnVisibility = new Set(['security_id','account_id','quantity','price','market_value','calculated_weight','currency']);
const text = value => value === null || value === undefined || value === '' ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value);
const friendly = value => String(value || '').replaceAll('_',' ').replace(/\b\w/g, letter => letter.toUpperCase());
const numeric = value => { try { return numberOrNull(value); } catch { return null; } };
const num = (value, digits=2) => numeric(value) === null ? '—' : numeric(value).toLocaleString(undefined,{maximumFractionDigits:typeof digits==='number'?digits:2});
const pct = value => numeric(value) === null ? '—' : `${num(numeric(value)*100,1)}%`;
const money = (value, currency=result?.summary?.currency) => numeric(value) === null ? '—' : `${num(value)}${currency ? ` ${currency}` : ''}`;
function el(tag, content, className) {
  const node = document.createElement(tag);
  if (content !== undefined && content !== null) node.textContent = String(content);
  if (className) node.className = className;
  return node;
}
function replace(id, ...children) { $(id).replaceChildren(...children.filter(Boolean)); }
function empty(message='Start a monthly review to calculate this view.', heading='No calculated result') {
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
  node.append(el('p',caption,'chart-note'));return node;
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
  page=next;
  for(const name of pages) $(`page-${name}`).hidden=name!==next;
  document.querySelectorAll('[data-page]').forEach(node=>{if(node.dataset.page===next)node.setAttribute('aria-current','page');else node.removeAttribute('aria-current');});
  $('page-title').textContent=friendly(next);history.replaceState(null,'',`#${next}`);
}
function renderState(writeStatus=false) {
  const unapplied=Object.keys(draft.rawEditors || {}).length;
  const state=busy?'calculating':unapplied?'dirty':previewReady(draft)?'preview':draft.dirty?'dirty':draft.baseRunId?'saved':'empty';
  $('draft-state').textContent=friendly(state);$('draft-state').className=`badge ${state}`;
  $('recalculate').disabled=busy || !draft.baseRunId || !!unapplied;
  $('recalculate').title=!draft.baseRunId?'Start a monthly review to retain base inputs.':unapplied?'Apply or discard advanced JSON edits first.':'';
  $('save-run').disabled=busy || !previewReady(draft) || !!unapplied;
  $('save-run').title=previewReady(draft)?'Save this calculation as an immutable child run.':'Recalculate the current draft before saving.';
  $('monthly-review').disabled=busy || !!unapplied;
  $('refresh-research').disabled=busy || !!unapplied;
  $('reset-draft').disabled=busy || !draft.dirty;
  if(!busy && writeStatus) announce(unapplied?'Advanced JSON has unapplied edits. Apply or discard them before calculating.':draft.dirty && !previewReady(draft)?'Draft changed · displayed results are out of date until recalculated.':previewReady(draft)?'Preview calculated · save a new run to retain this result.':draft.baseRunId?'Saved inputs · display filters leave the analytical scope unchanged.':'Run a monthly review to create the first retained input set.');
}
function renderDiff() {
  const changes=differences(draft.baseConfig,resolved());
  replace('resolved-diff',changes.length?table(changes,[{key:'field'},{key:'before',wrap:true},{key:'after',wrap:true}]):el('p','No configuration changes in this draft.','muted'));
}
function renderOverview() {
  const summary=result?.summary || {}, risk=result?.risk || {}, currency=summary.currency;
  replace('overview-metrics',metric('Portfolio NAV',money(summary.total_value,currency),'Declared account NAV'),
    metric('Explicit cash',summary.unknown_cash_accounts?.length?'Incomplete':money(summary.known_cash,currency),summary.unknown_cash_accounts?.length?`${summary.unknown_cash_accounts.length} accounts need cash evidence`:'Cash supplied with the snapshot'),
    metric('Coverage',pct(summary.coverage),summary.complete?'Accounts reconciled':'Unresolved amounts remain visible'),
    metric('Unclassified',money(summary.unclassified_value,currency),'Not assumed to be cash'));
  $('overview-caption').textContent=result?`${text(result.metadata?.valuation_date)} · ${summary.position_count ?? '—'} positions · ${friendly(result.metadata?.scope)}`:'Run a review to reconcile completed holdings and available research inputs.';
  $('exposure-state').replaceChildren(badge(result?.exposure_status));
  replace('issuer-chart',barChart(result?.issuer_exposure,'weight','issuer_id','Top issuer weights · portfolio NAV · direct and fund look-through'));
  replace('sector-chart',barChart(result?.sector_exposure,'weight','sector','Additive sector weights · portfolio NAV · unknown exposure retained'));
  replace('risk-metrics',metric('Volatility',pct(risk.annualized_volatility),'Annualized'),metric('Beta',num(risk.beta),'Approved benchmark'),metric('Tracking error',pct(risk.tracking_error),'Annualized'));
  replace('risk-history',lineChart(risk.history,risk.history_label || 'Hypothetical historical risk illustration'));
  $('risk-caption').textContent=`${risk.history_label || 'History unavailable'} · ${risk.observations ?? 0} common observations · ${friendly(risk.status)}`;
  replace('risk-detail',kv({status:risk.status,observations:risk.observations,max_drawdown:pct(risk.max_drawdown),volatility_interval:risk.volatility_interval}),
    autoTable(risk.risk_contributions || []),autoTable(risk.stresses || []),listIssues(risk.issues),
    disclosure('Five-year risk sensitivity',kv(risk.five_year_sensitivity || {status:'Unavailable; five-year history coverage is required.'})),
    disclosure('Benchmark history illustration',lineChart((risk.history || []).map(row=>({date:row.date,value:row.benchmark})),'Benchmark total-return history illustration')));
  replace('review-priorities',result?listIssues(result.issues?.slice(0,8),'No blocking data issues reported. Review the assumptions before considering changes.'):empty());
  const macros=(result?.macro || []).map(row=>{
    const node=el('article',null,'macro-card');node.append(metric(row.series_id,num(row.latest_value),`${row.units || 'Units unavailable'} · ${row.observation_date || 'Undated'}`),lineChart(row.history,`${row.series_id} observed history`),el('p',`Vintage ${row.vintage_date || 'unknown'} · context only`,'chart-note'));return node;
  });replace('macro-panels',...(macros.length?macros:[empty('Import dated macro vintages or configure a provider.','Macro observations unavailable')]));
}
function securityRow(id) { return (result?.holdings || []).find(row=>row.security_id===id) || (result?.signals || []).find(row=>row.security_id===id) || {}; }
function securityButton(id) { return button(id || 'Unresolved',()=>showSecurity(id),'text-button'); }
function showSecurity(id) {
  detailId=id;const holdings=(result?.holdings || []).filter(row=>row.security_id===id), score=(result?.signals || []).find(row=>row.security_id===id);
  $('security-dialog-title').textContent=id || 'Unresolved security';replace('security-dialog-content',autoTable(holdings),score?kv(score):empty('This instrument has no company score.','Company score unavailable'));
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
function epsDefaults() {
  const security=securityRow(securityId);
  return {starting_price:numeric(security.price),currency:security.currency || resolved().mandate?.base_currency || null,
    horizon_months:horizon(),eps_convention:'forward',pe_convention:'forward',scenarios:labels.map(label=>({label,eps:null,pe:null,distributions_per_starting_share:null}))};
}
function valuationValue(kind) { return draft.workspace.valuations?.[securityId]?.[kind]; }
function updateValuation(kind, mutate) {
  workspaceEdit(next=>{
    const values=next.valuations[securityId] ||= {origin:'manual',source:'User scenario input',version:1,horizon_months:horizon()};
    const model=clone(values[kind] || (kind==='eps'?epsDefaults():dcfDefaults()));mutate(model);values[kind]=model;
    values.version=(values.version || 0)+1;
  });
}
function sensitivity(id,grid,type) {
  if(!grid){replace(id,empty('Supply complete inputs and recalculate to compare drivers.','Sensitivity unavailable'));return;}
  const x=type==='eps'?grid.pe_values:grid.terminal_growth_rates, y=type==='eps'?grid.eps_values:grid.discount_rates;
  const rows=(grid.cells || []).map((cells,index)=>({axis:y[index],...Object.fromEntries(cells.map((cell,j)=>[`c${j}`,cell]))}));
  const node=table(rows,[{key:'axis',label:type==='eps'?'EPS / P/E':'Discount / growth',render:type==='eps'?num:pct},...x.map((value,index)=>({key:`c${index}`,label:type==='eps'?num(value):pct(value),render:cell=>{
    const item=el('span',money(type==='eps'?cell.horizon_price:cell.value_per_share,grid.currency));item.title=cell.issues?.join('; ') || (type==='eps'?`Horizon return ${pct(cell.total_return)}`:'Conditional intrinsic value per share');return item;
  }}))],type==='eps'?'Horizon price per share; hover a cell for total return or missing reason.':'Conditional intrinsic value per share; invalid terminal combinations remain unavailable.');
  node.classList.add('sensitivity');replace(id,node);
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
  replace('security-summary',kv({security_id:securityId,issuer:security.issuer_id,currency:security.currency,horizon:`${horizon()} months`,basis:'Subjective assumptions; prices may be unavailable'}),
    disclosure('Retained financial statements and source observations',autoTable((result?.observations?.fundamentals?.rows || []).filter(row=>row.security_id===securityId)),el('p',result?.observations?.fundamentals?.scope || 'Only facts eligible at the saved cutoff are displayed.','muted')));
  renderEPS();renderDCF();renderEvidence();
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
  replace('stress-editor',form(['stress_equity_shock','stress_growth_shock','stress_rates_shock'].map(key=>field(`${friendly(key)} (%)`,config.risk?.[key],value=>patch({risk:{[key]:value}}),{type:'number',percent:true}))));
  jsonEditor('scenario-json','Allocation and risk assumptions',{allocation:config.allocation,risk:config.risk},value=>patch(value));
  const outcome=result?.scenarios || {};
  replace('scenario-chart',barChart(outcome.scenarios,'return','scenario',`${outcome.horizon_months || horizon()}-month hypothetical portfolio total returns; bars show magnitude, labels show sign`));
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
  replace('trade-basket',autoTable(candidate.proposals || []));replace('basket-funding',autoTable(candidate.accounts || []));
  replace('basket-reasons',listIssues([...(allocation.issues || []),...(candidate.issues || []),...(candidate.decisions || []).map(row=>({message:`${row.security_id || row.account_id || ''}: ${row.reason || row.action}`}))]),candidate.validation?kv(candidate.validation):null,
    disclosure('Candidate joint outcomes after costs and tax reserve',table(Object.entries(candidate.scenarios || {}).map(([scenario,value])=>({scenario,return:value})),[{key:'scenario'},{key:'return',render:pct}],'Conditional horizon returns for the complete funded basket.')),
    disclosure('Trading-cost sensitivities',autoTable(candidate.cost_sensitivities || [])),
    disclosure('Substitution-hurdle sensitivities',autoTable(candidate.hurdle_sensitivities || [])),
    disclosure('Candidate method and solver diagnostics',kv(allocation.solver || {})));
  $('save-decision').disabled=!draft.baseRunId || busy;
  replace('decision-history',autoTable(decisionRecords));
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
    await api('/api/research/providers',providerDraft);await refreshService();renderData();announce('Provider settings saved for the next explicit refresh.');
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
  $('research-run').replaceChildren(...(runs.length?runs:[{run_id:'',as_of:'No saved run'}]).map(run=>{const option=el('option',`${run.as_of || run.created_at || 'Saved run'}${run.run_id?` · ${run.run_id.slice(0,12)}`:''}`);option.value=run.run_id;return option;}));$('research-run').value=draft.baseRunId || '';
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
async function loadDecisions() {
  if(!draft.baseRunId){decisionRecords=[];return;}
  const base=draft.baseRunId;
  try{const records=await api(`/api/research/records?kind=decision&run_id=${encodeURIComponent(base)}`);if(base===draft.baseRunId){decisionRecords=Array.isArray(records)?records:records.records || [];replace('decision-history',autoTable(decisionRecords));}}
  catch(failure){error(failure.message);}
}
async function loadRun(id,ask=true) {
  if(ask && draft.dirty){const choice=await draftChoice('Retain this run’s draft for later, discard it, or cancel loading another run.');if(choice==='cancel'){$('research-run').value=draft.baseRunId || '';return;}if(choice==='discard')sessionStorage.removeItem(draftKey(draft.baseRunId));else persist();}
  const ticket=gate.next();busy=true;renderState();announce('Loading saved inputs and results…');
  try {
    const response=await api(`/api/research/runs/${encodeURIComponent(id)}`);if(!gate.current(ticket))return;
    saved=response;result=response.result;draft=restoreDraft(sessionStorage,id,response.config,response.workspace);selectedCandidate='';latestEvaluation=null;
    $('comparator-records')?.replaceChildren();$('run-comparison').replaceChildren();renderAll();await loadDecisions();
  } catch(failure){error(`${failure.message} The previous result remains visible.`);}
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
    if(evaluationOnly){latestEvaluation=job.output;renderReview();announce('Evaluation saved.');return;}
    if(draft.revision!==revision || draft.baseRunId!==base){announce('An earlier calculation completed. Your newer draft was retained; recalculate it before saving.');return;}
    const response=job.output;result=response.result;
    if(kind==='preview')draft=acceptPreview(draft,revision,job.job_id);
    else {
      saved=response;
      const newId=result.run_id;
      if(kind==='save')sessionStorage.removeItem(draftKey(base));
      draft=newDraft(newId,response.config,response.workspace);selectedCandidate='';latestEvaluation=null;$('comparator-records')?.replaceChildren();$('run-comparison').replaceChildren();await loadDecisions();
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
for(const node of document.querySelectorAll('[data-page]'))node.addEventListener('click',()=>navigate(node.dataset.page));
for(const node of document.querySelectorAll('[data-go]'))node.addEventListener('click',()=>navigate(node.dataset.go));
document.addEventListener('portfolio:holdings',()=>navigate('holdings'));
window.addEventListener('hashchange',()=>navigate(location.hash.slice(1)));
for(const node of document.querySelectorAll('[data-company-tab]')){
  node.addEventListener('click',()=>{
    for(const button of document.querySelectorAll('[data-company-tab]'))button.setAttribute('aria-selected',String(button===node));
    for(const name of ['eps','dcf','evidence'])$(`company-${name}`).hidden=name!==node.dataset.companyTab;
  });
  node.addEventListener('keydown',event=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
    event.preventDefault();const tabs=[...document.querySelectorAll('[data-company-tab]')],i=tabs.indexOf(node);
    const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(i+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
    tabs[next].click();tabs[next].focus();
  });
}
function wire(id,action) {
  $(id).addEventListener('click',async()=>{
    try{error();await action();}catch(failure){error(failure.message);}finally{renderState();}
  });
}
wire('monthly-review',()=>monthly(false));wire('refresh-research',()=>monthly(true));
wire('recalculate',async()=>{
  if(!draft.baseRunId)throw new Error('Start a monthly review before recalculating frozen inputs.');
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
$('research-run').addEventListener('change',()=>{if($('research-run').value)loadRun($('research-run').value).catch(failure=>error(failure.message));});
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
wire('save-decision',async()=>{
  if(!draft.baseRunId)throw new Error('Choose a saved run before recording a decision.');
  if(draft.dirty)throw new Error('Save or reset the current draft so this decision refers to a retained run.');
  const rationale=$('decision-rationale').value.trim();if(!rationale)throw new Error('Record a decision rationale.');
  await api('/api/research/decision',{run_id:draft.baseRunId,candidate_id:selectedCandidate || null,action:$('decision-action').value,rationale});
  $('decision-rationale').value='';await loadDecisions();announce('Decision recorded without changing the saved forecast.');
});
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
    const [local,status]=await Promise.all([api('/api/state'),api('/api/research')]);token=local.token;service=status;
    try{const response=await api('/api/research/supplemental');supplementalSupported=response.supported;supplemental=clone(response.saved || response.template || null);}catch(failure){error(`Supplemental input is unavailable: ${failure.message}`);}
    if(service.latest_run_id)await loadRun(service.latest_run_id,false);
    else{draft=restoreDraft(sessionStorage,null,service.config,{});renderAll();}
  } catch(failure){error(`Research could not load: ${failure.message} Yahoo collection remains available in Data and Holdings.`);renderAll();}
}
initialize();
