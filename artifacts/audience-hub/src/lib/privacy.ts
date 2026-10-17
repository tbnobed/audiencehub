export type ConsentChannel = 'email' | 'sms' | 'phone' | 'mail' | 'ads_personalization';
export type ConsentStatus = 'opted_in' | 'opted_out' | 'unknown';
export type PartnerConsent = { channel: ConsentChannel; status: ConsentStatus; captured_at: string | null; source_id: number | null };
export type Suppression = { id: number; type: string; value_hash: string; reason: string; created_at: string };
export type DeletionPreview = { matched_partners: number; counts: Record<string, number>; backup_keep_days: number };
export type DeletionRequest = { id: number; requested_by: number | null; approved_by: number | null; status: string; created_at: string; approved_at: string | null; executed_at: string | null; result_counts: Record<string, number>; error: string | null };
export type PrivacyIdentifier = { type: 'email' | 'phone'; value: string };
