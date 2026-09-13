// In-memory DOM with the real local HTTP service. This does not test browser layout,
// accessibility APIs, Chrome policy, extension behavior or browser security enforcement.
import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {JSDOM, VirtualConsole} from 'jsdom';
import * as stateFunctions from '../static/research-state.js';

const project = fileURLToPath(new URL('..', import.meta.url));
const python = process.env.PORTFOLIO_TEST_PYTHON || join(project, '.venv', 'bin', 'python');
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

async function until(predicate, description, timeout = 30000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  throw new Error(`Timed out waiting for ${description}.`);
}

async function launch(t) {
  const directory = await mkdtemp(join(tmpdir(), 'portfolio-review-dom-http-'));
  const child = spawn(python, ['-u', '-c', serverScript, directory], {
    cwd: project, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '', errors = '', spawnError = null;
  child.stdout.on('data', chunk => { output += chunk; });
  child.stderr.on('data', chunk => { errors = (errors + chunk).slice(-8000); });
  child.on('error', error => { spawnError = error; });
  let dom;
  t.after(async () => {
    dom?.window.close();
    if (child.pid && child.exitCode === null && child.signalCode === null) {
      const exited = new Promise(resolve => child.once('exit', resolve));
      child.kill('SIGTERM');
      await exited;
    }
    await rm(directory, {recursive: true, force: true});
  });
  await until(() => {
    if (spawnError) throw new Error(`Python test server could not start: ${spawnError.message}`);
    if (child.exitCode !== null) throw new Error(`Python test server failed: ${errors}`);
    return output.includes('\n');
  }, 'synthetic server initialization', 60000);
  const {port, run_id: initialRunId} = JSON.parse(output.split('\n')[0]);
  const origin = `http://127.0.0.1:${port}`;
  const response = await fetch(origin);
  assert.equal(response.status, 200);
  const runtimeErrors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', error => runtimeErrors.push(error.message));
  dom = new JSDOM(await response.text(), {
    url: origin, runScripts: 'outside-only', virtualConsole, pretendToBeVisual: true,
  });
  const {window} = dom;
  window.addEventListener('error', event => runtimeErrors.push(event.message));
  // These DOM method shims implement state transitions only; they do not model
  // focus trapping, layout, user activation or native download behavior.
  window.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  window.HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
  window.HTMLElement.prototype.scrollIntoView = function () {};
  window.fetch = (path, options = {}) => fetch(new URL(path, origin), {
    ...options,
    headers: {...options.headers, Origin: origin},
  });
  window.__stateFunctions = stateFunctions;
  const researchPath = window.document.querySelector('script[src$="research.js"]').src;
  const scriptResponse = await fetch(researchPath);
  assert.equal(scriptResponse.status, 200);
  const researchSource = (await scriptResponse.text()).replace(
    /import\s*\{([\s\S]*?)\}\s*from\s*['"]\.\/research-state\.js['"];?/,
    'const {$1} = window.__stateFunctions;',
  );
  window.eval(`(() => {\n${researchSource}\n})();`);
  const collectorResponse = await fetch(window.document.querySelector('script[src$="app.js"]').src);
  assert.equal(collectorResponse.status, 200);
  window.eval(`(() => {\n${await collectorResponse.text()}\n})();`);
  const $ = id => window.document.getElementById(id);
  await until(() => $('draft-state').textContent === 'Saved', 'initial saved run render');
  assert.equal($('research-error').hidden, true, $('research-error').textContent);
  return {window, $, origin, initialRunId, runtimeErrors};
}

function namedInput(window, labelText) {
  const label = [...window.document.querySelectorAll('label')]
    .find(label => [...label.children].some(child => child.tagName === 'SPAN' && child.textContent === labelText));
  assert.ok(label, `Missing labeled input: ${labelText}`);
  const input = label.querySelector('input, select, textarea');
  assert.ok(input, `Label has no control: ${labelText}`);
  return input;
}

function setInput(window, control, value) {
  if (control.type === 'checkbox') control.checked = value;
  else control.value = String(value);
  control.dispatchEvent(new window.Event(control.type === 'checkbox' || control.tagName === 'SELECT' ? 'change' : 'input', {bubbles: true}));
}

test('research event handlers integrate with a disposable synthetic HTTP application', {timeout: 120000}, async t => {
  const {window, $, origin, initialRunId, runtimeErrors} = await launch(t);
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
