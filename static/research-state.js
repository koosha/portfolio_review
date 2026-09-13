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
  return editDraft(draft, {workspace, patch: merge(draft.patch, {allocation: {
    horizon_months: months, return_overrides: {}, prior_returns: {}, probability_overrides: {}, cash_return: null,
  }}), rawEditors: {}});
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
