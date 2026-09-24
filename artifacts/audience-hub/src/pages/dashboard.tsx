import { useEffect, useState } from 'react';
import { Activity, ArrowDownRight, ArrowUpRight, CalendarDays, ChevronDown, Download, RefreshCw, Table2, TrendingUp } from 'lucide-react';
import { Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { useAuth } from '@/hooks/use-auth';
import { useDashboard, useDashboardSettings, dashboardCsvUrl, type ChartRow, type DashboardName, type DashboardPayload } from '@/hooks/use-dashboards';
import DataHealth from '@/pages/data-health';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';

type Spec = { key: string; title: string; subtitle: string; label: string; value: string; secondary?: string; kind?: 'line' | 'table' | 'heatmap' | 'bars' };
const specs: Record<DashboardName, Spec[]> = {
  overview: [
    { key: 'monthly_giving', title: 'Giving volume', subtitle: 'Monthly gift amount and count', label: 'month', value: 'amount', kind: 'bars' },
    { key: 'donor_status', title: 'Donor composition', subtitle: 'Current computed donor status', label: 'status', value: 'profiles' },
    { key: 'top_campaigns', title: 'Top campaigns', subtitle: 'Ranked by giving in selected period', label: 'campaign', value: 'amount', kind: 'table' },
  ],
  giving: [
    { key: 'monthly_giving', title: 'Giving by month', subtitle: 'Amount across the selected period', label: 'month', value: 'amount' },
    { key: 'new_returning', title: 'New versus returning', subtitle: 'Gifts by donor relationship', label: 'month', value: 'new_donors', secondary: 'returning_donors' },
    { key: 'by_channel', title: 'Giving by channel', subtitle: 'Amount and gift count', label: 'channel', value: 'amount' },
    { key: 'by_fund', title: 'Giving by fund', subtitle: 'Allocation of contributed dollars', label: 'fund', value: 'amount' },
    { key: 'appeal_codes', title: 'Appeal response', subtitle: 'Response counts and total amount', label: 'appeal_code', value: 'responses', kind: 'table' },
    { key: 'gift_size_distribution', title: 'Gift size distribution', subtitle: 'Gift counts by amount band', label: 'bucket', value: 'gifts' },
    { key: 'top_campaigns', title: 'Campaign performance', subtitle: 'Top campaigns by amount', label: 'campaign', value: 'amount', kind: 'table' },
  ],
  retention: [
    { key: 'retention_by_year', title: 'Year-one retention', subtitle: 'Donors who returned the following year', label: 'year', value: 'retention_rate', kind: 'line' },
    { key: 'cohorts', title: 'Cohort retention', subtitle: 'First gift year × years since first gift · percent retained', label: 'first_year', value: 'retention_pct', kind: 'heatmap' },
    { key: 'status_over_time', title: 'Status snapshots', subtitle: 'Monthly donor status from computed traits', label: 'month', value: 'profiles', secondary: 'status' },
  ],
  engagement: [
    { key: 'events_by_day', title: 'Event volume', subtitle: 'Daily events across sources', label: 'day', value: 'events', secondary: 'source', kind: 'line' },
    { key: 'top_event_names', title: 'Top event names', subtitle: 'Most frequent tracked interactions', label: 'name', value: 'events', kind: 'table' },
    { key: 'viewer_to_donor', title: 'Viewer to donor', subtitle: 'First gift after a tracked event', label: 'month', value: 'conversions' },
  ],
  sources: [
    { key: 'profiles_by_source', title: 'Profiles by source', subtitle: 'Distinct unified profiles represented', label: 'source', value: 'profiles' },
    { key: 'overlap_matrix', title: 'Source overlap', subtitle: 'Shared profiles between source pairs', label: 'source_a', value: 'profiles', kind: 'heatmap' },
    { key: 'identifier_coverage', title: 'Identifier coverage', subtitle: 'Email, phone and address coverage by source', label: 'source', value: 'email_pct', secondary: 'phone_pct', kind: 'table' },
  ],
  'data-health': [
    { key: 'pending_resolutions', title: 'Pending resolutions', subtitle: 'Unresolved source records', label: 'source', value: 'pending' },
    { key: 'merges_per_day', title: 'Profile merges', subtitle: 'Completed merges by day', label: 'day', value: 'merges' },
    { key: 'rejected_rows_by_import', title: 'Rejected import rows', subtitle: 'Rejected rows by source and record type', label: 'day', value: 'rejected', kind: 'table' },
    { key: 'blocklist_hits', title: 'Blocklist hits', subtitle: 'Source records matching blocked identifiers', label: 'type', value: 'hits' },
    { key: 'blocklist_review', title: 'Awaiting blocklist review', subtitle: 'High-cardinality identifiers by type', label: 'type', value: 'awaiting_review' },
    { key: 'expiring_enrichment', title: 'Expiring enrichment', subtitle: 'Attributes expiring within 60 days', label: 'source', value: 'profiles', kind: 'table' },
  ],
};
const tabs: { id: DashboardName; label: string }[] = [
  { id: 'overview', label: 'Overview' }, { id: 'giving', label: 'Giving' }, { id: 'retention', label: 'Retention' },
  { id: 'engagement', label: 'Engagement' }, { id: 'sources', label: 'Sources' }, { id: 'data-health', label: 'Data Health' },
];
const palette = Array.from({ length: 8 }, (_, index) => `var(--kin-chart-${index + 1})`);
const iso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const dateLabel = (v: string | number | null) => typeof v === 'string' && /^\d{4}-\d\d-\d\d/.test(v)
  ? new Date(`${v.slice(0, 10)}T12:00:00`).toLocaleDateString(undefined, { month: 'short', year: '2-digit', day: v.endsWith('-01') ? undefined : 'numeric' })
  : String(v ?? '—');
const compact = (n: number) => new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 2 }).format(n);
const full = (v: string | number | null, key?: string) => v == null ? '—' : typeof v === 'number'
  ? `${new Intl.NumberFormat('en-US', { maximumFractionDigits: key?.includes('pct') || key?.includes('rate') ? 1 : 2 }).format(v)}${key?.includes('pct') || key?.includes('rate') ? '%' : ''}`
  : dateLabel(v);
const money = (n: number) => `$${compact(n)}`;

function presetRange(preset: string, fiscalStart = 1): [string, string] {
  const now = new Date();
  const end = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const start = new Date(end);
  if (preset === '30d') start.setDate(start.getDate() - 29);
  if (preset === '90d') start.setDate(start.getDate() - 89);
  if (preset === '12m') start.setFullYear(start.getFullYear() - 1), start.setDate(start.getDate() + 1);
  if (preset === 'ytd') start.setMonth(0, 1);
  if (preset === 'fy') {
    const fyYear = end.getMonth() + 1 >= fiscalStart ? end.getFullYear() : end.getFullYear() - 1;
    const fyStart = new Date(fyYear - 1, fiscalStart - 1, 1);
    const fyEnd = new Date(fyYear, fiscalStart - 1, 0);
    return [iso(fyStart), iso(fyEnd)];
  }
  return [iso(start), iso(end)];
}

function ChartPanel({ spec, rows, dashboard, from, to, canExport }: { spec: Spec; rows: ChartRow[]; dashboard: DashboardName; from: string; to: string; canExport: boolean }) {
  const [table, setTable] = useState(false);
  const keys = rows[0] ? Object.keys(rows[0]) : [spec.label, spec.value];
  const isCurrency = ['amount', 'ltv_sum', 'giving_12m_sum'].includes(spec.value);
  const chartRows = spec.secondary && ['source', 'status'].includes(spec.secondary)
    ? Array.from(new Set(rows.map(row => String(row[spec.label])))).map(label => ({
        [spec.label]: label,
        ...Object.fromEntries(Array.from(new Set(rows.map(row => String(row[spec.secondary!])))).map(category => [
          category, rows.filter(row => String(row[spec.label]) === label && String(row[spec.secondary!]) === category).reduce((sum, row) => sum + Number(row[spec.value] ?? 0), 0),
        ])),
      }))
    : rows;
  const series = spec.secondary && ['source', 'status'].includes(spec.secondary)
    ? Array.from(new Set(rows.map(row => String(row[spec.secondary!]))))
    : [spec.value, ...(spec.secondary ? [spec.secondary] : [])];
  const heatYears = Array.from(new Set(rows.map(r => Number(r.years_after_first)))).sort((a, b) => a - b);
  const sources = Array.from(new Set(rows.flatMap(r => [String(r.source_a), String(r.source_b)]))).sort();

  return <section className="min-w-0 rounded-md border border-line bg-surface flex flex-col" data-testid={`chart-${spec.key}`}>
    <div className="px-4 py-3 border-b border-line flex items-start justify-between gap-3">
      <div className="min-w-0">
        <h3 className="kin-heading text-ink">{spec.title}</h3>
        <p className="text-[11px] text-ink-muted mt-0.5">{spec.subtitle}</p>
      </div>
      <div className="flex items-center gap-1 shrink-0">
        <button type="button" data-testid={`toggle-table-${spec.key}`} aria-label={`${table ? 'Show chart' : 'Show table'} for ${spec.title}`} aria-pressed={table} onClick={() => setTable(!table)} className={`p-1.5 rounded border transition-colors ${table ? 'border-signal text-signal bg-signal-soft' : 'border-transparent text-ink-muted hover:text-ink hover:bg-surface-raised'}`}><Table2 size={15} /></button>
        {canExport && <a data-testid={`download-${spec.key}`} href={dashboardCsvUrl(dashboard, spec.key, from, to)} title={`Download ${spec.title} CSV`} aria-label={`Download ${spec.title} CSV`} className="p-1.5 rounded text-ink-muted hover:text-signal hover:bg-surface-raised"><Download size={15} /></a>}
      </div>
    </div>
    <div className="p-4 min-h-[230px] flex-1">
      {!rows.length ? <div className="h-[200px] flex flex-col items-center justify-center text-center"><Activity size={20} className="text-ink-muted mb-2" /><p className="text-xs text-ink-muted">No records in this range.</p><p className="text-[11px] text-ink-muted mt-1">Try a longer date range or import source data.</p></div>
      : table || spec.kind === 'table' ? <div className="overflow-auto max-h-[260px]">
          <table className="w-full text-xs tabular-nums">
            <thead className="sticky top-0 bg-surface-raised text-ink-muted"><tr>{keys.map(k => <th key={k} className={`px-2 py-2 font-medium whitespace-nowrap ${typeof rows[0][k] === 'number' ? 'text-right' : 'text-left'}`}>{k.replaceAll('_', ' ')}</th>)}</tr></thead>
            <tbody>{rows.map((row, i) => <tr key={i} className="border-t border-line hover:bg-surface-raised">{keys.map(k => <td key={k} className={`px-2 py-2 whitespace-nowrap ${typeof row[k] === 'number' ? 'text-right font-mono text-ink' : 'text-ink-muted'}`}>{full(row[k], k)}</td>)}</tr>)}</tbody>
          </table>
        </div>
      : spec.kind === 'heatmap' && spec.key === 'cohorts' ? <div className="overflow-auto max-h-[260px]"><table className="w-full text-xs tabular-nums"><thead><tr><th className="text-left text-ink-muted pb-2">First gift</th>{heatYears.map(y => <th key={y} className="text-right text-ink-muted pb-2 px-2">Year {y}</th>)}</tr></thead><tbody>{Array.from(new Set(rows.map(r => String(r.first_year)))).map(year => <tr key={year}><td className="font-mono py-1">{year}</td>{heatYears.map(y => { const v = Number(rows.find(r => String(r.first_year) === year && Number(r.years_after_first) === y)?.retention_pct ?? 0); return <td key={y} className="px-1 py-1"><div className="text-right px-2 py-1.5 rounded-sm font-mono" style={{ backgroundColor: `color-mix(in srgb, var(--kin-chart-1) ${Math.max(7, v / 1.4)}%, var(--surface))`, color: v ? 'var(--ink)' : 'var(--ink-muted)' }}>{v ? `${v.toFixed(1)}%` : '—'}</div></td> })}</tr>)}</tbody></table></div>
      : spec.kind === 'heatmap' && spec.key === 'overlap_matrix' ? <div className="overflow-auto max-h-[260px]"><table className="w-full text-xs tabular-nums"><thead><tr><th className="text-left text-ink-muted pb-2">Source</th>{sources.map(s => <th key={s} className="text-right text-ink-muted pb-2 px-2">{s}</th>)}</tr></thead><tbody>{sources.map(a => <tr key={a}><td className="font-mono py-1">{a}</td>{sources.map(b => { const v = Number(rows.find(r => (r.source_a === a && r.source_b === b) || (r.source_a === b && r.source_b === a))?.profiles ?? 0); const max = Math.max(...rows.map(r => Number(r.profiles)), 1); return <td key={b} className="px-1 py-1"><div className="text-right px-2 py-1.5 rounded-sm font-mono" style={{ backgroundColor: `color-mix(in srgb, var(--kin-chart-1) ${Math.max(7, v / max * 55)}%, var(--surface))` }}>{v ? v.toLocaleString() : '—'}</div></td> })}</tr>)}</tbody></table></div>
      : <div className="h-[200px] w-full">
          <ResponsiveContainer width="100%" height="100%">
            {spec.kind === 'line' ? <LineChart data={chartRows} margin={{ top: 8, right: 14, left: 0, bottom: 0 }}>
              <CartesianGrid stroke="var(--line)" vertical={false} /><XAxis dataKey={spec.label} tickFormatter={dateLabel} tick={{ fill: 'var(--ink-muted)', fontSize: 10 }} tickLine={false} axisLine={false} minTickGap={24} /><YAxis tickFormatter={compact} tick={{ fill: 'var(--ink-muted)', fontSize: 10 }} tickLine={false} axisLine={false} width={42} />
              <Tooltip contentStyle={{ background: 'var(--surface-raised)', color: 'var(--ink)', border: '1px solid var(--line)', borderRadius: 4, fontSize: 11 }} labelFormatter={dateLabel} />
              {series.map((s, i) => <Line key={s} type="monotone" dataKey={s} name={s.replaceAll('_', ' ')} stroke={palette[i % palette.length]} strokeWidth={2} dot={false} activeDot={{ r: 3 }} />)}
            </LineChart> : <BarChart data={chartRows} margin={{ top: 8, right: 14, left: 0, bottom: 0 }}>
              <CartesianGrid stroke="var(--line)" vertical={false} /><XAxis dataKey={spec.label} tickFormatter={dateLabel} tick={{ fill: 'var(--ink-muted)', fontSize: 10 }} tickLine={false} axisLine={false} minTickGap={16} /><YAxis tickFormatter={compact} tick={{ fill: 'var(--ink-muted)', fontSize: 10 }} tickLine={false} axisLine={false} width={42} />
              <Tooltip contentStyle={{ background: 'var(--surface-raised)', color: 'var(--ink)', border: '1px solid var(--line)', borderRadius: 4, fontSize: 11 }} labelFormatter={dateLabel} formatter={(v: number, name: string) => [isCurrency ? `$${Number(v).toLocaleString()}` : full(Number(v), name), name]} />
              {series.map((s, i) => <Bar key={s} dataKey={s} name={s.replaceAll('_', ' ')} stackId={series.length > 1 ? 'stack' : undefined} fill={palette[i % palette.length]} maxBarSize={36} radius={series.length === 1 ? [2, 2, 0, 0] : undefined} />)}
            </BarChart>}
          </ResponsiveContainer>
        </div>}
    </div>
    <div className="border-t border-line px-4 py-2 text-[10px] uppercase tracking-wider text-ink-muted font-mono">{rows.length.toLocaleString()} {rows.length === 1 ? 'record' : 'records'} · {table ? 'table view' : 'source data'}</div>
  </section>;
}

function Kpis({ data, dashboard }: { data: DashboardPayload; dashboard: DashboardName }) {
  const labels: Record<string, string> = { active_profiles: 'Active profiles', donors: 'All-time donors', active_donors_12m: 'Active donors · 12m', giving_12m: 'Giving · 12m', avg_gift: 'Average gift · 12m', recurring_donors: 'Recurring donors', email_opted_in: 'Email opted in', pending_resolution_count: 'Pending resolution', blocklist_review_count: 'Awaiting review', expiring_enrichment_count: 'Expiring enrichment' };
  const entries = Object.entries(data.metrics);
  if (!entries.length) return null;
  return <div className={`grid gap-2 ${dashboard === 'overview' ? 'grid-cols-2 lg:grid-cols-4 xl:grid-cols-7' : 'grid-cols-1 sm:grid-cols-3'}`}>
    {entries.map(([key, metric]) => {
      const cash = key === 'giving_12m' || key === 'avg_gift';
      const positive = (metric.change ?? 0) >= 0;
      return <div key={key} data-testid={`metric-${key}`} className="bg-surface border border-line rounded-md px-3 py-3 min-w-0">
        <p className="text-[10px] text-ink-muted uppercase tracking-[.09em] whitespace-nowrap truncate">{labels[key] || key.replaceAll('_', ' ')}</p>
        <div className="font-mono tabular-nums text-[28px] leading-8 font-semibold tracking-tight text-ink mt-2">{cash ? money(metric.value) : compact(metric.value)}</div>
        <div className="mt-2 flex items-center gap-1 text-[10px] font-mono tabular-nums">
          {metric.change_pct != null ? <span className={`inline-flex items-center ${positive ? 'text-ok' : 'text-danger'}`}>{positive ? <ArrowUpRight size={13} /> : <ArrowDownRight size={13} />}{Math.abs(metric.change_pct).toFixed(1)}%</span> : <span className="text-ink-muted">—</span>}
          <span className="text-ink-muted">vs prior</span>
        </div>
      </div>;
    })}
  </div>;
}

export default function Dashboard() {
  const [initial] = useState(() => {
    const params = new URLSearchParams(window.location.search);
    const requestedTab = params.get('tab');
    const tab = tabs.some(item => item.id === requestedTab) ? requestedTab as DashboardName : 'overview';
    const from = params.get('from');
    const to = params.get('to');
    const valid = from && to && /^\d{4}-\d{2}-\d{2}$/.test(from) &&
      /^\d{4}-\d{2}-\d{2}$/.test(to) && from <= to &&
      (Date.parse(to) - Date.parse(from)) <= 3660 * 86400000;
    return { tab, preset: valid ? 'custom' : '90d', range: valid ? [from, to] as [string, string] : presetRange('90d') };
  });
  const [tab, setTab] = useState<DashboardName>(initial.tab);
  const [preset, setPreset] = useState(initial.preset);
  const [[from, to], setRange] = useState<[string, string]>(initial.range);
  const { user } = useAuth();
  const settings = useDashboardSettings();
  const query = useDashboard(tab, from, to);
  useEffect(() => {
    if (preset === 'fy' && settings.data) setRange(presetRange('fy', settings.data.fiscal_year_start_month));
  }, [preset, settings.data]);
  const selectPreset = (value: string) => {
    setPreset(value);
    if (value !== 'custom' && (value !== 'fy' || settings.data)) {
      setRange(presetRange(value, settings.data?.fiscal_year_start_month ?? 1));
    }
  };
  return <div className="max-w-[1680px] mx-auto space-y-4 pb-8 animate-in fade-in duration-300">
    <header className="flex flex-col xl:flex-row xl:items-end xl:justify-between gap-4">
      <div><div className="flex items-center gap-2 text-[10px] uppercase tracking-[.16em] font-mono text-signal mb-1"><TrendingUp size={13} /> Audience intelligence <span className="text-ink-muted">/ 01</span></div><h1 className="kin-title">Dashboards</h1><p className="text-xs text-ink-muted mt-1">Unified audience signals, giving, and data operations.</p></div>
      <div className="flex flex-wrap items-center gap-1.5 text-xs">
        <CalendarDays size={14} className="text-ink-muted mr-1" />
        {([['30d', '30 d'], ['90d', '90 d'], ['12m', '12 m'], ['ytd', 'YTD'], ['fy', 'Last FY'], ['custom', 'Custom']] as const).map(([id, label]) => <button key={id} type="button" data-testid={`preset-${id}`} disabled={id === 'fy' && !settings.data} onClick={() => selectPreset(id)} className={`px-2.5 py-1.5 rounded border transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${preset === id ? 'border-signal bg-signal-soft text-signal' : 'border-line-strong text-ink-muted hover:text-ink hover:bg-surface-raised'}`}>{label}</button>)}
        {preset === 'custom' ? <div className="flex items-center gap-1.5 ml-1"><input data-testid="input-range-from" aria-label="From date" type="date" max={to} value={from} onChange={e => setRange([e.target.value, to])} className="bg-surface border border-line-strong rounded px-2 py-1.5 text-ink [color-scheme:dark]" /><span className="text-ink-muted">to</span><input data-testid="input-range-to" aria-label="To date" type="date" min={from} value={to} onChange={e => setRange([from, e.target.value])} className="bg-surface border border-line-strong rounded px-2 py-1.5 text-ink [color-scheme:dark]" /></div> : <span className="ml-2 text-ink-muted font-mono whitespace-nowrap">{from} — {to}</span>}
      </div>
    </header>
    {settings.isError && <div role="alert" className="border border-danger/30 bg-danger/5 rounded-md px-4 py-3 text-xs text-ink"><span>Dashboard settings are unavailable; Last FY cannot be calculated.</span><Button variant="outline" size="sm" className="ml-3 h-7" onClick={() => settings.refetch()}><RefreshCw size={12} className="mr-1.5" /> Retry</Button><span className="ml-2 text-ink-muted">{settings.error instanceof Error ? settings.error.message : 'The request could not be completed.'}</span></div>}
    <nav aria-label="Dashboard sections" className="flex overflow-x-auto gap-0 border-b border-line">
      {tabs.map(item => <button key={item.id} type="button" data-testid={`tab-${item.id}`} onClick={() => setTab(item.id)} className={`px-4 py-2.5 text-xs whitespace-nowrap border-b-2 transition-colors ${tab === item.id ? 'text-signal border-signal bg-signal-soft' : 'text-ink-muted border-transparent hover:text-ink'}`}>{item.label}</button>)}
    </nav>
    <div className="flex items-center justify-between text-[11px] text-ink-muted"><span className="font-mono uppercase tracking-wider">{tab.replace('-', ' ')} <ChevronDown size={12} className="inline ml-1" /></span>{query.data && <span data-testid="dashboard-period">Current {query.data.range.from} → {query.data.range.to}<span className="mx-2 text-ink-muted">|</span>Prior {query.data.range.prior_from} → {query.data.range.prior_to}</span>}</div>
    {query.isLoading ? <><div className="grid grid-cols-2 lg:grid-cols-4 gap-2">{Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-24 bg-surface-raised" />)}</div><div className="grid grid-cols-1 xl:grid-cols-2 gap-3">{Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-[295px] bg-surface-raised" />)}</div></>
      : query.isError ? <div role="alert" className="border border-danger/30 bg-danger/5 rounded-md p-8 text-center"><p className="text-ink font-medium">Dashboard data is unavailable</p><p className="text-ink-muted text-xs mt-1">{query.error instanceof Error ? query.error.message : 'The request could not be completed.'}</p><Button variant="outline" size="sm" className="mt-4" onClick={() => query.refetch()}><RefreshCw size={13} className="mr-2" /> Retry</Button></div>
      : query.data && <>
        <Kpis data={query.data} dashboard={tab} />
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">{specs[tab].map((spec, index) => <div key={spec.key} className={index === 0 && tab === 'overview' ? 'xl:col-span-2' : ''}><ChartPanel spec={spec} rows={query.data!.charts[spec.key] || []} dashboard={tab} from={from} to={to} canExport={user?.role === 'analyst' || user?.role === 'admin'} /></div>)}</div>
        {tab === 'data-health' && <div className="border-t border-line pt-5 mt-5"><div className="text-[10px] uppercase tracking-widest text-signal font-mono mb-2">Operational controls</div><DataHealth /></div>}
      </>}
  </div>;
}