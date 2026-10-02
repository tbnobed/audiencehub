import { useState } from 'react';
import { Download, Table as TableIcon } from 'lucide-react';
import { useSegmentHistoryExport } from '@/hooks/use-segments';
import { toast } from '@/hooks/use-toast';
import { Area, AreaChart, Bar, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import type { SegmentHistory } from '@/lib/segments';
import { abbreviate, fullNumber } from './model';

const C = { count: 'var(--kin-chart-1)', added: 'var(--kin-chart-2)', removed: 'var(--kin-chart-5)', grid: 'var(--line)', ink: 'var(--ink-muted)' };
const day = (iso: string) => new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });

export function CountSparkline({ items }: { items: SegmentHistory[] }) {
  const data = [...items].sort((a, b) => a.counted_at.localeCompare(b.counted_at)).slice(-30);
  if (data.length < 2) return <div className="h-10 flex items-center text-[11px] text-ink-muted" data-testid="sparkline-empty">{data.length ? 'One refresh recorded — trend appears after the next.' : 'No refresh history yet.'}</div>;
  return <div className="h-10" data-testid="chart-sparkline" aria-label={`Count history, ${data.length} refreshes`}>
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={data} margin={{ top: 2, right: 0, bottom: 0, left: 0 }}>
        <defs><linearGradient id="kin-spark" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stopColor={C.count} stopOpacity={0.35} /><stop offset="100%" stopColor={C.count} stopOpacity={0} /></linearGradient></defs>
        <YAxis hide domain={['dataMin', 'dataMax']} />
        <Tooltip cursor={false} contentStyle={{ background: 'var(--surface-raised)', border: '1px solid var(--line)', fontSize: 11 }} labelFormatter={(_, p) => p?.[0] ? day(String(p[0].payload.counted_at)) : ''} formatter={(v: number) => [fullNumber(v), 'Partners']} />
        <Area type="monotone" dataKey="count" stroke={C.count} strokeWidth={1.5} fill="url(#kin-spark)" isAnimationActive={false} />
      </AreaChart>
    </ResponsiveContainer>
  </div>;
}

/** Count over time with adds (up) and removes (down) per refresh. */
export function CountHistoryChart({ items }: { items: SegmentHistory[] }) {
  const data = [...items].sort((a, b) => a.counted_at.localeCompare(b.counted_at)).map(h => ({ ...h, removedNeg: -h.removed }));
  return <div className="h-64" data-testid="chart-count-history" role="img" aria-label={`Partner count across ${data.length} refreshes`}>
    <ResponsiveContainer width="100%" height="100%">
      <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid stroke={C.grid} vertical={false} strokeDasharray="2 4" />
        <XAxis dataKey="counted_at" tickFormatter={day} tick={{ fill: C.ink, fontSize: 11 }} stroke={C.grid} minTickGap={24} />
        <YAxis yAxisId="count" tickFormatter={v => abbreviate(Number(v))} tick={{ fill: C.ink, fontSize: 11, fontFamily: 'var(--app-font-mono)' }} stroke={C.grid} width={52} />
        <YAxis yAxisId="flow" orientation="right" tickFormatter={v => abbreviate(Number(v))} tick={{ fill: C.ink, fontSize: 10, fontFamily: 'var(--app-font-mono)' }} stroke={C.grid} width={44} />
        <Tooltip contentStyle={{ background: 'var(--surface-raised)', border: '1px solid var(--line)', fontSize: 12 }}
          labelFormatter={v => new Date(String(v)).toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' })}
          formatter={(v: number, name: string) => [fullNumber(Math.abs(v)), name]} />
        <Bar yAxisId="flow" dataKey="added" name="Added" fill={C.added} opacity={0.75} maxBarSize={10} isAnimationActive={false} />
        <Bar yAxisId="flow" dataKey="removedNeg" name="Removed" fill={C.removed} opacity={0.75} maxBarSize={10} isAnimationActive={false} />
        <Line yAxisId="count" type="monotone" dataKey="count" name="Partners" stroke={C.count} strokeWidth={2} dot={data.length < 10} isAnimationActive={false} />
      </ComposedChart>
    </ResponsiveContainer>
  </div>;
}

/** Chart/table toggle + server-generated, audit-logged CSV (analyst+). Table works for every role. */
export function HistoryView({ segmentId, items, canExport, variant }: { segmentId: number; items: SegmentHistory[]; canExport: boolean; variant: 'chart' | 'sparkline' }) {
  const [table, setTable] = useState(false);
  const exporter = useSegmentHistoryExport();
  const rows = [...items].sort((a, b) => b.counted_at.localeCompare(a.counted_at));
  const download = () => exporter.mutate(segmentId, {
    onSuccess: ({ filename, csv }) => {
      const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
      const a = document.createElement('a');
      a.href = url; a.download = filename || `segment-${segmentId}-count-history.csv`; a.dataset.unsavedOk = '1';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    },
    onError: e => toast({ variant: 'destructive', title: 'CSV export failed', description: e instanceof Error ? e.message : 'Request failed.' }),
  });
  const small = variant === 'sparkline';
  return <div data-testid={`history-view-${variant}`}>
    <div className="flex items-center justify-end gap-1 mb-1">
      <button type="button" onClick={() => setTable(t => !t)} aria-pressed={table} data-testid={`button-toggle-table-${variant}`}
        className="inline-flex items-center gap-1 h-5 px-1.5 rounded-sm text-[10px] text-ink-muted hover:text-ink border border-line"><TableIcon size={10} />{table ? 'Chart' : 'Table'}</button>
      {canExport && <button type="button" onClick={download} disabled={!items.length || exporter.isPending} data-testid={`button-export-csv-${variant}`}
        title={items.length ? 'Download CSV (audit logged)' : 'No history to export'}
        className="inline-flex items-center gap-1 h-5 px-1.5 rounded-sm text-[10px] text-ink-muted hover:text-signal border border-line disabled:opacity-40 disabled:pointer-events-none"><Download size={10} />{exporter.isPending ? 'Exporting…' : 'CSV'}</button>}
    </div>
    {table
      ? <div className={`${small ? 'max-h-32' : 'max-h-64'} overflow-auto border border-line rounded-sm`}>
          <table className="w-full text-[11px]" data-testid={`table-history-${variant}`}>
            <thead className="sticky top-0 bg-surface-raised text-ink-muted"><tr className="text-left h-7"><th className="px-2 font-medium">Refreshed</th><th className="px-2 font-medium text-right">Partners</th><th className="px-2 font-medium text-right">Added</th><th className="px-2 font-medium text-right">Removed</th></tr></thead>
            <tbody>{!rows.length ? <tr><td colSpan={4} className="h-12 text-center text-ink-muted">No refreshes recorded.</td></tr>
              : rows.map(h => <tr key={h.counted_at} className="border-t border-line h-7"><td className="px-2 font-mono text-ink-muted">{new Date(h.counted_at).toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' })}</td>
                <td className="px-2 text-right font-mono tabular-nums">{fullNumber(h.count)}</td><td className="px-2 text-right font-mono tabular-nums text-ok">+{fullNumber(h.added)}</td><td className="px-2 text-right font-mono tabular-nums text-danger">−{fullNumber(h.removed)}</td></tr>)}</tbody>
          </table>
        </div>
      : small ? <CountSparkline items={items} /> : items.length ? <CountHistoryChart items={items} /> : null}
  </div>;
}
