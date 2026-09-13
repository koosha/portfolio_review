const $ = id => document.getElementById(id);
let state = {sources: [], browser: {}}, sourceSignature = '', activeSource = null, savingSelection = false;
let stateRequest = 0, sourceViewRequest = 0;

function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}
function message(text) { $('notice').textContent = text; $('notice').hidden = !text; }
function when(value) { return value ? new Date(value).toLocaleString() : 'Never'; }
async function api(path, body) {
  const options = body === undefined ? {} : {method: 'POST', headers: {
    'Content-Type': 'application/json', 'X-Local-Token': state.token || ''
  }, body: JSON.stringify(body)};
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Request failed.');
  return result;
}
function button(text, action, className = 'secondary') {
  const el = node('button', text, className);
  el.type = 'button';
  el.onclick = async () => {
    el.disabled = true;
    try { message(''); await action(); }
    catch (error) { message(error.message); }
    finally { el.disabled = false; }
  };
  return el;
}
async function browserAction(action, source_ids = []) {
  await api('/api/browser', {action, source_ids});
  await reload();
}
function renderHoldingTotal(summary, detailed = false) {
  const block=node('div',undefined,'holding-total');
  if(!summary) {block.append(node('div','Market value: —','holding-total-value'));return block;}
  for(const group of summary.groups) {
    const suffix=group.currency ? ` ${group.currency}` : '';
    block.append(node('div',`Market value: ${group.formatted_total ?? '—'}${suffix}`,'holding-total-value'));
  }
  const notes=[];
  if(summary.cash_row_count) notes.push('Includes captured cash');
  if(summary.missing_market_values) notes.push(`${summary.missing_market_values} rows missing a value — subtotal only`);
  if(!summary.completeness_verified) notes.push('Completeness unverified — compare with Yahoo');
  if(detailed && notes.length) block.append(node('div',notes.join(' · '),'source-meta'));
  return block;
}
function renderSources() {
  const selected=state.sources.filter(s=>s.selected);
  $('source-count').textContent=`${selected.length} / ${state.sources.length} selected`;
  const target=$('sources'); target.replaceChildren();
  if(!state.sources.length) {
    const empty=node('div',undefined,'empty');
    empty.append(node('strong','Load your Yahoo portfolios'),node('div','Connect Yahoo in Data, then click Find portfolios.'));
    target.append(empty); return;
  }
  for(const source of state.sources) {
    const card=node('article',undefined,'source-card'+(source.selected?' selected':''));
    const checkbox=node('input'); checkbox.type='checkbox'; checkbox.checked=!!source.selected;
    checkbox.className='portfolio-checkbox'; checkbox.id=`portfolio-${source.id}`;
    checkbox.disabled=!!state.browser.busy; checkbox.dataset.selection='true';
    checkbox.setAttribute('aria-label',`Include ${source.name} in data pull`);
    checkbox.onchange=async()=>{
      savingSelection=true; renderBrowser();
      try { await api(`/api/sources/${source.id}/selection`,{selected:checkbox.checked}); message(''); await reload(); }
      catch(error){ checkbox.checked=!!source.selected; message(error.message); }
      finally { savingSelection=false; renderBrowser(); }
    };
    const body=node('div',undefined,'source-body');
    const label=node('label',source.name,'source-name'); label.htmlFor=checkbox.id;
    body.append(label,renderHoldingTotal(source.holding_summary));
    const running=state.browser.progress?.running_source_id===source.id;
    body.append(node('div',`Last pull: ${when(source.last_checked)}`,'source-meta'));
    if(running) body.append(node('div','Pulling…','source-meta pulling'));
    const actions=node('div',undefined,'source-actions');
    if(source.snapshot_id) actions.append(button('View holdings',()=>showSource(source)));
    if(source.selected) {
      const retry=button(source.last_error ? 'Retry' : 'Pull',()=>browserAction('refresh',[source.id]));
      retry.setAttribute('aria-label',`${source.last_error ? 'Retry' : 'Pull'} ${source.name}`);
      retry.dataset.pullOne='true'; retry.disabled=!!state.browser.busy || savingSelection;
      actions.append(retry);
    }
    card.append(checkbox,body,actions);
    if(source.last_error && !running) card.append(node('div',source.last_error,'source-error'));
    target.append(card);
  }
}
function renderBrowser() {
  const b = state.browser;
  $('connection-intro').textContent = 'Sign in to Yahoo in Chrome, then find your portfolios.';
  $('connection-intro').hidden = !!b.browser_open || state.sources.length>0;
  $('extension-path').value = b.extension_dir || '';
  $('pairing-key').value = b.pairing_key || '';
  $('extension-version').textContent = `Extension: ${b.extension_version || 'not connected'} · Required: 1.1.6`;
  $('browser-badge').textContent = b.busy ? 'Working…' : b.setup_required ? 'Setup required' : b.browser_open ? 'Connected' : 'Chrome offline';
  $('browser-badge').className = 'badge' + (b.browser_open ? ' connected' : '');
  $('browser-message').textContent = b.message || '';
  $('browser-message').hidden = !!b.error || !b.busy || b.action==='refresh';
  $('browser-error').hidden = !b.error || b.action==='refresh';
  $('browser-error').textContent = b.error || '';
  if (!b.error && b.discovery_warning) {
    $('browser-error').hidden = false;
    $('browser-error').textContent = b.discovery_warning;
  }
  $('connect').textContent = b.busy && b.action === 'connect' ? 'Opening…' : 'Open Yahoo';
  for (const id of ['connect', 'discover', 'disconnect']) $(id).disabled = !!b.busy;
  const selected=state.sources.filter(s=>s.selected && s.url);
  $('refresh').disabled=!!b.busy || savingSelection || !selected.length;
  const progress=b.progress;
  $('refresh').textContent=b.busy && b.action==='refresh'
    ? `Pulling ${progress?.completed || 0}/${progress?.total || selected.length}`
    : 'Pull latest holdings';
  document.querySelectorAll('[data-selection], [data-pull-one]').forEach(el=>{el.disabled=!!b.busy || savingSelection;});
  if((b.browser_open || state.sources.length) && !state.didCollapseSetup) {
    $('chrome-setup').open=false; state.didCollapseSetup=true;
  }
  $('refresh-results').replaceChildren();
  if(!b.busy && b.action==='refresh' && b.results?.length) {
    const saved=b.results.filter(result=>result.ok).length, failed=b.results.length-saved;
    const summary=[saved ? `${saved} ${saved===1?'portfolio':'portfolios'} updated` : '',failed ? `${failed} ${failed===1?'needs':'need'} attention` : ''].filter(Boolean).join(' · ');
    $('refresh-results').append(node('div',summary,'result-row'+(failed?' failed':'')));
  }
}
async function reload() {
  const request = ++stateRequest;
  const nextState = await api('/api/state');
  if (request !== stateRequest) return;
  nextState.didCollapseSetup = state.didCollapseSetup;
  state = nextState;
  const signature = JSON.stringify([state.sources,state.browser.progress]);
  if (signature !== sourceSignature) { sourceSignature = signature; renderSources(); }
  renderBrowser();
}
async function showSource(source) {
  if (typeof document.dispatchEvent === 'function' && typeof CustomEvent === 'function') {
    document.dispatchEvent(new CustomEvent('portfolio:holdings'));
  }
  const request = ++sourceViewRequest;
  activeSource = source;
  const snapshots = await api(`/api/sources/${source.id}/snapshots`);
  if (request !== sourceViewRequest) return;
  $('snapshot-title').textContent = source.name;
  const select = $('snapshot-select'); select.replaceChildren();
  for (const snapshot of snapshots) {
    const option = node('option', when(snapshot.captured_at)); option.value = snapshot.id; select.append(option);
  }
  $('snapshot-panel').hidden = false;
  if (snapshots.length) {
    select.value = String(snapshots[0].id);
    await showSnapshot(snapshots[0].id);
  }
  if (request === sourceViewRequest) $('snapshot-panel').scrollIntoView({behavior: 'smooth', block: 'start'});
}
async function showSnapshot(id) {
  const request = sourceViewRequest;
  if (!activeSource) return;
  $('raw-download').removeAttribute('href');
  $('snapshot-meta').textContent = 'Loading holdings…';
  for (const id of ['snapshot-total', 'warnings', 'records']) $(id).replaceChildren();
  const snapshot = await api(`/api/snapshots/${id}`);
  if (request !== sourceViewRequest || String($('snapshot-select').value) !== String(id)) return;
  $('raw-download').href = `/api/snapshots/${id}/csv`;
  $('snapshot-meta').textContent = `${snapshot.rows.length} records · Pulled ${when(snapshot.captured_at)}`;
  $('snapshot-total').replaceChildren(renderHoldingTotal(snapshot.holding_summary));
  $('warnings').replaceChildren(renderHoldingTotal(snapshot.holding_summary,true),...snapshot.warnings.map(text => node('div', text, 'warning')));
  const fragment = document.createDocumentFragment();
  for (const record of snapshot.rows) {
    const row = node('tr');
    for (const key of ['symbol', 'quantity', 'price', 'average_cost', 'total_cost', 'market_value', 'purchase_price', 'trade_date', 'currency']) {
      row.append(node('td', record[key] ?? '—'));
    }
    fragment.append(row);
  }
  $('records').replaceChildren(fragment);
}
for (const [id, action] of [['connect','connect'],['discover','discover'],['disconnect','disconnect'],['refresh','refresh']]) {
  $(id).onclick = async () => {
    $(id).disabled = true;
    try { message(''); if(action==='refresh') {await api('/api/pull',{});await reload();} else await browserAction(action); }
    catch (error) { message(error.message); }
    finally { renderBrowser(); }
  };
}
$('snapshot-select').onchange = () => showSnapshot($('snapshot-select').value).catch(error => message(error.message));
$('close-snapshot').onclick = () => {
  sourceViewRequest++;
  activeSource = null;
  $('snapshot-panel').hidden = true;
};
$('copy-pairing').onclick = async () => {
  try {
    await navigator.clipboard.writeText($('pairing-key').value);
    message('Pairing key copied. Paste it into the Local Portfolio Chrome extension and save the connection.');
  } catch {
    $('pairing-key').type = 'text'; $('pairing-key').focus(); $('pairing-key').select();
    message('Copy the selected pairing key into the Local Portfolio Chrome extension.');
  }
};
async function poll() {
  try { await reload(); }
  catch (error) { message('Cannot reach the local app. Make sure app.py is running.'); }
  setTimeout(poll, 2000);
}
poll();
