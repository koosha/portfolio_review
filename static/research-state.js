// Pure workbench state. All financial calculations belong to the Python service.
export const clone = value => JSON.parse(JSON.stringify(value));
const replaceMaps = new Set(['probability_overrides', 'return_overrides', 'prior_returns',
  'stress_sector_shocks', 'account_permissions', 'dealing_rules', 'sleeve_membership', 'new_flows']);
export function merge(base, patch) {
  const result = clone(base || {});
  for (const [key, value] of Object.entries(patch || {})) {
    if (['__proto__', 'constructor', 'prototype'].includes(key)) throw new Error('Unsupported object key.');
    result[key] = value && typeof value === 'object' && !Array.isArray(value) && !replaceMaps.has(key)
      ? merge(result[key] || {}, value) : clone(value);
  }
  return result;
}
export function numberOrNull(value) {
  if (value === null || value === undefined || typeof value === 'string' && !value.trim()) return null;
  if (!['number', 'string'].includes(typeof value)) throw new Error('Enter a finite number.');
  const number = Number(value);
  if (!Number.isFinite(number)) throw new Error('Enter a finite number.');
  return number;
}
function object(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${label} must be an object.`);
}
function records(value, label) {
  if (!Array.isArray(value)) throw new Error(`${label} must be an array.`);
  for (const row of value) object(row, `${label} entry`);
}
// Guard rendering structure here. Financial ranges and relationships are validated by Python.
export function validateConfigurationPatch(value) {
  object(value, 'Configuration');
  merge({}, value); // Reject prototype keys throughout the supplied document.
  const maps = ['account_permissions','dealing_rules','family_weights','stress_sector_shocks',
    'probability_overrides','return_overrides','prior_returns','sleeve_membership','new_flows'];
  for (const [group, fields] of Object.entries(value)) {
    if (!['mandate','signals','risk','allocation','tax'].includes(group)) throw new Error(`Unsupported editable group: ${group}.`);
    object(fields, group);
    for (const name of maps) if (name in fields) object(fields[name], name);
  }
  for (const [key, list] of Object.entries(value.mandate?.account_permissions || {})) {
    if (!Array.isArray(list) || list.some(item => typeof item !== 'string')) throw new Error(`Account ${key} permissions must be a list of security IDs.`);
  }
  if (value.mandate?.locked_security_ids !== undefined && !Array.isArray(value.mandate.locked_security_ids)) throw new Error('Locked security IDs must be a list.');
  for (const name of ['return_overrides','sleeve_membership','new_flows']) {
    for (const row of Object.values(value.allocation?.[name] || {})) object(row, name);
  }
  for (const account of Object.values(value.mandate?.dealing_rules || {})) {
    object(account, 'Account dealing rules');
    for (const rule of Object.values(account)) object(rule, 'Security dealing rule');
  }
  return value;
}
export function validateCompanyInputs(value) {
  object(value, 'Company workspace');merge({}, value);
  if ('assessment' in value) {
    object(value.assessment, 'Assessment');
    for (const key of ['sources','facts','operating_assumptions']) if (key in value.assessment) records(value.assessment[key], key);
  }
  if ('valuations' in value) {
    object(value.valuations, 'Valuations');
    for (const kind of ['eps','dcf']) if (kind in value.valuations) {
      object(value.valuations[kind], kind);
      records(value.valuations[kind][kind === 'eps' ? 'scenarios' : 'projections'], `${kind} ${kind === 'eps' ? 'scenarios' : 'projections'}`);
    }
  }
  return value;
}
export function reviseAssessmentInputs(previous, incoming) {
  const next = clone(incoming);
  if (previous && differences(previous, next).length === 0) return clone(previous);
  const oldVersion = Number.isInteger(previous?.version) ? previous.version : 0;
  const importedVersion = Number.isInteger(next.version) ? next.version : 0;
  next.version = Math.max(oldVersion, importedVersion) + 1;
  next.review_status = 'needs_review';next.reviewed_at = null;
  const content = fact => Object.fromEntries(Object.entries(fact || {}).filter(([key]) => !['review_status','reviewed_at','reviewed_by'].includes(key)));
  next.facts = (next.facts || []).map(fact => {
    const old = previous?.facts?.find(item => item.id === fact.id);
    const oldSource = previous?.sources?.find(item => item.id === old?.source_id);
    const newSource = next.sources?.find(item => item.id === fact.source_id);
    const changed = !old || differences(content(old), content(fact)).length || differences(oldSource || {}, newSource || {}).length;
    // JSON cannot assert a new review stamp. Unchanged facts retain their existing review.
    return {...fact,review_status:changed?'draft':old.review_status,reviewed_at:changed?null:old.reviewed_at,
      reviewed_by:changed?fact.reviewed_by || '':old.reviewed_by};
  });
  return next;
}
export function newDraft(baseRunId, config = {}, workspace = {}) {
  return {baseRunId, baseConfig: clone(config), patch: {}, workspace: merge({assessments: {}, valuations: {}}, workspace),
    revision: 0, calculatedRevision: null, previewJobId: null, dirty: false, rawEditors: {}};
}
export function editDraft(draft, changes = {}) {
  return {...draft, ...changes, revision: draft.revision + 1, dirty: true, previewJobId: null};
}
export function changeHorizon(draft, months) {
  if (![6, 12, 18].includes(months)) throw new Error('Choose 6, 12, or 18 months.');
  const workspace = clone(draft.workspace);
  for (const value of Object.values(workspace.valuations || {})) {
    if (value.eps && value.eps.horizon_months !== months) {
      value.eps.horizon_months = months;
      value.eps.scenarios = (value.eps.scenarios || []).map(row => ({...row, eps: null, pe: null, distributions_per_starting_share: null}));
      value.horizon_months = months;
    }
  }
  // An assessment states its operating assumptions over one horizon; a new horizon
  // restates them and reopens the review rather than leaving the two to diverge.
  for (const assessment of Object.values(workspace.assessments || {})) {
    const assumptions = assessment.operating_assumptions || [];
    if (assessment.horizon_months === months && assumptions.every(row => row.horizon_months === months)) continue;
    assessment.horizon_months = months;
    for (const row of assumptions) row.horizon_months = months;
    assessment.version = (Number.isInteger(assessment.version) ? assessment.version : 0) + 1;
    assessment.review_status = 'needs_review';assessment.reviewed_at = null;
  }
  return editDraft(draft, {workspace, patch: merge(draft.patch, {allocation: {
    horizon_months: months, return_overrides: {}, prior_returns: {}, probability_overrides: {}, cash_return: null,
  }}), rawEditors: {}});
}
// A proposal is prefilled, never adopted: the copy says where it came from and waits for
// review. A proposal built over a different horizon than the review states is restated to
// the review's horizon and its horizon-dependent outcomes are cleared, so a twelve-month
// horizon price is never read as an eighteen-month one.
export function applyProposal(draft, securityId, kind, proposal, reviewHorizonMonths = null) {
  if (!['eps', 'dcf'].includes(kind)) throw new Error('A proposal prefills an EPS or DCF valuation.');
  if (!securityId || typeof securityId !== 'string') throw new Error('Choose a security for this proposal.');
  object(proposal, 'Proposal');
  if (proposal.security_id && proposal.security_id !== securityId) throw new Error('This proposal belongs to another security.');
  const {proposal_meta: meta, ...inputs} = clone(proposal);
  const review = Number.isInteger(reviewHorizonMonths) ? reviewHorizonMonths : null;
  if (review !== null && inputs.horizon_months !== undefined && inputs.horizon_months !== review) {
    inputs.horizon_months = review;
    if (kind === 'eps') inputs.scenarios = (inputs.scenarios || [])
      .map(row => ({...row, eps: null, pe: null, distributions_per_starting_share: null}));
  }
  const workspace = clone(draft.workspace);
  workspace.valuations = workspace.valuations || {};
  const record = workspace.valuations[securityId] || {};
  const horizonMonths = review ?? inputs.horizon_months ?? record.horizon_months;
  workspace.valuations[securityId] = {...record, [kind]: inputs, origin: 'prefill',
    source: meta?.source || meta?.model || 'Proposed valuation', proposal_meta: meta ?? null,
    version: (Number.isInteger(record.version) ? record.version : 0) + 1,
    ...(horizonMonths === undefined ? {} : {horizon_months: horizonMonths})};
  return editDraft(draft, {workspace});
}
// A hand-edited model is no longer the proposal it was prefilled from: the record says
// manual and drops the proposal's evidence rather than crediting typed values to an
// estimate that never asserted them.
export function keepValuationEdit(draft, securityId, kind, model, fallback = {}) {
  if (!securityId || typeof securityId !== 'string') throw new Error('Choose a security for this valuation.');
  const workspace = clone(draft.workspace);
  workspace.valuations = workspace.valuations || {};
  const values = {...(workspace.valuations[securityId] || clone(fallback)), [kind]: clone(model)};
  values.version = (Number.isInteger(values.version) ? values.version : 0) + 1;
  if (values.origin === 'prefill') {
    values.origin = 'manual';
    values.source = 'User scenario input';
    delete values.proposal_meta;
  }
  workspace.valuations[securityId] = values;
  return editDraft(draft, {workspace});
}
const DECISION_ACTIONS = ['no_action', 'review_candidate', 'override'];
// What was decided this month, against which alternatives, on which retained run.
export function decisionRecord(draft, result, {action = 'no_action', candidate_id = null, rationale = ''} = {}) {
  if (!draft?.baseRunId) throw new Error('Choose a saved run before recording a decision.');
  if (draft.dirty) throw new Error('Save or reset the current draft so this decision refers to a retained run.');
  if (!DECISION_ACTIONS.includes(action)) throw new Error('Choose a recorded review action.');
  const text = typeof rationale === 'string' ? rationale.trim() : '';
  if (!text) throw new Error('Record a decision or no-action rationale.');
  const compared = (result?.allocation?.candidates || []).map(row => row?.candidate).filter(id => typeof id === 'string' && id);
  const candidate = candidate_id || null;
  if (candidate && !compared.includes(candidate)) throw new Error('Choose an alternative compared in this saved run.');
  // The recorded action and the recorded alternative are one statement. A no-change
  // decision that names an alternative is refused rather than stored with the
  // alternative silently dropped.
  if (candidate && action === 'no_action') throw new Error('Choose “Review selected candidate” or clear the alternative.');
  const metadata = result?.metadata || {};
  return {run_id: draft.baseRunId, action, candidate_id: candidate,
    selected_alternative: candidate, rationale: text,
    review_kind: metadata.review_kind ?? null, workflow_id: metadata.workflow_id ?? null, compared_candidates: compared,
    as_of: metadata.as_of ?? result?.as_of ?? result?.timeline?.decision_date ?? null};
}
// A draft edited before any review existed is keyed to no run at all. When the first
// review publishes, its retained edits move onto the run that was produced rather than
// staying under a key nothing can reach again.
export function adoptOrphanDraft(storage, draft) {
  if (!draft?.baseRunId) return draft;
  let saved = null;
  try { saved = JSON.parse(storage.getItem(draftKey(null)) || 'null'); } catch { return draft; }
  if (!saved || saved.baseRunId !== null || typeof saved !== 'object') return draft;
  try { storage.removeItem(draftKey(null)); } catch { /* the orphan is replaced regardless. */ }
  const patch = saved.patch && typeof saved.patch === 'object' ? saved.patch : {};
  const workspace = saved.workspace && typeof saved.workspace === 'object' ? saved.workspace : {};
  try { validateConfigurationPatch(patch); } catch { return draft; }
  const merged = {patch: merge(draft.patch, patch), workspace: merge(draft.workspace, workspace)};
  if (!differences({patch: draft.patch, workspace: draft.workspace}, merged).length) return draft;
  return editDraft(draft, merged);
}
export const CURRENT_VIEW = 'current';
// Current holdings and a saved analysis are dated separately; one selector, one of the two.
export function selectRun(runs, choice) {
  const ids = (Array.isArray(runs) ? runs : []).map(run => typeof run === 'string' ? run : run?.run_id);
  const saved = typeof choice === 'string' && choice && choice !== CURRENT_VIEW && ids.includes(choice) ? choice : null;
  return {view: saved ? 'saved' : CURRENT_VIEW, runId: saved};
}
export function previewReady(draft) {
  return Boolean(draft.previewJobId && draft.calculatedRevision === draft.revision);
}
export function acceptPreview(draft, revision, jobId) {
  return revision === draft.revision ? {...draft, calculatedRevision: revision, previewJobId: jobId} : draft;
}
export function draftKey(runId) { return `portfolio-review:draft:${runId || 'new'}`; }
export function restoreDraft(storage, runId, config, workspace) {
  try {
    const saved = JSON.parse(storage.getItem(draftKey(runId)) || 'null');
    if (saved && saved.baseRunId === runId && saved.workspace && saved.patch && Number.isInteger(saved.revision)) {
      validateConfigurationPatch(saved.patch);
      for (const id of new Set([...Object.keys(saved.workspace.assessments || {}), ...Object.keys(saved.workspace.valuations || {})])) {
        validateCompanyInputs({...(saved.workspace.assessments?.[id] ? {assessment:saved.workspace.assessments[id]} : {}),
          ...(saved.workspace.valuations?.[id] ? {valuations:saved.workspace.valuations[id]} : {})});
      }
      // A preview job is not accepted as current after a reload; recalculate retained edits.
      return {...saved, previewJobId: null, calculatedRevision: null};
    }
  } catch { /* A damaged local draft cannot replace a valid saved run. */ }
  return newDraft(runId, config, workspace);
}
export class RequestGate {
  constructor() { this.generation = 0; }
  next() { return ++this.generation; }
  current(ticket) { return ticket === this.generation; }
}
export function differences(before, after, prefix = '') {
  const rows = [];
  for (const key of [...new Set([...Object.keys(before || {}), ...Object.keys(after || {})])].sort()) {
    const left = before?.[key], right = after?.[key], path = prefix ? `${prefix}.${key}` : key;
    if (JSON.stringify(left) === JSON.stringify(right)) continue;
    if (left && right && typeof left === 'object' && typeof right === 'object' && !Array.isArray(left) && !Array.isArray(right)) {
      rows.push(...differences(left, right, path));
    } else rows.push({field: path, before: left ?? null, after: right ?? null});
  }
  return rows;
}
