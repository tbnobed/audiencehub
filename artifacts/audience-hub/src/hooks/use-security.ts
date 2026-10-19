import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchApi, apiUrl, csrfToken } from '@/lib/api';

// React Query retains variables: drop write-only secrets right after serialization, before any await.
const scrub = <T extends object>(v: T, keys: string[]) => { const body = JSON.stringify(v); keys.forEach(k => { delete (v as Record<string, unknown>)[k]; }); return body; };
const post = (b: object, keys: string[]) => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: scrub(b, keys) });
const json = (method: string, body: unknown) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

export interface AdminUser { id: number; email: string; name: string; role: 'admin' | 'analyst' | 'viewer'; is_active: boolean; last_login_at: string | null; locked_until: string | null; mfa_enabled: boolean; password_set: boolean }
export interface AuditItem { id: number; user_id: number | null; actor_type: string; action: string; entity_type: string; entity_id: string | null; at: string; details: unknown }
export interface AuditFilters { user_id: string; action: string; object: string; from_date: string; to_date: string }
export interface MaintenanceConfig { event_retention_days: number; audit_retention_days: number; job_retention_days: number; enrichment_retention_days: number; partitions_ahead_months: number }
export interface MaintenanceRun { id: number; status: string; payload: unknown; progress: unknown; error: string | null; created_at: string; finished_at: string | null }
export interface IssuedLink { user_id: number; token: string; expires_at: string }

export const inviteUrl = (token: string) => `${window.location.origin}${(import.meta.env.BASE_URL ?? '/').replace(/\/$/, '')}/login?token=${encodeURIComponent(token)}`;

export function auditQuery(f: AuditFilters, extra: Record<string, string> = {}) {
  const q = new URLSearchParams();
  Object.entries({ ...f, ...extra }).forEach(([k, v]) => { if (v) q.set(k, v); });
  return q.toString();
}

export const useAccountSecurity = () => useQuery<{ mfa_enabled: boolean; mfa_required: boolean }>({ queryKey: ['account-security'], queryFn: () => fetchApi('/api/account/security') });

export function useAccountActions() {
  const qc = useQueryClient();
  const refresh = () => { qc.invalidateQueries({ queryKey: ['account-security'] }); qc.invalidateQueries({ queryKey: ['me'] }); };
  return {
    password: useMutation({ mutationFn: (b: { current_password: string; new_password: string; totp?: string }) => fetchApi('/api/account/password', post(b, ['current_password', 'new_password', 'totp'])) }),
    setup: useMutation<{ secret: string; uri: string }, Error, { password: string }>({ mutationFn: b => fetchApi('/api/account/totp/setup', post(b, ['password'])) }),
    confirm: useMutation({ mutationFn: (b: { code: string }) => fetchApi('/api/account/totp/confirm', post(b, ['code'])), onSuccess: refresh }),
    disable: useMutation({ mutationFn: (b: { password: string; code: string }) => fetchApi('/api/account/totp/disable', post(b, ['password', 'code'])), onSuccess: refresh }),
  };
}

export const useAdminUsers = () => useQuery<{ items: AdminUser[] }>({ queryKey: ['admin-users'], queryFn: () => fetchApi('/api/admin/users') });

export function useUserActions() {
  const qc = useQueryClient();
  const inv = () => qc.invalidateQueries({ queryKey: ['admin-users'] });
  return {
    invite: useMutation<IssuedLink, Error, { email: string; name: string; role: string }>({ mutationFn: b => fetchApi('/api/admin/users', json('POST', b)), onSuccess: inv }),
    patch: useMutation({ mutationFn: ({ id, ...b }: { id: number; role?: string; is_active?: boolean }) => fetchApi(`/api/admin/users/${id}`, json('PATCH', b)), onSuccess: inv }),
    unlock: useMutation({ mutationFn: (id: number) => fetchApi(`/api/admin/users/${id}/unlock`, json('POST', {})), onSuccess: inv }),
    reset: useMutation<IssuedLink, Error, { id: number; reset_totp: boolean }>({ mutationFn: ({ id, reset_totp }) => fetchApi(`/api/admin/users/${id}/reset`, json('POST', { reset_totp })), onSuccess: inv }),
  };
}

export const useSecurityPolicy = () => useQuery<{ require_admin_totp: boolean }>({ queryKey: ['security-policy'], queryFn: () => fetchApi('/api/admin/security-policy') });
export function useUpdateSecurityPolicy() {
  const qc = useQueryClient();
  return useMutation({ mutationFn: (require_admin_totp: boolean) => fetchApi('/api/admin/security-policy', json('PATCH', { require_admin_totp })), onSuccess: () => qc.invalidateQueries({ queryKey: ['security-policy'] }) });
}

export async function fetchAuditPage(f: AuditFilters, before?: number): Promise<{ items: AuditItem[]; next_cursor: number | string | null }> {
  return fetchApi(`/api/admin/audit?${auditQuery(f, { limit: '100', ...(before ? { before_id: String(before) } : {}) })}`);
}

export async function downloadAuditCsv(f: AuditFilters) {
  const res = await fetch(apiUrl(`/api/admin/audit/export?${auditQuery(f)}`), { credentials: 'same-origin', headers: csrfToken ? { 'X-CSRF-Token': csrfToken } : {} });
  if (!res.ok) throw new Error('Audit export failed');
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement('a');
  a.href = url; a.download = 'audit-log.csv'; a.click();
  URL.revokeObjectURL(url);
}

export const useMaintenance = () => useQuery<{ config: MaintenanceConfig; runs: MaintenanceRun[] }>({ queryKey: ['admin-maintenance'], queryFn: () => fetchApi('/api/admin/maintenance'), refetchInterval: 10_000, refetchIntervalInBackground: false });
export function useMaintenanceActions() {
  const qc = useQueryClient();
  const inv = () => qc.invalidateQueries({ queryKey: ['admin-maintenance'] });
  return {
    save: useMutation({ mutationFn: (b: MaintenanceConfig) => fetchApi('/api/admin/maintenance', json('PATCH', b)), onSuccess: inv }),
    run: useMutation<{ job_id: number }, Error, void>({ mutationFn: () => fetchApi('/api/admin/maintenance/run', json('POST', {})), onSuccess: inv }),
  };
}
