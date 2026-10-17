// Run: node --test src/lib/date-range.test.ts  (Node >= 22.18 strips types natively)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { isValidIsoDate, validateCustomRange, parseRangeParams, withRangeParams, presetRange, millionsTick, rangeDays } from './date-range.ts';

const now = new Date('2026-09-24T00:00:00Z');

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
  assert.equal(withRangeParams(ytd, '90d').get('range'), '90d');
  assert.deepEqual(parseRangeParams(new URLSearchParams('range=fy'), null, now), { preset: 'fy', range: null });
  assert.equal(parseRangeParams(new URLSearchParams('range=custom&from=0001-01-01&to=2026-01-01'), 1, now).preset, '12m');
  assert.equal(parseRangeParams(new URLSearchParams('from=2026-01-01&to=2026-02-01'), 1, now).preset, 'custom');
});

test('server defaults apply only when URL has no valid explicit range', () => {
  const seeded = { dashboard_default_preset: 'custom' as const, dashboard_default_from: '2020-01-01', dashboard_default_to: '2024-12-31' };
  const base = new URLSearchParams('tab=giving');
  assert.deepEqual(parseRangeParams(base, 1, now, null).range, null);
  assert.deepEqual(parseRangeParams(base, 1, now, seeded), { preset: 'custom', range: ['2020-01-01', '2024-12-31'] });
  assert.deepEqual(parseRangeParams(new URLSearchParams('range=90d'), 1, now, seeded), { preset: '90d', range: ['2026-06-27', '2026-09-24'] });
  assert.deepEqual(parseRangeParams(new URLSearchParams('from=2024-01-01&to=2024-02-01'), 1, now, seeded), { preset: 'custom', range: ['2024-01-01', '2024-02-01'] });
  assert.deepEqual(parseRangeParams(new URLSearchParams('range=fy'), 10, now, seeded), { preset: 'fy', range: ['2024-10-01', '2025-09-30'] });
  assert.deepEqual(parseRangeParams(new URLSearchParams('range=custom&from=invalid&to=2024-01-01'), 1, now, seeded), { preset: 'custom', range: ['2020-01-01', '2024-12-31'] });
  assert.equal(withRangeParams(base, '90d').get('range'), '90d');
});

test('rolling default is 12m but preserves saved 90d and explicit choices', () => {
  const base = new URLSearchParams();
  const rolling = { dashboard_default_preset: '12m' as const, dashboard_default_from: null, dashboard_default_to: null };
  const ninety = { ...rolling, dashboard_default_preset: '90d' as const };
  const expected = { preset: '12m', range: ['2025-09-25', '2026-09-24'] };
  assert.deepEqual(parseRangeParams(base, 1, now), expected);
  assert.deepEqual(parseRangeParams(base, 1, now, rolling), expected);
  assert.deepEqual(parseRangeParams(base, 1, now, ninety), { preset: '90d', range: ['2026-06-27', '2026-09-24'] });
  assert.deepEqual(parseRangeParams(new URLSearchParams('range=12m'), 1, now, ninety), expected);
  assert.deepEqual(parseRangeParams(new URLSearchParams('range=90d'), 1, now, rolling), { preset: '90d', range: ['2026-06-27', '2026-09-24'] });
  assert.deepEqual(parseRangeParams(new URLSearchParams('range=custom&from=2026-01-01&to=2026-01-31'), 1, now, rolling), { preset: 'custom', range: ['2026-01-01', '2026-01-31'] });
});

test('presets use UTC days across timezones, year boundaries and leap anniversaries', () => {
  assert.deepEqual(presetRange('12m', 1, new Date('2024-02-29T23:30:00Z')), ['2023-03-01', '2024-02-29']);
  assert.deepEqual(presetRange('12m', 1, new Date('2025-02-28T00:30:00Z')), ['2024-02-29', '2025-02-28']);
  const utcNewYear = new Date('2025-12-31T20:30:00-04:00');
  assert.deepEqual(presetRange('12m', 1, utcNewYear), ['2025-01-02', '2026-01-01']);
  assert.deepEqual(presetRange('ytd', 1, utcNewYear), ['2026-01-01', '2026-01-01']);
});

test('y axis is always millions', () => {
  assert.equal(millionsTick(3e6)(2e6), '$2M');
  assert.equal(millionsTick(8e5)(5e5), '$0.5M');
  assert.equal(millionsTick(4e4)(2e4), '$0.02M');
  assert.equal(millionsTick(1e6)(0), '$0');
});
