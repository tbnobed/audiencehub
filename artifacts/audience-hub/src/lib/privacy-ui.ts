import type { ConsentChannel, ConsentStatus, DeletionRequest, PrivacyIdentifier } from './privacy.ts';

export const CONSENT_CHANNELS: ConsentChannel[] = ['email', 'sms', 'phone', 'mail', 'ads_personalization'];
export const CHANNEL_LABEL: Record<ConsentChannel, string> = { email: 'Email', sms: 'SMS', phone: 'Phone', mail: 'Mail', ads_personalization: 'Ads personalization' };
export const STATUS_LABEL: Record<ConsentStatus, string> = { opted_in: 'Opted in', opted_out: 'Opted out', unknown: 'Unknown' };
export const ACTIVE_DELETION = ['queued', 'running', 'approved', 'pending_execution'];

/** Client-side shape check only; the server normalizes and is authoritative. */
export function identifierError(id: PrivacyIdentifier): string | null {
  const v = id.value.trim();
  if (!v) return id.type === 'email' ? 'Enter an email address.' : 'Enter a phone number.';
  if (id.type === 'email' && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)) return 'Enter a valid email address.';
  if (id.type === 'phone' && v.replace(/\D/g, '').length < 7) return 'Enter a phone number with at least 7 digits.';
  return null;
}
export const identifierPayload = (id: PrivacyIdentifier): PrivacyIdentifier => ({ type: id.type, value: id.value.trim() });

export const canManagePrivacy = (role?: string | null) => role === 'admin';
export const canEditConsent = (role?: string | null) => role === 'admin';

export type ApproveState = { allowed: boolean; reason: string | null };
export function approveState(req: DeletionRequest, userId: number | undefined, twoPerson: boolean): ApproveState {
  if (req.status !== 'pending') return { allowed: false, reason: 'Only pending requests can be approved.' };
  if (twoPerson && userId != null && req.requested_by === userId) return { allowed: false, reason: 'Two-person approval is on. A different admin must approve this request.' };
  return { allowed: true, reason: null };
}
export const isActiveDeletion = (status: string) => ACTIVE_DELETION.includes(status);

export function consentNoteError(note: string, status: ConsentStatus): string | null {
  if (!note.trim()) return 'A note is required for manual consent changes.';
  if (status === 'opted_in' && note.trim().length < 10) return 'Opt-in requires a note describing the explicit authorization (at least 10 characters).';
  return null;
}
export const countEntries = (counts: Record<string, number> | null | undefined) => Object.entries(counts ?? {}).sort(([a], [b]) => a.localeCompare(b));
export const totalCount = (counts: Record<string, number> | null | undefined) => countEntries(counts).reduce((s, [, n]) => s + n, 0);
export const shortHash = (h: string) => h.length > 16 ? `${h.slice(0, 8)}…${h.slice(-6)}` : h;
export const fmtTime = (v: string | null | undefined) => v ? new Date(v).toLocaleString(undefined, { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
export const errMsg = (e: unknown) => e instanceof Error ? e.message : 'The request could not be completed.';
