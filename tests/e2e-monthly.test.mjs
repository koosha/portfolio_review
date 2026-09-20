// The six monthly steps an owner performs, in order, against one synthetic application:
// a collector holding two Yahoo-shaped accounts, a fake paired Chrome answering the
// collection and providers patched to return fixture research. No network is reached and
// no real holdings exist. In-memory DOM with the real local HTTP service: this does not
// test browser layout, accessibility APIs, Chrome policy, extension behavior or browser
// security enforcement, and where jsdom cannot model a browser behavior the test says so
// rather than pretending to have checked it.
import test, {after} from 'node:test';
import assert from 'node:assert/strict';
import {rm, writeFile} from 'node:fs/promises';
import {homedir} from 'node:os';
import {JSDOM} from 'jsdom';
import {boot, open, until, namedInput, setInput} from './support/dom-http.mjs';

const serverScript = `
import sys

from tests.support.monthly_journey import serve

serve(sys.argv[1])
`;

// One application shared by every case that only reads what the journey published; the
// cases that need their own process (a restart, a second application) boot their own.
const shared = [];
const context = {after: task => shared.push(task)};
after(async () => {
  while (shared.length) await shared.pop()();
});
let application = null;
const journey = () => (application ??= boot(context, serverScript));

// The shared application once it holds at least one published review, which every case
// after the journey reads. The journey publishes three; a case run on its own publishes
// the first itself rather than depending on another test having run.
let publishing = null;
async function published() {
  const server = await journey();
  publishing ??= (async () => {
    if ((await read(server.origin, '/api/research')).runs.length) return;
    const page = await open(context, server.origin, {ready: collected});
    await until(() => !page.$('update-analyze').disabled, 'the page to finish its first load');
    page.$('update-analyze').click();
    await operation(page.$, server.origin);
  })();
  await publishing;
  return server;
}

const collected = $ => $('current-collection-state')?.textContent === 'Published';
const savedRun = $ => $('draft-state')?.textContent === 'Saved';
const accountCards = $ => $('current-accounts').querySelectorAll('.account-card').length;
const exceptionForms = $ => $('current-exception-forms').querySelectorAll('form.exception-form');
const stageChips = $ => [...$('workflow-progress').querySelectorAll('.stage-chip')];
const stageChip = ($, name) => stageChips($).find(node => node.dataset.stage === name);
const detailTab = (window, name) => window.document.querySelector(`[data-detail-tab="${name}"]`);
// The explicit cash assumption is labelled as a sentence, not a bare field name.
const cashReturn = window => [...window.document.querySelectorAll('label')]
  .find(label => /cash.*return/i.test(label.textContent) && label.querySelector('input'))
  ?.querySelector('input');
const priorityButton = ($, id, symbol) =>
  [...$(id).querySelectorAll('button')].find(node => node.textContent.includes(symbol));

// Keyboard reach, as far as jsdom can honestly model it. jsdom implements no sequential
// focus navigation, so nothing here presses Tab: the controls a browser would stop at are
// read from the document in order, which is what decides that order in a real browser.
const FOCUSABLE = 'a[href],button,input,select,textarea,summary,[tabindex]';
const focusOrder = window => [...window.document.querySelectorAll(FOCUSABLE)]
  .filter(node => !node.disabled && node.getAttribute('tabindex') !== '-1'
    && !node.hidden && !node.closest('[hidden]'));
// A focused native button is activated by Enter. jsdom performs no activation behavior of
// its own, so this delivers the keydown the page may intercept and then the click the
// platform would synthesize from it; it does not test the browser's own key handling.
function pressEnter(window, element) {
  element.focus();
  assert.equal(window.document.activeElement, element, 'the control took focus');
  assert.equal(element.tagName, 'BUTTON', 'Enter activation is the platform behavior of a button');
  const delivered = element.dispatchEvent(new window.KeyboardEvent('keydown',
    {key: 'Enter', bubbles: true, cancelable: true}));
  assert.equal(delivered, true, 'no handler cancelled Enter before the platform activation');
  window.document.activeElement.click();
}
// jsdom implements neither modal dialogs nor the Escape key, so this models what a browser
// does with Escape on an open dialog: a cancelable `cancel` event and, unless the page
// prevents it, the platform closing the dialog. Focus trapping is not modelled.
function pressEscape(window, dialog) {
  assert.equal(dialog.tagName, 'DIALOG', 'Escape dismissal is the platform behavior of a dialog');
  dialog.dispatchEvent(new window.KeyboardEvent('keydown',
    {key: 'Escape', bubbles: true, cancelable: true}));
  if (dialog.dispatchEvent(new window.Event('cancel', {cancelable: true}))) dialog.close();
}

async function read(origin, path) {
  const response = await fetch(new URL(path, origin), {headers: {Origin: origin}});
  assert.equal(response.status, 200, `${path} answered ${response.status}`);
  return response.json();
}

// An operation publishes exactly one run and the page follows the durable record, so
// waiting on the page's own announcement is waiting on the operation itself.
// The page starts an operation with the local token; a case that needs one running while
// nothing is watching it starts the same one the same way, without a page to poll it.
async function startOperation(origin, operationKey) {
  const {token} = await read(origin, '/api/state');
  const response = await fetch(new URL('/api/research/workflows', origin), {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Local-Token': token, Origin: origin},
    body: JSON.stringify({operation_key: operationKey, review_kind: 'current', collect: true}),
  });
  assert.equal(response.status, 202, `starting an operation answered ${response.status}`);
  return response.json();
}

const LIVE = ['queued', 'running', 'waiting'];

// The operation record once it has stopped moving, whatever it stopped at.
async function settled(origin, workflowId, {timeout = 600000} = {}) {
  let record = null;
  await until(async () => {
    record = await read(origin, `/api/research/workflows/${encodeURIComponent(workflowId)}`);
    return !LIVE.includes(record.status);
  }, 'the operation to finish', timeout);
  return record;
}

async function operation($, origin, {timeout = 600000} = {}) {
  // Read from the recorded announcements rather than the live element: the page replaces
  // this line as soon as the reloaded run settles, so sampling it every 25 ms can miss
  // the very message being waited for and then wait out the whole timeout. Called right
  // after the click that starts the operation, so the mark excludes the previous run's
  // announcement and includes everything this one says.
  const said = $('research-status').ownerDocument.defaultView.__announcements ?? [];
  const from = said.length;
  await until(() => !$('workflow-progress').hidden, 'the operation progress strip');
  await until(() => said.slice(from).some(text => /Review saved/.test(text)),
    'the published review loaded and announced', timeout);
  await until(() => savedRun($), 'the published review saved into the page');
  const [newest] = await read(origin, '/api/research/workflows');
  assert.equal(newest.status, 'complete', JSON.stringify(newest.error || newest.stages));
  assert.equal($('research-run').value, newest.run_id);
  return newest.run_id;
}

test('the owner completes the six monthly steps with no developer intervention',
  {timeout: 1800000}, async t => {
  const {origin} = await journey();
  const {window, $, runtimeErrors} = await open(t, origin, {ready: collected});
  let first = '', second = '';

  // 1. Open Overview: what was collected, and the honest absence of an analysis.
  await t.test('step 1 · current holdings and their dates read with no saved review', async () => {
    await until(() => $('draft-state').textContent === 'Empty', 'an empty draft with no saved run');
    assert.equal(accountCards($), 2);
    assert.match($('current-dates').textContent, /Collected/);
    assert.match($('analysis-caption').textContent, /No completed analysis/);
    assert.equal($('workflow-progress').hidden, true, 'no operation has run yet');
    assert.doesNotMatch($('current-dates').textContent, /NaN|undefined|Invalid Date/);
    assert.equal($('research-error').hidden, true, $('research-error').textContent);
    assert.deepEqual(runtimeErrors, []);
  });

  // 2. One button: collect, resolve, fetch, analyze, publish. A second click during the
  // operation is the same operation, not a second one.
  await t.test('step 2 · Update & analyze progresses collecting → publishing once', async () => {
    $('update-analyze').click();
    $('update-analyze').click();
    await until(() => !$('workflow-progress').hidden, 'the operation progress strip');
    assert.deepEqual(stageChips($).map(node => node.dataset.stage),
      ['collecting', 'resolving', 'fetching', 'analyzing', 'publishing']);
    assert.equal($('cancel-workflow').hidden, false, 'cancellation stays available while running');
    assert.equal((await read(origin, '/api/research/workflows')).length, 1,
      'a duplicate click reuses the running operation');
    first = await operation($, origin);
    assert.equal(stageChip($, 'publishing').dataset.status, 'complete');
    assert.equal(stageChip($, 'collecting').dataset.status, 'complete');
    assert.equal($('cancel-workflow').hidden, true);
    assert.match($('analysis-caption').textContent, /Last completed analysis/);
    assert.equal((await read(origin, '/api/research/workflows')).length, 1,
      'exactly one operation was created');
    assert.equal($('research-error').hidden, true, $('research-error').textContent);
    assert.deepEqual(runtimeErrors, []);
  });

  // 3. The one question the collection could not answer itself, answered once.
  await t.test('step 3 · the open account-currency exception is resolved to none', async () => {
    await until(() => exceptionForms($).length === 1, 'exactly one open exception form');
    const form = exceptionForms($)[0];
    assert.equal(form.dataset.kind, 'account_currency');
    assert.match(form.textContent, /SIM03/);
    assert.match($('exception-count').textContent, /1/);
    setInput(window, namedInput(window, 'Currency of reported values'), 'USD');
    const save = [...form.querySelectorAll('button')].find(node => node.textContent === 'Save');
    assert.ok(save, 'the exception form has a Save button');
    save.click();
    await until(() => $('current-exception-forms').textContent.includes('No open exceptions'),
      'the resolved exception to disappear');
    assert.equal(exceptionForms($).length, 0);
    assert.equal((await read(origin, '/api/research/exceptions')).count, 0);
    assert.equal($('research-error').hidden, true, $('research-error').textContent);
    assert.deepEqual(runtimeErrors, []);
  });

  // 4. The reading list and the evidence under it.
  await t.test('step 4 · what changed, risks, holdings and candidates open their evidence',
    async () => {
    assert.match($('what-changed').textContent, /versus/, 'dated brief changes are stated');
    assert.match($('what-changed').textContent, /prices:SIM01|estimates:SIM01/,
      'each stated change names its source');
    assert.match($('portfolio-risks').textContent, /cap/i,
      'concentration is stated against the confirmed cap');
    assert.ok($('stress-table').textContent.trim(), 'the mechanical stresses are shown');
    assert.ok(priorityButton($, 'holdings-review', 'SIM01'), 'SIM01 is a holding to review');
    assert.ok(priorityButton($, 'new-candidates', 'SIM07'),
      'an unowned company collected a stated reason');
    assert.doesNotMatch($('what-changed').textContent, /undefined|NaN/);

    priorityButton($, 'holdings-review', 'SIM01').click();
    await until(() => $('security-dialog').hasAttribute('open'), 'the security detail dialog');
    assert.match($('security-dialog-title').textContent, /SIM01/);
    assert.deepEqual([...window.document.querySelectorAll('[data-detail-tab]')]
      .map(node => node.dataset.detailTab), ['evidence', 'calculation', 'assumptions', 'limitations']);
    assert.match($('detail-evidence').textContent, /prices:SIM01|estimates:SIM01/,
      'the evidence names the sources behind it');
    detailTab(window, 'calculation').click();
    assert.equal($('detail-evidence').hidden, true);
    assert.match($('detail-calculation').textContent, /EPS|value per share/i);
    detailTab(window, 'assumptions').click();
    assert.match($('detail-assumptions').textContent, /terminal multiple|discount rate/i);
    detailTab(window, 'limitations').click();
    assert.match($('detail-limitations').textContent, /ok|missing|stale/);
    for (const link of $('security-dialog').querySelectorAll('a')) {
      assert.ok(link.getAttribute('href'), `A named source link needs a target: ${link.textContent}`);
    }
    $('close-security-dialog').click();
    assert.equal($('security-dialog').hasAttribute('open'), false);
    assert.deepEqual(runtimeErrors, []);
  });

  // 5. A stated market view is the owner's, not the engine's: it changes the result and
  // reset puts the saved assumptions back.
  await t.test('step 5 · a shared-state knob recalculates and reset restores the run', async () => {
    window.document.querySelector('[data-page="scenarios"]').click();
    const central = namedInput(window, 'Central market return (%)');
    const savedCentral = central.value, savedCash = cashReturn(window).value;
    const savedResults = $('scenario-results').textContent;
    setInput(window, central, 12);
    await until(() => $('draft-state').textContent === 'Dirty', 'the edited draft');
    assert.match($('resolved-diff').textContent, /shared_state/,
      'the stated market view is a retained change');
    assert.match($('shared-state-editor').textContent, /next .*review/i,
      'the panel says when a stated market view is expanded into scenarios');
    // Two separate claims, kept apart. The stated market view is a retained change, and
    // where it goes is checked end to end by 'a stated market view saved for future
    // reviews reaches the next review' below. What a recalculation can move is something
    // else: a saved run's joint scenarios are frozen inputs, so the recalculation replays
    // them and only the assumption applied to those scenarios changes this run's outcome.
    setInput(window, cashReturn(window), 4);
    $('recalculate').click();
    await until(() => $('draft-state').textContent === 'Preview', 'the recalculated preview');
    assert.notEqual($('scenario-results').textContent, savedResults,
      'the explicit cash assumption changes the portfolio outcome');
    $('reset-draft').click();
    await until(() => $('draft-dialog').hasAttribute('open'), 'the explicit reset choice');
    $('draft-dialog').querySelector('[data-draft-choice="discard"]').click();
    await until(() => $('draft-state').textContent === 'Saved', 'the reset draft');
    assert.equal(namedInput(window, 'Central market return (%)').value, savedCentral);
    assert.equal(cashReturn(window).value, savedCash);
    assert.equal($('scenario-results').textContent, savedResults, 'reset restores the saved run');
    assert.equal($('research-error').hidden, true, $('research-error').textContent);
    assert.deepEqual(runtimeErrors, []);
  });

  // 6. The decision is the point of the month. A review analysed while the currency
  // question was still open produces no feasible alternative and says so; the review run
  // after the answer offers the alternatives the choice is weighed against.
  await t.test('step 6 · the recorded decision survives into the next review', async () => {
    window.document.querySelector('[data-page="review"]').click();
    assert.equal($('save-decision').disabled, false, 'a saved run can record a decision');
    $('save-decision').click();
    await new Promise(resolve => setTimeout(resolve, 250));
    assert.equal($('research-error').hidden, true, $('research-error').textContent);
    await until(() => $('decision-dialog').hasAttribute('open'), 'the decision dialog');
    assert.match($('decision-compared').textContent, /no feasible alternative/i,
      'a review that could not certify a basket says so instead of offering one');
    $('close-decision-dialog').click();
    assert.equal($('decision-dialog').hasAttribute('open'), false);

    // A dirty draft is the owner's: finishing an operation may not throw it away.
    setInput(window, namedInput(window, 'Cost per side (basis points)'), 27);
    await until(() => $('draft-state').textContent === 'Dirty', 'the edited draft');
    $('update-analyze').click();
    await until(() => $('draft-dialog').hasAttribute('open'), 'the explicit draft choice');
    $('draft-dialog').querySelector('[data-draft-choice="retain"]').click();
    second = await operation($, origin);
    assert.notEqual(second, first);

    window.document.querySelector('[data-page="review"]').click();
    assert.equal($('draft-state').textContent, 'Saved', 'the new run is the retained draft base');
    assert.equal($('save-decision').disabled, false, 'the new run can record a decision');
    $('save-decision').click();
    await new Promise(resolve => setTimeout(resolve, 250));
    assert.equal($('research-error').hidden, true, $('research-error').textContent);
    await until(() => $('decision-dialog').hasAttribute('open'), 'the decision dialog');
    assert.match($('decision-compared').textContent, /no_change|No Change/i,
      'the alternatives the choice is weighed against are shown');
    assert.equal($('decision-alternative').disabled, true,
      'a no-change decision records no alternative');
    $('confirm-decision').click();
    await until(() => !$('decision-error').hidden, 'the refusal stated inside the dialog');
    assert.match($('decision-error').textContent, /rationale/i);
    setInput(window, $('decision-action'), 'review_candidate');
    setInput(window, $('decision-alternative'), 'simple_equal_issuer_sleeve');
    setInput(window, $('decision-rationale'), 'The sleeve alternative is evidenced this month.');
    $('confirm-decision').click();
    await until(() => !$('decision-dialog').hasAttribute('open'), 'the recorded decision');
    await until(() => /simple_equal_issuer_sleeve/.test($('decision-history').textContent),
      'the recorded decision in the history');
    assert.match($('decision-history').textContent, /sleeve alternative is evidenced/);

    // The next review reads the decision back: it is what changed since last month.
    const third = await (async () => {
      $('update-analyze').click();
      return operation($, origin);
    })();
    assert.notEqual(third, second);
    window.document.querySelector('[data-page="overview"]').click();
    await until(() => /simple_equal_issuer_sleeve/.test($('what-changed').textContent),
      'the previous decision restated by the next review');
    assert.match($('what-changed').textContent, /sleeve alternative is evidenced/);

    // And the draft retained two operations ago is still where the owner left it.
    setInput(window, $('research-run'), first);
    await until(() => $('research-run').value === first
      && $('draft-state').textContent !== 'Calculating', 'the first run reloaded');
    assert.equal($('draft-state').textContent, 'Dirty', 'the retained draft survived the operations');
    assert.equal(namedInput(window, 'Cost per side (basis points)').value, '27');
    $('reset-draft').click();
    await until(() => $('draft-dialog').hasAttribute('open'), 'the explicit reset choice');
    $('draft-dialog').querySelector('[data-draft-choice="discard"]').click();
    await until(() => $('draft-state').textContent === 'Saved', 'the discarded draft');
    assert.equal($('research-error').hidden, true, $('research-error').textContent);
    assert.deepEqual(runtimeErrors, []);
  });
});

// Two pages open on one application. Each page keeps its own draft, so a calculation in
// one may not move the other's saved run or the record behind it.
test('a preview in one tab leaves the other tab’s saved run alone', {timeout: 600000},
  async t => {
  const {origin} = await published();
  const one = await open(t, origin);
  const two = await open(t, origin);
  const runId = one.$('research-run').value;
  assert.equal(two.$('research-run').value, runId, 'both tabs opened the same saved run');
  two.window.document.querySelector('[data-page="scenarios"]').click();
  const undisturbed = two.$('scenario-results').textContent;
  const stored = await read(origin, `/api/research/runs/${encodeURIComponent(runId)}`);

  one.window.document.querySelector('[data-page="scenarios"]').click();
  setInput(one.window, cashReturn(one.window), 4);
  await until(() => one.$('draft-state').textContent === 'Dirty', 'the first tab’s edited draft');
  assert.equal(two.$('draft-state').textContent, 'Saved', 'the second tab is still on saved inputs');
  one.$('recalculate').click();
  await until(() => one.$('draft-state').textContent === 'Preview', 'the first tab’s preview');

  assert.equal(two.$('draft-state').textContent, 'Saved', 'the second tab’s draft is untouched');
  assert.equal(two.$('reset-draft').disabled, true, 'the second tab has nothing to reset');
  assert.equal(two.$('scenario-results').textContent, undisturbed,
    'the second tab still shows the saved run’s results');
  assert.equal(two.$('research-run').value, runId);
  assert.deepEqual(await read(origin, `/api/research/runs/${encodeURIComponent(runId)}`), stored,
    'a preview published nothing: the saved run is exactly as it was');
  assert.equal(one.$('research-error').hidden, true, one.$('research-error').textContent);
  assert.equal(two.$('research-error').hidden, true, two.$('research-error').textContent);
  assert.deepEqual(one.runtimeErrors, []);
  assert.deepEqual(two.runtimeErrors, []);

  one.$('reset-draft').click();
  await until(() => one.$('draft-dialog').hasAttribute('open'), 'the explicit reset choice');
  one.$('draft-dialog').querySelector('[data-draft-choice="discard"]').click();
  await until(() => one.$('draft-state').textContent === 'Saved', 'the discarded draft');
});

// A saved run is a record, not a workspace: saving a recalculation writes a child run and
// the run it was calculated from is still byte-for-byte what it was when it was published.
test('saving a recalculation writes a child run and never rewrites its parent',
  {timeout: 600000}, async t => {
  const {origin} = await published();
  const {window, $, runtimeErrors} = await open(t, origin);
  const parent = $('research-run').value;
  const stored = await read(origin, `/api/research/runs/${encodeURIComponent(parent)}`);
  window.document.querySelector('[data-page="scenarios"]').click();
  const parentResults = $('scenario-results').textContent;

  setInput(window, cashReturn(window), 5);
  await until(() => $('draft-state').textContent === 'Dirty', 'the edited draft');
  $('recalculate').click();
  await until(() => $('draft-state').textContent === 'Preview', 'the recalculated preview');
  assert.equal($('save-run').disabled, false, 'a calculated preview can be retained');
  $('save-run').click();
  await until(() => $('draft-state').textContent === 'Saved' && $('research-run').value !== parent,
    'the child run saved and loaded');
  const child = $('research-run').value;
  assert.notEqual(child, parent, 'the saved calculation is a new run, not an overwrite');
  assert.notEqual($('scenario-results').textContent, parentResults,
    'the child run carries the recalculated result');

  assert.deepEqual(await read(origin, `/api/research/runs/${encodeURIComponent(parent)}`), stored,
    'the parent run is exactly what it was before the child was saved');
  const runs = (await read(origin, '/api/research')).runs;
  assert.ok(runs.some(run => run.run_id === child), 'the child run is listed');
  assert.ok(runs.some(run => run.run_id === parent), 'and so is the run it came from');
  const saved = await read(origin, `/api/research/runs/${encodeURIComponent(child)}`);
  assert.equal(saved.result.metadata.parent_run_id, parent,
    'the child names the run it was calculated from');
  assert.equal(saved.result.metadata.preview, false, 'a retained calculation is not a preview');

  // Reopening the parent shows the parent, not the calculation that was saved after it.
  setInput(window, $('research-run'), parent);
  await until(() => $('research-run').value === parent
    && $('draft-state').textContent === 'Saved', 'the parent run reopened');
  assert.equal($('scenario-results').textContent, parentResults,
    'the reopened parent shows its own retained results');
  assert.equal($('research-error').hidden, true, $('research-error').textContent);
  assert.deepEqual(runtimeErrors, []);
});

// What the owner keeps and what the owner sends out are the same review: the JSON export
// is the run the service serves, and the report names the same run without naming this
// machine. No filesystem path may travel with a document meant to be shared.
test('the JSON and HTML exports restate the loaded run and carry no filesystem paths',
  {timeout: 600000}, async t => {
  const {origin, directory} = await published();
  const {$, runtimeErrors} = await open(t, origin);
  const runId = $('research-run').value;
  assert.ok(runId, 'a saved run is loaded');
  assert.equal($('export-json').getAttribute('href'),
    `/api/research/export/${encodeURIComponent(runId)}.json`);
  assert.equal($('export-html').getAttribute('href'),
    `/api/research/export/${encodeURIComponent(runId)}.html`);
  assert.equal($('export-json').hasAttribute('aria-disabled'), false);

  const stored = await read(origin, `/api/research/runs/${encodeURIComponent(runId)}`);
  const exported = await fetch(new URL($('export-json').getAttribute('href'), origin),
    {headers: {Origin: origin}});
  assert.equal(exported.status, 200);
  assert.match(exported.headers.get('content-disposition') || '', new RegExp(runId));
  assert.deepEqual(await exported.json(), stored.result,
    'the exported JSON is the run result the service serves');

  const report = await fetch(new URL($('export-html').getAttribute('href'), origin),
    {headers: {Origin: origin}});
  assert.equal(report.status, 200);
  assert.match(report.headers.get('content-type') || '', /text\/html/);
  const html = await report.text();
  assert.ok(html.includes(runId), 'the report names the run it was made from');
  for (const secret of [directory, homedir(), '/Users/', '/private/var/folders', '.venv']) {
    assert.equal(html.includes(secret), false, `The report leaks a filesystem path: ${secret}`);
  }
  assert.equal($('research-error').hidden, true, $('research-error').textContent);
  assert.deepEqual(runtimeErrors, []);
});

// The month can be driven from the keyboard: the one button that starts it is reachable
// in order and activates on Enter, and every dialog the owner is stopped by can be
// dismissed with Escape without losing the work behind it.
test('the update button is reachable and activates, and every dialog closes on Escape',
  {timeout: 600000}, async t => {
  const {origin} = await published();
  const {window, $, runtimeErrors} = await open(t, origin);
  const update = $('update-analyze');

  const order = focusOrder(window);
  const index = order.indexOf(update);
  assert.ok(index >= 0, 'the update button is in the sequential focus order');
  assert.equal(update.hasAttribute('tabindex'), false,
    'and takes the document order, not a tabindex');
  assert.ok(order.slice(0, index).some(node => node.dataset.page),
    'the page navigation is reached before it');
  assert.ok(order.indexOf($('account-scope')) > index, 'and the context controls after it');

  // Escape on the decision dialog: nothing is recorded and nothing is lost.
  $('save-decision').click();
  await until(() => $('decision-dialog').hasAttribute('open'), 'the decision dialog');
  pressEscape(window, $('decision-dialog'));
  assert.equal($('decision-dialog').hasAttribute('open'), false, 'Escape dismissed the decision');

  // Escape on a security detail: the reading list is still behind it.
  const holding = $('holdings-review').querySelector('button');
  assert.ok(holding, 'the run lists a holding to review');
  holding.click();
  await until(() => $('security-dialog').hasAttribute('open'), 'the security detail dialog');
  pressEscape(window, $('security-dialog'));
  assert.equal($('security-dialog').hasAttribute('open'), false, 'Escape dismissed the detail');

  // Escape on the draft choice is the cancel it offers: the edits stay exactly as typed.
  setInput(window, namedInput(window, 'Cost per side (basis points)'), 31);
  await until(() => $('draft-state').textContent === 'Dirty', 'the edited draft');
  $('reset-draft').click();
  await until(() => $('draft-dialog').hasAttribute('open'), 'the explicit reset choice');
  pressEscape(window, $('draft-dialog'));
  await until(() => !$('draft-dialog').hasAttribute('open'), 'the dismissed reset choice');
  assert.equal($('draft-state').textContent, 'Dirty',
    'Escape cancelled the reset, keeping the draft');
  assert.equal(namedInput(window, 'Cost per side (basis points)').value, '31');
  $('reset-draft').click();
  await until(() => $('draft-dialog').hasAttribute('open'), 'the explicit reset choice');
  $('draft-dialog').querySelector('[data-draft-choice="discard"]').click();
  await until(() => $('draft-state').textContent === 'Saved', 'the discarded draft');

  // Enter on the focused update button starts the month, and cancelling ends it.
  assert.equal(update.disabled, false);
  const existing = (await read(origin, '/api/research/workflows')).length;
  pressEnter(window, update);
  await until(async () => (await read(origin, '/api/research/workflows')).length > existing,
    'the operation Enter started');
  const [started] = await read(origin, '/api/research/workflows');
  await until(() => !$('workflow-progress').hidden && !$('cancel-workflow').hidden,
    'the running operation shown as stoppable');
  $('cancel-workflow').click();
  await until(async () => !['queued', 'running', 'waiting'].includes(
    (await read(origin, `/api/research/workflows/${started.workflow_id}`)).status),
    'the operation started from the keyboard to stop', 600000);
  // The page announces the stop only once it has reloaded the holdings behind it, which
  // is what makes the operation finished rather than merely no longer running.
  await until(() => /Cancelled before publishing/.test($('research-status').textContent),
    'the stopped operation announced', 600000);
  assert.equal($('cancel-workflow').hidden, true);
  assert.equal(update.disabled, false, 'a new month can be started again');
  assert.deepEqual(runtimeErrors, []);
});

// A published review froze the research it was calculated from, so recalculating it is
// arithmetic on records the owner already holds. This runs the recalculation with every
// provider raising, which is what the owner's aeroplane, outage or rate limit looks like.
test('a recalculation of a published review reaches no provider', {timeout: 900000},
  async t => {
  const server = await boot(t, serverScript);
  const {window, $, runtimeErrors} = await open(t, server.origin, {ready: collected});
  await until(() => !$('update-analyze').disabled, 'the page to finish its first load');
  $('update-analyze').click();
  const parent = await operation($, server.origin);

  // From here every provider raises. Nothing else about the application changes.
  await writeFile(server.info.offline_flag, 'Every provider refuses while this file exists.\n');

  window.document.querySelector('[data-page="scenarios"]').click();
  const publishedResults = $('scenario-results').textContent;
  setInput(window, cashReturn(window), 6);
  await until(() => $('draft-state').textContent === 'Dirty', 'the edited draft');
  $('recalculate').click();
  await until(() => $('draft-state').textContent === 'Preview',
    'the offline recalculation to finish', 600000);
  assert.notEqual($('scenario-results').textContent, publishedResults,
    'the recalculation produced its own result with no provider available');
  assert.equal($('research-error').hidden, true, $('research-error').textContent);

  // Keeping it is offline work too: the child run is written from the frozen research.
  $('save-run').click();
  await until(() => $('draft-state').textContent === 'Saved'
    && $('research-run').value !== parent, 'the offline recalculation saved');
  const child = $('research-run').value;
  const saved = await read(server.origin, `/api/research/runs/${encodeURIComponent(child)}`);
  assert.equal(saved.result.metadata.parent_run_id, parent,
    'the offline child names the run it was calculated from');
  assert.equal($('research-error').hidden, true, $('research-error').textContent);
  assert.deepEqual(runtimeErrors, []);

  // And the refusal is real rather than a file nothing reads: a fresh month does need
  // providers, and its record says they could not be reached.
  const attempt = await startOperation(server.origin, 'offline-month');
  const finished = await settled(server.origin, attempt.workflow_id);
  const said = JSON.stringify({status: finished.status, error: finished.error,
    stages: finished.stages, providers: finished.providers});
  assert.match(said, /offline|unavailable|could not be reached|refus/i,
    `A month run with every provider refusing said nothing about it: ${said}`);
});

// The application is not the review. An operation dies with the process running it, so
// the next application to open the same data directory has to say that it died rather
// than leave a month that looks like it is still going; and the review published before
// the interruption is still the one that loads.
test('an operation killed with its application is failed on restart and the last run loads',
  {timeout: 900000}, async t => {
  const first = await boot(t, serverScript);
  const before = await open(t, first.origin, {ready: collected});
  await until(() => !before.$('update-analyze').disabled, 'the page to finish its first load');
  before.$('update-analyze').click();
  const published = await operation(before.$, first.origin);
  before.window.close();  // No page is watching when the application dies.

  // A second month is started and the application is killed while it is still running,
  // which is the one way the record cannot be closed by the application that wrote it.
  const doomed = await startOperation(first.origin, 'killed-by-restart');
  await until(async () => LIVE.includes(
    (await read(first.origin, '/api/research')).active_workflow?.status),
    'the second operation running');
  await first.stop('SIGKILL');

  // The same data directory, a new application.
  const restarted = await boot(t, serverScript, {directory: first.directory});
  const recovered = await read(restarted.origin,
    `/api/research/workflows/${encodeURIComponent(doomed.workflow_id)}`);
  assert.equal(recovered.status, 'failed',
    'the interrupted operation is failed, not still running');
  assert.match(recovered.error, /restart/i, 'and the record says why it stopped');
  const status = await read(restarted.origin, '/api/research');
  assert.ok(!status.active_workflow, 'no operation is claimed to be running');
  assert.equal(status.latest_run_id, published,
    'the review published before the kill is still the newest');

  // What the owner sees on reopening: the last review, and an account of the month that
  // did not finish. Nothing has to be repaired by hand before starting the next one.
  const after = await open(t, restarted.origin, {ready: collected});
  await until(() => after.$('draft-state').textContent === 'Saved',
    'the last review reloaded into the page');
  assert.equal(after.$('research-run').value, published,
    'the page reopened on the review published before the kill');
  assert.equal(after.$('workflow-progress').hidden, false,
    'the interrupted month is accounted for');
  assert.match(after.$('workflow-progress').textContent, /did not complete/i);
  assert.match(after.$('workflow-progress').textContent, /restart/i);
  assert.equal(after.$('cancel-workflow').hidden, true, 'there is nothing left to cancel');
  assert.equal(after.$('update-analyze').disabled, false, 'a new month can be started');
  assert.equal(after.$('research-error').hidden, true, after.$('research-error').textContent);
  assert.deepEqual(after.runtimeErrors, []);
  await restarted.stop();
});

// An operation is authorized with the local token the page reads on load, so the page
// the service serves may not offer one before it has read it. Exposed by the two cases
// above, whose first click landed while the page still read "Loading" and was refused
// with "Reload the local app before making changes." rather than starting the month.
test('the served page offers no operation before it can authorize one', {timeout: 600000},
  async t => {
  const {origin} = await journey();
  const response = await fetch(origin, {headers: {Origin: origin}});
  assert.equal(response.status, 200);
  const served = new JSDOM(await response.text()).window.document;
  const writes = await writeButtons(origin);
  // The set is read from the page's own wiring rather than listed here, so a control
  // added later is covered the day it is added instead of the day someone remembers it.
  for (const id of ['update-analyze', 'monthly-review', 'refresh-research',
                    'save-settings', 'save-supplemental']) {
    assert.ok(writes.includes(id), `${id} is not among the writes read from research.js`);
  }
  for (const id of writes) {
    const node = served.getElementById(id);
    if (node?.tagName !== 'BUTTON') continue;
    assert.ok(node.disabled || node.hidden,
      `${id} is offered by the served markup before the page holds the local token`);
  }
  // And the loaded page offers them, which is what every case that starts a month clicks.
  const {$} = await open(t, origin, {ready: collected});
  await until(() => !$('update-analyze').disabled, 'the page to finish its first load');
  assert.equal($('save-settings').disabled, false, 'a loaded page can save settings');
  assert.equal($('research-error').hidden, true, $('research-error').textContent);
});

// Every load of a page that declares no icon asks the service for /favicon.ico, which it
// does not serve. That is one console error on every load and every hard refresh, and a
// console the owner has been trained to ignore is a console that hides the real fault
// during the recovery steps the README sends them to.
test('the served page names its own icon, so a load asks for no missing file',
  {timeout: 600000}, async () => {
  const {origin} = await journey();
  const head = new JSDOM(await (await fetch(origin, {headers: {Origin: origin}})).text());
  const icon = head.window.document.querySelector('link[rel~="icon"]');
  assert.ok(icon, 'the served head declares an icon');
  assert.ok(!/^https?:/i.test(icon.getAttribute('href') || ''),
    'and names it without reaching the network for it');
  const guessed = await fetch(new URL('/favicon.ico', origin), {headers: {Origin: origin}});
  assert.equal(guessed.status, 404,
    'the path a browser guesses without that declaration is not served');
});

// Every id wired to a handler that writes through the local token, read from the served
// research.js: a handler either posts a body itself, submits a job, or delegates to the
// two workflow starters. A button offered before the page holds that token is not merely
// inert — the service refuses the click with "Reload the local app before making changes."
async function writeButtons(origin) {
  const source = await (await fetch(new URL('/research.js', origin), {
    headers: {Origin: origin},
  })).text();
  return source.split(/(?:^|[^A-Za-z])wire\('/).slice(1)
    .map(chunk => [chunk.slice(0, chunk.indexOf("'")), chunk.slice(chunk.indexOf("'"))])
    .filter(([, body]) => /\bapi\([^\n]*?,|submitJob\(|startWorkflow\(|monthly\(/.test(body))
    .map(([id]) => id);
}

// Step 5 recalculates against frozen joint scenarios, so the stated market view cannot
// move that run's outcome and the step-5 case checks the cash assumption instead. The
// view's only route to the portfolio is save-settings → the configuration record →
// resolved_config → the next full review, and no other case clicks save-settings: break
// any link and the whole suite stays green while the owner's stated view never arrives.
test('a stated market view saved for future reviews reaches the next review',
  {timeout: 1800000}, async t => {
  const server = await boot(t, serverScript);
  const {window, $} = await open(t, server.origin, {ready: collected});
  await until(() => !$('update-analyze').disabled, 'the page to finish its first load');
  $('update-analyze').click();
  const first = await operation($, server.origin);
  window.document.querySelector('[data-page="scenarios"]').click();
  const stateReturns = async runId => (await read(server.origin,
    `/api/research/runs/${encodeURIComponent(runId)}`)).config.allocation.shared_state.market_returns;
  const returns = await stateReturns(first);
  const [central] = Object.keys(returns).filter(name => /central/i.test(name));
  assert.ok(central, `no central market state among ${Object.keys(returns)}`);
  const stated = Math.round(returns[central] * 100) + 5;

  setInput(window, namedInput(window, 'Central market return (%)'), stated);
  await until(() => $('draft-state').textContent === 'Dirty', 'the edited draft');
  assert.equal($('save-settings').disabled, false, 'a stated view can be saved for later');
  $('save-settings').click();
  await until(() => /Settings saved/i.test($('research-status').textContent),
    'the stated market view saved for future reviews');
  assert.equal($('research-error').hidden, true, $('research-error').textContent);

  // The draft itself is discarded, so nothing but the saved configuration can carry the
  // view into the next review: this is the record, not the page's own pending edit.
  $('reset-draft').click();
  await until(() => $('draft-dialog').hasAttribute('open'), 'the explicit reset choice');
  $('draft-dialog').querySelector('[data-draft-choice="discard"]').click();
  await until(() => $('draft-state').textContent === 'Saved', 'the reset draft');
  assert.equal(Math.round(namedInput(window, 'Central market return (%)').value), stated - 5,
    'the reset page shows the saved run’s view again, not the one stated for next time');

  $('update-analyze').click();
  const second = await operation($, server.origin);
  assert.notEqual(second, first);
  const after = (await stateReturns(second))[central];
  assert.equal(Math.round(after * 100), stated,
    'the next review was run against the market view the owner stated');
  // And the page reopened on that review shows the stated view as its own saved one, so
  // the round trip is pinned at both ends. The portfolio outcome is deliberately not the
  // witness here: this fixture supplies a per-security joint forecast for every holding,
  // and an explicit per-security forecast overrides the market state it would otherwise
  // be expanded from (scenarios.py), so the stated view moves the inputs, not the total.
  window.document.querySelector('[data-page="scenarios"]').click();
  assert.equal(Math.round(namedInput(window, 'Central market return (%)').value), stated,
    'the new review carries the stated market view as its own saved assumptions');
  assert.equal($('research-error').hidden, true, $('research-error').textContent);
});

// Switching views swaps one long page for another under whatever scroll offset the last
// one was left at, so a view can open past its own heading and controls. jsdom performs
// no layout: this pins the offset the page asks for and where focus lands, not what a
// browser would paint.
test('opening another view starts it at the top and moves focus there', {timeout: 600000},
  async t => {
  const {origin} = await published();
  const {window, $} = await open(t, origin, {ready: savedRun});
  const nav = name => window.document.querySelector(`[data-page="${name}"]`);
  window.scrollTo({top: 1200});
  assert.equal(window.scrollY, 1200, 'the harness models a scrolled page');
  nav('research').click();
  assert.equal($('page-research').hidden, false, 'the research view opened');
  assert.equal(window.scrollY, 0, 'the new view opens at its top, not mid-page');
  assert.equal(window.document.activeElement, $('workspace-main'),
    'focus follows the view change, so it is announced rather than silent');
  // Re-selecting the view already open is not a view change and leaves the reader put.
  window.scrollTo({top: 640});
  nav('research').click();
  assert.equal(window.scrollY, 640, 'reselecting the open view does not yank the reader');
});

// A load that fails leaves the page with no token for the rest of its life, so the three
// workflow buttons stay disabled for good. Saying "Loading the local research service…"
// under a control that will never enable is worse than the refusal it replaced: it names
// a state the page is not in and gives the owner nothing to do about it.
test('a page whose load failed says so rather than claiming it is still loading',
  {timeout: 600000}, async t => {
  const {origin} = await published();
  const {$} = await open(t, origin, {
    expectError: true,
    ready: $ => !$('research-error').hidden,
    intercept: path => (String(path).includes('/api/state')
      ? new Response('<html>not json</html>', {status: 500}) : undefined),
  });
  await until(() => !$('research-error').hidden, 'the failed load to be reported');
  const writes = await writeButtons(origin);
  for (const id of writes) {
    const node = $(id);
    if (node?.tagName !== 'BUTTON' || node.hidden) continue;
    assert.equal(node.disabled, true,
      `${id} is offered after the load failed, and the service would refuse the click`);
  }
  const title = $('update-analyze').title;
  assert.ok(!/loading/i.test(title), `the tooltip still claims a load in progress: ${title}`);
  assert.match(title, /reload/i, 'the tooltip says what to do about it');
  assert.match($('research-error').textContent, /could not load/i);
});
