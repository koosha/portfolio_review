import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source = await readFile(new URL('../static/research-state.js', import.meta.url), 'utf8');
const {newDraft, editDraft, merge, numberOrNull, changeHorizon, acceptPreview, previewReady,
  RequestGate, restoreDraft, draftKey, differences, validateConfigurationPatch, validateCompanyInputs, reviseAssessmentInputs} = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

test('blank numerical inputs remain missing while an explicit zero survives', () => {
  for (const missing of ['', '  ', null, undefined]) assert.equal(numberOrNull(missing), null);
  assert.equal(numberOrNull('0'), 0);
  assert.throws(() => numberOrNull('Infinity'));
  assert.throws(() => numberOrNull(true));
  assert.throws(() => numberOrNull([]));
  assert.throws(() => numberOrNull({}));
});
test('edits invalidate calculated results and a stale preview cannot overwrite them', () => {
  let draft = newDraft('one', {allocation:{horizon_months:12}});
  draft = editDraft(draft, {patch:{allocation:{transaction_cost_bps:25}}});
  const previousRevision = draft.revision;
  draft = editDraft(draft, {patch:{allocation:{transaction_cost_bps:10}}});
  assert.equal(acceptPreview(draft, previousRevision, 'old-job'), draft);
  assert.equal(previewReady(draft), false);
  draft = acceptPreview(draft, draft.revision, 'new-job');
  assert.equal(previewReady(draft), true);
  assert.equal(draft.baseConfig.allocation.horizon_months, 12);
});
test('changing horizon clears incompatible returns, priors, probabilities and EPS outcomes', () => {
  let draft = newDraft('one', {}, {valuations:{DEMO:{eps:{horizon_months:12,
    scenarios:[{label:'central',eps:5,pe:20,distributions_per_starting_share:1}]},dcf:{discount_rate:.1}}}});
  draft.patch = {allocation:{return_overrides:{DEMO:{central:.2}}, prior_returns:{DEMO:.1}, probability_overrides:{central:1}, cash_return:.03}};
  const changed = changeHorizon(draft, 6);
  assert.deepEqual(changed.patch.allocation.return_overrides, {});
  assert.deepEqual(changed.patch.allocation.prior_returns, {});
  assert.equal(changed.patch.allocation.cash_return, null);
  assert.equal(changed.workspace.valuations.DEMO.eps.scenarios[0].eps, null);
  assert.equal(changed.workspace.valuations.DEMO.dcf.discount_rate, .1);
  assert.equal(draft.workspace.valuations.DEMO.eps.scenarios[0].eps, 5);
});
test('run-specific drafts survive navigation and reload without sharing a mutable base', () => {
  const storage = new Map();
  storage.getItem = storage.get.bind(storage);
  const first = editDraft(newDraft('one'), {patch:{mandate:{issuer_cap:.2}}});
  storage.set(draftKey('one'), JSON.stringify(first));
  assert.equal(restoreDraft(storage, 'one').patch.mandate.issuer_cap, .2);
  assert.deepEqual(restoreDraft(storage, 'two').patch, {});
  storage.set(draftKey('broken'), '{');
  assert.equal(restoreDraft(storage, 'broken').dirty, false);
});
test('a later request invalidates earlier run or calculation responses', () => {
  const gate = new RequestGate(), first = gate.next(), latest = gate.next();
  assert.equal(gate.current(first), false);
  assert.equal(gate.current(latest), true);
});
test('resolved changes replace explicit scenario maps and reject prototype keys', () => {
  const base = {allocation:{return_overrides:{A:{central:.1}}, transaction_cost_bps:10}};
  const resolved = merge(base, {allocation:{return_overrides:{}}});
  assert.deepEqual(resolved.allocation.return_overrides, {});
  assert.equal(resolved.allocation.transaction_cost_bps, 10);
  assert.equal(differences(base, resolved)[0].field, 'allocation.return_overrides.A');
  assert.throws(() => merge({}, JSON.parse('{"__proto__":{"x":1}}')));
});
test('malformed advanced containers cannot replace a renderable draft', () => {
  assert.throws(() => validateConfigurationPatch({mandate:{account_permissions:{A:'all'}}}));
  assert.throws(() => validateConfigurationPatch({mandate:{locked_security_ids:'ABC'}}));
  assert.throws(() => validateConfigurationPatch({allocation:{return_overrides:{A:3}}}));
  assert.throws(() => validateCompanyInputs({valuations:{eps:{starting_price:100}}}));
  assert.throws(() => validateCompanyInputs({assessment:{sources:[null]}}));
  assert.doesNotThrow(() => validateCompanyInputs({valuations:{eps:{scenarios:[]}}}));
  assert.doesNotThrow(() => validateConfigurationPatch({allocation:{cash_return:null}}));
});
test('JSON evidence edits advance the assessment and invalidate changed source-backed reviews', () => {
  const previous={version:3,thesis:'Retained thesis',review_status:'reviewed',reviewed_at:'2026-09-01T12:00:00Z',
    sources:[{id:'filing',content_hash:'original'}],facts:[{id:'eps',value:5,source_id:'filing',review_status:'reviewed',reviewed_by:'Reviewer',reviewed_at:'2026-09-01T12:00:00Z'}]};
  const next=structuredClone(previous);next.sources[0].content_hash='new document';
  const revised=reviseAssessmentInputs(previous,next);
  assert.equal(revised.version,4);
  assert.equal(revised.review_status,'needs_review');
  assert.equal(revised.reviewed_at,null);
  assert.equal(revised.facts[0].review_status,'draft');
  assert.equal(revised.facts[0].reviewed_at,null);
  const narrative=reviseAssessmentInputs(previous,{...previous,thesis:'Changed thesis'});
  assert.equal(narrative.facts[0].review_status,'reviewed');
  assert.equal(reviseAssessmentInputs(previous,previous).version,3);
});
test('one accessible application shell preserves collector controls and loads external scripts', async () => {
  const html = await readFile(new URL('../static/index.html', import.meta.url), 'utf8');
  for (const page of ['overview','holdings','research','scenarios','review','data','settings']) {
    assert.match(html, new RegExp(`data-page="${page}"`));
  }
  for (const id of ['sources','refresh','connect','discover','disconnect','snapshot-panel','snapshot-select','records','pairing-key','current-accounts','current-dates','analysis-caption']) {
    assert.equal([...html.matchAll(new RegExp(`id="${id}"`, 'g'))].length, 1);
  }
  assert.ok(!/<iframe|\sonclick=/.test(html));
  assert.match(html, /<title>Portfolio Review<\/title>/);
  assert.match(html, /id="draft-dialog"/);
});
