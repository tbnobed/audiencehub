import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';
import type { ConsentChannel, ConsentStatus, PartnerConsent, Suppression, DeletionPreview, DeletionRequest, PrivacyIdentifier } from '@/lib/privacy';

const post = <T,>(path: string, body: unknown) => fetchApi(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }) as Promise<T>;
export function usePartnerConsents(id?: number) { return useQuery({ queryKey: ['partner-consents', id], enabled: !!id, queryFn: () => fetchApi(`/api/profiles/${id}/consents`) as Promise<{ items: PartnerConsent[] }>, refetchInterval: 10000 }); }
export function useConsentChange(id: number) {
  const client = useQueryClient();
  return useMutation({ mutationFn: (body: { channel: ConsentChannel; status: ConsentStatus; note: string }) => post<PartnerConsent>(`/api/profiles/${id}/consents`, body), onSuccess: () => { client.invalidateQueries({ queryKey: ['partner-consents', id] }); client.invalidateQueries({ queryKey: ['profiles'] }); client.invalidateQueries({ queryKey: ['segment-preview'] }); client.invalidateQueries({ queryKey: ['segment-sample'] }); } });
}
export function useSuppressions() { return useQuery({ queryKey: ['suppressions'], queryFn: () => fetchApi('/api/admin/suppressions') as Promise<{ items: Suppression[] }>, refetchInterval: 10000 }); }
export function useSuppressionActions() {
  const client = useQueryClient(); const refresh = () => { client.invalidateQueries({ queryKey: ['suppressions'] }); client.invalidateQueries({ queryKey: ['partner-consents'] }); client.invalidateQueries({ queryKey: ['profiles'] }); };
  return {
    add: useMutation({ mutationFn: (body: PrivacyIdentifier) => post<Suppression>('/api/admin/suppressions', body), onSuccess: refresh }),
    remove: useMutation({ mutationFn: (id: number) => fetchApi(`/api/admin/suppressions/${id}`, { method: 'DELETE' }), onSuccess: refresh }),
  };
}
export function useDeletionRequests() { return useQuery({ queryKey: ['deletion-requests'], queryFn: () => fetchApi('/api/admin/deletion-requests') as Promise<{ items: DeletionRequest[]; two_person: boolean; backup_keep_days: number }>, refetchInterval: 3000, refetchIntervalInBackground: false }); }
export function useDeletionActions() {
  const client = useQueryClient(); const refresh = () => client.invalidateQueries({ queryKey: ['deletion-requests'] });
  return {
    preview: useMutation({ mutationFn: (body: PrivacyIdentifier) => post<DeletionPreview>('/api/admin/deletion-requests/preview', body) }),
    create: useMutation({ mutationFn: (body: PrivacyIdentifier) => post<DeletionRequest>('/api/admin/deletion-requests', body), onSuccess: refresh }),
    approve: useMutation({ mutationFn: (id: number) => post<{ job_id: number; request: DeletionRequest }>(`/api/admin/deletion-requests/${id}/approve`, {}), onSuccess: refresh }),
  };
}
