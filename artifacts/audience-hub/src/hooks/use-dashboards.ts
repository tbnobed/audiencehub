import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';
import { validateCustomRange, type DashboardDefaultRange } from '@/lib/date-range';

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
export type DashboardSettings = { fiscal_year_start_month: number } & DashboardDefaultRange;
export type DashboardSettingsPatch = { fiscal_year_start_month?: number } & (
  DashboardDefaultRange | {
    dashboard_default_preset?: never;
    dashboard_default_from?: never;
    dashboard_default_to?: never;
  }
);
export type OverviewCards = {
  kpis: { range: DashboardPayload['range'] } & Pick<OverviewPayload, 'kpis' | 'stats'>;
  'giving-by-month': { range: DashboardPayload['range']; giving_by_month: Pick<OverviewPayload, 'monthly_giving' | 'top_two_month_share'> };
  'needs-attention': Pick<OverviewPayload, 'attention'>;
  campaigns: { top_campaigns: Pick<OverviewPayload, 'campaigns' | 'campaigns_href'> };
  'partner-status': Pick<OverviewPayload, 'partner_status'>;
};

export function useOverviewCard<K extends keyof OverviewCards>(card: K, from: string, to: string) {
  return useQuery({
    queryKey: ['dashboard', 'overview', card, from, to],
    queryFn: ({ signal }) => fetchApi(`/api/dashboards/overview/${card}?${new URLSearchParams({ from, to })}`, { signal }) as Promise<OverviewCards[K]>,
    staleTime: 60_000, retry: false, retryOnMount: false, refetchOnWindowFocus: false,
  });
}

function validateSettings(settings: DashboardSettings): DashboardSettings {
  if (!Number.isInteger(settings?.fiscal_year_start_month) || settings.fiscal_year_start_month < 1 || settings.fiscal_year_start_month > 12) {
    throw new Error('The server returned an invalid fiscal year start month.');
  }
  if (
    !['90d', 'custom'].includes(settings.dashboard_default_preset)
    || (settings.dashboard_default_preset === 'custom' && validateCustomRange(settings.dashboard_default_from ?? '', settings.dashboard_default_to ?? '') !== null)
    || (settings.dashboard_default_preset === '90d' && (settings.dashboard_default_from !== null || settings.dashboard_default_to !== null))
  ) throw new Error('The server returned an invalid dashboard default date range.');
  return settings;
}

async function fetchDashboardSettings(signal: AbortSignal): Promise<DashboardSettings> {
  return validateSettings(await fetchApi('/api/dashboards/settings', { signal }) as DashboardSettings);
}

export function useDashboard(name: DashboardName, from: string, to: string, enabled = true) {
  return useQuery({
    enabled: enabled && name !== 'overview',
    queryKey: ['dashboard', name, from, to],
    queryFn: ({ signal }) => fetchApi(`/api/dashboards/${name}?${new URLSearchParams({ from, to })}`, { signal }) as Promise<DashboardPayload>,
    staleTime: 60_000,
    retry: false,
    retryOnMount: false,
    refetchOnWindowFocus: false,
  });
}

export function useDashboardSettings() {
  return useQuery({
    queryKey: ['dashboard-settings'],
    queryFn: ({ signal }) => fetchDashboardSettings(signal),
    staleTime: 300_000,
    retry: false,
    retryOnMount: false,
    refetchOnWindowFocus: false,
  });
}

export function useAdminSettings() {
  return useQuery({
    queryKey: ['admin-settings'],
    queryFn: async () => validateSettings(await fetchApi('/api/admin/settings') as DashboardSettings),
    staleTime: 300_000,
  });
}

export function useUpdateAdminSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (settings: DashboardSettingsPatch) => fetchApi('/api/admin/settings', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings),
    }).then(response => validateSettings(response as DashboardSettings)),
    onSuccess: async settings => {
      queryClient.setQueryData(['admin-settings'], settings);
      queryClient.setQueryData(['dashboard-settings'], settings);
      await queryClient.invalidateQueries({ queryKey: ['admin-settings'] });
      await queryClient.invalidateQueries({ queryKey: ['dashboard-settings'] });
    },
  });
}

export function dashboardCsvUrl(name: DashboardName, chart: string, from: string, to: string) {
  return `/api/dashboards/${name}/${chart}/csv?${new URLSearchParams({ from, to })}`;
}