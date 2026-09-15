// In-memory DOM with the real local HTTP service. This does not test browser layout,
// accessibility APIs, Chrome policy, extension behavior or browser security enforcement.
import test from 'node:test';
import assert from 'node:assert/strict';
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
