#!/usr/bin/env node
'use strict';
// validate.js — structural + referential integrity check for the master contract.
// Mirrors contract.schema.json AND adds checks JSON Schema can't express
// (id/kind agreement, ref resolution, cycles).
//
// Usage:
//   node validate.js                 # report to stdout; exit 0 ok / 1 invalid
//   node validate.js --hook          # PostToolUse hook: stderr + exit 2 on invalid (blocks)
//
// Note: coverage ("every feature has a ticket") is a STAGE gate, not a continuous
// invariant — a mid-planning contract is legitimately incomplete. See gate_check.js.

const C = require('./common.js');

const NODE_KIND_BY_PREFIX = { FEAT: 'feature', EP: 'epic', TK: 'ticket', TASK: 'task' };
const NODE_STATUS = ['backlog', 'todo', 'in_progress', 'blocked', 'done', 'canceled'];
const PRIORITY = ['P1', 'P2', 'P3'];
const ESTIMATE = ['S', 'M', 'L', 'XL'];
const ENG = ['prototype', 'mvp', 'production', 'regulated'];
const GOV_TYPES = ['open_question', 'assumption', 'risk', 'issue', 'dependency', 'decision', 'deferral', 'non_goal'];
const LOWMH = ['low', 'medium', 'high'];

function validate(doc) {
  const errors = [];
  const warnings = [];
  const E = (m) => errors.push(m);
  const W = (m) => warnings.push(m);

  if (!doc || typeof doc !== 'object') { E('Contract is empty or not an object.'); return { errors, warnings }; }
  if (doc.schema_version !== '1.0') E(`schema_version must be "1.0" (got ${JSON.stringify(doc.schema_version)}).`);
  if (doc.kind !== 'contract') E(`kind must be "contract" (got ${JSON.stringify(doc.kind)}).`);

  // meta
  const m = doc.meta || {};
  if (!doc.meta) E('meta is required.');
  else {
    for (const f of ['project_id', 'title', 'status', 'created_at', 'updated_at'])
      if (!m[f]) E(`meta.${f} is required.`);
    if (m.status && !['planning', 'building', 'shipped', 'paused'].includes(m.status))
      E(`meta.status invalid: ${m.status}`);
    if (m.project_id && !/^[a-z0-9][a-z0-9-]*$/.test(m.project_id))
      E(`meta.project_id must be lowercase-kebab: ${m.project_id}`);
  }

  // vision
  const v = doc.vision || {};
  if (!doc.vision) E('vision is required.');
  else {
    if (!v.problem) E('vision.problem is required.');
    if (!v.outcome) E('vision.outcome is required.');
  }

  // nodes
  const nodes = Array.isArray(doc.nodes) ? doc.nodes : [];
  if (!Array.isArray(doc.nodes)) E('nodes must be an array.');
  const nodeIds = new Set();
  for (const n of nodes) {
    const id = n && n.id;
    if (!id) { E('A node is missing an id.'); continue; }
    if (nodeIds.has(id)) E(`Duplicate node id: ${id}`);
    nodeIds.add(id);
    const mm = /^(FEAT|EP|TK|TASK)-\d+$/.exec(id);
    if (!mm) { E(`Node id has bad format: ${id} (expected FEAT-/EP-/TK-/TASK- + number).`); continue; }
    const expectedKind = NODE_KIND_BY_PREFIX[mm[1]];
    if (n.kind !== expectedKind) E(`${id}: kind "${n.kind}" disagrees with id prefix (expected "${expectedKind}").`);
    if (!n.title) E(`${id}: title is required.`);
    if (!NODE_STATUS.includes(n.status)) E(`${id}: status invalid: ${n.status}`);
    if (n.priority && !PRIORITY.includes(n.priority)) E(`${id}: priority invalid: ${n.priority}`);
    if (n.estimate && !ESTIMATE.includes(n.estimate)) E(`${id}: estimate invalid: ${n.estimate}`);
    if (n.engineering_level && !ENG.includes(n.engineering_level)) E(`${id}: engineering_level invalid: ${n.engineering_level}`);
    // ticket required fields
    if (n.kind === 'ticket') {
      if (!Array.isArray(n.acceptance_criteria) || n.acceptance_criteria.length < 1)
        E(`${id}: a ticket requires at least one acceptance_criteria entry.`);
      if (!n.engineering_level) E(`${id}: a ticket requires engineering_level.`);
      if (!n.priority) E(`${id}: a ticket requires priority.`);
    }
    if (Array.isArray(n.acceptance_criteria))
      n.acceptance_criteria.forEach((ac, i) => { if (!ac || !ac.then) E(`${id}: acceptance_criteria[${i}] needs a "then".`); });
  }
  // ref resolution + cycles (parent, depends_on)
  const byId = Object.fromEntries(nodes.filter(n => n && n.id).map(n => [n.id, n]));
  for (const n of nodes) {
    if (!n || !n.id) continue;
    if (n.parent != null && !byId[n.parent]) E(`${n.id}: parent "${n.parent}" does not exist.`);
    for (const d of (n.depends_on || [])) if (!byId[d]) E(`${n.id}: depends_on "${d}" does not exist.`);
    for (const b of (n.blocks || [])) if (!byId[b]) E(`${n.id}: blocks "${b}" does not exist.`);
  }
  detectCycle(nodes, 'parent', (n) => (n.parent != null ? [n.parent] : []), E, 'parent');
  detectCycle(nodes, 'depends_on', (n) => (n.depends_on || []), E, 'depends_on');

  // governance
  const gov = Array.isArray(doc.governance) ? doc.governance : [];
  const govIds = new Set();
  for (const g of gov) {
    const id = g && g.id;
    if (!id) { E('A governance item is missing an id.'); continue; }
    if (govIds.has(id)) E(`Duplicate governance id: ${id}`);
    govIds.add(id);
    if (!/^(Q|ASMP|RISK|ISS|DEP|DEC|DEF|NG)-\d+$/.test(id)) E(`Governance id bad format: ${id}`);
    if (!GOV_TYPES.includes(g.type)) E(`${id}: type invalid: ${g.type}`);
    if (!g.title) E(`${id}: title is required.`);
    if (!g.status) E(`${id}: status is required.`);
    if (g.type === 'decision') for (const f of ['context', 'decision', 'consequences']) if (!g[f]) E(`${id} (decision): ${f} is required.`);
    if (g.type === 'deferral' && !g.revisit_trigger) E(`${id} (deferral): revisit_trigger is required (distinguishes "later" from "never").`);
    if (g.type === 'risk') {
      for (const f of ['probability', 'impact', 'mitigation']) if (!g[f]) E(`${id} (risk): ${f} is required.`);
      if (g.probability && !LOWMH.includes(g.probability)) E(`${id}: probability invalid: ${g.probability}`);
      if (g.impact && !LOWMH.includes(g.impact)) E(`${id}: impact invalid: ${g.impact}`);
    }
    if (g.type === 'assumption' && !g.validation_method) E(`${id} (assumption): validation_method is required.`);
    if (g.scope && g.scope !== 'project' && !byId[g.scope]) E(`${id}: scope "${g.scope}" is not a node id or 'project'.`);
  }
  for (const g of gov) {
    if (!g || !g.id) continue;
    for (const f of ['supersedes', 'superseded_by', 'converted_from'])
      if (g[f] && !govIds.has(g[f])) E(`${g.id}: ${f} "${g[f]}" does not exist in governance.`);
  }

  // changelog
  for (const c of (doc.changelog || [])) {
    if (!c || !c.date || !c.change) E('changelog entries require date and change.');
  }

  return { errors, warnings };
}

function detectCycle(nodes, _name, edgesOf, E, label) {
  const byId = Object.fromEntries(nodes.filter(n => n && n.id).map(n => [n.id, n]));
  const state = {}; // 0=unseen,1=in-stack,2=done
  function dfs(id, stack) {
    if (state[id] === 1) { E(`Cycle in ${label}: ${stack.concat(id).join(' -> ')}`); return; }
    if (state[id] === 2 || !byId[id]) return;
    state[id] = 1;
    for (const nxt of edgesOf(byId[id])) dfs(nxt, stack.concat(id));
    state[id] = 2;
  }
  for (const n of nodes) if (n && n.id && !state[n.id]) dfs(n.id, []);
}

function main() {
  const hookMode = process.argv.includes('--hook');
  let stdin = {};
  if (hookMode) {
    stdin = C.readStdinSync();
    if (!C.targetsContract(stdin)) process.exit(0); // not our file; do nothing
  }
  const root = C.projectRoot(stdin);
  const loaded = C.loadContract(root);
  if (loaded._missing) {
    if (hookMode) process.exit(0);
    console.log(`No contract at ${loaded._path}. Run \`npx planning-worx init\` or /plan-vision.`);
    process.exit(1);
  }
  if (loaded._parseError) {
    const msg = `Contract YAML failed to parse: ${loaded._parseError}`;
    if (hookMode) { process.stderr.write(msg + '\n'); process.exit(2); }
    console.log('INVALID\n' + msg);
    process.exit(1);
  }
  const { errors, warnings } = validate(loaded.doc);

  if (hookMode) {
    if (errors.length) {
      process.stderr.write(
        'planning-worx: contract.yaml is invalid after this edit. Fix these before continuing:\n' +
        errors.map(e => '  - ' + e).join('\n') + '\n');
      process.exit(2); // block; Claude receives stderr as feedback
    }
    process.exit(0);
  }

  // report mode
  if (!errors.length) {
    console.log(`VALID — contract.yaml passes (${(loaded.doc.nodes || []).length} nodes, ${(loaded.doc.governance || []).length} governance items).`);
    if (warnings.length) console.log('\nWarnings:\n' + warnings.map(w => '  - ' + w).join('\n'));
    process.exit(0);
  }
  console.log(`INVALID — ${errors.length} error(s):\n` + errors.map(e => '  - ' + e).join('\n'));
  if (warnings.length) console.log('\nWarnings:\n' + warnings.map(w => '  - ' + w).join('\n'));
  process.exit(1);
}

main();
