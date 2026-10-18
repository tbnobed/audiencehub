import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiUrl, fetchApi } from '@/lib/api';
import type { ActivationAlert, BootstrapResult, Activation, ActivationRun, ActivationWrite, Destination, DestinationWrite, ExportField } from '@/lib/activations';
const send = <T,>(url: string, method: string, body?: unknown) => fetchApi(url, { method, headers: { 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) }) as Promise<T>;
export function useDestinations() { return useQuery({ queryKey: ['destinations'], queryFn: () => fetchApi('/api/destinations') as Promise<{ items: Destination[] }>, refetchInterval: 10000, refetchIntervalInBackground: false }); }
export function useActivationFields() { return useQuery({ queryKey: ['activation-fields'], queryFn: () => fetchApi('/api/activations/fields') as Promise<{ fields: ExportField[] }>, staleTime: 60000 }); }
export function useActivations() { return useQuery({ queryKey: ['activations'], queryFn: () => fetchApi('/api/activations') as Promise<{ items: Activation[] }>, refetchInterval: 5000, refetchIntervalInBackground: false }); }
export function useActivation(id?: number) { return useQuery({ queryKey: ['activation', id], enabled: !!id, queryFn: () => fetchApi(`/api/activations/${id}`) as Promise<Activation>, refetchInterval: 5000, refetchIntervalInBackground: false }); }
export function useActivationRuns(id?: number) { return useQuery({ queryKey: ['activation-runs', id], enabled: !!id, queryFn: () => fetchApi(`/api/activations/${id}/runs`) as Promise<{ items: ActivationRun[] }>, refetchInterval: 3000, refetchIntervalInBackground: false }); }
/** Durable failed-run alerts (server returns the latest 100 failed runs). */
export function useActivationAlerts(enabled = true) { return useQuery({ queryKey: ['activation-alerts'], enabled, queryFn: () => fetchApi('/api/activation-alerts') as Promise<{ items: ActivationAlert[] }>, refetchInterval: 30000, refetchIntervalInBackground: false }); }
export function useBootstrapActivations() {
  const client = useQueryClient();
  return useMutation({ mutationFn: () => send<BootstrapResult>('/api/activations/bootstrap', 'POST', {}), onSuccess: async () => { await Promise.all(['activations', 'destinations'].map(key => client.invalidateQueries({ queryKey: [key] }))); } });
}
export function useDestinationActions() {
  const client = useQueryClient();
  const refresh = () => client.invalidateQueries({ queryKey: ['destinations'] });
  return {
    save: useMutation({ mutationFn: ({ id, ...body }: DestinationWrite & { id?: number }) => send<Destination>(id ? `/api/destinations/${id}` : '/api/destinations', id ? 'PATCH' : 'POST', body), onSuccess: refresh }),
    archive: useMutation({ mutationFn: (id: number) => send(`/api/destinations/${id}`, 'DELETE'), onSuccess: refresh }),
    test: useMutation({ mutationFn: (id: number) => send<{ delivered: boolean; mode?: 'connection_only' }>(`/api/destinations/${id}/test`, 'POST', {}) }),
  };
}
export function useActivationActions() {
  const client = useQueryClient();
  const refresh = async () => { await Promise.all(['activations', 'activation', 'activation-runs', 'activation-alerts'].map(key => client.invalidateQueries({ queryKey: [key] }))); };
  return {
    save: useMutation({ mutationFn: ({ id, ...body }: ActivationWrite & { id?: number }) => send<Activation>(id ? `/api/activations/${id}` : '/api/activations', id ? 'PATCH' : 'POST', body), onSuccess: refresh }),
    archive: useMutation({ mutationFn: (id: number) => send(`/api/activations/${id}`, 'DELETE'), onSuccess: refresh }),
    run: useMutation({ mutationFn: (id: number) => send<{ run_id: number; job_id: number }>(`/api/activations/${id}/run`, 'POST', {}), onSuccess: refresh }),
  };
}
/** Authenticated browser download; response streams, never loads a CSV into JS memory. */
export function activationDownloadUrl(runId: number) { return apiUrl(`/api/activation-runs/${runId}/download`); }
