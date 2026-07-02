#!/usr/bin/env node
'use strict';
// append_guard.js --hook — PreToolUse guard: keep `decisions` and `changelog`
// append-only. Blocks (exit 2) any edit that REMOVES or ALTERS an existing
// accepted decision or a past changelog entry. Allowed: appending new items, and
// superseding a decision (status accepted -> superseded, adding superseded_by).
//
// Reads the hook JSON on stdin. No-ops on anything that isn't the contract file.

const C = require('./common.js');

function acceptedDecisions(doc) {
  return (doc.governance || []).filter(g => g && g.type === 'decision' && g.status === 'accepted');
}
function byId(arr) { return Object.fromEntries((arr || []).filter(x => x && x.id).map(x => [x.id, x])); }

function checkWrite(currentDoc, proposedDoc) {
  const reasons = [];
  // changelog must be an append-only prefix
  const oldLog = currentDoc.changelog || [];
  const newLog = proposedDoc.changelog || [];
  for (let i = 0; i < oldLog.length; i++) {
    if (JSON.stringify(oldLog[i]) !== JSON.stringify(newLog[i])) {
      reasons.push(`changelog entry #${i + 1} was changed or removed. changelog is append-only — only add new entries at the end.`);
      break;
    }
  }
  // accepted decisions are immutable except status->superseded (+superseded_by)
  const newById = byId(proposedDoc.governance);
  for (const d of acceptedDecisions(currentDoc)) {
    const nd = newById[d.id];
    if (!nd) { reasons.push(`decision ${d.id} was deleted. Decisions are append-only — supersede, never remove.`); continue; }
    for (const f of ['context', 'decision', 'consequences', 'title', 'type']) {
      if (JSON.stringify(d[f]) !== JSON.stringify(nd[f]))
        reasons.push(`decision ${d.id}: "${f}" was edited. Accepted decisions are immutable — add a new decision that supersedes it.`);
    }
    if (nd.status !== 'accepted' && nd.status !== 'superseded')
      reasons.push(`decision ${d.id}: status may only move accepted -> superseded (got "${nd.status}").`);
  }
  return reasons;
}

function checkEdit(currentDoc, rawCurrent, toolInput) {
  // Conservative heuristic for partial edits.
  const reasons = [];
  const oldStr = (toolInput.old_string || '').trim();
  if (!oldStr || oldStr.length < 8) return reasons;

  // changelog region = from a line starting with "changelog:" to EOF
  const lines = rawCurrent.split(/\r?\n/);
  const idx = lines.findIndex(l => /^changelog\s*:/.test(l));
  const changelogRegion = idx >= 0 ? lines.slice(idx).join('\n') : '';
  if (changelogRegion && changelogRegion.includes(oldStr) && oldStr !== (toolInput.new_string || '').trim())
    reasons.push('This edit changes text inside the append-only `changelog`. Append a new entry instead of editing existing ones.');

  // accepted decision id tokens
  for (const d of acceptedDecisions(currentDoc)) {
    if (oldStr.includes(d.id)) {
      reasons.push(`This edit touches accepted decision ${d.id}. Decisions are append-only — add a superseding decision instead of editing it.`);
      break;
    }
  }
  return reasons;
}

function main() {
  const stdin = C.readStdinSync();
  if (!C.targetsContract(stdin)) process.exit(0);
  const root = C.projectRoot(stdin);
  const loaded = C.loadContract(root);
  if (loaded._missing || loaded._parseError) process.exit(0); // nothing to protect / can't compare

  const ti = stdin.tool_input || {};
  const tool = stdin.tool_name || '';
  let reasons = [];

  if (tool === 'Write' && typeof ti.content === 'string') {
    const parsed = C.parseYamlString(ti.content);
    if (parsed._parseError) process.exit(0); // validate hook will catch parse errors post-write
    reasons = checkWrite(loaded.doc, parsed.doc || {});
  } else if (tool === 'Edit') {
    reasons = checkEdit(loaded.doc, loaded.raw, ti);
  } else {
    process.exit(0);
  }

  if (reasons.length) {
    process.stderr.write('planning-worx: blocked — append-only history must be preserved.\n' +
      reasons.map(r => '  - ' + r).join('\n') + '\n');
    process.exit(2);
  }
  process.exit(0);
}

main();
