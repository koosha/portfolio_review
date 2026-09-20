// In-memory DOM with the real local HTTP service. This does not test browser layout,
// accessibility APIs, Chrome policy, extension behavior or browser security enforcement.
import test from 'node:test';
import assert from 'node:assert/strict';
import {writeFile} from 'node:fs/promises';
import {launch, until, namedInput, setInput} from './support/dom-http.mjs';

const serverScript = `
import json, sys
from pathlib import Path
from portfolio_lab.config import load_config
from portfolio_lab.demo import create_demo
from portfolio_research.demo import demo_workspace
from portfolio_research.server import make_server
config = load_config(create_demo(Path(sys.argv[1]) / 'demo'))
config['risk']['bootstrap_samples'] = 50
server = make_server(config, port=0, collector_directory=Path(sys.argv[1]) / 'collector')
job = server.research.submit({'kind':'monthly','as_of':'2026-08-31',
    'request_key':'dom-http-initial-synthetic','workspace':demo_workspace()})
initial = server.research.wait(job['job_id'])
if initial['status'] != 'complete':
    raise RuntimeError(initial.get('error'))
print(json.dumps({'port':server.server_address[1],
    'run_id':initial['output']['result']['run_id']}), flush=True)
try:
    server.serve_forever()
finally:
    server.server_close()
`;

test('research event handlers integrate with a disposable synthetic HTTP application', {timeout: 120000}, async t => {
  const {window, $, origin, runtimeErrors, info} = await launch(t, serverScript);
  const initialRunId = info.run_id;
  const readRun = async id => {
    const response = await fetch(`${origin}/api/research/runs/${encodeURIComponent(id)}`);
    assert.equal(response.status, 200);
    return response.json();
  };
  const initial = await readRun(initialRunId);

  await t.test('real initialization, navigation, tables and valuation use one shared run', async () => {
    assert.equal($('research-run').value, initialRunId);
    assert.match($('research-mode').textContent, /SYNTHETIC DEMO/);
    assert.ok($('overview-metrics').textContent.includes('Portfolio NAV'));
    assert.ok($('company-screen').querySelectorAll('tbody tr').length > 0);
    for (const page of ['holdings', 'research', 'scenarios', 'review', 'data', 'settings', 'overview']) {
      window.document.querySelector(`[data-page="${page}"]`).click();
      assert.equal($(`page-${page}`).hidden, false);
      assert.equal($('research-run').value, initialRunId);
    }
    setInput(window, $('research-security'), 'SIM01');
    assert.ok($('eps-output').textContent.includes('11%'));
    assert.match($('dcf-output').textContent, /ready/i);
    assert.equal($('save-run').disabled, true);
    assert.deepEqual(runtimeErrors, []);
  });

  let childRunId;
  await t.test('editing, recalculation and save retain the exact preview and immutable parent', async () => {
    setInput(window, namedInput(window, 'Cost per side (basis points)'), 25);
    assert.equal($('draft-state').textContent, 'Dirty');
    assert.equal($('save-run').disabled, true);
    assert.match($('resolved-diff').textContent, /transaction_cost_bps/);
    $('recalculate').click();
    await until(() => $('draft-state').textContent === 'Preview', 'calculated preview');
    assert.equal($('save-run').disabled, false);
    const previewTable = $('candidate-comparison').textContent;
    $('save-run').click();
    await until(() => $('draft-state').textContent === 'Saved' && $('research-run').value !== initialRunId, 'immutable child save');
    childRunId = $('research-run').value;
    const child = await readRun(childRunId);
    assert.equal(child.config.allocation.transaction_cost_bps, 25);
    assert.equal(child.result.metadata.parent_run_id, initialRunId);
    assert.equal($('candidate-comparison').textContent, previewTable);
    assert.deepEqual((await readRun(initialRunId)).result, initial.result);
  });

  await t.test('older run loading and discard/reset restore its saved assumptions', async () => {
    setInput(window, $('research-run'), initialRunId);
    await until(() => $('draft-state').textContent === 'Saved' && namedInput(window, 'Cost per side (basis points)').value === '10', 'older run load');
    setInput(window, namedInput(window, 'Cost per side (basis points)'), 15);
    $('reset-draft').click();
    await until(() => $('draft-dialog').hasAttribute('open'), 'explicit reset choice');
    $('draft-dialog').querySelector('[data-draft-choice="discard"]').click();
    await until(() => $('draft-state').textContent === 'Saved', 'discarded draft reset');
    assert.equal(namedInput(window, 'Cost per side (basis points)').value, '10');
    assert.equal($('research-run').value, initialRunId);
    assert.equal($('save-run').disabled, true);
  });

  // The selector, the view and the sections on screen are one statement. A load the owner
  // cancelled may not leave the selector naming one run while the page shows another, or
  // withdraw every section under a selector that names a run.
  await t.test('a cancelled run load leaves the selector, the view and the sections agreeing', async () => {
    const analysed = [...window.document.querySelectorAll('#page-overview [data-analysis]')];
    assert.ok(analysed.length >= 4, `Overview needs analysed sections: ${analysed.length}`);
    setInput(window, namedInput(window, 'Cost per side (basis points)'), 15);
    await until(() => $('draft-state').textContent === 'Dirty', 'the edited draft');
    setInput(window, $('research-run'), 'current');
    await until(() => analysed.every(node => node.hidden), 'the analysed sections to withdraw');
    assert.match($('analysis-caption').textContent, /No completed analysis/);
    setInput(window, $('research-run'), childRunId);
    await until(() => $('draft-dialog').hasAttribute('open'), 'the explicit draft choice');
    $('draft-dialog').querySelector('[data-draft-choice="cancel"]').click();
    await until(() => analysed.every(node => !node.hidden), 'the loaded run shown again');
    assert.equal($('research-run').value, initialRunId, 'the selector names the run on screen');
    assert.match($('analysis-caption').textContent, /Last completed analysis/);
    assert.match($('analysis-caption').textContent, new RegExp(initialRunId.slice(0, 8)));
    assert.equal($('draft-state').textContent, 'Dirty', 'cancelling keeps the draft it asked about');
    $('reset-draft').click();
    await until(() => $('draft-dialog').hasAttribute('open'), 'the explicit reset choice');
    $('draft-dialog').querySelector('[data-draft-choice="discard"]').click();
    await until(() => $('draft-state').textContent === 'Saved', 'the discarded draft reset');
    assert.equal($('research-error').hidden, true, $('research-error').textContent);
  });

  await t.test('newer edits defeat an older completed request and server errors keep usable results', async () => {
    const before = $('scenario-results').textContent;
    setInput(window, namedInput(window, 'Cost per side (basis points)'), 15);
    $('recalculate').click();
    setInput(window, namedInput(window, 'Cost per side (basis points)'), 20);
    await until(() => $('draft-state').textContent === 'Dirty', 'stale preview completion');
    assert.equal($('save-run').disabled, true);
    assert.equal(namedInput(window, 'Cost per side (basis points)').value, '20');
    assert.equal($('scenario-results').textContent, before);
    setInput(window, namedInput(window, 'Cost per side (basis points)'), 5000);
    $('recalculate').click();
    await until(() => !$('research-error').hidden, 'real API validation error');
    assert.match($('research-error').textContent, /transaction_cost_bps/);
    assert.match($('research-error').textContent, /retained/);
    assert.equal($('scenario-results').textContent, before);
    assert.equal($('save-run').disabled, true);
    assert.deepEqual((await readRun(initialRunId)).result, initial.result);
  });

  await t.test('unknown cash stays unavailable in rendered results and unweighted mode remains usable', async () => {
    setInput(window, namedInput(window, 'Cost per side (basis points)'), 10);
    const probabilityToggle = [...window.document.querySelectorAll('input[type="checkbox"]')]
      .find(input => input.closest('label')?.textContent.includes('probabilit'));
    assert.ok(probabilityToggle, 'Unweighted exploration needs its explicit probability control');
    setInput(window, probabilityToggle, false);
    const cashLabel = [...window.document.querySelectorAll('label')]
      .find(label => /cash.*return/i.test(label.textContent) && label.querySelector('input'));
    assert.ok(cashLabel, 'Explicit cash return needs a labeled input');
    setInput(window, cashLabel.querySelector('input'), '');
    $('recalculate').click();
    await until(() => $('draft-state').textContent === 'Preview', 'missing-cash unweighted preview');
    assert.equal($('research-error').hidden, true, $('research-error').textContent);
    assert.match($('scenario-results').textContent, /—/);
    const outcomes = $('scenario-results').querySelector('table');
    const headings = [...outcomes.querySelectorAll('thead th')].map(cell => cell.textContent);
    for (const heading of ['Probability', 'Portfolio return', 'Terminal value']) {
      const index = headings.indexOf(heading);
      assert.ok(index >= 0, `Missing scenario result column: ${heading}`);
      for (const row of outcomes.querySelectorAll('tbody tr')) {
        assert.equal(row.cells[index].textContent, '—', `${heading} must remain unavailable, not zero`);
      }
    }
    assert.match($('scenario-issues').textContent, /cash.*explicit|cash.*assumption/i);
    assert.doesNotMatch($('scenario-results').textContent, /NaN|undefined|Infinity/);
    assert.deepEqual((await readRun(childRunId)).config.allocation.transaction_cost_bps, 25);
    assert.deepEqual(runtimeErrors, []);
  });
});

// A second synthetic application, identical to the demo above except that the market
// research a live provider would answer with is injected into the bundle the review
// freezes: dated briefs, reviewable EPS and DCF proposals, a coverage report, a screened
// universe and fund sector weights. The same patch point refuses every provider once the
// test creates the offline flag file, so a recalculation that reached for one would fail
// loudly instead of passing unnoticed. This is not a provider test: nothing here asserts
// what Yahoo, the SEC or FRED actually return.
const researchScript = `
import json, sys
from pathlib import Path
import pandas as pd
from portfolio_lab import pipeline, providers
from portfolio_lab.config import load_config
from portfolio_lab.demo import create_demo
from portfolio_research import enrichment
from portfolio_research.briefs import build_brief
from portfolio_research.demo import demo_workspace
from portfolio_research.enrichment import coverage_report
from portfolio_research.proposals import propose_dcf_inputs, propose_eps_model
from portfolio_research.server import make_server
directory = Path(sys.argv[1])
offline = directory / 'providers-offline'
config = load_config(create_demo(directory / 'demo'))
config['risk']['bootstrap_samples'] = 50
RESEARCHED = ('SIM01', 'SIM03')

def estimates(sid, eps):
    return {'currency': 'USD', 'source_id': 'estimates:' + sid, 'received_at': '2026-08-31T20:00:00Z',
        'eps': {'+1y': {'avg': eps, 'low': eps * 0.8, 'high': eps * 1.2}},
        'revenue': {'+1y': {'avg': 5400.0, 'low': 5100.0, 'high': 5700.0}}}

def trailing(sid):
    return {'period_end': '2026-06-30', 'currency': 'USD', 'source_id': 'statements:' + sid,
        'revenue': 5000.0, 'operating_income': 900.0, 'net_income': 600.0,
        'operating_cash_flow': 800.0, 'capex': 200.0, 'depreciation': 150.0,
        'change_working_capital': 20.0, 'tax_provision': 200.0, 'pretax_income': 800.0,
        'assets': 9000.0, 'debt': 1200.0, 'cash': 700.0, 'diluted_shares': 500.0,
        'stock_compensation': 50.0}

def inject(bundle, as_of):
    research, inputs = {}, {}
    prices = bundle.get('prices')
    for sid in RESEARCHED:
        rows = prices[prices.security_id == sid]
        close = round(float(rows.close.iloc[-1]), 2)
        security = {'security_id': sid, 'name': 'Simulated Company ' + sid, 'sector': 'Technology',
            'instrument_type': 'equity', 'equity_type': 'ordinary_common', 'currency': 'USD',
            'price_source_id': 'prices:' + sid}
        estimate, ttm, reasons = estimates(sid, close / 18.0), trailing(sid), []
        proposals = {
            'eps': propose_eps_model(security, price_major=close, quote_currency='USD',
                estimates=estimate, ttm=ttm, dividends_ttm_per_share=1.25, horizon_months=12,
                dividends_source_id='prices:' + sid, issues=reasons),
            'dcf': propose_dcf_inputs(security, ttm=ttm, annual_statements=[ttm],
                shares_outstanding=500.0, price_major=close, currency='USD', quote_currency='USD',
                estimates=estimate, defaults={'discount_rate': 0.09, 'terminal_growth_rate': 0.025,
                'projection_years': 5}, issues=reasons),
            'reasons': reasons}
        brief = build_brief(security,
            prices=[{'security_id': sid, 'date': as_of, 'close': close, 'currency': 'USD',
                'source_id': 'prices:' + sid, 'received_at': '2026-08-31T20:00:00Z'}],
            estimates=estimate, ttm=ttm,
            events=[{'security_id': sid, 'kind': 'filing', 'event_date': '2026-08-14',
                'title': 'Quarterly report', 'source_id': 'filings:' + sid,
                'received_at': '2026-08-15T12:00:00Z'}],
            previous={'as_of': '2026-07-31', 'close': close * 0.9, 'currency': 'USD',
                'source_id': 'prices:' + sid, 'estimates': estimates(sid, close / 20.0)},
            fx_note=None, as_of=as_of, generated_at='2026-08-31T21:00:00Z', horizon_months=12)
        coverage = {'prices': 'ok', 'statements': 'ok', 'estimates': 'ok',
            'fund_disclosures': 'not_applicable', 'events': 'ok', 'profile': 'ok'}
        research[sid] = {'brief': brief, 'proposals': proposals, 'coverage': coverage}
        inputs[sid] = {'symbol': sid, 'currency': 'USD', 'quote_currency': 'USD',
            'estimates': estimate, 'fund_overview': {}, 'coverage': coverage}
    bundle['research_inputs'] = inputs
    bundle['universe'] = {'scope_base': 'Simulated screened universe',
        'scope_label': 'Simulated screened universe; 2 of 3 enriched',
        'counts': {'acquired': 3, 'qualified': 3, 'in_scope': 3, 'enriched': 2},
        'coverage': {'acquired': 3, 'qualified': 3, 'in_scope': 3, 'enriched': 2,
            'not_enriched': ['SIM20'], 'identity_unresolved': [], 'time_budget_exhausted': False,
            'stopped': False}}
    bundle['coverage'] = coverage_report(bundle, config)
    bundle['research'] = research
    bundle['fund_sectors'] = pd.DataFrame([
        {'fund_id': 'SIMETF', 'sector': sector, 'weight': weight, 'as_of': as_of,
         'source_id': 'fund:SIMETF', 'received_at': '2026-08-31T20:00:00Z'}
        for sector, weight in (('Technology', 0.6), ('Industrials', 0.3))])

original = pipeline.enrich_bundle

def refuse(*args, **kwargs):
    raise RuntimeError('No provider may be reached while this review is offline.')

def enrich(bundle, configuration, as_of):
    if offline.exists():
        refuse()
    enriched = original(bundle, configuration, as_of)
    inject(enriched, as_of)
    return enriched

pipeline.enrich_bundle = enrich
providers.enrich_bundle = enrich
enrichment.enrich_market = refuse
server = make_server(config, port=0, collector_directory=directory / 'collector')
job = server.research.submit({'kind':'monthly','as_of':'2026-08-31',
    'request_key':'dom-http-research-synthetic','workspace':demo_workspace()})
initial = server.research.wait(job['job_id'])
if initial['status'] != 'complete':
    raise RuntimeError(initial.get('error'))
print(json.dumps({'port':server.server_address[1],'offline_flag':str(offline),
    'run_id':initial['output']['result']['run_id']}), flush=True)
try:
    server.serve_forever()
finally:
    server.server_close()
`;

const detailTab = (window, name) => window.document.querySelector(`[data-detail-tab="${name}"]`);
const cashField = window => [...window.document.querySelectorAll('label')]
  .find(label => /cash.*return/i.test(label.textContent) && label.querySelector('input'))
  ?.querySelector('input');

test('one researched review drives the dashboard, its controls and a recorded decision',
  {timeout: 300000}, async t => {
  const {window, $, runtimeErrors, info} = await launch(t, researchScript);
  const initialRunId = info.run_id;
  let savedRunId = '', reviewedRunId = '';

  // Every chart the page can reach, not only the ones on the entry point: a shaded
  // sensitivity grid or a benchmark illustration with no stated date is a picture too.
  await t.test('every chart states its units, date, scope and coverage', () => {
    window.document.querySelector('[data-page="research"]').click();
    setInput(window, $('research-security'), 'SIM01');
    assert.ok($('eps-grid').querySelector('.chart-caption'), 'a shaded grid states its basis');
    for (const name of ['scenarios', 'review', 'overview']) {
      window.document.querySelector(`[data-page="${name}"]`).click();
    }
    const captions = [...window.document.querySelectorAll('.chart-caption')];
    assert.ok(captions.length >= 7, `Charts need captions: ${captions.length}`);
    for (const caption of captions) {
      const parts = caption.textContent.split(' · ').map(part => part.trim()).filter(Boolean);
      assert.ok(parts.length >= 4, `Caption states units, date, scope and coverage: ${caption.textContent}`);
      assert.match(caption.textContent, /2026-08-31/, caption.textContent);
      assert.match(caption.textContent, /coverage/i, caption.textContent);
      assert.doesNotMatch(caption.textContent, /undefined|NaN|\[object/, caption.textContent);
    }
    assert.ok(window.document.querySelectorAll('.chart-note').length >= captions.length);
  });

  await t.test('what changed, portfolio risks and this month’s priorities read from the run', () => {
    assert.match($('what-changed').textContent, /versus/, 'the briefs report changes since the previous review');
    assert.match($('what-changed').textContent, /prices:SIM01/, 'each stated change names its source');
    assert.match($('portfolio-risks').textContent, /broad equity/i, 'the stress table is on the risks panel');
    assert.match($('portfolio-risks').textContent, /SIM01/, 'risk contributions name their securities');
    assert.match($('portfolio-risks').textContent, /cap/i, 'concentration is stated against the confirmed cap');
    assert.ok($('holdings-review').querySelectorAll('button').length > 0, 'holdings to review are openable');
    assert.match($('holdings-review').textContent, /SIM01/);
    assert.ok($('new-candidates').querySelectorAll('button').length > 0, 'unowned candidates are openable');
    assert.match($('overlap-view').textContent, /SIMETF/, 'the fund contributing indirect exposure is named');
    assert.match($('overlap-view').textContent, /Technology/, 'per-fund sector weights are shown');
    assert.doesNotMatch($('what-changed').textContent, /undefined|NaN/);
    assert.deepEqual(runtimeErrors, []);
  });

  await t.test('a priority opens evidence, calculation, assumptions and limitations', async () => {
    const priority = [...$('holdings-review').querySelectorAll('button')]
      .find(node => node.textContent.includes('SIM01'));
    assert.ok(priority, 'SIM01 is listed among the holdings to review');
    priority.click();
    await until(() => $('security-dialog').hasAttribute('open'), 'the security detail dialog');
    assert.match($('security-dialog-title').textContent, /SIM01/);
    assert.deepEqual([...window.document.querySelectorAll('[data-detail-tab]')]
      .map(node => node.dataset.detailTab), ['evidence', 'calculation', 'assumptions', 'limitations']);
    assert.equal($('detail-evidence').hidden, false);
    assert.match($('detail-evidence').textContent, /versus/, 'the brief’s dated bullets are the evidence');
    assert.match($('detail-evidence').textContent, /estimates:SIM01/, 'sources are named');
    detailTab(window, 'calculation').click();
    assert.equal($('detail-evidence').hidden, true);
    assert.equal($('detail-calculation').hidden, false);
    assert.match($('detail-calculation').textContent, /EPS|value per share/i);
    detailTab(window, 'assumptions').click();
    assert.match($('detail-assumptions').textContent, /terminal multiple/i,
      'the proposal’s consequential assumptions are visible');
    detailTab(window, 'limitations').click();
    assert.match($('detail-limitations').textContent, /prices/i);
    assert.match($('detail-limitations').textContent, /ok|missing|stale/);
    // The tabs are operable from the keyboard, like the company research tabs.
    detailTab(window, 'limitations').dispatchEvent(
      new window.KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}));
    assert.equal($('detail-evidence').hidden, false, 'arrow keys move between detail tabs');
    $('close-security-dialog').click();
    assert.equal($('security-dialog').hasAttribute('open'), false);
    assert.deepEqual(runtimeErrors, []);
  });

  await t.test('keeping a proposed EPS model prefills it, recalculates and survives save and reopen',
    async () => {
    window.document.querySelector('[data-page="research"]').click();
    setInput(window, $('research-security'), 'SIM01');
    const keep = [...$('proposal-actions').querySelectorAll('button')]
      .find(node => /EPS/.test(node.textContent));
    assert.ok(keep, 'the EPS proposal can be kept');
    assert.match($('proposal-actions').textContent, /DCF/, 'the DCF proposal can be kept too');
    const before = namedInput(window, 'Starting price / share').value;
    keep.click();
    await until(() => $('draft-state').textContent === 'Dirty', 'the prefilled draft');
    const prefilled = namedInput(window, 'Starting price / share').value;
    assert.notEqual(prefilled, before, 'the proposal’s own starting price is prefilled');
    assert.match($('security-summary').textContent, /prefill/i, 'a prefilled model says so');
    assert.match($('assumption-strip').textContent, /terminal multiple/i);
    assert.match($('assumption-strip').textContent, /estimates:SIM01/, 'each assumption carries its evidence');
    // Typing over a prefilled model makes it the owner's own input: it may no longer be
    // archived as a proposal backed by an estimate that never asserted the typed value.
    setInput(window, namedInput(window, 'Starting price / share'), Number(prefilled) + 5);
    $('recalculate').click();
    await until(() => $('draft-state').textContent === 'Preview', 'the edited preview');
    assert.match($('security-summary').textContent, /manual/i,
      'a hand-typed value is no longer credited to the proposal');
    setInput(window, namedInput(window, 'Starting price / share'), prefilled);
    $('recalculate').click();
    await until(() => $('draft-state').textContent === 'Preview', 'the recalculated preview');
    assert.match($('eps-output').textContent, /%/, 'the kept assumptions produce a valuation');
    const calculated = $('eps-output').textContent;
    $('save-run').click();
    await until(() => $('draft-state').textContent === 'Saved' && $('research-run').value !== initialRunId,
      'the saved child run');
    savedRunId = $('research-run').value;
    assert.equal($('eps-output').textContent, calculated);
    setInput(window, $('research-run'), initialRunId);
    await until(() => $('research-run').value === initialRunId && $('draft-state').textContent === 'Saved',
      'the parent run reloaded');
    setInput(window, $('research-run'), savedRunId);
    await until(() => $('research-run').value === savedRunId && $('draft-state').textContent === 'Saved',
      'the saved run reopened');
    assert.equal($('eps-output').textContent, calculated, 'a reopened run reproduces its own valuation');
    assert.equal($('research-error').hidden, true, $('research-error').textContent);
  });

  await t.test('a stated market view is retained and reset restores the saved assumptions', async () => {
    window.document.querySelector('[data-page="scenarios"]').click();
    const central = namedInput(window, 'Central market return (%)');
    const savedCentral = central.value, savedCash = cashField(window).value;
    const savedResults = $('scenario-results').textContent;
    setInput(window, central, 12);
    assert.equal($('draft-state').textContent, 'Dirty');
    assert.match($('resolved-diff').textContent, /shared_state/, 'the stated market view is a retained change');
    assert.match($('shared-state-editor').textContent, /next .*review/i,
      'the panel says when a stated market view is expanded into scenarios');
    // The grouped readout is the only non-colour encoding of those bars, so it wraps
    // rather than sharing the single-line axis row written for three short spans.
    assert.ok($('scenario-comparison').querySelector('.chart-readout'),
      'the grouped comparison states each value in a wrapping readout');
    assert.equal($('scenario-comparison').querySelector('.chart-axis'), null,
      'the grouped readout no longer reuses the non-wrapping axis row');
    // The joint scenarios of a saved run are frozen inputs; a recalculation replays them.
    setInput(window, cashField(window), 4);
    $('recalculate').click();
    await until(() => $('draft-state').textContent === 'Preview', 'the recalculated preview');
    assert.notEqual($('scenario-results').textContent, savedResults,
      'the explicit cash assumption changes the portfolio outcome');
    $('reset-draft').click();
    await until(() => $('draft-dialog').hasAttribute('open'), 'the explicit reset choice');
    $('draft-dialog').querySelector('[data-draft-choice="discard"]').click();
    await until(() => $('draft-state').textContent === 'Saved', 'the reset draft');
    assert.equal(namedInput(window, 'Central market return (%)').value, savedCentral);
    assert.equal(cashField(window).value, savedCash);
    assert.equal($('scenario-results').textContent, savedResults, 'reset restores the saved calculation');
    assert.equal($('research-error').hidden, true, $('research-error').textContent);
  });

  await t.test('a recorded decision names its alternative and the next review reads it back',
    async () => {
    window.document.querySelector('[data-page="review"]').click();
    // A leg carries what still has to be reviewed about it, and the lots the conditional
    // reserve was estimated from stay inspectable beside it.
    const basketHeadings = [...$('trade-basket').querySelectorAll('thead th')].map(cell => cell.textContent);
    for (const heading of ['Executable', 'Review flags']) {
      assert.ok(basketHeadings.includes(heading), `Missing basket column ${heading}: ${basketHeadings.join(', ')}`);
    }
    assert.match($('trade-basket').textContent, /Tax lot plan/,
      'the lots behind the conditional reserve remain inspectable');
    $('save-decision').click();
    await until(() => $('decision-dialog').hasAttribute('open'), 'the decision dialog');
    assert.match($('decision-compared').textContent, /no_change|No Change/i,
      'the alternatives the choice is weighed against are shown');
    assert.match($('decision-compared').textContent, /sleeve/i);
    // The dialog opens on a no-change decision, so it may not also show a preselected
    // alternative that recording would silently discard.
    assert.equal($('decision-alternative').value, '');
    assert.equal($('decision-alternative').disabled, true,
      'a no-change decision records no alternative');
    // A refused decision is answered inside the dialog; the page's own error line sits
    // behind the backdrop where neither eye nor screen reader reaches it.
    $('confirm-decision').click();
    await until(() => !$('decision-error').hidden, 'the refusal stated inside the dialog');
    assert.match($('decision-error').textContent, /rationale/i);
    assert.equal($('decision-dialog').hasAttribute('open'), true, 'a refused decision keeps the dialog open');
    assert.equal($('decision-rationale').getAttribute('aria-invalid'), 'true');
    assert.equal($('research-error').hidden, true, 'the refusal is not left behind the backdrop');
    setInput(window, $('decision-action'), 'review_candidate');
    assert.equal($('decision-alternative').disabled, false,
      'naming an alternative becomes possible once the action allows one');
    setInput(window, $('decision-alternative'), 'simple_equal_issuer_sleeve');
    setInput(window, $('decision-rationale'), 'The sleeve alternative is evidenced this month.');
    $('confirm-decision').click();
    await until(() => !$('decision-dialog').hasAttribute('open'), 'the dialog to close on a recorded decision');
    assert.equal($('decision-error').hidden, true);
    await until(() => /simple_equal_issuer_sleeve/.test($('decision-history').textContent),
      'the recorded decision in the history');
    assert.match($('decision-history').textContent, /sleeve alternative is evidenced/);
    assert.equal($('research-error').hidden, true, $('research-error').textContent);

    window.document.querySelector('[data-page="data"]').click();
    setInput(window, $('review-date'), '2026-08-31');
    $('monthly-review').click();
    await until(() => $('draft-state').textContent === 'Saved' && $('research-run').value !== savedRunId,
      'the next review to publish', 240000);
    reviewedRunId = $('research-run').value;
    window.document.querySelector('[data-page="overview"]').click();
    await until(() => /simple_equal_issuer_sleeve/.test($('what-changed').textContent),
      'the previous decision restated by the next review');
    assert.match($('what-changed').textContent, /sleeve alternative is evidenced/);
    assert.match($('what-changed').textContent, /flows/i,
      'a balance change is never reported as a return while flows are unknown');
    assert.match($('what-changed').textContent, new RegExp(savedRunId.slice(0, 8)),
      'the comparison names the run it was made against');
    assert.deepEqual(runtimeErrors, []);
  });

  await t.test('recalculation and saving succeed with every provider refused', async () => {
    await writeFile(info.offline_flag, 'refuse every provider');
    setInput(window, cashField(window), 3);
    $('recalculate').click();
    await until(() => $('draft-state').textContent === 'Preview', 'the offline preview');
    assert.equal($('research-error').hidden, true, $('research-error').textContent);
    assert.ok($('holdings-review').querySelectorAll('button').length > 0,
      'the frozen research replays without a provider');
    assert.match($('what-changed').textContent, /versus/);
    $('save-run').click();
    await until(() => $('draft-state').textContent === 'Saved' && $('research-run').value !== reviewedRunId,
      'the offline save');
    assert.equal($('research-error').hidden, true, $('research-error').textContent);
    assert.deepEqual(runtimeErrors, []);
  });
});
