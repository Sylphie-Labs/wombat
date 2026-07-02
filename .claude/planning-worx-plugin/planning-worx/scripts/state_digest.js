#!/usr/bin/env node
'use strict';
// state_digest.js — compute rollups + the "where am I" digest.
// Writes planning/.state.json (derived; never hand-edited) and prints:
//   --digest   tiny text block for SessionStart hook injection (re-grounding)
//   --status   full progress table for /plan-status
// Default: --digest.

const C = require('./common.js');

function chainsToFeature(node, byId) {
  let cur = node, guard = 0;
  while (cur && guard++ < 100) {
    if (cur.kind === 'feature') return true;
    cur = cur.parent != null ? byId[cur.parent] : null;
  }
  return false;
}

function compute(doc) {
  const nodes = Array.isArray(doc.nodes) ? doc.nodes : [];
  const byId = Object.fromEntries(nodes.filter(n => n && n.id).map(n => [n.id, n]));
  const gov = Array.isArray(doc.governance) ? doc.governance : [];
  const isDone = (id) => byId[id] && byId[id].status === 'done';

  const tickets = nodes.filter(n => n.kind === 'ticket');
  const counts = {};
  for (const n of nodes) {
    counts[n.kind] = counts[n.kind] || { total: 0 };
    counts[n.kind].total++;
    counts[n.kind][n.status] = (counts[n.kind][n.status] || 0) + 1;
  }

  // next ready ticket
  const ready = tickets
    .filter(t => ['todo', 'backlog'].includes(t.status) && (t.depends_on || []).every(isDone))
    .sort((a, b) => (pri(a) - pri(b)) || idNum(a.id) - idNum(b.id));
  const next = ready[0] || null;

  // blockers
  const blockers = [];
  for (const t of tickets) {
    if (t.status === 'blocked') blockers.push(`${t.id} blocked`);
    const unmet = (t.depends_on || []).filter(d => !isDone(d));
    if (['todo', 'in_progress'].includes(t.status) && unmet.length)
      blockers.push(`${t.id} waiting on ${unmet.join(', ')}`);
  }
  const openQs = gov.filter(g => g.type === 'open_question' && g.status === 'open');

  // per-feature rollup
  const features = nodes.filter(n => n.kind === 'feature').map(f => {
    const descTickets = tickets.filter(t => rootFeature(t, byId) === f.id);
    const done = descTickets.filter(t => t.status === 'done').length;
    return { id: f.id, title: f.title, tickets: descTickets.length, done,
             pct: descTickets.length ? Math.round((done / descTickets.length) * 100) : 0 };
  });

  const ticketsDone = tickets.filter(t => t.status === 'done').length;
  return {
    project: (doc.meta || {}).title || (doc.meta || {}).project_id || 'project',
    status: (doc.meta || {}).status || 'planning',
    stage: (doc.meta || {}).stage || 'constitution',
    counts,
    overall_pct: tickets.length ? Math.round((ticketsDone / tickets.length) * 100) : 0,
    next_ready_ticket: next ? { id: next.id, title: next.title, priority: next.priority || '-' } : null,
    open_questions: openQs.map(q => q.id),
    blockers,
    features,
  };
}

function pri(t) { return ({ P1: 1, P2: 2, P3: 3 })[t.priority] || 9; }
function idNum(id) { const m = /-(\d+)$/.exec(id || ''); return m ? +m[1] : 0; }
function rootFeature(node, byId) {
  let cur = node, guard = 0;
  while (cur && guard++ < 100) { if (cur.kind === 'feature') return cur.id; cur = cur.parent != null ? byId[cur.parent] : null; }
  return null;
}

function digestText(s) {
  const lines = [];
  lines.push(`planning-worx — ${s.project} [${s.status}] stage:${s.stage} ${s.overall_pct}% tickets done`);
  if (s.next_ready_ticket) lines.push(`Next ready ticket: ${s.next_ready_ticket.id} (${s.next_ready_ticket.priority}) ${s.next_ready_ticket.title}`);
  else lines.push('Next ready ticket: none (decompose more, or all done/blocked).');
  if (s.open_questions.length) lines.push(`Open questions: ${s.open_questions.join(', ')}`);
  if (s.blockers.length) lines.push(`Blockers: ${s.blockers.slice(0, 5).join('; ')}${s.blockers.length > 5 ? ' …' : ''}`);
  lines.push('Source of truth: planning/contract.yaml — re-read before acting; it wins over chat.');
  return lines.join('\n');
}

function statusText(s) {
  const out = [];
  out.push(`# ${s.project}  [${s.status}]  stage: ${s.stage}`);
  out.push(`Overall: ${s.overall_pct}% of tickets done\n`);
  if (s.features.length) {
    out.push('Features:');
    for (const f of s.features) out.push(`  ${f.id}  ${bar(f.pct)} ${f.pct}%  (${f.done}/${f.tickets})  ${f.title}`);
    out.push('');
  }
  out.push('Counts: ' + Object.entries(s.counts).map(([k, v]) => `${k}=${v.total}`).join('  '));
  out.push(s.next_ready_ticket
    ? `\nNext ready ticket → ${s.next_ready_ticket.id} (${s.next_ready_ticket.priority}): ${s.next_ready_ticket.title}`
    : '\nNext ready ticket → none.');
  if (s.open_questions.length) out.push(`Open questions: ${s.open_questions.join(', ')}`);
  if (s.blockers.length) out.push('Blockers:\n' + s.blockers.map(b => '  - ' + b).join('\n'));
  return out.join('\n');
}

function bar(pct) { const f = Math.round(pct / 10); return '[' + '#'.repeat(f) + '.'.repeat(10 - f) + ']'; }

function main() {
  const mode = process.argv.includes('--status') ? 'status' : 'digest';
  // Runs from the project root (CLI command or SessionStart hook); use the
  // project-dir env or cwd. Does not read stdin (avoids blocking).
  const root = C.projectRoot({});
  const loaded = C.loadContract(root);
  if (loaded._missing || loaded._parseError) {
    // Stay quiet for hook injection; be explicit for the status command.
    if (mode === 'status') console.log('No readable contract yet. Run `npx planning-worx init` then /plan-vision.');
    process.exit(0);
  }
  const s = compute(loaded.doc);
  try { C.fs.writeFileSync(C.path.join(root, 'planning', '.state.json'), JSON.stringify(s, null, 2)); } catch (e) {}
  console.log(mode === 'status' ? statusText(s) : digestText(s));
  process.exit(0);
}

main();
