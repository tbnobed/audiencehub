import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';

export type DashboardName = 'overview' | 'giving' | 'retention' | 'engagement' | 'sources' | 'data-health';
export type ChartRow = Record<string, string | number | null>;
export type DashboardPayload = {
  range: { from: string; to: string; prior_from: string; prior_to: string };
  metrics: Record<string, { value: number; prior: number | null; change: number | null; change_pct: number | null }>;
  charts: Record<string, ChartRow[]>;
  overview?: OverviewPayload;
};
export type SparkPoint = { month: string; value: number };
export type DeltaMetric = { value: number; prior: number; change: number; change_pct: number | null; delta_label: string | null };
export type KpiMetric = DeltaMetric & { sparkline: SparkPoint[] };
export type RetentionKpi = KpiMetric & { denominator: number; retained: number; prior_denominator: number };
export type OverviewPayload = {
  kpis: { giving: KpiMetric; active_partners: KpiMetric; retention_yoy: RetentionKpi; average_gift: KpiMetric };
  stats: {
    profiles: { value: number };
    recurring_partners: DeltaMetric;
    email_opted_in: { value: number; percentage: number };
    lapsing: DeltaMetric;
  };
  monthly_giving: { month: string; from: string; to: string; prior_from: string; prior_to: string; amount: number; prior_amount: number; gifts: number }[];
  top_two_month_share: number | null;
  campaigns: { campaign: string; share: number; gifts: number; average_gift: number; amount: number }[];
  campaigns_href: string;
  partner_status: { givers: number; statuses: { status: string; count: number; share: number }[]; prospects: number };
  attention: { severity: 'error' | 'warning' | 'notice' | 'healthy'; title: string; explanation: string; href: string | null }[];
};
export type DashboardSettings = { fiscal_year_start_month: number };

export function useDashboard(name: DashboardName, from: string, to: string, enabled = true) {
  return useQuery({
    enabled,
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