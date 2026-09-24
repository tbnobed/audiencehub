import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';

export type AutoBlocklisted = {
  id: number;
  type: string;
  value: string;
  reason: string;
  created_at: string;
};

export type RecentImport = {
  id: number;
  filename: string;
  record_type: string;
  status: string;
  rows_total: number;
  rows_ok: number;
  rows_rejected: number;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  source_id: number;
  source_key: string;
  source_name: string;
};

export type DataHealthResponse = {
  pending_count: number;
  merges_per_day: { day: string; count: number }[];
  blocklist_hits: number;
  auto_blocklisted: AutoBlocklisted[];
  rejected_rows: number;
  recent_imports: RecentImport[];
};

export function useDataHealth() {
  return useQuery({
    queryKey: ['data-health'],
    queryFn: () => fetchApi('/api/data-health') as Promise<DataHealthResponse>,
  });
}

export function useApproveBlocklist() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => fetchApi(`/api/data-health/blocklist/${id}/approve`, { method: 'POST' }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['data-health'] }),
  });
}

export function useUnblockBlocklist() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => fetchApi(`/api/data-health/blocklist/${id}/unblock`, { method: 'POST' }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['data-health'] }),
  });
}
