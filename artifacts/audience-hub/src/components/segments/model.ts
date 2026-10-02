// Pure helpers for the partner audience builder. No React, no network — unit tested.
import type { SegmentDefinition, SegmentField, SegmentGroup, SegmentNode, SegmentRule } from '@/lib/segments';

export type Path = number[];
export type Issue = { path: string; message: string };

export const MAX_DEPTH = 5;
export const MAX_RULES = 50;
export const GIFT_WHERE: Record<string, 'text' | 'number' | 'boolean'> = {
  fund: 'text', campaign: 'text', appeal_code: 'text', channel: 'text', amount: 'number', is_recurring: 'boolean',
};
export const VALUELESS = new Set(['is_null', 'is_not_null', 'is_true', 'is_false']);
export const OP_LABEL: Record<string, string> = {
  eq: 'is', neq: 'is not', in: 'is any of', not_in: 'is none of', contains: 'contains', starts_with: 'starts with',
  is_null: 'is empty', is_not_null: 'is present', gt: '>', gte: '≥', lt: '<', lte: '≤', between: 'between',
  before: 'before', after: 'after', in_last_days: 'in last N days', not_in_last_days: 'not in last N days',
  is_true: 'is true', is_false: 'is false',
};

export const blankDefinition = (): SegmentDefinition => ({ version: 1, root: { op: 'and', rules: [] } });
export const isGroup = (node: SegmentNode | undefined | null): node is SegmentGroup =>
  !!node && typeof node === 'object' && 'op' in node && Array.isArray((node as SegmentGroup).rules);
export const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v)) as T;

export function pathString(path: Path) { return 'root' + path.map(i => `.rules[${i}]`).join(''); }
export function parsePath(value: string): Path | null {
  const m = /^root((?:\.rules\[\d+\])*)/.exec(value.trim());
  if (!m) return null;
  return [...m[1].matchAll(/\[(\d+)\]/g)].map(x => Number(x[1]));
}
/** "root.rules[2].value: Expected…" -> { path, message } (backend message format). */
export function parseIssue(message: string): Issue | null {
  const m = /^(root(?:\.rules\[\d+\])*)((?:\.[A-Za-z_][\w.]*|\[\d+\])*)\s*:\s*(.+)$/s.exec(message.split('; ')[0]);
  return m ? { path: m[1] + m[2], message: m[3] } : null;
}

export function getNode(root: SegmentGroup, path: Path): SegmentNode | undefined {
  let node: SegmentNode | undefined = root;
  for (const i of path) node = isGroup(node) ? node.rules[i] : undefined;
  return node;
}
function parentOf(root: SegmentGroup, path: Path) { return getNode(root, path.slice(0, -1)) as SegmentGroup; }

export function updateNode(def: SegmentDefinition, path: Path, fn: (n: SegmentNode) => SegmentNode): SegmentDefinition {
  const next = clone(def);
  if (!path.length) { next.root = fn(next.root) as SegmentGroup; return next; }
  const parent = parentOf(next.root, path);
  parent.rules[path[path.length - 1]] = fn(parent.rules[path[path.length - 1]]);
  return next;
}
export function insertNode(def: SegmentDefinition, groupPath: Path, node: SegmentNode, index?: number) {
  const next = clone(def);
  const g = getNode(next.root, groupPath);
  if (!isGroup(g)) return def;
  g.rules.splice(index ?? g.rules.length, 0, node);
  return next;
}
export function removeNode(def: SegmentDefinition, path: Path) {
  if (!path.length) return def;
  const next = clone(def);
  parentOf(next.root, path).rules.splice(path[path.length - 1], 1);
  return next;
}
export function duplicateNode(def: SegmentDefinition, path: Path) {
  const node = getNode(def.root, path);
  if (!node || !path.length) return def;
  return insertNode(def, path.slice(0, -1), clone(node), path[path.length - 1] + 1);
}
const isPrefix = (a: Path, b: Path) => a.length <= b.length && a.every((v, i) => b[i] === v);
/** Move node at `from` so it lands at index `toIndex` inside group `toGroup` (indices of the pre-move tree). */
export function moveNode(def: SegmentDefinition, from: Path, toGroup: Path, toIndex: number) {
  if (!from.length || isPrefix(from, toGroup)) return def;
  const node = getNode(def.root, from);
  if (!node) return def;
  const target = [...toGroup];
  let index = toIndex;
  const fromParent = from.slice(0, -1), fromIdx = from[from.length - 1];
  // Removing shifts later siblings — adjust target group path and index.
  if (fromParent.length < target.length && isPrefix(fromParent, target) && target[fromParent.length] > fromIdx) target[fromParent.length] -= 1;
  if (fromParent.length === target.length && isPrefix(fromParent, target) && index > fromIdx) index -= 1;
  if (fromParent.join() === target.join() && index === fromIdx) return def;
  return insertNode(removeNode(def, from), target, clone(node), index);
}

export function countRules(node: SegmentNode): number { return isGroup(node) ? node.rules.reduce((s, r) => s + countRules(r), 0) : 1; }
export function depth(node: SegmentNode): number { return isGroup(node) ? 1 + Math.max(0, ...node.rules.map(depth)) : 0; }

/** Locate the registry entry a rule was built from. */
export function fieldFor(rule: SegmentRule, fields: SegmentField[]): SegmentField | undefined {
  return fields.find(f => {
    if (f.type !== rule.type) return false;
    switch (rule.type) {
      case 'gift': case 'event': return f.aggregate === rule.aggregate;
      case 'consent': return (f.channel ?? f.field) === rule.channel;
      case 'source': case 'segment': return true;
      case 'enrichment': return f.field === rule.field && f.source === rule.source;
      default: return f.field === rule.field;
    }
  });
}
export const fieldKeyFor = (rule: SegmentRule, fields: SegmentField[]) => fieldFor(rule, fields)?.key ?? '';

export function defaultValue(op: string, dataType: string): unknown {
  if (VALUELESS.has(op)) return undefined;
  if (op === 'in' || op === 'not_in') return [];
  if (op === 'between') return dataType === 'number' ? [0, 100] : ['', ''];
  if (op === 'in_last_days' || op === 'not_in_last_days') return 30;
  if (dataType === 'number') return 0;
  return '';
}
export function withOperator(rule: SegmentRule, op: string, dataType: string): SegmentRule {
  const next: SegmentRule = { ...rule, operator: op };
  const keepList = Array.isArray(rule.value) && (op === 'in' || op === 'not_in') && (rule.operator === 'in' || rule.operator === 'not_in');
  if (keepList) return next;
  const v = defaultValue(op, dataType);
  if (v === undefined) delete next.value; else next.value = v;
  return next;
}
/** Build a fresh rule for a registry entry, using exact DSL shapes from docs/SEGMENTS.md. */
export function ruleFromField(f: SegmentField): SegmentRule {
  switch (f.type) {
    case 'gift': return f.aggregate === 'exists'
      ? { type: 'gift', aggregate: 'exists', operator: 'is_true' }
      : { type: 'gift', aggregate: f.aggregate, operator: 'gte', value: 1 };
    case 'event': return f.aggregate === 'exists'
      ? { type: 'event', name: '', aggregate: 'exists', operator: 'is_true', window: { kind: 'last_days', days: 30 } }
      : { type: 'event', name: '', aggregate: 'count', operator: 'gte', value: 1, window: { kind: 'last_days', days: 30 } };
    case 'consent': return { type: 'consent', channel: f.channel ?? f.field ?? 'email', status: 'opted_in' };
    case 'source': return { type: 'source', source_key: f.enum_values[0] ?? '', operator: 'in' };
    case 'segment': return { type: 'segment', segment_id: 0, operator: 'in' };
    default: {
      const op = f.operators[0] ?? 'eq';
      const base: SegmentRule = { type: f.type, field: f.field ?? f.key.split('.').pop() };
      if (f.type === 'enrichment') base.source = f.source;
      return withOperator(base, op, f.data_type);
    }
  }
}

const isDate = (v: unknown) => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}/.test(v) && !Number.isNaN(Date.parse(v));
const isNum = (v: unknown) => typeof v === 'number' && Number.isFinite(v);
function checkValue(rule: SegmentRule, type: string, p: string, out: Issue[]) {
  const op = rule.operator ?? '';
  if (VALUELESS.has(op)) return;
  const v = rule.value;
  const one = (x: unknown, at: string) => {
    if (type === 'number' && !isNum(x)) out.push({ path: at, message: 'Enter a number.' });
    else if ((type === 'date' || type === 'timestamp') && !isDate(x)) out.push({ path: at, message: 'Pick a date.' });
    else if ((type === 'text' || type === 'enum') && (typeof x !== 'string' || !x.trim())) out.push({ path: at, message: 'Enter a value.' });
  };
  if (op === 'in' || op === 'not_in') {
    if (!Array.isArray(v) || !v.length) out.push({ path: p + '.value', message: 'Choose at least one value.' });
    else v.forEach((x, i) => one(x, `${p}.value[${i}]`));
  } else if (op === 'between') {
    if (!Array.isArray(v) || v.length !== 2) out.push({ path: p + '.value', message: 'Enter a start and end.' });
    else { v.forEach((x, i) => one(x, `${p}.value[${i}]`)); if (v.every(x => isNum(x) || isDate(x)) && String(v[0]) !== '' && (isNum(v[0]) ? (v[0] as number) > (v[1] as number) : String(v[0]) > String(v[1]))) out.push({ path: p + '.value', message: 'Start must not be after end.' }); }
  } else if (op === 'in_last_days' || op === 'not_in_last_days') {
    if (!Number.isInteger(v) || (v as number) < 1 || (v as number) > 36600) out.push({ path: p + '.value', message: 'Days must be 1–36600.' });
  } else one(v, p + '.value');
}
function checkWindow(w: unknown, p: string, out: Issue[]) {
  if (w == null) return;
  const win = w as Record<string, unknown>;
  if (win.kind === 'last_days') { if (!Number.isInteger(win.days) || (win.days as number) < 1) out.push({ path: p + '.days', message: 'Days must be a whole number ≥ 1.' }); }
  else if (win.kind === 'between') { if (!isDate(win.from) || !isDate(win.to)) out.push({ path: p, message: 'Pick both window dates.' }); else if (String(win.from) > String(win.to)) out.push({ path: p, message: 'Window start is after end.' }); }
  else if (win.kind === 'before' || win.kind === 'after') { if (!isDate(win.date)) out.push({ path: p + '.date', message: 'Pick a window date.' }); }
  else out.push({ path: p + '.kind', message: 'Unknown window kind.' });
}

/** Client-side mirror of compiler checks so analysts see node paths before a round trip. */
export function validate(def: SegmentDefinition, fields: SegmentField[]): Issue[] {
  const out: Issue[] = [];
  if (def.version !== 1) out.push({ path: 'version', message: 'Only version 1 is supported.' });
  if (!isGroup(def.root)) return [...out, { path: 'root', message: 'Root must be a group.' }];
  if (depth(def.root) > MAX_DEPTH) out.push({ path: 'root', message: `Maximum nesting depth is ${MAX_DEPTH}.` });
  if (countRules(def.root) > MAX_RULES) out.push({ path: 'root', message: `Maximum ${MAX_RULES} rules.` });
  const walk = (node: SegmentNode, p: string) => {
    if (isGroup(node)) {
      if (node.op !== 'and' && node.op !== 'or') out.push({ path: p + '.op', message: 'Group must be AND or OR.' });
      if (!node.rules.length && p !== 'root') out.push({ path: p, message: 'Empty group — add a rule or remove it.' });
      node.rules.forEach((r, i) => walk(r, `${p}.rules[${i}]`));
      return;
    }
    const r = node as SegmentRule;
    if (r.type === 'consent') { if (!['opted_in', 'opted_out', 'unknown'].includes(String(r.status))) out.push({ path: p + '.status', message: 'Choose a permission status.' }); if (!r.channel) out.push({ path: p + '.channel', message: 'Choose a channel.' }); return; }
    if (r.type === 'source') { if (!r.source_key) out.push({ path: p + '.source_key', message: 'Choose a source.' }); if (!['in', 'not_in'].includes(String(r.operator))) out.push({ path: p + '.operator', message: 'Expected in or not_in.' }); return; }
    if (r.type === 'segment') { if (!Number.isInteger(r.segment_id) || (r.segment_id as number) < 1) out.push({ path: p + '.segment_id', message: 'Choose a saved audience.' }); if (!['in', 'not_in'].includes(String(r.operator))) out.push({ path: p + '.operator', message: 'Expected in or not_in.' }); return; }
    const f = fieldFor(r, fields);
    if (!f) { out.push({ path: p + (r.type === 'gift' || r.type === 'event' ? '.aggregate' : '.field'), message: 'Unknown or unavailable field.' }); return; }
    if (!f.operators.includes(String(r.operator))) { out.push({ path: p + '.operator', message: `Unsupported operator for ${f.data_type}.` }); return; }
    if (r.type === 'event' && (typeof r.name !== 'string' || !r.name.trim())) out.push({ path: p + '.name', message: 'An event name is required.' });
    checkValue(r, f.data_type, p, out);
    if (f.data_type === 'enum' && f.enum_values.length && Array.isArray(r.value)) r.value.forEach((x, i) => { if (!f.enum_values.includes(String(x))) out.push({ path: `${p}.value[${i}]`, message: `"${String(x)}" is not an allowed value.` }); });
    if (r.type === 'gift' || r.type === 'event') {
      checkWindow(r.window, p + '.window', out);
      const where = (r.where ?? {}) as Record<string, { operator?: string; value?: unknown }>;
      for (const [k, cond] of Object.entries(where)) {
        const at = `${p}.where.${k}`;
        if (r.type === 'event') { if (!k.startsWith('properties.') || k.length <= 11) out.push({ path: at, message: 'Use a property key.' }); else if (cond?.operator !== 'eq' || typeof cond.value !== 'string' || !cond.value) out.push({ path: at, message: 'Enter a property value.' }); continue; }
        const t = GIFT_WHERE[k];
        if (!t) { out.push({ path: at, message: 'Unknown gift filter.' }); continue; }
        if (t === 'boolean') { if (!['is_true', 'is_false'].includes(String(cond?.operator))) out.push({ path: at, message: 'Choose yes or no.' }); continue; }
        checkValue({ type: 'x', operator: cond?.operator, value: cond?.value }, t, at, out);
      }
    }
  };
  walk(def.root, 'root');
  return out;
}

/** Parse pasted/imported JSON into a definition, or return issues. */
export function parseDefinition(text: string): { definition?: SegmentDefinition; issues: Issue[] } {
  let raw: unknown;
  try { raw = JSON.parse(text); } catch (e) { return { issues: [{ path: 'json', message: e instanceof Error ? e.message : 'Invalid JSON.' }] }; }
  const obj = raw as Record<string, unknown>;
  const candidate = obj && typeof obj === 'object' && 'definition' in obj ? obj.definition as Record<string, unknown> : obj;
  if (!candidate || typeof candidate !== 'object' || !isGroup(candidate.root as SegmentNode)) return { issues: [{ path: 'root', message: 'Expected {"version": 1, "root": {"op": …, "rules": […]}}.' }] };
  const extra = Object.keys(candidate).filter(k => k !== 'version' && k !== 'root');
  if (extra.length) return { issues: [{ path: extra[0], message: 'Unknown top-level property.' }] };
  return { definition: candidate as unknown as SegmentDefinition, issues: [] };
}

// ---------- Human-readable summary ----------
const money = (n: unknown) => isNum(n) ? `$${(n as number).toLocaleString('en-US')}` : String(n);
const list = (v: unknown, joiner = 'or') => {
  const a = Array.isArray(v) ? v.map(String) : [String(v ?? '')];
  return a.length <= 1 ? a.join('') : `${a.slice(0, -1).join(', ')} ${joiner} ${a[a.length - 1]}`;
};
function fmt(v: unknown, field?: string) { return field && /amount|ltv/.test(field) ? money(v) : isNum(v) ? (v as number).toLocaleString('en-US') : String(v ?? ''); }
export function describeCondition(op: string | undefined, v: unknown, field?: string): string {
  switch (op) {
    case 'eq': return `is ${fmt(v, field)}`; case 'neq': return `is not ${fmt(v, field)}`;
    case 'in': return `is ${list(v)}`; case 'not_in': return `is not ${list(v, 'or')}`;
    case 'contains': return `contains "${v}"`; case 'starts_with': return `starts with "${v}"`;
    case 'is_null': return 'is empty'; case 'is_not_null': return 'is present';
    case 'gt': return `> ${fmt(v, field)}`; case 'gte': return `≥ ${fmt(v, field)}`;
    case 'lt': return `< ${fmt(v, field)}`; case 'lte': return `≤ ${fmt(v, field)}`;
    case 'between': return Array.isArray(v) ? `between ${fmt(v[0], field)} and ${fmt(v[1], field)}` : 'between';
    case 'before': return `before ${v}`; case 'after': return `after ${v}`;
    case 'in_last_days': return `in the last ${v} days`; case 'not_in_last_days': return `not in the last ${v} days`;
    case 'is_true': return 'is true'; case 'is_false': return 'is false';
    default: return `${op ?? '?'} ${fmt(v)}`;
  }
}
export function describeWindow(w: unknown): string {
  const win = w as Record<string, unknown> | undefined;
  if (!win) return 'all time';
  if (win.kind === 'last_days') return `in the last ${win.days} days`;
  if (win.kind === 'between') return `between ${win.from} and ${win.to}`;
  if (win.kind === 'before') return `before ${win.date}`;
  if (win.kind === 'after') return `after ${win.date}`;
  return 'all time';
}
export function describeRule(r: SegmentRule, fields: SegmentField[], segmentName?: (id: number) => string | undefined): string {
  const f = fieldFor(r, fields);
  const label = (f?.label || String(r.field ?? r.type)).replace(/\bdonors?\b/gi, m => m[0] === 'D' ? 'Partner' : 'partner');
  switch (r.type) {
    case 'gift': {
      const where = Object.entries((r.where ?? {}) as Record<string, { operator?: string; value?: unknown }>)
        .map(([k, c]) => k === 'is_recurring' ? (c.operator === 'is_true' ? 'recurring' : 'one-time') : `${k.replace('_', ' ')} ${describeCondition(c.operator, c.value, k)}`).join(', ');
      const scope = `${where ? ` (${where})` : ''} ${describeWindow(r.window)}`;
      if (r.aggregate === 'exists') return r.operator === 'is_false' ? `gave no gift${scope}` : `gave at least one gift${scope}`;
      const noun = r.aggregate === 'count' ? 'gift count' : `${r.aggregate} gift amount`;
      return `${noun}${scope} ${describeCondition(r.operator, r.value, r.aggregate === 'count' ? undefined : 'amount')}`;
    }
    case 'event': {
      const props = Object.entries((r.where ?? {}) as Record<string, { value?: unknown }>).map(([k, c]) => `${k.slice(11)} = "${c.value}"`).join(', ');
      const name = `"${r.name || '…'}"${props ? ` (${props})` : ''}`;
      if (r.aggregate === 'exists') return `${r.operator === 'is_false' ? 'never had' : 'had'} ${name} ${describeWindow(r.window)}`;
      return `${name} count ${describeWindow(r.window)} ${describeCondition(r.operator, r.value)}`;
    }
    case 'consent': return `${String(r.channel)} permission is ${String(r.status).replace('_', ' ')}`;
    case 'source': return `${r.operator === 'not_in' ? 'not from' : 'from'} source ${r.source_key || '…'}`;
    case 'segment': return `${r.operator === 'not_in' ? 'not in' : 'in'} audience ${segmentName?.(Number(r.segment_id)) ?? `#${r.segment_id || '…'}`}`;
    case 'enrichment': return `${label} (${r.source}) ${describeCondition(r.operator, r.value, String(r.field))}`;
    default: return `${label.toLowerCase()} ${describeCondition(r.operator, r.value, String(r.field))}`;
  }
}
export function describeNode(node: SegmentNode, fields: SegmentField[], segmentName?: (id: number) => string | undefined, nested = false): string {
  if (!isGroup(node)) return describeRule(node as SegmentRule, fields, segmentName);
  if (!node.rules.length) return nested ? '(empty group)' : '';
  const parts = node.rules.map(r => describeNode(r, fields, segmentName, true));
  let text = parts.join(node.op === 'and' ? ' and ' : ' or ');
  if (nested && node.rules.length > 1) text = `(${text})`;
  return node.negate ? `not ${node.rules.length > 1 && !nested ? `(${text})` : text}` : text;
}
export function summarize(def: SegmentDefinition, fields: SegmentField[], segmentName?: (id: number) => string | undefined): string {
  const body = describeNode(def.root, fields, segmentName);
  return body ? `Partners whose ${body}.` : 'All active partners — add a rule to narrow this audience.';
}

// ---------- Formatting ----------
export const fullNumber = (n: number | null | undefined) => n == null || !Number.isFinite(n) ? '—' : n.toLocaleString('en-US');
export function abbreviate(n: number | null | undefined) {
  if (n == null || !Number.isFinite(n)) return '—';
  const a = Math.abs(n);
  if (a >= 1e6) return `${(n / 1e6).toFixed(a >= 1e7 ? 1 : 2).replace(/\.0+$/, '')}M`;
  if (a >= 1e4) return `${(n / 1e3).toFixed(1).replace(/\.0$/, '')}K`;
  return n.toLocaleString('en-US');
}
export const signed = (n: number | null | undefined) => n == null ? '—' : `${n > 0 ? '+' : n < 0 ? '−' : '±'}${Math.abs(n).toLocaleString('en-US')}`;
export function relativeTime(iso: string | null | undefined, now = Date.now()) {
  if (!iso) return 'never';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '—';
  const s = Math.round((now - t) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`;
  return new Date(t).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}
export const SCHEDULES: { value: string; label: string }[] = [
  { value: 'daily', label: 'Daily at 04:00 UTC' },
  { value: 'manual', label: 'Manual only' },
];
/** API accepts refresh_schedule "daily" | null; map anything else to a valid option. */
export const normalizeSchedule = (v: string | null | undefined) => v == null ? 'manual' : 'daily';
