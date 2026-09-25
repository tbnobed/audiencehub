// Pure date-range helpers for dashboards. No imports so they run under `node --test`.
export type Preset = '30d' | '90d' | '12m' | 'ytd' | 'fy' | 'custom';
export const PRESETS: Preset[] = ['30d', '90d', '12m', 'ytd', 'fy', 'custom'];
export const DEFAULT_PRESET = '90d' as const;
export const MAX_RANGE_DAYS = 3660;

export const toIso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

/** True only for real calendar dates YYYY-MM-DD with a plausible year (1900–2999). */
export function isValidIsoDate(value: string | null | undefined): value is string {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [y, m, d] = value.split('-').map(Number);
  if (y < 1900 || y > 2999) return false;
  const date = new Date(Date.UTC(y, m - 1, d));
  return date.getUTCFullYear() === y && date.getUTCMonth() === m - 1 && date.getUTCDate() === d;
}

const dayNum = (iso: string) => Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10)) / 86400000;

/** Returns a user-facing error, or null when the range may be sent to the API. */
export function validateCustomRange(from: string, to: string): string | null {
  if (!isValidIsoDate(from)) return 'Enter a complete start date.';
  if (!isValidIsoDate(to)) return 'Enter a complete end date.';
  if (from > to) return 'Start date must be on or before the end date.';
  if (dayNum(to) - dayNum(from) > MAX_RANGE_DAYS) return `Ranges can span at most ${MAX_RANGE_DAYS.toLocaleString('en-US')} days.`;
  return null;
}

export function rangeDays(from: string, to: string) {
  return dayNum(to) - dayNum(from) + 1;
}

export function presetRange(preset: Exclude<Preset, 'custom'>, fiscalStart = 1, now = new Date()): [string, string] {
  const end = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const start = new Date(end);
  if (preset === '30d') start.setDate(start.getDate() - 29);
  if (preset === '90d') start.setDate(start.getDate() - 89);
  if (preset === '12m') { start.setFullYear(start.getFullYear() - 1); start.setDate(start.getDate() + 1); }
  if (preset === 'ytd') start.setMonth(0, 1);
  if (preset === 'fy') {
    const fyYear = end.getMonth() + 1 >= fiscalStart ? end.getFullYear() : end.getFullYear() - 1;
    return [toIso(new Date(fyYear - 1, fiscalStart - 1, 1)), toIso(new Date(fyYear, fiscalStart - 1, 0))];
  }
  return [toIso(start), toIso(end)];
}

export type ParsedRange = { preset: Preset; range: [string, string] | null };

/**
 * Reads `range`, `from`, `to` query params. Custom needs a valid from/to;
 * legacy links with only from/to are treated as custom. `range` is null for
 * Last FY until the fiscal start month is known (caller must not query yet).
 */
export function parseRangeParams(params: URLSearchParams, fiscalStart: number | null, now = new Date()): ParsedRange {
  const raw = params.get('range');
  const from = params.get('from') ?? '';
  const to = params.get('to') ?? '';
  const customOk = validateCustomRange(from, to) === null;
  const preset: Preset = PRESETS.includes(raw as Preset) ? raw as Preset : (!raw && customOk ? 'custom' : DEFAULT_PRESET);
  if (preset === 'custom') return customOk ? { preset, range: [from, to] } : { preset: DEFAULT_PRESET, range: presetRange(DEFAULT_PRESET, 1, now) };
  if (preset === 'fy') return { preset, range: fiscalStart == null ? null : presetRange('fy', fiscalStart, now) };
  return { preset, range: presetRange(preset, 1, now) };
}

/** Returns new params with the range selection applied; other params (e.g. tab) are kept. */
export function withRangeParams(params: URLSearchParams, preset: Preset, custom?: [string, string]): URLSearchParams {
  const next = new URLSearchParams(params);
  next.delete('from'); next.delete('to');
  if (preset === DEFAULT_PRESET) next.delete('range'); else next.set('range', preset);
  if (preset === 'custom' && custom) { next.set('from', custom[0]); next.set('to', custom[1]); }
  return next;
}

/** Y-axis tick in millions of dollars, with enough decimals to keep ticks distinct. */
export function millionsTick(max: number) {
  const decimals = max >= 5e6 ? 0 : max >= 5e5 ? 1 : max >= 5e4 ? 2 : 3;
  return (v: number) => v === 0 ? '$0' : `$${parseFloat((v / 1e6).toFixed(decimals))}M`;
}
