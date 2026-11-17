// The same local denials used by Claude/Cursor, applied before Pi executes tools.
import fs from 'node:fs';
import path from 'node:path';

const glob = (pattern, value) => new RegExp('^' + pattern.split('*').map(p => p.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('.*') + '$', 'i').test(value);
function resolved(file, seen = new Set()) {
  let candidate = path.resolve(file), tail = [];
  while (!fs.existsSync(candidate)) {
    try {
      if (fs.lstatSync(candidate).isSymbolicLink()) {
        if (seen.has(candidate)) throw new Error('Symlink cycle');
        seen.add(candidate);
        return resolved(path.resolve(path.dirname(candidate), fs.readlinkSync(candidate), ...tail), seen);
      }
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    tail.unshift(path.basename(candidate));
    const parent = path.dirname(candidate);
    if (parent === candidate) break;
    candidate = parent;
  }
  return path.join(fs.realpathSync(candidate), ...tail);
}
export function denied(event, policy, cwd = process.cwd()) {
  const tool = event.toolName, input = event.input || {};
  if (!policy || !Array.isArray(policy.deny)) return 'Roosta permission policy is missing';
  if (policy.unrestricted) return '';
  if (tool.startsWith('mcp__')) {
    const name = tool.split('__').slice(2).join('__');
    for (const rule of policy.deny) {
      const keyword = /^Mcp\(\*:\*(.+)\*\)$/.exec(rule)?.[1];
      if (keyword && name.toLowerCase().includes(keyword.toLowerCase())) return 'External changes require approval';
    }
    return '';
  }
  if (tool === 'tool_search' || ['list_mcp_resources', 'list_mcp_resource_templates', 'read_mcp_resource'].includes(tool)) return '';
  if (!['read', 'edit', 'write', 'bash'].includes(tool)) return 'Tool is outside the Roosta policy';
  if (['read', 'edit', 'write'].includes(tool)) {
    const file = resolved(path.resolve(cwd, input.path || '.'));
    if ((policy.protected || []).some(root => file === root || file.startsWith(root + path.sep))) return 'AI credentials and runtime state are private';
    const kind = tool === 'read' ? 'Read' : 'Write';
    for (const rule of policy.deny) {
      const match = /^(Read|Write)\((.*)\)$/.exec(rule);
      if (match?.[1] === kind && [input.path, path.relative(cwd, file), file, path.basename(file)].some(p => glob(match[2], p || ''))) return 'File is protected by the Roosta policy';
    }
  }
  if (tool === 'bash') {
    const command = String(input.command || '');
    if ((policy.protected || []).some(root => command.includes(root)) || /(?:\.env\b|\.pem\b|\.key\b|auth\.json|credentials\.json)/i.test(command)) return 'Credential files are private';
    // Shell policies are guardrails, as in the other runtimes, not an OS sandbox.
    for (const rule of policy.deny) {
      const match = /^Shell\(([^:)]+)(?::(.*))?\)$/.exec(rule);
      if (!match) continue;
      const prefix = (match[1] + (match[2] ? ' ' + match[2].replace(/\*$/, '') : '')).trim();
      const words = prefix.split(/\s+/).map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('\\s+');
      if (new RegExp('(?:^|[\\s;/|&()])' + words + '(?:\\s|$|[;/|&()])', 'i').test(command.replace(/["']/g, ''))) return 'Command requires approval';
    }
    if (/\bgit\b.*\bpush\b/i.test(command) && !(policy.allow || []).includes('Shell(git:push origin main)')) return 'Git push requires approval';
    if (/\bgit\b.*\bpush\b/i.test(command) && !/^git push origin main\s*$/.test(command)) return 'Only git push origin main has standing approval';
  }
  return '';
}
export default function (pi) {
  const policy = JSON.parse(process.env.ROOSTA_PI_POLICY || 'null');
  if (!policy || !Array.isArray(policy.deny)) throw new Error('Missing Roosta policy');
  pi.on('session_start', (_event, ctx) => { ctx.ui.notify('roosta-policy-ready', 'info'); });
  pi.on('tool_call', event => {
    try {
      const reason = denied(event, policy);
      if (reason) return { block: true, reason };
    } catch {
      return { block: true, reason: 'Roosta could not validate this tool call' };
    }
  });
}
