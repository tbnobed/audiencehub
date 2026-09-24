import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';

export type DashboardName = 'overview' | 'giving' | 'retention' | 'engagement' | 'sources' | 'data-health';
export type ChartRow = Record<string, string | number | null>;
export type DashboardPayload = {
  range: { from: string; to: string; prior_from: string; prior_to: string };
  metrics: Record<string, { value: number; prior: number | null; change: number | null; change_pct: number | null }>;
  charts: Record<string, ChartRow[]>;
};
export type DashboardSettings = { fiscal_year_start_month: number };

export function useDashboard(name: DashboardName, from: string, to: string) {
  return useQuery({
    queryKey: ['dashboard', name, from, to],
    queryFn: () => fetchApi(`/api/dashboards/${name}?${new URLSearchParams({ from, to })}`) as Promise<DashboardPayload>,
    staleTime: 60_000,
  });
}

export function useDashboardSettings() {
  return useQuery({
    queryKey: ['dashboard-settings'],
    queryFn: () => fetchApi('/api/dashboards/settings') as Promise<DashboardSettings>,
    staleTime: 300_000,
  });
}

export function useAdminSettings() {
  return useQuery({
    queryKey: ['admin-settings'],
    queryFn: () => fetchApi('/api/admin/settings') as Promise<DashboardSettings>,
    staleTime: 300_000,
  });
}

export function useUpdateAdminSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (fiscalYearStartMonth: number) => fetchApi('/api/admin/settings', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fiscal_year_start_month: fiscalYearStartMonth }),
    }) as Promise<DashboardSettings>,
    onSuccess: async settings => {
      queryClient.setQueryData(['admin-settings'], settings);
      queryClient.setQueryData(['dashboard-settings'], settings);
      await queryClient.invalidateQueries({ queryKey: ['dashboard-settings'] });
    },
  });
}

export function dashboardCsvUrl(name: DashboardName, chart: string, from: string, to: string) {
  return `/api/dashboards/${name}/${chart}/csv?${new URLSearchParams({ from, to })}`;
}