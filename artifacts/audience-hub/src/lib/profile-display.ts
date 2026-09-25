import { format, isValid, parseISO } from 'date-fns';

export const EMPTY = '—';

/** API values are untrusted at the rendering boundary, even with TS declarations. */
export function displayValue(value: unknown): string {
  if (value == null || value === '') return EMPTY;
  if (typeof value === 'string') return value.trim() ? value : EMPTY;
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : EMPTY;
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (Array.isArray(value)) return value.map(displayValue).filter(v => v !== EMPTY).join(', ') || EMPTY;
  return EMPTY;
}

export function displayDate(value: unknown, pattern = 'MMM d, yyyy'): string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}(?:$|T| )/.test(value)) return EMPTY;
  const date = parseISO(value);
  return isValid(date) ? format(date, pattern) : EMPTY;
}

export function displayNumber(value: unknown): string {
  const number = finiteNumber(value);
  return number == null ? EMPTY : number.toLocaleString('en-US');
}

function finiteNumber(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null;
  if (typeof value === 'string' && (!value.trim() || !/^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(value.trim()))) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function displayAmount(value: unknown, currency: unknown = 'USD'): string {
  const number = finiteNumber(value);
  if (number == null) return EMPTY;
  if (typeof currency !== 'string' || !/^[A-Z]{3}$/.test(currency)) return EMPTY;
  try {
    return number.toLocaleString('en-US', { style: 'currency', currency });
  } catch {
    return EMPTY;
  }
}

export function records(value: unknown): Record<string, any>[] {
  return Array.isArray(value) ? value.filter((item): item is Record<string, any> => item != null && typeof item === 'object' && !Array.isArray(item)) : [];
}

export function record(value: unknown): Record<string, any> {
  return value != null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, any> : {};
}

export function displayJoined(values: unknown[], separator = ', '): string {
  return values.map(displayValue).filter(value => value !== EMPTY).join(separator) || EMPTY;
}