// Pure helpers for the activation console. No React, no fetch — unit tested in logic.test.ts.
import type { AttributeMode, ExportKind, Activation, ActivationRun, ActivationWrite, Destination, DestinationConfig, DestinationType, DestinationWrite, ExportField, FieldMapping, MappingValue } from '../../lib/activations.ts';

export type Role = 'viewer' | 'analyst' | 'admin';
export const DEST_TYPES: { value: DestinationType; label: string; short: string; blurb: string }[] = [
  { value: 'csv', label: 'CSV export', short: 'CSV', blurb: 'Plain file with your columns, delimiter and PII toggle. Downloaded by analysts.' },
  { value: 'google_customer_match', label: 'Google Customer Match file', short: 'Google CM', blurb: 'Normalized, SHA-256 hashed file for manual upload in Google Ads. No API connection.' },
  { value: 'meta_custom_audience', label: 'Meta custom audience file', short: 'Meta CA', blurb: 'Normalized, SHA-256 hashed file for manual upload in Meta Ads Manager. No API connection.' },
  { value: 'webhook', label: 'Webhook', short: 'Webhook', blurb: 'Signed JSON POST in batches to an admin-entered URL.' },
  { value: 'hubspot', label: 'HubSpot list', short: 'HubSpot', blurb: 'Consented emails from a segment synced to a static HubSpot list; Master IDs written back to contacts.' },
  { value: 'sftp', label: 'SFTP drop', short: 'SFTP', blurb: 'Comma CSV pushed over key-auth SFTP with a pinned host key; manifest.json marks each complete run.' },
];
export const typeLabel = (t: DestinationType) => DEST_TYPES.find(d => d.value === t)?.short ?? t;
export const isAdsType = (t?: DestinationType | null) => t === 'google_customer_match' || t === 'meta_custom_audience';
export const CONSENT_CHANNELS = ['email', 'sms', 'phone', 'mail', 'ads_personalization'];
export const ADS_CONSENT = 'ads_personalization';

/* ---------- role gating ---------- */
export const can = (role: Role | undefined) => ({
  viewActivations: role === 'analyst' || role === 'admin',
  runActivations: role === 'analyst' || role === 'admin',
  editActivations: role === 'analyst' || role === 'admin',
  manageDestinations: role === 'admin',
  overrideAdsConsent: role === 'admin',
});

/* ---------- funnel ---------- */
export type FunnelStage = { key: string; label: string; count: number; dropped: number };
export function runFunnel(r: Pick<ActivationRun, 'profiles_selected' | 'profiles_excluded_consent' | 'profiles_excluded_suppressed' | 'profiles_excluded_missing_fields' | 'profiles_sent'> & Partial<Pick<ActivationRun, 'profiles_excluded_household'>>): FunnelStage[] {
  const selected = r.profiles_selected;
  const consent = Math.max(0, selected - r.profiles_excluded_consent);
  const unsuppressed = Math.max(0, consent - r.profiles_excluded_suppressed);
  const complete = Math.max(0, unsuppressed - r.profiles_excluded_missing_fields);
  const hh = r.profiles_excluded_household ?? 0;
  const household = Math.max(0, complete - hh);
  return [
    { key: 'selected', label: 'Selected', count: selected, dropped: 0 },
    { key: 'consent', label: 'Eligible consent', count: consent, dropped: r.profiles_excluded_consent },
    { key: 'unsuppressed', label: 'Unsuppressed', count: unsuppressed, dropped: r.profiles_excluded_suppressed },
    { key: 'complete', label: 'Fields complete', count: complete, dropped: r.profiles_excluded_missing_fields },
    ...(hh > 0 ? [{ key: 'household', label: 'One per household', count: household, dropped: hh }] : []),
    { key: 'sent', label: 'Sent', count: r.profiles_sent, dropped: Math.max(0, household - r.profiles_sent) },
  ];
}

export type RunPhase = 'queued' | 'running' | 'succeeded' | 'failed' | 'other';
export function runPhase(status: string | undefined | null): RunPhase {
  const s = (status || '').toLowerCase();
  if (['queued', 'pending', 'scheduled'].includes(s)) return 'queued';
  if (['running', 'in_progress', 'materializing', 'sending'].includes(s)) return 'running';
  if (['succeeded', 'success', 'completed', 'complete', 'done'].includes(s)) return 'succeeded';
  if (['failed', 'error', 'cancelled', 'canceled'].includes(s)) return 'failed';
  return 'other';
}
export const isLive = (r?: ActivationRun | null) => !!r && (runPhase(r.status) === 'queued' || runPhase(r.status) === 'running');
export function elapsedMs(r: Pick<ActivationRun, 'started_at' | 'finished_at'>, now = Date.now()): number | null {
  if (!r.started_at) return null;
  const end = r.finished_at ? Date.parse(r.finished_at) : now;
  return Math.max(0, end - Date.parse(r.started_at));
}
export function fmtDuration(ms: number | null): string {
  if (ms == null) return '—';
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
}
export const fmtInt = (n: number | null | undefined) => (n == null ? '—' : n.toLocaleString('en-US'));

/* ---------- schedule ---------- */
export type SchedulePreset = 'manual' | 'daily' | 'weekly' | 'monthly' | 'custom';
export const DEFAULT_TZ = 'America/Chicago';
export const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
export type ScheduleState = { preset: SchedulePreset; hour: number; minute: number; weekday: number; monthday: number; custom: string };
export const defaultSchedule = (): ScheduleState => ({ preset: 'manual', hour: 6, minute: 0, weekday: 1, monthday: 1, custom: '' });

export function buildCron(s: ScheduleState): string | null {
  switch (s.preset) {
    case 'manual': return null;
    case 'daily': return `${s.minute} ${s.hour} * * *`;
    case 'weekly': return `${s.minute} ${s.hour} * * ${s.weekday}`;
    case 'monthly': return `${s.minute} ${s.hour} ${s.monthday} * *`;
    case 'custom': return s.custom.trim() || null;
  }
}
export function parseCron(cron: string | null | undefined): ScheduleState {
  const base = defaultSchedule();
  if (!cron) return base;
  const p = cron.trim().split(/\s+/);
  const num = (v: string) => (/^\d+$/.test(v) ? Number(v) : NaN);
  if (p.length === 5 && !isNaN(num(p[0])) && !isNaN(num(p[1])) && p[3] === '*') {
    const t = { ...base, minute: num(p[0]), hour: num(p[1]) };
    if (p[2] === '*' && p[4] === '*') return { ...t, preset: 'daily' };
    if (p[2] === '*' && !isNaN(num(p[4]))) return { ...t, preset: 'weekly', weekday: num(p[4]) % 7 };
    if (!isNaN(num(p[2])) && p[4] === '*') return { ...t, preset: 'monthly', monthday: num(p[2]) };
  }
  return { ...base, preset: 'custom', custom: cron };
}
const FIELD_RE = /^(\*|\d+(-\d+)?)(\/\d+)?(,(\*|\d+(-\d+)?)(\/\d+)?)*$/;
const RANGES: [number, number][] = [[0, 59], [0, 23], [1, 31], [1, 12], [0, 7]];
/** Lightweight shape check only; the backend validates the exact expression. */
export function cronShapeError(cron: string | null): string | null {
  if (cron == null) return null;
  const p = cron.trim().split(/\s+/);
  if (p.length !== 5) return 'Use 5 fields: minute hour day-of-month month day-of-week.';
  for (let i = 0; i < 5; i++) {
    if (!FIELD_RE.test(p[i])) return `Field ${i + 1} ("${p[i]}") is not a valid cron field.`;
    for (const n of p[i].match(/\d+/g) ?? []) {
      if (p[i].includes('/' + n)) continue;
      const v = Number(n);
      if (v < RANGES[i][0] || v > RANGES[i][1]) return `Field ${i + 1} value ${v} is out of range ${RANGES[i][0]}–${RANGES[i][1]}.`;
    }
  }
  return null;
}
export function tzLabel(tz: string): string {
  const known: Record<string, string> = { 'America/Chicago': 'Central', 'America/New_York': 'Eastern', 'America/Denver': 'Mountain', 'America/Los_Angeles': 'Pacific', 'UTC': 'UTC' };
  return known[tz] ?? tz;
}
const hhmm = (h: number, m: number) => `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
const ordinal = (n: number) => { const s = ['th', 'st', 'nd', 'rd'], v = n % 100; return n + (s[(v - 20) % 10] || s[v] || s[0]); };
export function describeSchedule(cron: string | null | undefined, tz = DEFAULT_TZ): string {
  if (!cron) return 'Manual — runs only when someone clicks Run now';
  const s = parseCron(cron);
  const at = `at ${hhmm(s.hour, s.minute)} ${tzLabel(tz)}`;
  if (s.preset === 'daily') return `Every day ${at}`;
  if (s.preset === 'weekly') return `Every ${DAYS[s.weekday]} ${at}`;
  if (s.preset === 'monthly') return `Monthly on the ${ordinal(s.monthday)} ${at}`;
  return `Custom cron "${cron}" (${tzLabel(tz)})`;
}
export function isValidTimezone(tz: string): boolean {
  try { new Intl.DateTimeFormat('en-US', { timeZone: tz }); return !!tz; } catch { return false; }
}
export function timezoneList(): string[] {
  const intl = Intl as unknown as { supportedValuesOf?: (k: string) => string[] };
  const list = intl.supportedValuesOf ? intl.supportedValuesOf('timeZone') : [];
  const base = ['America/Chicago', 'America/New_York', 'America/Denver', 'America/Los_Angeles', 'UTC'];
  return Array.from(new Set([...base, ...list]));
}

/* ---------- field mapping ---------- */
export type MapRow = { id: string; column: string; value: MappingValue | null };
let seq = 0;
export const rowId = () => `r${++seq}`;
export const mappingToRows = (m: FieldMapping | null | undefined): MapRow[] => Object.entries(m ?? {}).map(([column, value]) => ({ id: rowId(), column, value }));
export function rowsToMapping(rows: MapRow[]): FieldMapping {
  const out: FieldMapping = {};
  for (const r of rows) if (r.column.trim() && r.value) out[r.column.trim()] = r.value;
  return out;
}
export const fieldToValue = (f: ExportField): MappingValue => ({ kind: f.kind, key: f.key, ...(f.source ? { source: f.source } : {}) });
export const fieldId = (f: { kind: string; key?: string; source?: string }) => `${f.kind}:${f.source ?? ''}:${f.key ?? ''}`;
export function valueLabel(v: MappingValue | null, fields: ExportField[]): string {
  if (!v) return 'Unmapped';
  if (v.kind === 'constant') return `"${v.value ?? ''}"`;
  return fields.find(f => fieldId(f) === fieldId(v))?.label ?? `${v.kind}.${v.key}`;
}
/** Fields an activation may map: registry only, minus PII when the destination excludes PII. */
export function allowedFields(fields: ExportField[], dest?: Pick<Destination, 'type' | 'config'> | null): ExportField[] {
  if (dest?.type === 'csv' && dest.config.include_pii === false) return fields.filter(f => !f.pii);
  return fields;
}
export function mappingProblems(rows: MapRow[], fields: ExportField[], dest?: Pick<Destination, 'type' | 'config'> | null, mode: AttributeMode = 'selected'): string[] {
  const out: string[] = [];
  const allowed = new Set(allowedFields(fields, dest).map(fieldId));
  const seen = new Set<string>();
  rows.forEach(r => {
    const c = r.column.trim();
    if (!c) out.push('Every column needs a name.');
    else if (seen.has(c.toLowerCase())) out.push(`Column "${c}" is used twice.`);
    seen.add(c.toLowerCase());
    if (r.value && r.value.kind !== 'constant' && !allowed.has(fieldId(r.value))) out.push(`"${c}" maps a field this destination may not export.`);
  });
  if (mode === 'selected' && !rows.some(r => r.value)) out.push('Map at least one column.');
  return Array.from(new Set(out));
}

export type PresetCol = { column: string; candidates: string[]; hashed: boolean; locked?: MappingValue };
export const PRESETS: Partial<Record<DestinationType, PresetCol[]>> = {
  google_customer_match: [
    { column: 'Email', candidates: ['email'], hashed: true },
    { column: 'Phone', candidates: ['phone'], hashed: true },
    { column: 'First Name', candidates: ['first_name'], hashed: true },
    { column: 'Last Name', candidates: ['last_name'], hashed: true },
    { column: 'Country', candidates: ['country'], hashed: false },
    { column: 'Zip', candidates: ['postal_code', 'zip', 'zip_code'], hashed: false },
  ],
  meta_custom_audience: [
    { column: 'email', candidates: ['email'], hashed: true },
    { column: 'phone', candidates: ['phone'], hashed: true },
    { column: 'fn', candidates: ['first_name'], hashed: true },
    { column: 'ln', candidates: ['last_name'], hashed: true },
    { column: 'zip', candidates: ['postal_code', 'zip', 'zip_code'], hashed: true },
    { column: 'ct', candidates: ['city'], hashed: true },
    { column: 'st', candidates: ['state', 'region'], hashed: true },
    { column: 'country', candidates: ['country'], hashed: true },
    { column: 'external_id', candidates: [], hashed: true, locked: { kind: 'profile', key: 'id' } },
  ],
};
export const lockedSource = (type: DestinationType | undefined, column: string): MappingValue | undefined => (type ? PRESETS[type]?.find(c => c.column === column)?.locked : undefined);
/** Re-pin locked preset columns (Meta external_id = profile.id) so no edit path can override or clear them. */
export const enforceLocked = (type: DestinationType | undefined, rows: MapRow[]): MapRow[] => rows.map(r => { const l = lockedSource(type, r.column); return l ? { ...r, value: l } : r; });
export function presetRows(type: DestinationType, fields: ExportField[], existing?: FieldMapping): MapRow[] {
  const cols = PRESETS[type] ?? [];
  return cols.map(c => {
    if (c.locked) return { id: rowId(), column: c.column, value: c.locked };
    const prior = existing?.[c.column];
    const f = fields.find(x => x.kind === 'profile' && c.candidates.includes(x.key));
    return { id: rowId(), column: c.column, value: prior ?? (f ? fieldToValue(f) : null) };
  });
}

/* ---------- request builders ---------- */
export type HeaderRow = { name: string; value: string };
export type SftpForm = { output_dir: string; gzip: boolean; export_kind: ExportKind; approved_purpose: string; replaceCredentials: boolean; host: string; port: string; username: string; private_key: string; passphrase: string; host_key_sha256: string };
export const blankSftp = (): SftpForm => ({ output_dir: '/exports', gzip: false, export_kind: 'audience', approved_purpose: '', replaceCredentials: true, host: '', port: '22', username: '', private_key: '', passphrase: '', host_key_sha256: '' });
/** Wipe every write-only secret field (used after save/cancel). */
export const clearSftpSecrets = (s: SftpForm): SftpForm => ({ ...s, private_key: '', passphrase: '' });
export type DestinationForm = { meta?: { meta_source_id: number; meta_audience_id: string } | null; sftp: SftpForm; id?: number; name: string; type: DestinationType; is_active: boolean; delimiter: string; include_pii: boolean; url: string; batch_size: string; mode: 'full' | 'delta'; allow_internal_network: boolean; secret: string; replaceHeaders: boolean; headers: HeaderRow[]; hubspot: HubspotForm };
export type HubspotForm = { list_name: string; list_id: string; segment_id: string; frequency: 'manual' | 'hourly' | 'daily' | 'monthly'; hour: string; minute: string; day: string; token: string; replaceToken: boolean };
export function destinationToForm(d?: Destination | null): DestinationForm {
  return {
    meta: d?.type === 'meta_custom_audience' && d.config.meta_source_id != null && d.config.meta_audience_id ? { meta_source_id: d.config.meta_source_id, meta_audience_id: d.config.meta_audience_id } : null,
    id: d?.id, name: d?.name ?? '', type: d?.type ?? 'csv', is_active: d?.is_active ?? true,
    delimiter: d?.config.delimiter ?? ',', include_pii: d?.config.include_pii ?? d?.type === 'sftp',
    url: d?.config.url ?? '', batch_size: String(d?.config.batch_size ?? 500), mode: d?.config.mode ?? 'full',
    allow_internal_network: d?.config.allow_internal_network ?? false,
    secret: '', replaceHeaders: !d, headers: [],
    hubspot: { list_name: d?.config.list_name ?? '', list_id: d?.config.list_id ?? '', segment_id: d?.config.segment_id ? String(d.config.segment_id) : '', frequency: d?.config.schedule?.frequency ?? 'manual', hour: String(d?.config.schedule?.hour ?? 0), minute: String(d?.config.schedule?.minute ?? 0), day: String(d?.config.schedule?.day ?? 1), token: '', replaceToken: !d?.has_credentials },
    sftp: { ...blankSftp(), output_dir: d?.config.output_dir ?? '/exports', gzip: d?.config.gzip ?? false, export_kind: d?.config.export_kind ?? 'audience', approved_purpose: d?.config.approved_purpose ?? '', replaceCredentials: !d?.has_credentials },
  };
}
export function destinationFormErrors(f: DestinationForm): Record<string, string> {
  const e: Record<string, string> = {};
  if (!f.name.trim()) e.name = 'Name is required.';
  if (f.type === 'csv' && f.delimiter.length !== 1 && f.delimiter !== '\t') e.delimiter = 'Delimiter must be a single character.';
  if (f.type === 'webhook') {
    try { const u = new URL(f.url); if (u.protocol !== 'https:' && u.protocol !== 'http:') e.url = 'Use an http(s) URL.'; } catch { e.url = 'Enter a full URL, e.g. https://partner.example.org/hook'; }
    const b = Number(f.batch_size);
    if (!Number.isInteger(b) || b < 1 || b > 10000) e.batch_size = 'Batch size must be a whole number between 1 and 10,000.';
    if (f.replaceHeaders && f.headers.some(h => !h.name.trim() || !/^[A-Za-z0-9-]+$/.test(h.name.trim()))) e.headers = 'Header names use letters, digits and dashes only.';
  }
  if (f.type === 'hubspot') {
    const h = f.hubspot;
    if (!h.list_name.trim()) e.list_name = 'List name is required.';
    if (h.list_id.trim() && !/^\d+$/.test(h.list_id.trim())) e.list_id = 'List ID must be numeric.';
    if (!h.segment_id) e.segment_id = 'Choose a segment.';
    if (h.replaceToken && !h.token.trim()) e.token = 'Access token is required.';
    if (h.frequency !== 'manual' && (Number(h.minute) < 0 || Number(h.minute) > 59)) e.schedule = 'Minute must be 0-59.';
    if ((h.frequency === 'daily' || h.frequency === 'monthly') && (Number(h.hour) < 0 || Number(h.hour) > 23)) e.schedule = 'Hour must be 0-23.';
    if (h.frequency === 'monthly' && (Number(h.day) < 1 || Number(h.day) > 28)) e.schedule = 'Day must be 1-28.';
  }
  if (f.type === 'sftp') {
    const s = f.sftp;
    if (!s.output_dir.startsWith('/') || /\/\.{1,2}(\/|$)|\/\/|.\/$/.test(s.output_dir)) e.output_dir = 'Use an absolute, normalized path such as /exports/kinship.';
    if (s.export_kind === 'crosswalk' && f.is_active && !s.approved_purpose.trim()) e.approved_purpose = 'Crosswalk exports need an admin-approved purpose.';
    if (s.replaceCredentials) {
      if (!s.host.trim()) e.host = 'Host is required.';
      const p = Number(s.port); if (!Number.isInteger(p) || p < 1 || p > 65535) e.port = 'Port must be 1-65535.';
      if (!s.username.trim()) e.username = 'Username is required.';
      if (!s.private_key.includes('PRIVATE KEY')) e.private_key = 'Paste the full private key (PEM / OpenSSH), including BEGIN/END lines.';
      if (!s.host_key_sha256.trim()) e.host_key_sha256 = 'Pin the server host key fingerprint (SHA256).';
    } else if (f.is_active && !f.id) e.host = 'Active SFTP destinations need credentials.';
  }
  return e;
}
/** Secrets/headers are write-only: blank secret on edit is omitted (server keeps it); headers sent only when replacing. */
export function buildDestinationWrite(f: DestinationForm): DestinationWrite & { id?: number } {
  let config: DestinationConfig = {};
  if (f.type === 'csv') config = { delimiter: f.delimiter, include_pii: f.include_pii };
  if (f.type === 'sftp') config = { output_dir: f.sftp.output_dir.trim(), gzip: f.sftp.gzip, include_pii: f.sftp.export_kind === 'crosswalk' ? true : f.include_pii, delimiter: ',', export_kind: f.sftp.export_kind, ...(f.sftp.approved_purpose.trim() ? { approved_purpose: f.sftp.approved_purpose.trim() } : {}) };
  if (f.meta) config = { meta_source_id: f.meta.meta_source_id, meta_audience_id: f.meta.meta_audience_id, delimiter: ',', include_pii: true };
  if (f.type === 'webhook') config = { url: f.url.trim(), batch_size: Number(f.batch_size), mode: f.mode, allow_internal_network: f.allow_internal_network };
  if (f.type === 'hubspot') {
    const h = f.hubspot;
    const schedule: NonNullable<DestinationConfig['schedule']> = { frequency: h.frequency, timezone: 'America/New_York' };
    if (h.frequency !== 'manual') schedule.minute = Number(h.minute) || 0;
    if (h.frequency === 'daily' || h.frequency === 'monthly') schedule.hour = Number(h.hour) || 0;
    if (h.frequency === 'monthly') schedule.day = Number(h.day) || 1;
    config = { list_name: h.list_name.trim(), ...(h.list_id.trim() ? { list_id: h.list_id.trim() } : {}), segment_id: Number(h.segment_id), schedule };
  }
  const body: DestinationWrite & { id?: number } = { name: f.name.trim(), type: f.type, config, is_active: f.is_active };
  if (f.id) body.id = f.id;
  if (f.type === 'webhook') {
    if (f.secret.trim()) body.signing_secret = f.secret.trim();
    if (f.replaceHeaders) body.headers = Object.fromEntries(f.headers.filter(h => h.name.trim()).map(h => [h.name.trim(), h.value]));
  }
  if (f.type === 'hubspot' && f.hubspot.replaceToken && f.hubspot.token.trim()) body.credentials = { access_token: f.hubspot.token.trim() };
  if (f.type === 'sftp' && f.sftp.replaceCredentials) {
    const s = f.sftp;
    body.credentials = { host: s.host.trim(), port: Number(s.port), username: s.username.trim(), private_key: s.private_key, host_key_sha256: s.host_key_sha256.trim(), ...(s.passphrase ? { private_key_passphrase: s.passphrase } : {}) };
  }
  return body;
}

export type ActivationForm = { id?: number; name: string; segment_id: number | null; destination_id: number | null; rows: MapRow[]; required_consent: string | null; consent_override: boolean; override_note: string; schedule: ScheduleState; timezone: string; is_active: boolean; attribute_mode: AttributeMode; one_per_household: boolean };
export function activationToForm(a?: Activation | null): ActivationForm {
  return {
    id: a?.id, name: a?.name ?? '', segment_id: a?.segment_id ?? null, destination_id: a?.destination_id ?? null,
    rows: mappingToRows(a?.field_mapping), required_consent: a?.required_consent ?? null,
    consent_override: !!a && isAdsType(a.destination_type) && a.required_consent !== ADS_CONSENT, override_note: '',
    schedule: parseCron(a?.schedule), timezone: a?.timezone ?? DEFAULT_TZ, is_active: a?.is_active ?? true,
    attribute_mode: a?.attribute_mode ?? 'selected',
    one_per_household: a?.one_per_household ?? false,
  };
}
export function effectiveConsent(f: Pick<ActivationForm, 'required_consent' | 'consent_override'>, destType: DestinationType | undefined, role: Role | undefined): string | null {
  if (isAdsType(destType) && !(f.consent_override && can(role).overrideAdsConsent)) return ADS_CONSENT;
  return f.required_consent;
}
export function activationFormErrors(f: ActivationForm, destType: DestinationType | undefined, role: Role | undefined): Record<string, string> {
  const e: Record<string, string> = {};
  if (!f.name.trim()) e.name = 'Name the activation.';
  if (!f.segment_id) e.segment = 'Pick an active segment.';
  if (!f.destination_id) e.destination = 'Pick a destination.';
  if (isAdsType(destType) && f.consent_override) {
    if (!can(role).overrideAdsConsent) e.consent = 'Only admins can override ads_personalization consent.';
    else if (f.override_note.trim().length < 10) e.consent = 'Give an override reason of at least 10 characters; it is written to the audit log.';
  }
  const cron = buildCron(f.schedule);
  if (f.schedule.preset === 'custom' && !cron) e.schedule = 'Enter a cron expression or pick a preset.';
  const ce = cronShapeError(cron); if (ce) e.schedule = ce;
  if (!isValidTimezone(f.timezone)) e.timezone = 'Unknown timezone.';
  if (f.attribute_mode === 'all' && isAdsType(destType)) e.mapping = 'Ad destinations require selected provider columns.';
  return e;
}
/** Crosswalk exports: admin only, explicit consent channel when active. Server re-enforces. */
export function crosswalkErrors(f: Pick<ActivationForm, 'required_consent' | 'is_active'>, dest: Pick<Destination, 'config'> | null | undefined, role: Role | undefined): string[] {
  if (dest?.config.export_kind !== 'crosswalk') return [];
  const out: string[] = [];
  if (role !== 'admin') out.push('Crosswalk activations are restricted to admins.');
  if (f.is_active && !f.required_consent) out.push('Crosswalk exports require an explicit consent channel.');
  if (!dest.config.approved_purpose?.trim()) out.push('The crosswalk destination has no approved purpose; an admin must set one.');
  return out;
}
export function buildActivationWrite(f: ActivationForm, destType: DestinationType | undefined, role: Role | undefined): ActivationWrite & { id?: number } {
  const body: ActivationWrite & { id?: number } = {
    name: f.name.trim(), segment_id: f.segment_id!, destination_id: f.destination_id!, field_mapping: rowsToMapping(enforceLocked(destType, f.rows)),
    required_consent: effectiveConsent(f, destType, role), schedule: buildCron(f.schedule), timezone: f.timezone, is_active: f.is_active, attribute_mode: f.attribute_mode, one_per_household: f.one_per_household,
  };
  if (f.id) body.id = f.id;
  if (isAdsType(destType) && f.consent_override && can(role).overrideAdsConsent) { body.consent_override = true; body.override_note = f.override_note.trim(); }
  return body;
}
/** Full PATCH body for pause/resume, reusing the stored activation verbatim. */
export function toggleActiveBody(a: Activation, is_active: boolean): ActivationWrite & { id: number } {
  return { id: a.id, name: a.name, segment_id: a.segment_id, destination_id: a.destination_id, field_mapping: a.field_mapping, required_consent: a.required_consent, schedule: a.schedule, timezone: a.timezone, is_active, one_per_household: a.one_per_household ?? false, ...(a.attribute_mode ? { attribute_mode: a.attribute_mode } : {}) };
}

/** Run manifest summary: file count, total rows, total bytes. */
export function manifestSummary(m: { files: { row_count: number; bytes: number }[] } | null | undefined) {
  if (!m) return null;
  return { files: m.files.length, rows: m.files.reduce((s, f) => s + f.row_count, 0), bytes: m.files.reduce((s, f) => s + f.bytes, 0) };
}
export const shortHash = (h?: string | null) => (h ? `${h.slice(0, 12)}…` : '—');
export const plural = (n: number, one: string, many = one + 's') => `${fmtInt(n)} ${n === 1 ? one : many}`;
/** DELETE /api/activations/:id is a soft delete (is_active=false); the row stays listed as paused with its history. */
export const DEACTIVATE_COPY = {
  button: 'Deactivate',
  title: (name: string) => `Deactivate "${name}"?`,
  body: 'Scheduled runs stop. The activation stays in the list as paused, with its run history and audit entries. Resume re-enables the schedule.',
  done: (name: string) => `${name} deactivated`,
};

/** DELETE /api/destinations/:id is also a soft delete (is_active=false). */
export const DEACTIVATE_DEST_COPY = {
  button: 'Deactivate',
  title: (name: string) => `Deactivate "${name}"?`,
  body: 'The destination is retained as inactive, not permanently removed. Activations using it stop sending, and it is hidden from new activations. Edit it and turn Active back on to restore it.',
  done: (name: string) => `${name} deactivated`,
};
