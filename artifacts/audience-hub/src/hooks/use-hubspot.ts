import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';

export type HsSchedule = { frequency: 'manual' | 'hourly' | 'daily' | 'monthly'; hour?: number; minute?: number; day?: number; timezone: 'America/New_York' };
export type HsMapping = { id: string; feed: 'people'; properties: string[]; columns: Record<string, string>; schedule: HsSchedule };
export type HsConfig = { mappings: HsMapping[]; schedules: Record<string, unknown>; engagement: boolean };
export type HsConnection = { configured: boolean; has_credentials: boolean; config: Partial<HsConfig>; recovery_pending?: boolean };
export type HsTest = { account: { hub_id: number | string; app_id?: number | string }; scopes: string[]; missing_scopes: Record<string, string[]>; delivered?: boolean; mode?: string };
export type HsProperty = { name: string; label: string; type: string };
export type HsProperties = { items: HsProperty[]; suggested_columns: Record<string, string> };
export type HsDryItem = { mapping_id: string; rows_checked: number; rows_rejected: number; fields: string[]; identity_keys?: string[]; rows?: Record<string, unknown>[]; suggested_columns?: Record<string, string> };
export type HsDryRun = { limit_per_mapping: number; items: HsDryItem[] };
export type HsPreview = { items: { master_id: string; hubspot_record_id: string | null; email: string }[]; limit: number };

const json = (url: string, method: string, body: unknown, timeoutMs?: number) =>
  fetchApi(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), timeoutMs });

const connKey = (id: number) => ['sources', id, 'connection'] as const;
export function useHsConnection(id: number) {
  return useQuery<HsConnection>({ queryKey: connKey(id), retry: false, gcTime: 0, queryFn: ({ signal }) => fetchApi(`/api/sources/${id}/connection`, { signal, timeoutMs: 15000 }) });
}
/** Token is write-only: stripped from mutation variables before the request is sent; response carries no token. */
export function useHsSave(id: number) {
  const client = useQueryClient();
  return useMutation<HsConnection, Error, { config: HsConfig; credentials?: { access_token: string } }>({
    mutationFn: v => { const body = JSON.stringify(v); delete v.credentials; return fetchApi(`/api/sources/${id}/connection`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body }); },
    onSuccess: d => client.setQueryData(connKey(id), d),
  });
}
export function useHsSourceTest(id: number) {
  return useMutation<HsTest, Error, { credentials?: { access_token: string } }>({
    mutationFn: v => { const body = JSON.stringify(v.credentials ? { credentials: v.credentials } : {}); delete v.credentials; return fetchApi(`/api/sources/${id}/test`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, timeoutMs: 60000 }); },
  });
}
export function useHsProperties(id: number) {
  return useMutation<HsProperties, Error, void>({ mutationFn: () => fetchApi(`/api/sources/${id}/hubspot-properties`, { timeoutMs: 60000 }) });
}
export function useHsDryRun(id: number) {
  return useMutation<HsDryRun, Error, void>({ mutationFn: () => json(`/api/sources/${id}/dry-run`, 'POST', {}, 120000) });
}

export function useHsDestinationTest() {
  return useMutation<HsTest, Error, number>({ mutationFn: id => json(`/api/destinations/${id}/test`, 'POST', {}, 60000) });
}
export function useHsDestinationRun() {
  const client = useQueryClient();
  return useMutation<{ run_id: number; job_id: number; status: string }, Error, number>({
    mutationFn: id => json(`/api/destinations/${id}/run`, 'POST', {}),
    onSettled: () => { for (const k of ['activation-runs', 'activation-alerts', 'activations', 'destinations']) client.invalidateQueries({ queryKey: [k] }); },
  });
}
export function useHsDestinationPreview(id: number | null) {
  return useQuery<HsPreview>({ queryKey: ['destinations', id, 'hubspot-preview'], enabled: id !== null, retry: false, queryFn: ({ signal }) => fetchApi(`/api/destinations/${id}/hubspot-preview`, { signal }) });
}
