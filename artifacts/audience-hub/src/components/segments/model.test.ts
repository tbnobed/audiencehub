// Run: node --test src/components/segments/model.test.ts (Node >= 22.18)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { SegmentDefinition, SegmentField } from '../../lib/segments.ts';
import { blankDefinition, duplicateNode, getNode, moveNode, parseDefinition, parseIssue, pathString, removeNode, ruleFromField, summarize, updateNode, validate, withOperator, abbreviate, signed } from './model.ts';

const F = (o: Partial<SegmentField>): SegmentField => ({ key: '', label: '', group: 'Giving', type: 'trait', data_type: 'text', operators: [], enum_values: [], ...o });
const fields: SegmentField[] = [
  F({ key: 'trait.donor_status', type: 'trait', field: 'donor_status', label: 'Partner status', data_type: 'enum', operators: ['in', 'not_in'], enum_values: ['lapsing', 'lapsed', 'active'] }),
  F({ key: 'trait.ltv_total', type: 'trait', field: 'ltv_total', label: 'Lifetime giving', data_type: 'number', operators: ['eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'between', 'is_null', 'is_not_null'] }),
  F({ key: 'profile.first_seen_at', type: 'profile', field: 'first_seen_at', label: 'First seen', data_type: 'timestamp', operators: ['before', 'after', 'between', 'in_last_days', 'not_in_last_days', 'is_null', 'is_not_null'] }),
  F({ key: 'gift.count', type: 'gift', field: 'count', aggregate: 'count', data_type: 'number', operators: ['eq', 'gte', 'between'] }),
  F({ key: 'gift.exists', type: 'gift', field: 'exists', aggregate: 'exists', data_type: 'boolean', operators: ['is_true', 'is_false'] }),
  F({ key: 'event.count', type: 'event', field: 'count', aggregate: 'count', data_type: 'number', operators: ['gte'] }),
  F({ key: 'consent.email', type: 'consent', field: 'email', channel: 'email', data_type: 'enum', operators: ['in'], enum_values: ['opted_in', 'opted_out', 'unknown'] }),
  F({ key: 'source.source_key', type: 'source', field: 'source_key', data_type: 'enum', operators: ['in', 'not_in'], enum_values: ['donor_crm'] }),
  F({ key: 'segment.segment_id', type: 'segment', field: 'segment_id', data_type: 'enum', operators: ['in', 'not_in'] }),
  F({ key: 'enrichment.zeta.hh_income_band', type: 'enrichment', field: 'hh_income_band', source: 'zeta', data_type: 'enum', operators: ['in', 'not_in'], enum_values: ['100-150k', '150k+'] }),
];
const docExample: SegmentDefinition = { version: 1, root: { op: 'and', rules: [
  { type: 'trait', field: 'donor_status', operator: 'in', value: ['lapsing', 'lapsed'] },
  { type: 'trait', field: 'ltv_total', operator: 'gte', value: 250 },
  { type: 'gift', aggregate: 'count', operator: 'gte', value: 1, where: { fund: { operator: 'eq', value: 'Missions' } }, window: { kind: 'between', from: '2023-01-01', to: '2024-12-31' } },
  { type: 'event', name: 'Video Watched', aggregate: 'count', operator: 'gte', value: 3, window: { kind: 'last_days', days: 30 } },
  { type: 'consent', channel: 'email', status: 'opted_in' },
  { op: 'or', rules: [{ type: 'enrichment', source: 'zeta', field: 'hh_income_band', operator: 'in', value: ['150k+'] }, { type: 'source', source_key: 'donor_crm', operator: 'in' }] },
  { type: 'segment', segment_id: 12, operator: 'not_in' },
] } };

test('blank definition is version 1 with an empty AND root', () => {
  assert.deepEqual(blankDefinition(), { version: 1, root: { op: 'and', rules: [] } });
  assert.deepEqual(validate(blankDefinition(), fields), []);
});

test('documented example validates and summarizes with partner copy', () => {
  assert.deepEqual(validate(docExample, fields), []);
  const s = summarize(docExample, fields, id => id === 12 ? 'Major pipeline' : undefined);
  assert.match(s, /^Partners whose /);
  assert.match(s, /partner status is lapsing or lapsed/);
  assert.match(s, /lifetime giving ≥ \$250/);
  assert.match(s, /fund is Missions/);
  assert.match(s, /not in audience Major pipeline/);
  assert.doesNotMatch(s, /\bdonors?\b/i);
});

test('rules built from registry use exact DSL shapes', () => {
  assert.deepEqual(ruleFromField(fields[3]), { type: 'gift', aggregate: 'count', operator: 'gte', value: 1 });
  assert.deepEqual(ruleFromField(fields[6]), { type: 'consent', channel: 'email', status: 'opted_in' });
  assert.deepEqual(ruleFromField(fields[8]), { type: 'segment', segment_id: 0, operator: 'in' });
  assert.deepEqual(ruleFromField(fields[9]), { type: 'enrichment', field: 'hh_income_band', source: 'zeta', operator: 'in', value: [] });
  const ev = ruleFromField(fields[5]);
  assert.equal(ev.type, 'event'); assert.deepEqual(ev.window, { kind: 'last_days', days: 30 });
  assert.equal('value' in withOperator({ type: 'trait', field: 'ltv_total', operator: 'gte', value: 3 }, 'is_null', 'number'), false);
  assert.equal(withOperator({ type: 'profile', operator: 'after', value: '2024-01-01' }, 'in_last_days', 'timestamp').value, 30);
});

test('validation reports node paths for bad field, operator, enum, empty values and windows', () => {
  const bad: SegmentDefinition = { version: 1, root: { op: 'and', rules: [
    { type: 'trait', field: 'nope', operator: 'eq', value: 'x' },
    { op: 'or', rules: [{ type: 'trait', field: 'ltv_total', operator: 'contains', value: 'x' }, { type: 'trait', field: 'donor_status', operator: 'in', value: ['ghost'] }] },
    { type: 'event', name: '', aggregate: 'count', operator: 'gte', value: 1, window: { kind: 'last_days', days: 0 } },
    { op: 'and', rules: [] },
    { type: 'trait', field: 'ltv_total', operator: 'between', value: [500, 10] },
  ] } };
  const paths = validate(bad, fields).map(i => i.path);
  assert.ok(paths.includes('root.rules[0].field'));
  assert.ok(paths.includes('root.rules[1].rules[0].operator'));
  assert.ok(paths.includes('root.rules[1].rules[1].value[0]'));
  assert.ok(paths.includes('root.rules[2].name'));
  assert.ok(paths.includes('root.rules[2].window.days'));
  assert.ok(paths.includes('root.rules[3]'));
  assert.ok(paths.includes('root.rules[4].value'));
});

test('injection-looking text values are kept as literal strings', () => {
  const def: SegmentDefinition = { version: 1, root: { op: 'and', rules: [{ type: 'gift', aggregate: 'count', operator: 'gte', value: 1, where: { campaign: { operator: 'eq', value: "'; DROP TABLE gifts; --%_" } } }] } };
  assert.deepEqual(validate(def, fields), []);
  assert.equal((getNode(def.root, [0]) as { where: { campaign: { value: string } } }).where.campaign.value, "'; DROP TABLE gifts; --%_");
});

test('tree operations: update, duplicate, delete, move across groups, refuse moving into self', () => {
  let d = docExample;
  d = updateNode(d, [5], n => ({ ...(n as { op: 'or'; rules: [] }), negate: true }));
  assert.equal((getNode(d.root, [5]) as { negate?: boolean }).negate, true);
  d = duplicateNode(d, [0]);
  assert.equal(d.root.rules.length, 8);
  assert.deepEqual(d.root.rules[0], d.root.rules[1]);
  d = removeNode(d, [1]);
  assert.equal(d.root.rules.length, 7);
  const moved = moveNode(d, [0], [5], 1); // into the OR group
  assert.equal(moved.root.rules.length, 6);
  assert.equal((getNode(moved.root, [4]) as { rules: unknown[] }).rules.length, 3);
  assert.deepEqual(getNode(moved.root, [4, 1]), docExample.root.rules[0]);
  assert.equal(moveNode(d, [5], [5], 0), d);
  const down = moveNode(d, [0], [], 2);
  assert.deepEqual(down.root.rules[1], docExample.root.rules[0]);
  assert.equal(docExample.root.rules.length, 7, 'original untouched');
});

test('paths and server error parsing', () => {
  assert.equal(pathString([2, 0]), 'root.rules[2].rules[0]');
  assert.deepEqual(parseIssue('root.rules[2].rules[0].value: Expected a nonempty list'), { path: 'root.rules[2].rules[0].value', message: 'Expected a nonempty list' });
  assert.equal(parseIssue('Partner audience query timed out.'), null);
});

test('JSON import accepts a definition or a wrapped segment and rejects junk', () => {
  assert.equal(parseDefinition(JSON.stringify(docExample)).issues.length, 0);
  assert.equal(parseDefinition(JSON.stringify({ definition: docExample })).issues.length, 0);
  assert.equal(parseDefinition('{nope').issues[0].path, 'json');
  assert.equal(parseDefinition('{"version":1,"root":{"op":"and","rules":[]},"x":1}').issues[0].path, 'x');
});

test('number formatting', () => {
  assert.equal(abbreviate(1240000), '1.24M');
  assert.equal(abbreviate(12340), '12.3K');
  assert.equal(signed(-12340), '−12,340');
  assert.equal(signed(5), '+5');
});
