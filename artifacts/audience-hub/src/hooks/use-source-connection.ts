import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';

/** Pull-connection kinds accepted by backend/app/source_api.py. */
export const PULL_KINDS = ['sftp_jsonl', 'snowflake', 'zeta_exit'] as const;
export type PullKind = (typeof PULL_KINDS)[number];
export const isPullKind = (kind: string): kind is PullKind => (PULL_KINDS as readonly string[]).includes(kind);

export const FEEDS = ['people', 'events', 'resources', 'models', 'enrichment', 'lookups'] as const;
export type Feed = (typeof FEEDS)[number];
export const RESOURCE_TYPES = ['source_codes', 'products', 'vod_titles'] as const;
export const SFTP_FOLDERS = ['/users/', '/resources/', '/events_non_real_time/'] as const;

export type FeedMapping = {
  id: string;
  feed: Feed;
  folder?: string;
  pattern?: string;
  table?: string;
  query?: string;
  cursor_column?: string;
  resource_type?: string;
};

export type FeedSchedule = {
  frequency: 'hourly' | 'daily' | 'monthly' | 'manual';
  hour?: number;
  day?: number;
  timezone?: 'America/New_York';
};

export type ConnectionConfig = {
  mappings: FeedMapping[];
  schedules: Partial<Record<Feed, FeedSchedule>>;
};

/** GET response. Credentials are never echoed; only a configured flag. */
export type SourceConnection = {
  configured: boolean;
  has_credentials: boolean;
  config: ConnectionConfig;
  default_schedules: Partial<Record<Feed, FeedSchedule>>;
  recovery_pending: boolean;
};

export type DryRunPreview = { mapping_id: string; rows_checked: number; fields: string[]; rows_rejected: number };
export type DryRunResult = { limit_per_mapping: number; items: DryRunPreview[] };
export type PullResult = { job_id: number | string; status: string };
export type PullCounts = { imports_total: number; imports_completed: number; rows_total: number; rows_ok: number; rows_rejected: number };
export type PullHistoryItem = {
  job_id: number; status: string; feed: Feed | null; attempts: number; max_attempts: number;
  created_at: string | null; run_after: string | null; started_at: string | null; finished_at: string | null; heartbeat_at: string | null;
  counts: PullCounts; error: string | null;
};
export const ACTIVE_PULL_STATUSES = ['queued', 'running'];
const pullsKey = (id: number) => ['sources', id, 'pulls'] as const;

/** Durable pull history; polls every 5s while any pull is queued/running. */
export function useSourcePulls(id: number, enabled = true) {
  return useQuery<{ items: PullHistoryItem[] }>({
    queryKey: pullsKey(id),
    enabled,
    retry: false,
    queryFn: ({ signal }) => fetchApi(`/api/sources/${id}/pulls?limit=50`, { signal, timeoutMs: 15000 }),
    refetchInterval: q => (q.state.data?.items?.some(i => ACTIVE_PULL_STATUSES.includes(i.status)) ? 5000 : false),
  });
}

const key = (id: number) => ['sources', id, 'connection'] as const;

export function useSourceConnection(id: number, enabled = true) {
  return useQuery<SourceConnection>({
    queryKey: key(id),
    enabled,
    retry: false,
    // Fresh each open; never persisted beyond the in-memory query cache.
    gcTime: 0,
    queryFn: ({ signal }) => fetchApi(`/api/sources/${id}/connection`, { signal, timeoutMs: 15000 }),
  });
}

export function useSaveSourceConnection(id: number) {
  const client = useQueryClient();
  return useMutation<SourceConnection, Error, { config: ConnectionConfig; credentials?: Record<string, unknown> }>({
    // Credentials omitted entirely when unchanged so the server keeps the encrypted blob.
    mutationFn: ({ config, credentials }) =>
      fetchApi(`/api/sources/${id}/connection`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(credentials ? { config, credentials } : { config }),
      }),
    onSuccess: data => client.setQueryData(key(id), data),
  });
}

export function useSourceDryRun(id: number) {
  return useMutation<DryRunResult, Error, { feed?: Feed | null }>({
    mutationFn: ({ feed }) =>
      fetchApi(`/api/sources/${id}/dry-run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ feed: feed ?? null }),
      }),
  });
}

export function useSourcePull(id: number) {
  const client = useQueryClient();
  return useMutation<PullResult, Error, { feed?: Feed | null }>({
    mutationFn: ({ feed }) =>
      fetchApi(`/api/sources/${id}/pull`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ feed: feed ?? null }),
      }),
    onSettled: () => client.invalidateQueries({ queryKey: pullsKey(id) }),
  });
}
