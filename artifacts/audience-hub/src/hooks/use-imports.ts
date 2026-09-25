import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';

export type ImportJob = {
  id: number;
  source_id: number;
  filename: string;
  record_type: string;
  status: 'uploaded' | 'mapped' | 'running' | 'completed' | 'failed';
  rows_total: number;
  rows_ok: number;
  rows_rejected: number;
  rejected_report_reviewed?: boolean;
  last_committed_record_number?: number;
  warning_count?: number;
  job_error?: string | null;
  warning_counts?: Record<string, number>;
  mapping: Record<string, string>;
  created_at: string;
  progress?: { done?: number; total?: number; message?: string };
  valid?: boolean;
  is_valid?: boolean;
  validation_errors?: Array<string | { row?: number; field?: string; message?: string }>;
  errors?: Array<string | { row?: number; field?: string; message?: string }>;
  validation?: {
    valid?: boolean;
    scope?: number;
    errors?: Array<string | { row?: number; field?: string; message?: string }>;
  };
  error_report_available?: boolean;
  has_error_report?: boolean;
  error_report_url?: string | null;
  report_url?: string | null;
  error_report?: string | null | Record<string, unknown>;
  report_available?: boolean;
  report?: string | null | Record<string, unknown>;
};

export function useImports() {
  return useQuery({
    queryKey: ['imports'],
    queryFn: () => fetchApi('/api/imports').then(res => res as { items: ImportJob[] }),
    refetchInterval: (query) => {
      const imports = query.state.data as { items: ImportJob[] } | undefined;
      return imports?.items.some(job => job.status === 'running') ? 2000 : false;
    },
  });
}

export function useImport(id: number) {
  return useQuery({
    queryKey: ['imports', id],
    queryFn: () => fetchApi(`/api/imports/${id}`).then(res => res as ImportJob),
    enabled: !!id,
    refetchInterval: (query) => {
        const state = query.state?.data as ImportJob | undefined;
        if (state && ['uploaded', 'mapped', 'running'].includes(state.status)) {
            return 2000;
        }
        return false;
    }
  });
}

export function useCreateImport() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ source_id, record_type, file, confirm_duplicate }: { source_id: number; record_type: string; file: File; confirm_duplicate?: boolean }) => {
      const formData = new FormData();
      formData.append('source_id', source_id.toString());
      formData.append('record_type', record_type);
      formData.append('file', file);
      let url = '/api/imports';
      if (confirm_duplicate) {
        url += '?confirm_duplicate=true';
      }
      return fetchApi(url, {
        method: 'POST',
        body: formData,
      });
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['imports'] }),
  });
}

export function useImportPreview(id: number) {
  return useQuery({
    queryKey: ['imports', id, 'preview'],
    queryFn: () => fetchApi(`/api/imports/${id}/preview`).then(res => res as { headers: string[], rows: Record<string, any>[], suggested_mapping: Record<string, string> }),
    enabled: !!id,
  });
}

export function useUpdateMapping() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, mapping }: { id: number; mapping: Record<string, string> }) =>
      fetchApi(`/api/imports/${id}/mapping`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ columns: mapping, options: {} }),
      }),
    onSuccess: (_, { id }) => queryClient.invalidateQueries({ queryKey: ['imports', id] }),
  });
}

export function useValidateImport() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) =>
      fetchApi(`/api/imports/${id}/validate`, { method: 'POST' }),
    onSuccess: (_, id) => queryClient.invalidateQueries({ queryKey: ['imports', id] }),
  });
}

export function useRunImport() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) =>
      fetchApi(`/api/imports/${id}/run`, { method: 'POST' }),
    onSuccess: (_, id) => {
      // The accepted response commits the running state before the detail poll
      // starts. Preserve the last-known job while the next GET is in flight.
      queryClient.setQueryData<ImportJob>(['imports', id], current =>
        current ? { ...current, status: 'running',
          progress: {
            done: Math.max(0, (current.last_committed_record_number || 1) - 1),
            total: current.rows_total,
            message: (current.last_committed_record_number || 1) > 1 ? 'Preparing resume…' : 'Preparing validation…',
          } } : current
      );
      queryClient.invalidateQueries({ queryKey: ['imports'] });
    },
  });
}

export async function downloadImportErrors(id: number) {
  const url = `/api/imports/${id}/errors.csv`;
  const response = await fetch(url, { credentials: 'include' });
  if (!response.ok) {
    throw new Error('Failed to download error report');
  }
  const blob = await response.blob();
  const downloadUrl = window.URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.style.display = 'none';
  a.href = downloadUrl;
  a.download = `import-${id}-errors.csv`;
  document.body.appendChild(a);
  a.click();
  window.URL.revokeObjectURL(downloadUrl);
  document.body.removeChild(a);
}
