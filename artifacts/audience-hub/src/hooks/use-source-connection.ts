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
  target_type?: TargetType;
  columns?: Record<string, string>;
  schedule?: MappingSchedule;
};

export const TARGET_TYPES = ['people', 'donation', 'donation_return', 'order_shipped', 'vod_viewed', 'source_codes', 'products', 'vod_titles', 'model_scores'] as const;
export type TargetType = (typeof TARGET_TYPES)[number];
export type MappingSchedule = { frequency: 'manual' | 'hourly' | 'daily' | 'monthly'; hour?: number; minute?: number; day?: number; timezone: 'America/New_York' };
/** Derive feed (and resource_type) from a target type. */
export function deriveFeed(t: TargetType): { feed: Feed; resource_type?: string } {
  if (t === 'people') return { feed: 'people' };
  if (t === 'model_scores') return { feed: 'models' };
  if (t === 'source_codes' || t === 'products' || t === 'vod_titles') return { feed: 'resources', resource_type: t };
  return { feed: 'events' };
}

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
  connection_fields?: Record<string, string>;
};

export type DryRunPreview = { mapping_id: string; rows_checked: number; fields: string[]; rows_rejected: number; rows?: Record<string, unknown>[]; identity_keys?: string[]; suggested_columns?: Record<string, string> };
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
    mutationFn: variables => {
      const body = JSON.stringify(variables);
      // React Query retains mutation variables. Remove write-only credentials
      // before the first await; only the in-flight request owns the serialized body.
      delete variables.credentials;
      return fetchApi(`/api/sources/${id}/connection`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body,
      });
    },
    onSuccess: data => client.setQueryData(key(id), data),
  });
}

const jsonPost = (url: string, body: unknown) =>
  fetchApi(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

export function useSourceDryRun(id: number) {
  return useMutation<DryRunResult, Error, { mapping_id?: string | null }>({
    mutationFn: ({ mapping_id }) => jsonPost(`/api/sources/${id}/dry-run`, mapping_id ? { mapping_id } : {}),
  });
}

export function useSourcePull(id: number) {
  const client = useQueryClient();
  return useMutation<PullResult, Error, { mapping_id?: string | null }>({
    mutationFn: ({ mapping_id }) => jsonPost(`/api/sources/${id}/pull`, mapping_id ? { mapping_id } : {}),
    onSettled: () => { client.invalidateQueries({ queryKey: pullsKey(id) }); client.invalidateQueries({ queryKey: ['imports'] }); },
  });
}

/** Credentials are sent once and never cached; omit to test the saved source. */
export function useSourceTest(id: number) {
  return useMutation<{ version: string; tables: string[] }, Error, { credentials?: Record<string, unknown> }>({
    mutationFn: variables => {
      const body = JSON.stringify(variables);
      delete variables.credentials;
      return fetchApi(`/api/sources/${id}/test`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body, timeoutMs: 60000,
      });
    },
  });
}

export type ColumnsResult = { columns: string[]; suggested_columns: Record<string, string> };
export function useSourceColumns(id: number) {
  return useMutation<ColumnsResult, Error, { table: string }>({
    mutationFn: ({ table }) => fetchApi(`/api/sources/${id}/columns?table=${encodeURIComponent(table)}`, { timeoutMs: 60000 }),
  });
}
