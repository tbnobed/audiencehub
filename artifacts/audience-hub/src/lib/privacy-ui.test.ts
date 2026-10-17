// Run: node --test src/lib/privacy-ui.test.ts
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { approveState, canEditConsent, canManagePrivacy, consentNoteError, identifierError, identifierPayload, isActiveDeletion, totalCount, countEntries, CONSENT_CHANNELS } from './privacy-ui.ts';
import type { DeletionRequest } from './privacy.ts';

const req = (o: Partial<DeletionRequest> = {}): DeletionRequest => ({ id: 7, requested_by: 1, approved_by: null, status: 'pending', created_at: '2026-01-01T00:00:00Z', approved_at: null, executed_at: null, result_counts: {}, error: null, ...o });

test('permissions: only admin manages privacy and edits consent', () => {
  assert.equal(canManagePrivacy('admin'), true);
  for (const r of ['analyst', 'viewer', undefined]) { assert.equal(canManagePrivacy(r), false); assert.equal(canEditConsent(r), false); }
});
test('two-person: requester cannot self-approve; second admin can', () => {
  assert.equal(approveState(req(), 1, true).allowed, false);
  assert.match(approveState(req(), 1, true).reason!, /different admin/);
  assert.equal(approveState(req(), 2, true).allowed, true);
  assert.equal(approveState(req(), 1, false).allowed, true);
});
test('approve disabled for non-pending statuses', () => {
  for (const s of ['queued', 'running', 'completed', 'failed']) assert.equal(approveState(req({ status: s }), 2, true).allowed, false);
});
test('polling statuses', () => {
  assert.equal(isActiveDeletion('queued'), true); assert.equal(isActiveDeletion('running'), true); assert.equal(isActiveDeletion('completed'), false);
});
test('identifier validation and payload trimming', () => {
  assert.ok(identifierError({ type: 'email', value: '' }));
  assert.ok(identifierError({ type: 'email', value: 'nope' }));
  assert.equal(identifierError({ type: 'email', value: 'a@b.org' }), null);
  assert.ok(identifierError({ type: 'phone', value: '12' }));
  assert.deepEqual(identifierPayload({ type: 'phone', value: ' 555 010 2233 ' }), { type: 'phone', value: '555 010 2233' });
});
test('consent note required; opt-in needs authorization detail', () => {
  assert.ok(consentNoteError('  ', 'opted_out'));
  assert.equal(consentNoteError('Called in', 'opted_out'), null);
  assert.ok(consentNoteError('ok', 'opted_in'));
  assert.equal(consentNoteError('Signed reply card 3/4', 'opted_in'), null);
  assert.equal(CONSENT_CHANNELS.length, 5);
});
test('preview counts aggregate', () => {
  assert.equal(totalCount({ events: 12, gifts: 3 }), 15);
  assert.deepEqual(countEntries({ gifts: 3, events: 12 }).map(([k]) => k), ['events', 'gifts']);
});
