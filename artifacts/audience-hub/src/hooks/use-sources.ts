import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';

export type Source = {
  id: number;
  key: string;
  name: string;
  kind: string;
  priority: number;
  record_types: string[];
  is_active: boolean;
  has_write_key?: boolean;
  settings: Record<string, any>;
};

export function useSources() {
  return useQuery({
    queryKey: ['sources'],
    queryFn: () => fetchApi('/api/sources').then(res => res as { items: Source[] }),
  });
}

export function useCreateSource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: Partial<Source>) =>
      fetchApi('/api/sources', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['sources'] }),
  });
}

export function useUpdateSource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: number; data: Partial<Source> }) =>
      fetchApi(`/api/sources/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['sources'] }),
  });
}

export function useDeleteSource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) =>
      fetchApi(`/api/sources/${id}`, { method: 'DELETE' }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['sources'] }),
  });
}

export type SourceEvent = {
  id: number | string;
  name?: string;
  type?: string;
  profile_id?: number | null;
  occurred_at?: string;
  received_at?: string;
  timestamp_clamped?: boolean;
};

export function useSourceEvents(id: number, enabled: boolean) {
  return useQuery<{ items: SourceEvent[]; next_cursor: number | null }>({
    queryKey: ['sources', id, 'events'],
    queryFn: ({ signal }) => fetchApi(`/api/sources/${id}/events?limit=50`, { signal, timeoutMs: 10000 }),
    enabled,
    refetchInterval: 3000,
    retry: false,
  });
}

export function useSDKManifest(enabled: boolean) {
  return useQuery<{ sha256: string }>({
    queryKey: ['sdk-manifest'],
    enabled,
    staleTime: Infinity,
    retry: false,
    queryFn: async ({ signal }) => {
      const response = await fetch(`${import.meta.env.BASE_URL}sdk/manifest.json`, {
        signal: AbortSignal.any([signal, AbortSignal.timeout(10000)]), cache: 'no-store', credentials: 'omit',
      });
      if (!response.ok) throw new Error(`SDK manifest unavailable (HTTP ${response.status}). Build the SDK and serve /sdk/manifest.json.`);
      const manifest = await response.json();
      if (manifest.file !== 'ah.js' || !/^[a-f0-9]{64}$/.test(manifest.sha256)) throw new Error('SDK manifest has an invalid content hash.');
      return { sha256: manifest.sha256 };
    },
  });
}
