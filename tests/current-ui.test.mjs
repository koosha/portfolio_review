// Current holdings render from the newest published collection with no saved review.
// In-memory DOM with the real local HTTP service backed by a collector store; this does
// not test browser layout, accessibility APIs, Chrome policy or extension behavior.
import test from 'node:test';
import assert from 'node:assert/strict';
import {launch, until} from './support/dom-http.mjs';

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

