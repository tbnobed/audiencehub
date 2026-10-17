// Pure helpers for the activation console. No React, no fetch — unit tested in logic.test.ts.
import type { Activation, ActivationRun, ActivationWrite, Destination, DestinationConfig, DestinationType, DestinationWrite, ExportField, FieldMapping, MappingValue } from '../../lib/activations.ts';

export type Role = 'viewer' | 'analyst' | 'admin';
export const DEST_TYPES: { value: DestinationType; label: string; short: string; blurb: string }[] = [
  { value: 'csv', label: 'CSV export', short: 'CSV', blurb: 'Plain file with your columns, delimiter and PII toggle. Downloaded by analysts.' },
  { value: 'google_customer_match', label: 'Google Customer Match file', short: 'Google CM', blurb: 'Normalized, SHA-256 hashed file for manual upload in Google Ads. No API connection.' },
  { value: 'meta_custom_audience', label: 'Meta custom audience file', short: 'Meta CA', blurb: 'Normalized, SHA-256 hashed file for manual upload in Meta Ads Manager. No API connection.' },
  { value: 'webhook', label: 'Webhook', short: 'Webhook', blurb: 'Signed JSON POST in batches to an admin-entered URL.' },
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
export function runFunnel(r: Pick<ActivationRun, 'profiles_selected' | 'profiles_excluded_consent' | 'profiles_excluded_suppressed' | 'profiles_excluded_missing_fields' | 'profiles_sent'>): FunnelStage[] {
  const selected = r.profiles_selected;
  const consent = Math.max(0, selected - r.profiles_excluded_consent);
  const unsuppressed = Math.max(0, consent - r.profiles_excluded_suppressed);
  const complete = Math.max(0, unsuppressed - r.profiles_excluded_missing_fields);
  return [
    { key: 'selected', label: 'Selected', count: selected, dropped: 0 },
    { key: 'consent', label: 'Eligible consent', count: consent, dropped: r.profiles_excluded_consent },
    { key: 'unsuppressed', label: 'Unsuppressed', count: unsuppressed, dropped: r.profiles_excluded_suppressed },
    { key: 'complete', label: 'Fields complete', count: complete, dropped: r.profiles_excluded_missing_fields },
    { key: 'sent', label: 'Sent', count: r.profiles_sent, dropped: Math.max(0, complete - r.profiles_sent) },
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
export function mappingProblems(rows: MapRow[], fields: ExportField[], dest?: Pick<Destination, 'type' | 'config'> | null): string[] {
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
  if (!rows.some(r => r.value)) out.push('Map at least one column.');
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
export type DestinationForm = { id?: number; name: string; type: DestinationType; is_active: boolean; delimiter: string; include_pii: boolean; url: string; batch_size: string; mode: 'full' | 'delta'; allow_internal_network: boolean; secret: string; replaceHeaders: boolean; headers: HeaderRow[] };
export function destinationToForm(d?: Destination | null): DestinationForm {
  return {
    id: d?.id, name: d?.name ?? '', type: d?.type ?? 'csv', is_active: d?.is_active ?? true,
    delimiter: d?.config.delimiter ?? ',', include_pii: d?.config.include_pii ?? false,
    url: d?.config.url ?? '', batch_size: String(d?.config.batch_size ?? 500), mode: d?.config.mode ?? 'full',
    allow_internal_network: d?.config.allow_internal_network ?? false,
    secret: '', replaceHeaders: !d, headers: [],
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
  return e;
}
/** Secrets/headers are write-only: blank secret on edit is omitted (server keeps it); headers sent only when replacing. */
export function buildDestinationWrite(f: DestinationForm): DestinationWrite & { id?: number } {
  let config: DestinationConfig = {};
  if (f.type === 'csv') config = { delimiter: f.delimiter, include_pii: f.include_pii };
  if (f.type === 'webhook') config = { url: f.url.trim(), batch_size: Number(f.batch_size), mode: f.mode, allow_internal_network: f.allow_internal_network };
  const body: DestinationWrite & { id?: number } = { name: f.name.trim(), type: f.type, config, is_active: f.is_active };
  if (f.id) body.id = f.id;
  if (f.type === 'webhook') {
    if (f.secret.trim()) body.signing_secret = f.secret.trim();
    if (f.replaceHeaders) body.headers = Object.fromEntries(f.headers.filter(h => h.name.trim()).map(h => [h.name.trim(), h.value]));
  }
  return body;
}

export type ActivationForm = { id?: number; name: string; segment_id: number | null; destination_id: number | null; rows: MapRow[]; required_consent: string | null; consent_override: boolean; override_note: string; schedule: ScheduleState; timezone: string; is_active: boolean };
export function activationToForm(a?: Activation | null): ActivationForm {
  return {
    id: a?.id, name: a?.name ?? '', segment_id: a?.segment_id ?? null, destination_id: a?.destination_id ?? null,
    rows: mappingToRows(a?.field_mapping), required_consent: a?.required_consent ?? null,
    consent_override: !!a && isAdsType(a.destination_type) && a.required_consent !== ADS_CONSENT, override_note: '',
    schedule: parseCron(a?.schedule), timezone: a?.timezone ?? DEFAULT_TZ, is_active: a?.is_active ?? true,
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
  return e;
}
export function buildActivationWrite(f: ActivationForm, destType: DestinationType | undefined, role: Role | undefined): ActivationWrite & { id?: number } {
  const body: ActivationWrite & { id?: number } = {
    name: f.name.trim(), segment_id: f.segment_id!, destination_id: f.destination_id!, field_mapping: rowsToMapping(enforceLocked(destType, f.rows)),
    required_consent: effectiveConsent(f, destType, role), schedule: buildCron(f.schedule), timezone: f.timezone, is_active: f.is_active,
  };
  if (f.id) body.id = f.id;
  if (isAdsType(destType) && f.consent_override && can(role).overrideAdsConsent) { body.consent_override = true; body.override_note = f.override_note.trim(); }
  return body;
}
/** Full PATCH body for pause/resume, reusing the stored activation verbatim. */
export function toggleActiveBody(a: Activation, is_active: boolean): ActivationWrite & { id: number } {
  return { id: a.id, name: a.name, segment_id: a.segment_id, destination_id: a.destination_id, field_mapping: a.field_mapping, required_consent: a.required_consent, schedule: a.schedule, timezone: a.timezone, is_active };
}

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
