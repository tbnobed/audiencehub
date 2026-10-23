import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';
import type { MergeHistoryItem } from '@/lib/identity';

export type IdentityHealth = {
  profiles: number;
  missing_master_ids: number;
  conflicts: number;
  duplicate_hubspot_profiles: number;
  hash_only_profiles: number;
  key_coverage: { key_type: string; profiles: number }[];
  tbn_plus_identity: {
    total: number; okta: number; fusionauth: number; both: number;
    okta_only: number; fusionauth_only: number; neither: number;
  };
  review_items: { id: number; source_record_id: number | null; reason: string; created_at: string; master_id: string | null }[];
  duplicate_hubspot: { master_id: string; hubspot_record_ids: string[]; status: string }[];
};

export type MasterIdentity = {
  requested_master_id: string;
  master_id: string;
  profile_id: number | null;
  keys: { key_type: string; key_value: string; source_id: number | null; first_seen_at: string | null; last_seen_at: string | null; status: string | null; method?: string | null; confidence?: number | null; rule?: string | null; source_name?: string | null }[];
  merge_history: MergeHistoryItem[];
};

export type UnmergeResult = { merge_id: number; retained_master_id: string; new_master_id: string; retained_side: string };

export function useIdentityHealth() {
  return useQuery({
    queryKey: ['identity-health'],
    queryFn: () => fetchApi('/api/identity/health') as Promise<IdentityHealth>,
  });
}

export function useMasterIdentity(masterId: string | null | undefined) {
  return useQuery({
    queryKey: ['identity-master', masterId],
    queryFn: () => fetchApi(`/api/identity/master/${encodeURIComponent(masterId!)}`) as Promise<MasterIdentity>,
    enabled: !!masterId,
  });
}

export function resolveMaster(masterId: string) {
  return fetchApi(`/api/identity/master/${encodeURIComponent(masterId)}`) as Promise<MasterIdentity>;
}

export function useUnmerge() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (mergeId: number) =>
      fetchApi(`/api/identity/merges/${mergeId}/unmerge`, { method: 'POST' }) as Promise<UnmergeResult>,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['profiles'] });
      qc.invalidateQueries({ queryKey: ['identity-master'] });
      qc.invalidateQueries({ queryKey: ['identity-health'] });
      qc.invalidateQueries({ queryKey: ['data-health'] });
    },
  });
}

export type GraphPolicy = { enabled: boolean; review_threshold: number; auto_threshold: number; device_expiry_days: number; household_surname: boolean; schedule_hour: number };
export type GraphRun = { id: number; status: string; dry_run: boolean; started_at: string | null; finished_at: string | null; counts: { auto?: number; review?: number; rejected?: number; conflicts?: number; linked?: number } | null; error: string | null; job_id: number | string | null };
export type QualityItem = { id: number; left_id: number; right_id: number; left_master_id: string | null; right_master_id: string | null; left_name: string | null; right_name: string | null; confidence: number; rule: string; status: string; verdict: string | null; merge_id: number | null; created_at: string; sampled?: boolean; left_source_record_id?: number | null; right_source_record_id?: number | null; evidence_record_ids?: (number | string)[] | null };
export type TrendItem = { day: string; source_id: number | null; source_name: string | null; candidates: number; linked: number; review: number; reviewed: number; correct: number; queue_open?: number };
export type ProfileGraph = {
  address: { number: string | null; street: string | null; unit: string | null; city: string | null; state: string | null; zip5: string | null; zip4: string | null; normalized_key: string | null; parser: string | null; vendor: string | null; vendor_result: unknown } | null;
  household: { id: number | string; members: { id: number; master_id: string | null; first_name: string | null; last_name: string | null }[] } | null;
  devices: { key_type: string; device_id: string; first_seen_at: string | null; last_seen_at: string | null; expires_at: string | null; source_id: number | null }[];
};
export type Verdict = 'match' | 'not_match' | 'unsure';

const G = '/api/identity/graph';
const json = (method: string, body: unknown) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

export const useGraphPolicy = () => useQuery({ queryKey: ['identity-graph-policy'], queryFn: () => fetchApi(`${G}/policy`) as Promise<GraphPolicy> });
export const useGraphRuns = () => useQuery({ queryKey: ['identity-graph-runs'], queryFn: () => fetchApi(`${G}/runs`) as Promise<{ items: GraphRun[] }>, refetchInterval: q => (q.state.data?.items.some(r => ['queued', 'running'].includes(r.status)) ? 5000 : false) });
export const useQualityItems = () => useQuery({ queryKey: ['identity-graph-quality'], queryFn: () => fetchApi(`${G}/quality?limit=100`) as Promise<{ items: QualityItem[] }> });
export const useGraphTrends = (o: { enabled?: boolean } = {}) => useQuery({ enabled: o.enabled ?? true, queryKey: ['identity-graph-trends'], queryFn: () => fetchApi(`${G}/trends`) as Promise<{ items: TrendItem[] }> });
export const useProfileGraph = (profileId: number, enabled = true) => useQuery({ queryKey: ['identity-graph-profile', profileId], queryFn: () => fetchApi(`${G}/profiles/${profileId}`) as Promise<ProfileGraph>, enabled: enabled && Number.isFinite(profileId) });

function useInvalidateGraph() {
  const qc = useQueryClient();
  return () => { for (const k of ['identity-graph-policy', 'identity-graph-runs', 'identity-graph-quality', 'identity-graph-trends', 'identity-graph-profile', 'identity-master', 'identity-health', 'data-health', 'profiles']) qc.invalidateQueries({ queryKey: [k] }); };
}
export function useUpdateGraphPolicy() { const inv = useInvalidateGraph(); return useMutation({ mutationFn: (p: GraphPolicy) => fetchApi(`${G}/policy`, json('PUT', p)) as Promise<GraphPolicy>, onSuccess: inv }); }
export function useStartGraphRun() { const inv = useInvalidateGraph(); return useMutation({ mutationFn: (dry_run: boolean) => fetchApi(`${G}/runs`, json('POST', { dry_run })) as Promise<{ job_id: number | string }>, onSuccess: inv }); }
export function useSampleQuality() { const inv = useInvalidateGraph(); return useMutation({ mutationFn: (count: number) => fetchApi(`${G}/quality/sample`, json('POST', { count })) as Promise<{ sampled: number }>, onSuccess: inv }); }
export function useQualityVerdict() { const inv = useInvalidateGraph(); return useMutation({ mutationFn: ({ id, verdict, note }: { id: number; verdict: Verdict; note: string }) => fetchApi(`${G}/quality/${id}/verdict`, json('POST', { verdict, note })), onSuccess: inv }); }
