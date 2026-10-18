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
  review_items: { id: number; source_record_id: number | null; reason: string; created_at: string; master_id: string | null }[];
  duplicate_hubspot: { master_id: string; hubspot_record_ids: string[]; status: string }[];
};

export type MasterIdentity = {
  requested_master_id: string;
  master_id: string;
  profile_id: number | null;
  keys: { key_type: string; key_value: string; source_id: number | null; first_seen_at: string | null; last_seen_at: string | null; status: string | null }[];
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
