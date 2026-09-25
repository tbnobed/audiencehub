import { Link } from 'react-router-dom';
import { ArrowRight, BarChart3 } from 'lucide-react';
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { useOverviewCard, type DeltaMetric, type KpiMetric } from '@/hooks/use-dashboards';
import { Skeleton } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';
import { millionsTick } from '@/lib/date-range';

const nf = new Intl.NumberFormat('en-US');
const money = (n: number) => {
  const a = Math.abs(n);
  if (a >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (a >= 10_000) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};
const monthShort = (iso: string) => new Date(`${iso.slice(0, 10)}T12:00:00`).toLocaleDateString('en-US', { month: 'short' });
const monthLong = (iso: string) => new Date(`${iso.slice(0, 10)}T12:00:00`).toLocaleDateString('en-US', { month: 'short', year: 'numeric' });
const dayLabel = (iso: string) => new Date(`${iso.slice(0, 10)}T12:00:00`).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });

function Panel({ title, meta, children, className }: { title: string; meta?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return <section className={cn('min-w-0 rounded-lg border border-line bg-surface', className)} aria-label={title}>
    <header className="flex items-center justify-between gap-3 px-4 pt-3.5 pb-2">
      <h2 className="kin-heading text-ink">{title}</h2>
      {meta && <div className="text-xs text-ink-muted shrink-0">{meta}</div>}
    </header>
    {children}
  </section>;
}

function DeltaChip({ m }: { m: DeltaMetric }) {
  if (m.delta_label) {
    return <span className={cn('rounded px-1.5 py-0.5 font-mono text-[11px] leading-4', m.delta_label === 'New' ? 'bg-signal-soft text-signal' : 'bg-surface-raised text-ink-muted')}>{m.delta_label}</span>;
  }
  if (m.change_pct == null) return null;
  const up = m.change_pct >= 0;
  return <span className={cn('rounded px-1.5 py-0.5 font-mono text-[11px] leading-4 tabular-nums', up ? 'bg-ok/12 text-ok' : 'bg-danger/12 text-danger')} aria-label={`${up ? 'Up' : 'Down'} ${Math.abs(m.change_pct).toFixed(1)} percent versus prior period`}>
    <span aria-hidden>{up ? '▲' : '▼'}</span> {Math.abs(m.change_pct).toFixed(1)}%
  </span>;
}

function Sparkline({ points, negative, format }: { points: KpiMetric['sparkline']; negative: boolean; format: (n: number) => string }) {
  const max = Math.max(...points.map(p => p.value), 0);
  return <div className="flex items-end gap-[3px] h-[30px]" role="img" aria-label={`Monthly trend, ${points.length} months: ${points.map(p => `${monthShort(p.month)} ${format(p.value)}`).join(', ')}`}>
    {points.map((p, i) => {
      const last = i === points.length - 1;
      const h = max > 0 ? Math.round(3 + (p.value / max) * 27) : 3;
      return <div key={p.month} title={`${monthLong(p.month)} · ${format(p.value)}`} className="flex-1 rounded-[1px]" style={{ height: h, background: last ? (negative ? 'var(--danger)' : 'var(--signal)') : 'color-mix(in srgb, var(--line-strong) 45%, transparent)' }} />;
    })}
  </div>;
}

function KpiTile({ label, m, value, note, format }: { label: string; m: KpiMetric; value: string; note: string; format: (n: number) => string }) {
  return <div className="min-w-0 rounded-lg border border-line bg-surface p-4 flex flex-col gap-3" data-testid={`kpi-${label.toLowerCase().replace(/\W+/g, '-')}`}>
    <div className="flex items-center justify-between gap-2">
      <span className="text-[11px] font-medium uppercase tracking-[.08em] text-ink-muted truncate">{label}</span>
      <DeltaChip m={m} />
    </div>
    <div className="font-mono tabular-nums text-[30px] leading-9 font-medium tracking-tight text-ink truncate">{value}</div>
    <Sparkline points={m.sparkline} negative={(m.change_pct ?? 0) < 0 && !m.delta_label} format={format} />
    <p className="text-xs text-ink-muted truncate" title={note}>{note}</p>
  </div>;
}

function monthDelta(m: DeltaMetric) {
  if (m.change === 0) return 'No change this month';
  return `${m.change > 0 ? '▲' : '▼'} ${nf.format(Math.abs(m.change))} this month`;
}

type RangeProps = { from: string; to: string };
type GivingProps = RangeProps & { canImport: boolean; onWiden: () => void };

export function OverviewView({ from, to, canImport, onWiden, periodNoun }: GivingProps & { periodNoun: string }) {
  return <div className="space-y-3">
    <OverviewKpis from={from} to={to} periodNoun={periodNoun} />
    <div className="grid grid-cols-1 xl:grid-cols-3 gap-3">
      <MonthlyGiving from={from} to={to} canImport={canImport} onWiden={onWiden} />
      <Attention from={from} to={to} />
    </div>
    <div className="grid grid-cols-1 xl:grid-cols-3 gap-3">
      <Campaigns from={from} to={to} onWiden={onWiden} />
      <PartnerStatus from={from} to={to} />
    </div>
  </div>;
}

function CardState({ query, label }: { query: { error: Error | null; isFetching: boolean; refetch: () => unknown }; label: string }) {
  return <div className="px-4 pb-4" aria-label={label}>
    {query.error ? <SectionError error={query.error} retry={() => query.refetch()} fetching={query.isFetching} />
      : <div role="status" aria-label={`Loading ${label}`}><Skeleton className="h-24" /><span className="text-xs text-ink-muted">Loading {label}…</span></div>}
  </div>;
}

function OverviewKpis({ from, to, periodNoun }: RangeProps & { periodNoun: string }) {
  const query = useOverviewCard('kpis', from, to);
  if (!query.data || query.isError) return <Panel title="Key metrics"><CardState query={query} label="key metrics and audience stats" /></Panel>;
  const { kpis, stats } = query.data;
  const ret = kpis.retention_yoy;
  const strip = [
    { label: 'Profiles', value: nf.format(stats.profiles.value), sub: 'Current active profiles', cls: 'text-ink-muted' },
    { label: 'Recurring partners', value: nf.format(stats.recurring_partners.value), sub: monthDelta(stats.recurring_partners), cls: stats.recurring_partners.change > 0 ? 'text-ok' : stats.recurring_partners.change < 0 ? 'text-danger' : 'text-ink-muted' },
    { label: 'Email opted in', value: nf.format(stats.email_opted_in.value), sub: `${stats.email_opted_in.percentage.toFixed(1)}% of profiles`, cls: 'text-ink-muted' },
    { label: 'Lapsing', value: nf.format(stats.lapsing.value), sub: monthDelta(stats.lapsing), cls: stats.lapsing.change !== 0 ? 'text-warn' : 'text-ink-muted' },
  ];

  return <div className="space-y-3">
    <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">
      <KpiTile label="Giving" m={kpis.giving} value={money(kpis.giving.value)} format={money} note={`${money(kpis.giving.prior)} the prior ${periodNoun}`} />
      <KpiTile label="Active partners" m={kpis.active_partners} value={nf.format(kpis.active_partners.value)} format={n => nf.format(n)} note="Gave at least once in this period" />
      <KpiTile label="Partner retention" m={ret} value={ret.denominator === 0 ? '—' : `${ret.value.toFixed(1)}%`} format={n => `${n.toFixed(1)}%`}
        note={ret.denominator === 0 ? 'No prior-year partners to measure' : `${nf.format(ret.retained)} of ${nf.format(ret.denominator)} last-year partners gave again`} />
      <KpiTile label="Average gift" m={kpis.average_gift} value={money(kpis.average_gift.value)} format={money} note={`${money(kpis.average_gift.prior)} the prior ${periodNoun}`} />
    </div>

    <section aria-label="Audience stats" className="rounded-lg border border-line bg-surface grid grid-cols-2 lg:grid-cols-4">
      {strip.map((s, i) => <div key={s.label} className={cn('px-4 py-3 min-w-0', i > 0 && 'lg:border-l border-line', i % 2 === 1 && 'border-l', i >= 2 && 'border-t lg:border-t-0')}>
        <div className="text-xs text-ink-muted">{s.label}</div>
        <div className="font-mono tabular-nums text-lg text-ink mt-0.5 break-words">{s.value}</div>
        <div className={cn('text-[11px] font-mono tabular-nums mt-0.5 truncate', s.cls)}>{s.sub}</div>
      </div>)}
    </section>
  </div>;
}

function MonthlyGiving({ from, to, canImport, onWiden }: GivingProps) {
  const query = useOverviewCard('giving-by-month', from, to);
  if (!query.data || query.isError) return <Panel title="Giving by month" className="xl:col-span-2"><CardState query={query} label="giving by month" /></Panel>;
  const data = query.data.giving_by_month;
  const months = data.monthly_giving.map(r => ({ ...r, label: monthShort(r.month), holiday: ['11', '12'].includes(r.month.slice(5, 7)) }));
  const hasMonthly = months.some(m => m.amount > 0 || m.prior_amount > 0);
  const maxY = Math.max(...months.map(m => Math.max(m.amount, m.prior_amount)), 0);
  const top2 = [...months].sort((a, b) => b.amount - a.amount).slice(0, 2).filter(m => m.amount > 0).sort((a, b) => a.month.localeCompare(b.month));
  const takeaway = data.top_two_month_share != null && top2.length === 2 && months.length > 2
    ? `${top2[0].label} and ${top2[1].label} brought in ${data.top_two_month_share.toFixed(1)}% of giving in this period.`
    : null;
  return <Panel title="Giving by month" className="xl:col-span-2" meta={hasMonthly && <span className="flex items-center gap-3">
        <span className="flex items-center gap-1.5"><i className="h-2 w-2 rounded-sm bg-signal" />This period</span>
        <span className="flex items-center gap-1.5"><i className="h-2 w-2 rounded-sm" style={{ background: 'color-mix(in srgb, var(--line-strong) 55%, transparent)' }} />Prior period</span>
      </span>}>
        {!hasMonthly ? <EmptyLine text="No giving recorded in this range or the prior one." action={canImport ? <Link to="/imports" className="text-signal hover:underline">Import gifts</Link> : <button type="button" onClick={onWiden} className="text-signal hover:underline">Try the last 12 months</button>} />
          : <div className="px-4 pb-4">
            {takeaway && <p className="text-xs text-ink-muted -mt-1 mb-3">{takeaway}</p>}
            <div className="h-[240px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={months} margin={{ top: 4, right: 4, left: 0, bottom: 0 }} barGap={2} barCategoryGap="22%">
                  <CartesianGrid stroke="var(--line)" vertical={false} />
                  <XAxis dataKey="label" tick={{ fill: 'var(--ink-muted)', fontSize: 11 }} tickLine={false} axisLine={false} interval="preserveStartEnd" />
                  <YAxis tickFormatter={millionsTick(maxY)} tick={{ fill: 'var(--ink-muted)', fontSize: 11, fontFamily: 'var(--app-font-mono)' }} tickLine={false} axisLine={false} width={44} />
                  <Tooltip cursor={{ fill: 'var(--surface-raised)' }} contentStyle={{ background: 'var(--surface-raised)', color: 'var(--ink)', border: '1px solid var(--line)', borderRadius: 6, fontSize: 12 }}
                    labelFormatter={(_, p) => { const r = p?.[0]?.payload as typeof months[number] | undefined; return r ? `${dayLabel(r.from)} – ${dayLabel(r.to)} · ${nf.format(r.gifts)} gifts` : ''; }}
                    formatter={(v, name, item) => { const r = item.payload as typeof months[number]; return [`$${Number(v).toLocaleString('en-US', { maximumFractionDigits: 2 })}`, name === 'amount' ? 'This period' : `Prior (${dayLabel(r.prior_from)} – ${dayLabel(r.prior_to)})`] as [string, string]; }} />
                  <Bar dataKey="amount" radius={[2, 2, 0, 0]} maxBarSize={22}>
                    {months.map(m => <Cell key={m.month} fill={m.holiday ? 'color-mix(in srgb, var(--signal) 55%, var(--surface))' : 'var(--signal)'} />)}
                  </Bar>
                  <Bar dataKey="prior_amount" fill="color-mix(in srgb, var(--line-strong) 55%, transparent)" radius={[2, 2, 0, 0]} maxBarSize={22} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>}
      </Panel>;
}

function Attention({ from, to }: RangeProps) {
  const query = useOverviewCard('needs-attention', from, to);
  if (!query.data || query.isError) return <Panel title="Needs attention"><CardState query={query} label="needs attention" /></Panel>;
  const ranks = { error: 0, warning: 1, notice: 2, healthy: 3 };
  const attentionItems = [...query.data.attention].sort((a, b) => ranks[a.severity] - ranks[b.severity]);
  const sev = { error: { c: 'var(--danger)', t: 'Error' }, warning: { c: 'var(--warn)', t: 'Warning' }, notice: { c: 'var(--signal)', t: 'Notice' }, healthy: { c: 'var(--ok)', t: 'Healthy' } } as const;
  return <Panel title="Needs attention">
        {!attentionItems.length ? <EmptyLine text="Nothing needs attention right now." />
          : <ul className="px-2 pb-2">
            {attentionItems.slice(0, 5).map((a, i) => <li key={i} className="flex gap-3 rounded-md px-2 py-2.5 border-t border-line first:border-t-0">
              <span className="w-[3px] shrink-0 rounded-full" style={{ background: sev[a.severity].c }} aria-hidden />
              <div className="min-w-0 flex-1">
                <div className="text-[13px] text-ink leading-5"><span className="font-mono text-[10px] tracking-wider mr-2" style={{ color: sev[a.severity].c }}>{sev[a.severity].t.toUpperCase()}</span>{a.title}</div>
                <p className="text-xs text-ink-muted mt-0.5 line-clamp-2">{a.explanation}</p>
                {a.href && <Link to={a.href} className="mt-1 inline-flex items-center gap-1 text-xs text-signal hover:underline">{fixLabel(a.href, a.title)} <ArrowRight size={12} strokeWidth={1.75} /></Link>}
              </div>
            </li>)}
          </ul>}
      </Panel>;
}

function Campaigns({ from, to, onWiden }: RangeProps & { onWiden: () => void }) {
  const query = useOverviewCard('campaigns', from, to);
  if (!query.data || query.isError) return <Panel title="Top campaigns" className="xl:col-span-2"><CardState query={query} label="top campaigns" /></Panel>;
  const data = query.data.top_campaigns;
  const maxShare = Math.max(...data.campaigns.map(c => c.share), 0);
  return <Panel title="Top campaigns" className="xl:col-span-2" meta={<Link to={data.campaigns_href} className="inline-flex items-center gap-1 text-signal hover:underline">All campaigns <ArrowRight size={12} strokeWidth={1.75} /></Link>}>
        {!data.campaigns.length ? <EmptyLine text="No campaign giving in this range." action={<button type="button" onClick={onWiden} className="text-signal hover:underline">Try the last 12 months</button>} />
          : <div className="overflow-x-auto px-4 pb-3">
            <table className="w-full text-[13px]">
              <thead><tr className="text-[11px] uppercase tracking-[.06em] text-ink-muted">
                <th scope="col" className="text-left font-medium py-2">Campaign</th>
                <th scope="col" className="text-left font-medium py-2 pl-4 w-[30%]">Share of giving</th>
                <th scope="col" className="text-right font-medium py-2 pl-4">Gifts</th>
                <th scope="col" className="text-right font-medium py-2 pl-4">Avg gift</th>
                <th scope="col" className="text-right font-medium py-2 pl-4">Total</th>
              </tr></thead>
              <tbody>{data.campaigns.map(c => <tr key={c.campaign} className="border-t border-line">
                <td className="py-2.5 text-ink truncate max-w-[220px]" title={c.campaign}>{c.campaign}</td>
                <td className="py-2.5 pl-4"><div className="flex items-center gap-2">
                  <div className="h-1.5 flex-1 rounded-full bg-surface-raised overflow-hidden"><div className="h-full bg-signal rounded-full origin-left" style={{ transform: `scaleX(${maxShare ? c.share / maxShare : 0})` }} /></div>
                  <span className="font-mono tabular-nums text-xs text-ink-muted w-12 text-right">{c.share.toFixed(1)}%</span>
                </div></td>
                <td className="py-2.5 pl-4 text-right font-mono tabular-nums text-ink-muted">{nf.format(c.gifts)}</td>
                <td className="py-2.5 pl-4 text-right font-mono tabular-nums text-ink-muted">{money(c.average_gift)}</td>
                <td className="py-2.5 pl-4 text-right font-mono tabular-nums text-ink">{money(c.amount)}</td>
              </tr>)}</tbody>
            </table>
          </div>}
      </Panel>;
}

function PartnerStatus({ from, to }: RangeProps) {
  const query = useOverviewCard('partner-status', from, to);
  if (!query.data || query.isError) return <Panel title="Partner status"><CardState query={query} label="partner status" /></Panel>;
  const data = query.data;
  const statusColor: Record<string, string> = { active: 'var(--signal)', new: 'var(--ok)', reactivated: 'var(--kin-chart-4)', lapsing: 'var(--warn)', lapsed: 'var(--line-strong)' };
  return <Panel title="Partner status" meta={`${nf.format(data.partner_status.givers)} givers`}>
        <div className="px-4 pb-4">
          {data.partner_status.givers === 0 ? <p className="text-xs text-ink-muted py-1">No partners have given through this date.</p> : <>
            <div className="flex h-2.5 rounded-full overflow-hidden gap-[2px]" role="img" aria-label={data.partner_status.statuses.map(s => `${s.status} ${s.share.toFixed(1)}%`).join(', ')}>
              {data.partner_status.statuses.filter(s => s.count > 0).map(s => <div key={s.status} style={{ flexGrow: s.count, background: statusColor[s.status] ?? 'var(--kin-chart-5)' }} />)}
            </div>
            <ul className="mt-3 space-y-1.5">
              {data.partner_status.statuses.map(s => <li key={s.status} className="flex items-center gap-2 text-[13px]">
                <i className="h-2 w-2 rounded-sm shrink-0" style={{ background: statusColor[s.status] ?? 'var(--kin-chart-5)' }} />
                <span className="capitalize text-ink flex-1">{s.status}</span>
                <span className="font-mono tabular-nums text-xs text-ink-muted w-14 text-right">{s.share.toFixed(1)}%</span>
                <span className="font-mono tabular-nums text-xs text-ink w-20 text-right">{nf.format(s.count)}</span>
              </li>)}
            </ul>
          </>}
          <div className="mt-3 pt-3 border-t border-line flex items-center justify-between text-[13px]">
            <span className="text-ink-muted">Prospects who have never given</span>
            <span className="font-mono tabular-nums text-ink">{nf.format(data.partner_status.prospects)}</span>
          </div>
        </div>
      </Panel>;
}

function SectionError({ error, retry, fetching }: { error: Error; retry: () => void; fetching: boolean }) {
  return <div role="alert" className="text-xs font-sans text-danger whitespace-normal">
    <span>{error.message}</span>{' '}
    <button type="button" disabled={fetching} onClick={retry} className="text-signal underline disabled:opacity-50">{fetching ? 'Retrying…' : 'Retry'}</button>
  </div>;
}

function fixLabel(href: string, title: string) {
  if (href.startsWith('/imports')) return href.includes('import_id') ? 'Open the error report' : 'Open imports';
  if (href.startsWith('/segments')) return /laps/i.test(title) ? 'Build a reactivation segment' : 'See affected segments';
  if (href.startsWith('/system')) return 'View System status';
  if (href.startsWith('/data-health')) return 'Review in Data Health';
  if (href.startsWith('/profiles')) return 'View profiles';
  return 'Open';
}

function EmptyLine({ text, action }: { text: string; action?: React.ReactNode }) {
  return <div className="flex items-center gap-2 px-4 pb-4 text-xs text-ink-muted">
    <BarChart3 size={16} strokeWidth={1.75} className="shrink-0" />
    <span>{text}</span>{action && <span className="ml-auto shrink-0">{action}</span>}
  </div>;
}

export function OverviewSkeleton() {
  return <div className="space-y-3" aria-busy="true" aria-label="Loading overview">
    <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">{Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-[168px] bg-surface-raised" />)}</div>
    <Skeleton className="h-[78px] bg-surface-raised" />
    <div className="grid grid-cols-1 xl:grid-cols-3 gap-3"><Skeleton className="h-[300px] xl:col-span-2 bg-surface-raised" /><Skeleton className="h-[300px] bg-surface-raised" /></div>
  </div>;
}
