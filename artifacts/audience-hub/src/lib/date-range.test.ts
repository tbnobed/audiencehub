// Run: node --test src/lib/date-range.test.ts  (Node >= 22.18 strips types natively)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { isValidIsoDate, validateCustomRange, parseRangeParams, withRangeParams, presetRange, millionsTick, rangeDays } from './date-range.ts';

const now = new Date(2026, 8, 24); // Sep 24, 2026

test('rejects partial / implausible dates typed into date inputs', () => {
  for (const v of ['', '0001-01-01', '0020-05-01', '2026-02-30', '2026-13-01', '202-01-01', '2026-1-01']) assert.equal(isValidIsoDate(v), false, v);
  for (const v of ['2024-02-29', '2026-09-24', '1900-01-01']) assert.equal(isValidIsoDate(v), true, v);
});

test('custom range validation', () => {
  assert.equal(validateCustomRange('2026-01-01', '2026-01-01'), null);
  assert.match(validateCustomRange('2026-02-01', '2026-01-01')!, /on or before/);
  assert.match(validateCustomRange('0002-01-01', '2026-01-01')!, /start/);
  assert.match(validateCustomRange('2000-01-01', '2026-01-01')!, /at most/);
});

test('presets', () => {
  assert.deepEqual(presetRange('30d', 1, now), ['2026-08-26', '2026-09-24']);
  assert.deepEqual(presetRange('12m', 1, now), ['2025-09-25', '2026-09-24']);
  assert.deepEqual(presetRange('ytd', 1, now), ['2026-01-01', '2026-09-24']);
  assert.deepEqual(presetRange('fy', 10, now), ['2024-10-01', '2025-09-30']);
  assert.equal(rangeDays('2026-01-01', '2026-01-31'), 31);
});

test('URL parsing round-trips and keeps tab', () => {
  const base = new URLSearchParams('tab=giving');
  const custom = withRangeParams(base, 'custom', ['2026-01-01', '2026-03-31']);
  assert.equal(custom.get('tab'), 'giving');
  assert.deepEqual(parseRangeParams(custom, 1, now), { preset: 'custom', range: ['2026-01-01', '2026-03-31'] });
  const ytd = withRangeParams(custom, 'ytd');
  assert.equal(ytd.get('from'), null);
  assert.equal(ytd.get('tab'), 'giving');
  assert.equal(parseRangeParams(ytd, 1, now).preset, 'ytd');
  assert.equal(withRangeParams(ytd, '90d').get('range'), null);
  assert.deepEqual(parseRangeParams(new URLSearchParams('range=fy'), null, now), { preset: 'fy', range: null });
  assert.equal(parseRangeParams(new URLSearchParams('range=custom&from=0001-01-01&to=2026-01-01'), 1, now).preset, '90d');
  assert.equal(parseRangeParams(new URLSearchParams('from=2026-01-01&to=2026-02-01'), 1, now).preset, 'custom');
});

test('y axis is always millions', () => {
  assert.equal(millionsTick(3e6)(2e6), '$2M');
  assert.equal(millionsTick(8e5)(5e5), '$0.5M');
  assert.equal(millionsTick(4e4)(2e4), '$0.02M');
  assert.equal(millionsTick(1e6)(0), '$0');
});
