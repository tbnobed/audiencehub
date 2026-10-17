// Run: node --test src/components/activations/logic.test.ts  (Node >= 22.18 strips types natively)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  runFunnel, runPhase, buildDestinationWrite, destinationToForm, destinationFormErrors, buildActivationWrite, activationToForm,
  activationFormErrors, buildCron, parseCron, describeSchedule, cronShapeError, can, presetRows, allowedFields, mappingProblems,
  rowsToMapping, toggleActiveBody, enforceLocked, plural, DEACTIVATE_COPY, DEACTIVATE_DEST_COPY, fmtDuration, elapsedMs,
} from './logic.ts';

const fields = [
  { key: 'email', label: 'Email', kind: 'profile' as const, pii: true },
  { key: 'phone', label: 'Phone', kind: 'profile' as const, pii: true },
  { key: 'first_name', label: 'First name', kind: 'profile' as const, pii: true },
  { key: 'postal_code', label: 'Zip', kind: 'profile' as const, pii: false },
  { key: 'donor_status', label: 'Donor status', kind: 'trait' as const, pii: false },
];
const webhook = { id: 7, name: 'Partner hook', type: 'webhook' as const, config: { url: 'https://p.example.org/h', batch_size: 500, mode: 'delta' as const, allow_internal_network: false }, is_active: true, has_secret: true, secret_last4: 'a1b2', header_names: ['X-Partner'], created_at: '2026-01-01T00:00:00Z' };

test('funnel derives each stage from excluded counts', () => {
  const f = runFunnel({ profiles_selected: 1000, profiles_excluded_consent: 120, profiles_excluded_suppressed: 30, profiles_excluded_missing_fields: 47, profiles_sent: 803 });
  assert.deepEqual(f.map(s => s.count), [1000, 880, 850, 803, 803]);
  assert.deepEqual(f.map(s => s.dropped), [0, 120, 30, 47, 0]);
});

test('run status phases distinguish queued from complete', () => {
  assert.equal(runPhase('queued'), 'queued');
  assert.equal(runPhase('running'), 'running');
  assert.equal(runPhase('succeeded'), 'succeeded');
  assert.equal(runPhase('failed'), 'failed');
  assert.equal(fmtDuration(elapsedMs({ started_at: '2026-01-01T00:00:00Z', finished_at: '2026-01-01T00:01:05Z' })), '1m 05s');
});

test('editing a webhook with blank secret omits signing_secret and preserves headers', () => {
  const body = buildDestinationWrite(destinationToForm(webhook));
  assert.equal(body.id, 7);
  assert.equal('signing_secret' in body, false);
  assert.equal('headers' in body, false);
  assert.deepEqual(body.config, { url: 'https://p.example.org/h', batch_size: 500, mode: 'delta', allow_internal_network: false });
  const f = { ...destinationToForm(webhook), secret: ' new-secret ', replaceHeaders: true, headers: [{ name: 'X-Key', value: 'v' }] };
  const b2 = buildDestinationWrite(f);
  assert.equal(b2.signing_secret, 'new-secret');
  assert.deepEqual(b2.headers, { 'X-Key': 'v' });
});

test('new destination defaults: internal network off, CSV PII off', () => {
  const f = destinationToForm(null);
  assert.equal(f.allow_internal_network, false);
  assert.equal(f.include_pii, false);
  assert.deepEqual(buildDestinationWrite({ ...f, name: 'Board CSV' }).config, { delimiter: ',', include_pii: false });
  assert.ok(destinationFormErrors({ ...f, type: 'webhook', name: 'x', url: 'not a url' }).url);
});

test('ads destinations force ads_personalization for analysts', () => {
  const f = { ...activationToForm(null), name: 'A', segment_id: 1, destination_id: 2, required_consent: null, consent_override: true, override_note: 'board approved exception' };
  assert.equal(buildActivationWrite(f, 'google_customer_match', 'analyst').required_consent, 'ads_personalization');
  assert.equal(buildActivationWrite(f, 'google_customer_match', 'analyst').consent_override, undefined);
  assert.ok(activationFormErrors(f, 'meta_custom_audience', 'analyst').consent);
});

test('admin override requires reason and is sent explicitly', () => {
  const f = { ...activationToForm(null), name: 'A', segment_id: 1, destination_id: 2, required_consent: null, consent_override: true, override_note: 'short' };
  assert.ok(activationFormErrors(f, 'google_customer_match', 'admin').consent);
  const ok = { ...f, override_note: 'Legal approved existing-donor match' };
  assert.deepEqual(activationFormErrors(ok, 'google_customer_match', 'admin'), {});
  const body = buildActivationWrite(ok, 'google_customer_match', 'admin');
  assert.equal(body.required_consent, null);
  assert.equal(body.consent_override, true);
  assert.equal(body.override_note, 'Legal approved existing-donor match');
});

test('role gating', () => {
  assert.equal(can('viewer').viewActivations, false);
  assert.equal(can('analyst').manageDestinations, false);
  assert.equal(can('analyst').runActivations, true);
  assert.equal(can('admin').manageDestinations, true);
});

test('schedule presets build, parse and describe', () => {
  const s = parseCron('0 6 * * 1');
  assert.equal(s.preset, 'weekly');
  assert.equal(buildCron(s), '0 6 * * 1');
  assert.equal(describeSchedule('0 6 * * 1', 'America/Chicago'), 'Every Monday at 06:00 Central');
  assert.equal(describeSchedule('30 7 1 * *'), 'Monthly on the 1st at 07:30 Central');
  assert.equal(buildCron({ ...s, preset: 'manual' }), null);
  assert.equal(cronShapeError('*/15 * * * *'), null);
  assert.ok(cronShapeError('61 * * * *'));
  assert.ok(cronShapeError('0 6 * *'));
});

test('presets, PII filtering and mapping validation', () => {
  const g = presetRows('google_customer_match', fields);
  assert.deepEqual(g.map(r => r.column), ['Email', 'Phone', 'First Name', 'Last Name', 'Country', 'Zip']);
  assert.equal(g[0].value?.key, 'email');
  assert.equal(g[3].value, null);
  const csv = { type: 'csv' as const, config: { include_pii: false } };
  assert.equal(allowedFields(fields, csv).some(f => f.pii), false);
  const rows = [{ id: 'a', column: 'email', value: { kind: 'profile' as const, key: 'email' } }, { id: 'b', column: 'Source', value: { kind: 'constant' as const, value: 'kinship' } }];
  assert.ok(mappingProblems(rows, fields, csv).some(p => p.includes('may not export')));
  assert.deepEqual(Object.keys(rowsToMapping(rows)), ['email', 'Source']);
});

test('pause body keeps the stored activation', () => {
  const a = { id: 3, name: 'Lapsed', segment_id: 12, segment_name: 'L', destination_id: 7, destination_name: 'h', destination_type: 'webhook' as const, field_mapping: { email: { kind: 'profile' as const, key: 'email' } }, required_consent: 'email', schedule: '0 6 * * *', timezone: 'America/Chicago', is_active: true, created_by: 1, last_run: null, next_run_at: null };
  assert.deepEqual(toggleActiveBody(a, false), { id: 3, name: 'Lapsed', segment_id: 12, destination_id: 7, field_mapping: a.field_mapping, required_consent: 'email', schedule: '0 6 * * *', timezone: 'America/Chicago', is_active: false });
});

test('Meta external_id is locked to profile.id', () => {
  const rows = presetRows('meta_custom_audience', fields, { external_id: { kind: 'trait', key: 'donor_status' } });
  assert.deepEqual(rows.find(r => r.column === 'external_id')?.value, { kind: 'profile', key: 'id' });
  const tampered = rows.map(r => r.column === 'external_id' ? { ...r, value: null } : r);
  assert.deepEqual(enforceLocked('meta_custom_audience', tampered).find(r => r.column === 'external_id')?.value, { kind: 'profile', key: 'id' });
  const f = { ...activationToForm(null), name: 'M', segment_id: 1, destination_id: 2, rows: tampered };
  assert.deepEqual(buildActivationWrite(f, 'meta_custom_audience', 'admin').field_mapping.external_id, { kind: 'profile', key: 'id' });
  assert.equal(enforceLocked('meta_custom_audience', rows).find(r => r.column === 'email')?.value?.key, 'email');
});

test('deactivate copy matches soft-delete semantics; counts pluralize', () => {
  assert.equal(DEACTIVATE_COPY.button, 'Deactivate');
  assert.match(DEACTIVATE_COPY.body, /stays in the list as paused/);
  assert.doesNotMatch(DEACTIVATE_COPY.body + DEACTIVATE_COPY.title('x'), /archive|leaves the list/i);
  assert.equal(plural(1, 'partner'), '1 partner');
  assert.equal(plural(1204, 'partner'), '1,204 partners');
});

test('destination deactivate copy says retained inactive', () => {
  assert.equal(DEACTIVATE_DEST_COPY.button, 'Deactivate');
  assert.match(DEACTIVATE_DEST_COPY.body, /retained as inactive, not permanently removed/);
  assert.doesNotMatch(DEACTIVATE_DEST_COPY.body + DEACTIVATE_DEST_COPY.title('x'), /archive/i);
});
