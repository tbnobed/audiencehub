import { test } from 'node:test';
import assert from 'node:assert/strict';
import { displayAmount, displayDate, displayNumber, displayValue, records, record } from './profile-display.ts';

test('missing and malformed display values never become React object children', () => {
  for (const value of [undefined, null, '', {}, { value: 10 }, NaN, Infinity]) {
    assert.equal(displayValue(value), '—');
  }
  assert.equal(displayValue(false), 'No');
  assert.equal(displayValue(0), '0');
  assert.equal(displayValue(['donor_crm', null, {}, 'five9']), 'donor_crm, five9');
});

test('dates reject invalid API strings without calling format on Invalid Date', () => {
  for (const value of [undefined, null, '', {}, 'not-a-date', '2026-02-30', '2026-13-01']) {
    assert.equal(displayDate(value), '—');
  }
  assert.equal(displayDate('2024-02-29'), 'Feb 29, 2024');
});

test('amounts preserve real zero and numeric strings, reject nonfinite/non-numeric values', () => {
  assert.equal(displayAmount(0), '$0.00');
  assert.equal(displayAmount('1234.50'), '$1,234.50');
  assert.equal(displayAmount(-5), '-$5.00');
  assert.equal(displayNumber('0'), '0');
  for (const value of [undefined, null, '', ' ', 'abc', {}, false, Infinity, 'Infinity']) {
    assert.equal(displayAmount(value), '—');
    assert.equal(displayNumber(value), '—');
  }
  assert.equal(displayAmount(10, 'not-currency'), '—');
});

test('nullable collections and traits are safe without mutating API values', () => {
  for (const value of [null, undefined, {}, 'bad']) assert.deepEqual(records(value), []);
  const row = { id: 1 };
  assert.deepEqual(records([null, row, false, []]), [row]);
  assert.deepEqual(record(null), {});
  assert.equal(record(row), row);
});