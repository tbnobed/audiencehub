import { useQuery } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';

export type SourceJob = {
  job_id: number; source_id: number; source_name: string; status: string; phase: string | null; rows_read: number | null;
  counts: { imports_total: number; imports_completed: number; rows_total: number; rows_ok: number; rows_rejected: number };
  created_at: string | null; started_at: string | null; finished_at: string | null; error: string | null;
};
export const ACTIVE_JOB_STATUSES = ['queued', 'running'];

export function useSourceJobs(enabled = true) {
  return useQuery<{ items: SourceJob[] }>({
    queryKey: ['source-jobs'], enabled, retry: false,
    queryFn: ({ signal }) => fetchApi('/api/source-jobs', { signal, timeoutMs: 15000 }),
    refetchInterval: q => (q.state.data?.items?.some(i => ACTIVE_JOB_STATUSES.includes(i.status)) ? 3000 : false),
  });
}
