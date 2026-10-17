import { test } from 'node:test';
import assert from 'node:assert/strict';
import { groupEventsByDay, eventTypes, UNDATED } from './activity-timeline.ts';

const events = [
  { id: 1, type: 'email_open', occurred_at: '2025-01-02T08:00:00' },
  { id: 2, type: 'call', occurred_at: '2025-01-03T09:00:00' },
  { id: 3, type: 'email_open', occurred_at: '2025-01-02T17:30:00' },
  { id: 4, type: 'call', occurred_at: null },
];

test('groups by day, newest day and event first, undated last', () => {
  const days = groupEventsByDay(events);
  assert.deepEqual(days.map(d => d.day), ['2025-01-03', '2025-01-02', UNDATED]);
  assert.deepEqual(days[1].events.map(e => e.id), [3, 1]);
});

test('type filter retains only matching events', () => {
  const days = groupEventsByDay(events, 'email_open');
  assert.equal(days.length, 1);
  assert.equal(days[0].events.length, 2);
  assert.deepEqual(groupEventsByDay(events, 'all').length, 3);
  assert.deepEqual(eventTypes(events), ['call', 'email_open']);
});
