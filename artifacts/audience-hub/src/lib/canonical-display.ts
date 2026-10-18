import { displayAmount, displayDate, displayNumber, displayValue } from './profile-display';

export type CanonicalMeta = { data_type: string; group: string; sensitive: boolean; model_date?: string };
export type CanonicalRow = { key: string; label: string; value: string; present: boolean; modelDate?: string };
export type CanonicalGroup = { group: string; rows: CanonicalRow[]; modelDates: { key: string; label: string; value: string }[] };

export const GROUP_ORDER = ['Donor', 'Imported models', 'SMS evidence', 'TBN+', 'Email hygiene', 'Address', 'Activity'];
const MONEY = /amount|amt|lifetime_avg|est_best/;

export function labelFor(key: string): string {
  return key.replace(/_/g, ' ').replace(/\btbn\b/g, 'TBN').replace(/\brfm\b/g, 'RFM').replace(/\bsms\b/g, 'SMS').replace(/\bncoa\b/g, 'NCOA').replace(/^./, c => c.toUpperCase());
}

export function formatCanonical(key: string, dataType: string, value: unknown): string {
  if (value == null || value === '') return displayValue(null);
  if (dataType === 'boolean') {
    if (typeof value === 'boolean') return value ? 'Yes' : 'No';
    const s = String(value).toLowerCase();
    return ['true', 'yes', 'y', '1'].includes(s) ? 'Yes' : ['false', 'no', 'n', '0'].includes(s) ? 'No' : displayValue(value);
  }
  if (dataType === 'number') return MONEY.test(key) ? displayAmount(value) : displayNumber(value);
  if (dataType === 'date') return displayDate(String(value));
  if (dataType === 'timestamp') return displayDate(String(value), 'MMM d, yyyy HH:mm');
  return displayValue(value);
}

/**
 * Group catalog fields. Only fields present in the (role-filtered) catalog are listed,
 * so a viewer never sees a placeholder row that would hint at a hidden sensitive value.
 */
export function groupCanonical(fields: Record<string, CanonicalMeta>, values: Record<string, unknown>, opts: { hideSensitive: boolean }): CanonicalGroup[] {
  const modelDateKeys = new Set(Object.values(fields).map(m => m.model_date).filter((k): k is string => !!k));
  const groups = new Map<string, CanonicalGroup>();
  for (const [key, meta] of Object.entries(fields)) {
    if (meta.sensitive && opts.hideSensitive) continue;
    const g = groups.get(meta.group) ?? { group: meta.group, rows: [], modelDates: [] };
    groups.set(meta.group, g);
    const value = formatCanonical(key, meta.data_type, values[key]);
    if (modelDateKeys.has(key)) { g.modelDates.push({ key, label: labelFor(key), value }); continue; }
    g.rows.push({ key, label: labelFor(key), value, present: values[key] != null && values[key] !== '',
      modelDate: meta.model_date ? formatCanonical(meta.model_date, fields[meta.model_date]?.data_type ?? 'date', values[meta.model_date]) : undefined });
  }
  const rank = (g: string) => { const i = GROUP_ORDER.indexOf(g); return i < 0 ? 99 : i; };
  return [...groups.values()].sort((a, b) => rank(a.group) - rank(b.group) || a.group.localeCompare(b.group));
}
