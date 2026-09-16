// Current holdings render from the newest published collection with no saved review.
// In-memory DOM with the real local HTTP service backed by a collector store; this does
// not test browser layout, accessibility APIs, Chrome policy or extension behavior.
import test from 'node:test';
import assert from 'node:assert/strict';
import {launch, namedInput, setInput, until} from './support/dom-http.mjs';

// Two synthetic accounts published in one batch on a Sunday: A labels every row in USD,
// B carries no currency column, so captured subtotals must stay separate and unconverted.
const script = afterStart => `
import json, sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from portfolio.storage import Store
from portfolio_research.server import make_server
from portfolio_research.service import default_config
directory = Path(sys.argv[1])
store = Store(directory)
a = store.add_source('Synthetic USD account',
    url='https://finance.yahoo.com/portfolio/p_fixture_current_usd')
b = store.add_source('Synthetic unlabeled account',
    url='https://finance.yahoo.com/portfolio/p_fixture_current_unlabeled')
def table(symbol, currency=None):
    headers = ['Symbol', 'Shares', 'Last Price', 'Market Value ($)', 'AC/Share', 'Total Cost ($)']
    rows = [[symbol, '2', '10.50', '21.00', '8.00', '16.00'], ['Total Cash', '', '3.25', '', '', '']]
    if currency:
        headers = headers + ['Currency']
        rows = [row + [currency] for row in rows]
    return {'method': 'yahoo-holdings-table-v1', 'headers': headers, 'rows': rows,
        'page_count': 1, 'expected_count': len(rows), 'completeness': 'count-verified'}
with patch('portfolio.storage.now', return_value='2026-09-13T20:00:00+00:00'):
    batch = store.begin_batch([a, b])
    store.ingest_table(a, table('SIMA', 'USD'), batch_id=batch)
    store.ingest_table(b, table('SIMB'), batch_id=batch)
# Pin the review calendar clock to Sunday evening so the market observation date is the
# preceding Friday regardless of when the suite runs.
sunday_generated = datetime(2026, 9, 13, 21, 0, tzinfo=timezone.utc)
class SundayClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return sunday_generated if tz is None else sunday_generated.astimezone(tz)
with patch('portfolio_research.calendar.datetime', SundayClock):
    server = make_server(default_config(directory), port=0, collector_directory=directory)
${afterStart}
    print(json.dumps({'port': server.server_address[1], 'batch_id': batch}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
`;
const serverScript = script('');
// A newer pull began after the collector started (startup abandons unfinished pulls) and
// has captured one account but has not published yet.
const pullingScript = script(`
    with patch('portfolio.storage.now', return_value='2026-09-13T20:30:00+00:00'):
        pulling = store.begin_batch([a, b])
        store.ingest_table(a, table('SIMC', 'USD'), batch_id=pulling)
`);

// Supplemental dated mappings identify both held symbols, so no listing question remains.
// Account A labels its values in USD; account B carries no currency column and no listing
// metadata or FX observation explains its values, so exactly one account_currency
// exception stays open until the owner attests the currency.
const exceptionScript = script(`
    from portfolio_research.adapter import validate_supplemental
    def mapping(source, symbol):
        return {'source_id': source, 'raw_symbol': symbol, 'security_id': 'SIM-' + symbol,
            'issuer_id': 'issuer:' + symbol, 'ticker': symbol, 'instrument_type': 'equity',
            'eligible': False, 'valid_from': '2026-01-01'}
    server.research.store.append_record('supplemental', validate_supplemental({'version': 1,
        'accounts': [], 'securities': [mapping(a, 'SIMA'), mapping(b, 'SIMB')], 'tax_lots': []}))
`);

const pages = ['holdings', 'research', 'scenarios', 'review', 'data', 'settings', 'overview'];
const accountCards = $ => $('current-accounts').querySelectorAll('.account-card').length;

test('a fresh Sunday collection appears on Overview without a saved review', {timeout: 120000}, async t => {
  const {window, $, runtimeErrors} = await launch(t, serverScript, {
    ready: $ => $('current-collection-state')?.textContent === 'Published',
  });

  await t.test('current holdings, dates and caption render from the newest collection', async () => {
    await until(() => $('draft-state').textContent === 'Empty', 'empty draft state with no saved run');
    assert.equal($('draft-state').textContent, 'Empty');
    assert.equal(accountCards($), 2);
    assert.match($('current-dates').textContent, /Collected/);
    assert.match($('current-dates').textContent, /unknown/);
    assert.match($('current-dates').textContent, /2026-09-11/);
    assert.match($('current-totals').textContent, /USD/);
    assert.match($('current-totals').textContent, /unlabeled/);
    assert.match($('analysis-caption').textContent, /No completed analysis/);
    assert.equal($('research-error').hidden, true, $('research-error').textContent);
    assert.doesNotMatch($('current-dates').textContent, /NaN|undefined|Invalid Date/);
    assert.deepEqual(runtimeErrors, []);
  });

  await t.test('navigating to every page and back keeps the current panel populated', () => {
    for (const page of pages) {
      window.document.querySelector(`[data-page="${page}"]`).click();
      assert.equal($(`page-${page}`).hidden, false);
      assert.equal($('current-collection-state').textContent, 'Published');
      assert.equal(accountCards($), 2);
      assert.match($('analysis-caption').textContent, /No completed analysis/);
    }
    assert.equal($('page-overview').hidden, false);
    assert.equal($('research-error').hidden, true, $('research-error').textContent);
    assert.deepEqual(runtimeErrors, []);
  });
});

test('a pull in progress is not shown as a failed collection', {timeout: 120000}, async t => {
  const {$, runtimeErrors} = await launch(t, pullingScript, {
    ready: $ => $('current-collection-state')?.textContent === 'Published',
  });
  await until(() => $('draft-state').textContent === 'Empty', 'empty draft state with no saved run');
  await until(() => $('current-exceptions').textContent.includes('in progress'), 'in-progress note');
  assert.equal(accountCards($), 2);
  assert.match($('current-exceptions').textContent, /still in progress/);
  assert.doesNotMatch($('current-exceptions').textContent, /did not complete|did not publish/);
  assert.equal($('current-positions').textContent.includes('SIMC'), false);
  assert.equal($('research-error').hidden, true, $('research-error').textContent);
  assert.deepEqual(runtimeErrors, []);
});

const exceptionForms = $ => $('current-exception-forms').querySelectorAll('form.exception-form');
const coveredMetric = $ => [...$('current-totals').querySelectorAll('.metric')]
  .find(node => node.querySelector('.metric-label')?.textContent === 'Covered value (USD)');
const unlabeledCard = $ => [...$('current-accounts').querySelectorAll('.account-card')]
  .find(node => node.textContent.includes('Synthetic unlabeled account'));

test('an open account currency exception is resolved from Overview', {timeout: 120000}, async t => {
  const {window, $, runtimeErrors} = await launch(t, exceptionScript, {
    ready: $ => $('current-collection-state')?.textContent === 'Published',
  });

  await until(() => exceptionForms($).length === 1, 'one open exception form');
  // Nothing in the unlabeled account is converted yet, so no USD subtotal is claimed.
  assert.doesNotMatch(unlabeledCard($).textContent, /USD 0/);
  const form = exceptionForms($)[0];
  assert.match(form.textContent, /SIMB/);
  assert.equal(form.dataset.kind, 'account_currency');
  const headers = [...$('current-positions').querySelectorAll('th')].map(th => th.textContent);
  for (const column of ['Quote currency', 'Value currency', 'USD value', 'FX (pair · date)']) {
    assert.ok(headers.includes(column), `Missing positions column ${column}: ${headers.join(', ')}`);
  }

  setInput(window, namedInput(window, 'Currency of reported values'), 'USD');
  const save = [...form.querySelectorAll('button')].find(node => node.textContent === 'Save');
  assert.ok(save, 'The exception form has a Save button');
  save.click();

  await until(() => $('current-exception-forms').textContent.includes('No open exceptions'),
    'the resolved exception to disappear');
  assert.equal(exceptionForms($).length, 0);
  await until(() => coveredMetric($)?.querySelector('.metric-value')?.textContent === '48.5',
    'covered USD value including the attested account');
  assert.match(unlabeledCard($).textContent, /USD 24.25/);
  assert.match(coveredMetric($).querySelector('.metric-note').textContent, /not reconciled NAV/);
  assert.equal($('research-error').hidden, true, $('research-error').textContent);
  assert.deepEqual(runtimeErrors, []);
});

// The forms are keyed to the snapshot behind them, so the panel has to be reloaded
// whenever that snapshot can have moved on: after a review or a new pull, and after the
// service refuses an answer because the exception it named is already closed.
test('open exceptions reload after a refused answer and after a sources change',
  {timeout: 120000}, async t => {
  const {window, $, runtimeErrors} = await launch(t, exceptionScript, {
    ready: $ => $('current-collection-state')?.textContent === 'Published',
  });
  await until(() => exceptionForms($).length === 1, 'one open exception form');

  const requested = [];
  const live = window.fetch;
  let refuse = true;
  window.fetch = (path, options = {}) => {
    requested.push(String(path));
    if (refuse && String(path).includes('/api/research/resolutions')) {
      return Promise.resolve({ok: false, json: async () => ({error: 'This exception is no longer open.'})});
    }
    return live(path, options);
  };

  setInput(window, namedInput(window, 'Currency of reported values'), 'USD');
  const save = [...exceptionForms($)[0].querySelectorAll('button')]
    .find(node => node.textContent === 'Save');
  save.click();
  await until(() => requested.some(path => path.includes('/api/research/exceptions')),
    'the exceptions panel to reload after the service refused the answer');
  await until(() => /no longer open/.test($('research-error').textContent),
    'the refused answer to be reported to the owner');
  assert.equal(exceptionForms($).length, 1, 'the still-open exception survives the reload');

  refuse = false;
  requested.length = 0;
  window.document.dispatchEvent(new window.Event('portfolio:sources-changed'));
  await until(() => requested.some(path => path.includes('/api/research/exceptions')),
    'the exceptions panel to reload when the holdings behind it change');
  assert.equal(exceptionForms($).length, 1);
  assert.deepEqual(runtimeErrors, []);
});
