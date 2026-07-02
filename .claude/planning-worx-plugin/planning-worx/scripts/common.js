'use strict';
// Shared helpers for planning-worx enforcement scripts.
// Node only; YAML via vendored js-yaml (no user-side dependencies).

const fs = require('fs');
const path = require('path');
const yaml = require(path.join(__dirname, 'vendor', 'js-yaml.js'));

// Resolve the user's project root. Hooks set CLAUDE_PROJECT_DIR; otherwise we
// read the hook's stdin `cwd`, otherwise fall back to process.cwd().
function projectRoot(stdin) {
  if (process.env.CLAUDE_PROJECT_DIR) return process.env.CLAUDE_PROJECT_DIR;
  if (stdin && stdin.cwd) return stdin.cwd;
  return process.cwd();
}

function contractPath(root) {
  return path.join(root, 'planning', 'contract.yaml');
}

function readStdinSync() {
  // Only read when stdin is piped (hook context). Avoid blocking on a TTY.
  try {
    if (process.stdin.isTTY) return {};
    const data = fs.readFileSync(0, 'utf8');
    if (!data || !data.trim()) return {};
    return JSON.parse(data);
  } catch (e) {
    return {};
  }
}

function loadContract(root) {
  const p = contractPath(root);
  if (!fs.existsSync(p)) return { _missing: true, _path: p };
  const raw = fs.readFileSync(p, 'utf8');
  try {
    const doc = yaml.load(raw);
    return { doc, raw, _path: p };
  } catch (e) {
    return { _parseError: e.message, raw, _path: p };
  }
}

function parseYamlString(s) {
  try {
    return { doc: yaml.load(s) };
  } catch (e) {
    return { _parseError: e.message };
  }
}

// Is the tool targeting the contract file?
function targetsContract(stdin) {
  const ti = stdin && stdin.tool_input ? stdin.tool_input : {};
  const fp = ti.file_path || ti.path || '';
  return typeof fp === 'string' && fp.replace(/\\/g, '/').endsWith('planning/contract.yaml');
}

module.exports = {
  yaml, fs, path,
  projectRoot, contractPath, readStdinSync, loadContract, parseYamlString, targetsContract,
};
