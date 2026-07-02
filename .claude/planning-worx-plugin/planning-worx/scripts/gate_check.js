#!/usr/bin/env node
'use strict';
// gate_check.js <stage> — assert a pipeline stage's exit criteria.
// Invoked by the /plan-* commands at stage boundaries (NOT a blocking hook, to
// avoid Stop-hook loops). Exit 0 = gate passes; exit 1 = gate fails (reasons printed).
//
// Stages: vision | clarify | design | tickets | analyze

const C = require('./common.js');

function rootFeature(node, byId) {
  let cur = node, guard = 0;
  while (cur && guard++ < 100) { if (cur.kind === 'feature') return cur.id; cur = cur.parent != null ? byId[cur.parent] : null; }
  return null;
}

function gate(stage, doc) {
  const fail = [];
  const nodes = Array.isArray(doc.nodes) ? doc.nodes : [];
  const byId = Object.fromEntries(nodes.filter(n => n && n.id).map(n => [n.id, n]));
  const gov = Array.isArray(doc.governance) ? doc.governance : [];
  const features = nodes.filter(n => n.kind === 'feature');
  const epics = nodes.filter(n => n.kind === 'epic');
  const tickets = nodes.filter(n => n.kind === 'ticket');
  const meta = doc.meta || {};
  const vision = doc.vision || {};
  const placeholder = (s) => !s || /^\s*todo\b/i.test(s);

  switch (stage) {
    case 'vision':
      if (placeholder(vision.problem)) fail.push('vision.problem is still a placeholder.');
      if (placeholder(vision.outcome)) fail.push('vision.outcome is still a placeholder.');
      if (!meta.project_id || meta.project_id === 'my-project') fail.push('meta.project_id has not been set.');
      if (features.length < 1) fail.push('No feature nodes yet — ingest the feature list.');
      break;

    case 'clarify': {
      const openQs = gov.filter(g => g.type === 'open_question' && g.status === 'open');
      if (openQs.length) fail.push(`Unresolved open questions: ${openQs.map(q => q.id).join(', ')}. Resolve each into a decision, deferral, or non_goal.`);
      break;
    }

    case 'design':
      if (!Array.isArray(doc.tech_stack) || doc.tech_stack.length < 1) fail.push('tech_stack is empty — record the chosen stack (with decision refs).');
      if (gov.filter(g => g.type === 'decision').length < 1) fail.push('No decisions (ADRs) recorded — capture at least the key architectural decisions.');
      for (const f of features) {
        const hasEpic = epics.some(e => e.parent === f.id);
        if (!hasEpic) fail.push(`Feature ${f.id} has no epics.`);
      }
      break;

    case 'tickets': // coverage gate — the block on invented scope
      for (const f of features) {
        const hasTicket = tickets.some(t => rootFeature(t, byId) === f.id);
        if (!hasTicket) fail.push(`Feature ${f.id} has no tickets (coverage gap).`);
      }
      for (const t of tickets) {
        if (rootFeature(t, byId) === null) fail.push(`Ticket ${t.id} does not trace up to a feature (orphan / invented scope).`);
        if (!Array.isArray(t.acceptance_criteria) || t.acceptance_criteria.length < 1) fail.push(`Ticket ${t.id} has no acceptance criteria.`);
      }
      for (const e of epics) {
        const hasTicket = tickets.some(t => t.parent === e.id);
        if (!hasTicket) fail.push(`Epic ${e.id} has no tickets.`);
      }
      break;

    case 'analyze': {
      // re-run the coverage + clarity gates, plus no open issues
      const sub = gate('tickets', doc).concat(gate('clarify', doc));
      for (const s of sub) fail.push(s);
      const openIssues = gov.filter(g => g.type === 'issue' && g.status === 'open');
      if (openIssues.length) fail.push(`Open issues: ${openIssues.map(i => i.id).join(', ')}.`);
      break;
    }

    default:
      fail.push(`Unknown stage "${stage}". Use: vision | clarify | design | tickets | analyze.`);
  }
  return fail;
}

function main() {
  const stage = process.argv[2];
  if (!stage) { console.log('Usage: gate_check.js <vision|clarify|design|tickets|analyze>'); process.exit(1); }
  const root = C.projectRoot({});
  const loaded = C.loadContract(root);
  if (loaded._missing) { console.log('No contract found.'); process.exit(1); }
  if (loaded._parseError) { console.log('Contract YAML does not parse: ' + loaded._parseError); process.exit(1); }
  const fails = gate(stage, loaded.doc);
  if (!fails.length) { console.log(`GATE PASS: ${stage}`); process.exit(0); }
  console.log(`GATE FAIL: ${stage}\n` + fails.map(f => '  - ' + f).join('\n'));
  process.exit(1);
}

main();
