import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source = await readFile(new URL('../static/research-state.js', import.meta.url), 'utf8');
const {newDraft, editDraft, merge, numberOrNull, changeHorizon, acceptPreview, previewReady,
  RequestGate, restoreDraft, draftKey, differences, validateConfigurationPatch, validateCompanyInputs, reviseAssessmentInputs,
  selectRun, CURRENT_VIEW, applyProposal, keepValuationEdit, adoptOrphanDraft,
  decisionRecord} = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

// A disposable session store with the two methods the draft helpers use.
function storageOf(entries = {}) {
  const map = new Map(Object.entries(entries));
  return {getItem: key => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, value),
    removeItem: key => map.delete(key), size: () => map.size};
}

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
test('changing horizon realigns operating assumptions with their assessment and reopens its review', () => {
  const assessment = {security_id:'DEMO',version:2,horizon_months:12,review_status:'reviewed',reviewed_at:'2026-09-01T12:00:00Z',
    operating_assumptions:[{field:'revenue_growth',value:.05,horizon_months:12},{field:'ebit_margin',value:.2,horizon_months:6}]};
  const settled = {security_id:'SETTLED',version:7,horizon_months:18,review_status:'reviewed',reviewed_at:'2026-09-01T12:00:00Z',operating_assumptions:[]};
  const draft = newDraft('one', {}, {assessments:{DEMO:assessment,SETTLED:settled}, valuations:{}});
  const changed = changeHorizon(draft, 18);
  const revised = changed.workspace.assessments.DEMO;
  assert.equal(revised.horizon_months, 18);
  assert.deepEqual(revised.operating_assumptions.map(row => row.horizon_months), [18, 18]);
  assert.equal(revised.version, 3);
  assert.equal(revised.review_status, 'needs_review');
  assert.equal(revised.reviewed_at, null);
  // An assessment already stated over the chosen horizon keeps the review it has.
  assert.deepEqual(changed.workspace.assessments.SETTLED, settled);
  assert.equal(draft.workspace.assessments.DEMO.horizon_months, 12);
  assert.equal(draft.workspace.assessments.DEMO.operating_assumptions[0].horizon_months, 12);
});
test('keeping a proposal prefills the valuation with its origin, source and a new version', () => {
  const draft = newDraft('one', {}, {valuations:{DEMO:{origin:'manual',source:'User scenario input',version:4,horizon_months:12,
    eps:{starting_price:10,scenarios:[{label:'central',eps:1,pe:9,distributions_per_starting_share:0}]}}}});
  const proposal = {security_id:'DEMO',starting_price:120,currency:'USD',horizon_months:12,eps_convention:'forward',pe_convention:'forward',
    scenarios:[{label:'central',eps:5,pe:24,distributions_per_starting_share:1}],
    proposal_meta:{basis:'proposed',model:'eps_multiple',source:'Consensus estimates 2027-06',assumptions_visible:['eps_growth','terminal_multiple']}};
  const kept = applyProposal(draft, 'DEMO', 'eps', proposal);
  const values = kept.workspace.valuations.DEMO;
  assert.equal(values.origin, 'prefill');
  assert.equal(values.source, 'Consensus estimates 2027-06');
  assert.equal(values.version, 5);
  assert.equal(values.horizon_months, 12);
  assert.equal(values.eps.starting_price, 120);
  assert.equal(values.eps.scenarios[0].pe, 24);
  // The engine input stays an engine input; where it came from rides the record.
  assert.equal(values.eps.proposal_meta, undefined);
  assert.deepEqual(values.proposal_meta.assumptions_visible, ['eps_growth','terminal_multiple']);
  assert.equal(kept.revision, draft.revision + 1);
  assert.equal(kept.dirty, true);
  assert.equal(draft.workspace.valuations.DEMO.eps.starting_price, 10);
  proposal.scenarios[0].eps = 99;
  assert.equal(values.eps.scenarios[0].eps, 5);
  const fresh = applyProposal(draft, 'NEW', 'dcf', {discount_rate:.09,projections:[],proposal_meta:{model:'fcff_dcf'}});
  assert.equal(fresh.workspace.valuations.NEW.dcf.discount_rate, .09);
  assert.equal(fresh.workspace.valuations.NEW.version, 1);
  assert.equal(fresh.workspace.valuations.NEW.source, 'fcff_dcf');
  assert.throws(() => applyProposal(draft, 'DEMO', 'scenarios', proposal));
  assert.throws(() => applyProposal(draft, 'DEMO', 'eps', null));
  assert.throws(() => applyProposal(draft, 'OTHER', 'eps', proposal));
});
test('a saved decision names the alternative, the basket it was compared against and its run', () => {
  const draft = newDraft('run-9');
  const result = {metadata:{review_kind:'current',workflow_id:'wf-3',as_of:'2026-09-18'},
    allocation:{candidates:[{candidate:'rebalance_only'},{candidate:'add_ABC'}]}};
  const record = decisionRecord(draft, result, {action:'review_candidate',candidate_id:'add_ABC',rationale:'  Evidence is complete.  '});
  assert.equal(record.run_id, 'run-9');
  assert.equal(record.action, 'review_candidate');
  assert.equal(record.candidate_id, 'add_ABC');
  assert.equal(record.selected_alternative, 'add_ABC');
  assert.equal(record.rationale, 'Evidence is complete.');
  assert.equal(record.review_kind, 'current');
  assert.equal(record.workflow_id, 'wf-3');
  assert.deepEqual(record.compared_candidates, ['rebalance_only','add_ABC']);
  assert.equal(record.as_of, '2026-09-18');
  const unchanged = decisionRecord(draft, result, {action:'no_action',rationale:'Nothing merits a trade.'});
  assert.equal(unchanged.candidate_id, null);
  assert.equal(unchanged.selected_alternative, null);
  // A no-change decision naming an alternative is refused rather than stored with the
  // alternative silently dropped.
  assert.throws(() => decisionRecord(draft, result, {action:'no_action',candidate_id:'add_ABC',
    rationale:'Named an alternative but recorded no change.'}), /alternative/i);
  const bare = decisionRecord(draft, {}, {action:'no_action',rationale:'No analysis to compare.'});
  assert.equal(bare.workflow_id, null);
  assert.deepEqual(bare.compared_candidates, []);
  assert.equal(bare.as_of, null);
  assert.throws(() => decisionRecord(draft, result, {action:'no_action',rationale:'   '}));
  assert.throws(() => decisionRecord(draft, result, {action:'sell_everything',rationale:'Panic.'}));
  assert.throws(() => decisionRecord(draft, result, {action:'review_candidate',candidate_id:'unknown',rationale:'Chosen elsewhere.'}));
  assert.throws(() => decisionRecord(newDraft(null), result, {action:'no_action',rationale:'No saved run.'}));
  assert.throws(() => decisionRecord(editDraft(draft, {patch:{mandate:{issuer_cap:.2}}}), result, {action:'no_action',rationale:'Unsaved edits.'}));
});
test('the run selector separates current holdings from one saved analysis', () => {
  const runs = [{run_id:'r-new',as_of:'2026-09-18'},{run_id:'r-old',as_of:'2026-08-18'}];
  assert.equal(CURRENT_VIEW, 'current');
  assert.deepEqual(selectRun(runs, 'r-old'), {view:'saved', runId:'r-old'});
  assert.deepEqual(selectRun(runs, 'r-new'), {view:'saved', runId:'r-new'});
  assert.deepEqual(selectRun(runs, CURRENT_VIEW), {view:'current', runId:null});
  for (const absent of ['', null, undefined, 'deleted-run']) {
    assert.deepEqual(selectRun(runs, absent), {view:'current', runId:null});
  }
  assert.deepEqual(selectRun([], 'r-new'), {view:'current', runId:null});
  assert.deepEqual(selectRun(undefined, 'r-new'), {view:'current', runId:null});
  assert.deepEqual(selectRun(['r-new','r-old'], 'r-old'), {view:'saved', runId:'r-old'});
});
// A proposal is built over the horizon its own review stated. Keeping it on a review that
// has since moved to another horizon must not quietly value a 12-month price over 18.
test('a proposal kept on a changed horizon is restated rather than valued over its own', () => {
  const draft = newDraft('one', {}, {valuations:{DEMO:{origin:'manual',version:2,horizon_months:18,
    eps:{horizon_months:18,scenarios:[{label:'central',eps:null,pe:null,distributions_per_starting_share:null}]}}}});
  const proposal = {security_id:'DEMO',starting_price:120,currency:'USD',horizon_months:12,
    scenarios:[{label:'central',eps:5,pe:24,distributions_per_starting_share:1}],
    proposal_meta:{model:'eps_multiple',source:'Consensus estimates 2027-06'}};
  const kept = applyProposal(draft, 'DEMO', 'eps', proposal, 18);
  const values = kept.workspace.valuations.DEMO;
  assert.equal(values.horizon_months, 18, 'the record states the review horizon');
  assert.equal(values.eps.horizon_months, 18, 'the model states the review horizon');
  assert.equal(values.eps.starting_price, 120, 'horizon-free inputs are still prefilled');
  assert.deepEqual(values.eps.scenarios, [{label:'central',eps:null,pe:null,distributions_per_starting_share:null}],
    'values stated over the other horizon are cleared rather than reused');
  // A proposal already stated over the review horizon is prefilled unchanged.
  const aligned = applyProposal(draft, 'DEMO', 'eps', {...proposal, horizon_months:18}, 18);
  assert.equal(aligned.workspace.valuations.DEMO.eps.scenarios[0].eps, 5);
  // A DCF proposal states no horizon, so the record simply takes the review's.
  const dcf = applyProposal(draft, 'DEMO', 'dcf', {discount_rate:.09,proposal_meta:{model:'fcff_dcf'}}, 18);
  assert.equal(dcf.workspace.valuations.DEMO.dcf.discount_rate, .09);
  assert.equal(dcf.workspace.valuations.DEMO.horizon_months, 18);
});
test('a hand-edited model stops naming the proposal it was prefilled from', () => {
  const draft = newDraft('one', {}, {valuations:{}});
  const proposal = {security_id:'DEMO',starting_price:120,horizon_months:12,
    scenarios:[{label:'central',eps:5,pe:24,distributions_per_starting_share:1}],
    proposal_meta:{model:'eps_multiple',source:'Consensus estimates 2027-06',
      evidence:{eps_central:'estimates:DEMO'},assumptions_visible:['eps_growth']}};
  const kept = applyProposal(draft, 'DEMO', 'eps', proposal, 12);
  assert.equal(kept.workspace.valuations.DEMO.origin, 'prefill');
  const model = {...kept.workspace.valuations.DEMO.eps,
    scenarios:[{label:'central',eps:9,pe:24,distributions_per_starting_share:1}]};
  const edited = keepValuationEdit(kept, 'DEMO', 'eps', model, {origin:'manual',source:'User scenario input',version:1});
  const values = edited.workspace.valuations.DEMO;
  assert.equal(values.origin, 'manual', 'a typed value is not credited to an estimate');
  assert.equal(values.source, 'User scenario input');
  assert.equal(values.proposal_meta, undefined, 'the proposal evidence no longer rides a typed model');
  assert.equal(values.eps.scenarios[0].eps, 9);
  assert.equal(values.version, kept.workspace.valuations.DEMO.version + 1);
  assert.equal(kept.workspace.valuations.DEMO.eps.scenarios[0].eps, 5);
  // A model that was manual all along keeps saying so.
  const again = keepValuationEdit(edited, 'DEMO', 'eps', model);
  assert.equal(again.workspace.valuations.DEMO.origin, 'manual');
  // A first edit for an unseen security starts from the stated fallback record.
  const fresh = keepValuationEdit(draft, 'NEW', 'dcf', {discount_rate:.08},
    {origin:'manual',source:'User scenario input',version:1,horizon_months:12});
  assert.equal(fresh.workspace.valuations.NEW.version, 2);
  assert.equal(fresh.workspace.valuations.NEW.horizon_months, 12);
});
// The retain choice is a promise: edits made before any review existed have to reach the
// review that is finally published, not an unreachable key.
test('a draft retained before the first review moves onto the run that publishes', () => {
  const orphan = editDraft(newDraft(null, {}, {}), {patch:{mandate:{issuer_cap:.2}}});
  const storage = storageOf({[draftKey(null)]: JSON.stringify(orphan)});
  const published = newDraft('run-1', {mandate:{issuer_cap:.3}}, {});
  const adopted = adoptOrphanDraft(storage, published);
  assert.equal(adopted.baseRunId, 'run-1');
  assert.equal(adopted.patch.mandate.issuer_cap, .2, 'the retained edit reached the new run');
  assert.equal(adopted.dirty, true, 'the carried edit still has to be recalculated');
  assert.equal(storage.size(), 0, 'the unreachable key is not left behind');
  assert.equal(adoptOrphanDraft(storageOf(), published), published);
  // Nothing is carried onto no run at all, and a damaged orphan is simply ignored.
  const kept = storageOf({[draftKey(null)]: JSON.stringify(orphan)});
  assert.equal(adoptOrphanDraft(kept, newDraft(null)).baseRunId, null);
  assert.equal(kept.size(), 1);
  assert.equal(adoptOrphanDraft(storageOf({[draftKey(null)]: '{'}), published), published);
  const unusable = storageOf({[draftKey(null)]: JSON.stringify({...orphan, patch:{mandate:{locked_security_ids:'ABC'}}})});
  assert.equal(adoptOrphanDraft(unusable, published), published);
});
// A 10px provenance tag is read, not decoration: it has to clear WCAG AA where it sits.
test('the new chart and assumption styles stay legible and wrap on narrow layouts', async () => {
  const css = await readFile(new URL('../static/style.css', import.meta.url), 'utf8');
  const block = name => {
    const match = css.match(new RegExp(`\\${name}\\s*\\{([^}]*)\\}`));
    assert.ok(match, `Missing style block ${name}`);
    return match[1];
  };
  const evidence = block('.assumption-tag.evidence');
  const hex = (declaration, source) => {
    const match = source.match(new RegExp(`${declaration}\\s*:\\s*(#[0-9a-f]{6})`, 'i'));
    assert.ok(match, `${declaration} must be a stated colour, not a shared token: ${source}`);
    return match[1];
  };
  const channel = value => (value <= .03928 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4);
  const luminance = colour => {
    const parts = [1, 3, 5].map(index => channel(parseInt(colour.slice(index, index + 2), 16) / 255));
    return .2126 * parts[0] + .7152 * parts[1] + .0722 * parts[2];
  };
  const contrast = (front, back) => {
    const [high, low] = [luminance(front), luminance(back)].sort((a, b) => b - a);
    return (high + .05) / (low + .05);
  };
  assert.ok(contrast(hex('color', evidence), hex('background', evidence)) >= 4.5,
    `Evidence tags need 4.5:1 at 10px: ${evidence}`);
  assert.match(block('.chart-readout'), /flex-wrap\s*:\s*wrap/,
    'the grouped-bar readout wraps rather than overflowing its panel');
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
