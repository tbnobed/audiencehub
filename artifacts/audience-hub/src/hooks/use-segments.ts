import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';
import type { SegmentFieldCatalog, Segment, SegmentDefinition, SegmentField, SegmentHistory, SegmentPreview, SegmentSample, SegmentWrite, PartnerSample } from '@/lib/segments';

const base = '/api/segments';
const post = <T,>(url: string, body: unknown, signal?: AbortSignal) => fetchApi(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal, timeoutMs: 15000 }) as Promise<T>;
export function useSegmentFields() { return useQuery({ queryKey: ['segment-fields'], queryFn: () => fetchApi(`${base}/fields`) as Promise<SegmentFieldCatalog>, staleTime: 300000 }); }
export function useSegments(status = '', owner = '') { return useQuery({ queryKey: ['segments', status, owner], queryFn: () => fetchApi(`${base}?${new URLSearchParams({ status, owner })}`) as Promise<{ items: Segment[] }>, staleTime: 10000, refetchInterval: 10000, refetchIntervalInBackground: false }); }
export function useSegment(id?: number) { return useQuery({ queryKey: ['segment', id], enabled: !!id, queryFn: () => fetchApi(`${base}/${id}`) as Promise<Segment>, refetchInterval: 10000, refetchIntervalInBackground: false }); }
export function useSegmentHistory(id?: number) { return useQuery({ queryKey: ['segment-history', id], enabled: !!id, queryFn: () => fetchApi(`${base}/${id}/count-history`) as Promise<{ items: SegmentHistory[] }>, refetchInterval: 10000, refetchIntervalInBackground: false }); }
export function useSegmentPreview(definition: SegmentDefinition | null) { return useQuery({ queryKey: ['segment-preview', definition], enabled: !!definition, queryFn: ({ signal }) => post<SegmentPreview>(`${base}/preview`, { definition }, signal), retry: false }); }
export function useSegmentSample(definition: SegmentDefinition | null) { return useQuery({ queryKey: ['segment-sample', definition], enabled: !!definition, queryFn: ({ signal }) => post<SegmentSample>(`${base}/sample`, { definition }, signal), retry: false }); }
export function useSegmentMembers(id?: number, cursor = '') { return useQuery({ queryKey: ['segment-members', id, cursor], enabled: !!id, queryFn: () => fetchApi(`${base}/${id}/members?${new URLSearchParams({ cursor })}`) as Promise<{ items: PartnerSample[]; next_cursor: string | null }>, refetchInterval: 10000, refetchIntervalInBackground: false }); }
export function useSegmentActivity(id?: number) { return useQuery({ queryKey: ['segment-activity', id], enabled: !!id, queryFn: () => fetchApi(`${base}/${id}/activity`) as Promise<{ audit: { action: string; created_at: string; user_id: number | null }[]; activations: { id: number; name: string; status: string }[] }> }); }
export function useSegmentTypeahead(field: string, q = '') { return useQuery({ queryKey: ['segment-typeahead', field, q], enabled: !!field, queryFn: () => fetchApi(`${base}/typeahead?${new URLSearchParams({ field, q })}`) as Promise<{ items: string[] }> }); }
export function useSegmentDelta() { return useMutation({ mutationFn: (body: { definition: SegmentDefinition; path: string }) => post<{ count: number; without_count: number; delta: number }>(`${base}/delta`, body) }); }
export function useSegmentExplain() { return useMutation({ mutationFn: (definition: SegmentDefinition) => post<{ plan: unknown }>(`${base}/explain`, { definition }) }); }
export function useSegmentHistoryExport() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => post<{ filename: string; csv: string }>(`${base}/${id}/count-history/export`, {}),
    onSuccess: async (_data, id) => { await client.invalidateQueries({ queryKey: ['segment-activity', id] }); },
  });
}
export function useSegmentActions() {
  const client = useQueryClient();
  const refresh = async () => { await Promise.all(['segments', 'segment', 'segment-history', 'segment-members', 'segment-activity', 'segment-preview', 'segment-sample'].map(key => client.invalidateQueries({ queryKey: [key] }))); };
  const save = useMutation({ mutationFn: ({ id, ...body }: SegmentWrite & { id?: number }) => fetchApi(id ? `${base}/${id}` : base, { method: id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }) as Promise<Segment>, onSuccess: refresh });
  const archive = useMutation({ mutationFn: (id: number) => fetchApi(`${base}/${id}`, { method: 'DELETE' }), onSuccess: refresh });
  const materialize = useMutation({ mutationFn: (id: number) => post<{ job_id: number }>(`${base}/${id}/materialize`, {}), onSuccess: refresh });
  return { save, archive, materialize };
}