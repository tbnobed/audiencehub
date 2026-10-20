import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';

export type MetaConfig = {
  page_id: string; app_id: string; graph_version: string; backfill_days: number;
  field_mapping: Record<string, string>;
  consent_form_id: string; consent_field: string; consent_text: string; consent_channel: string; consent_true_values: string[];
  ad_account_id: string;
};
export type MetaConnection = { configured: boolean; config: MetaConfig; has_credentials: boolean; webhook_path: string };
export type MetaCredentials = { page_access_token: string; app_secret: string; verify_token: string; ads_access_token?: string };
export type MetaAudience = { id: number; source_id: number; meta_audience_id: string; name: string; destination_id: number; created_at: string; last_refreshed_at: string | null };
export type MetaResultItem = { date_start: string; date_stop: string; adset_id: string; adset_name: string; spend: number | string; reach: number; conversions: number; currency: string; attribution_note: string | null };
export type MetaJob = { id: number; status: string; error: string | null; created_at: string };
export type MetaResults = { audiences: MetaAudience[]; items: MetaResultItem[]; jobs: MetaJob[] };

const json = (method: string, body: unknown) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const active = (s: string) => ['queued', 'running'].includes(s);

export const useMetaConnection = (id: number) => useQuery({ queryKey: ['meta-connection', id], queryFn: () => fetchApi(`/api/meta/sources/${id}/connection`) as Promise<MetaConnection> });
export type MetaRuns = { items: MetaJob[]; leads: { queued: number; completed: number; suppressed: number } };
export const useMetaRuns = (id: number) => useQuery({
  queryKey: ['meta-runs', id],
  queryFn: () => fetchApi(`/api/meta/sources/${id}/runs`) as Promise<MetaRuns>,
  refetchInterval: q => (q.state.data?.items.some(j => active(j.status)) ? 5000 : false),
});
export const useMetaForms = (id: number, enabled: boolean) => useQuery({ queryKey: ['meta-forms', id], enabled, queryFn: () => fetchApi(`/api/meta/sources/${id}/forms`) as Promise<{ items: { id: string; name: string; status: string }[] }> });
export const useMetaSegmentResults = (segmentId: number) => useQuery({
  queryKey: ['meta-results', segmentId],
  queryFn: () => fetchApi(`/api/meta/segments/${segmentId}/results`) as Promise<MetaResults>,
  refetchInterval: q => (q.state.data?.jobs.some(j => active(j.status)) ? 5000 : false),
});

function useInvalidateMeta() {
  const qc = useQueryClient();
  return () => { for (const k of ['meta-connection', 'meta-forms', 'meta-runs', 'meta-results', 'sources', 'source-jobs', 'destinations', 'segments', 'segment', 'segment-activity', 'segment-history', 'activations', 'activation-runs', 'imports', 'profiles', 'data-health']) qc.invalidateQueries({ queryKey: [k] }); };
}
export function useSaveMetaConnection(id: number) { const inv = useInvalidateMeta(); return useMutation({ mutationFn: (b: { config: MetaConfig; credentials?: Partial<MetaCredentials> }) => fetchApi(`/api/meta/sources/${id}/connection`, json('PUT', b)) as Promise<MetaConnection>, onSuccess: inv }); }
export function useConnectMeta(id: number) { const inv = useInvalidateMeta(); return useMutation({ mutationFn: () => fetchApi(`/api/meta/sources/${id}/connect`, { method: 'POST' }) as Promise<{ page: { id: string; name: string }; job_id: number | string; subscribed: boolean }>, onSuccess: inv }); }
export function useRunMeta(id: number) { const inv = useInvalidateMeta(); return useMutation({ mutationFn: () => fetchApi(`/api/meta/sources/${id}/run`, { method: 'POST' }) as Promise<{ job_id: number | string }>, onSuccess: inv }); }
export function useCreateMetaAudience(segmentId: number) { const inv = useInvalidateMeta(); return useMutation({ mutationFn: (b: { source_id: number; name: string }) => fetchApi(`/api/meta/segments/${segmentId}/audiences`, json('POST', b)) as Promise<{ meta_audience_id: string; destination_id: number }>, onSuccess: inv }); }
export function useRefreshMetaResults(segmentId: number) { const inv = useInvalidateMeta(); return useMutation({ mutationFn: () => fetchApi(`/api/meta/segments/${segmentId}/results/refresh`, { method: 'POST' }) as Promise<{ job_id: number | string }>, onSuccess: inv }); }
