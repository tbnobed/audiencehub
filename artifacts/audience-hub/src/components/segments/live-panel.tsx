import { AlertTriangle, Clock, RefreshCw, Users } from 'lucide-react';
import { Skeleton } from '@/components/ui/skeleton';
import type { PartnerSample, SegmentHistory, SegmentPreview, SegmentSample } from '@/lib/segments';
import { HistoryView } from './history-chart';
import { abbreviate, fullNumber, signed } from './model';

type Q<T> = { data?: T; isFetching: boolean; isError: boolean; error: unknown; refetch: () => unknown };
export type Idle = { reason: 'empty' | 'invalid' | 'loading-fields' | null; pending: boolean };

const errText = (e: unknown) => e instanceof Error ? e.message : 'Request failed.';
export const STATUS_TONE: Record<string, string> = {
  prospect: 'var(--kin-chart-8)', new: 'var(--kin-chart-1)', active: 'var(--kin-chart-2)', reactivated: 'var(--kin-chart-6)', lapsing: 'var(--kin-chart-3)', lapsed: 'var(--kin-chart-5)',
};
const PALETTE = ['var(--kin-chart-1)', 'var(--kin-chart-2)', 'var(--kin-chart-3)', 'var(--kin-chart-4)', 'var(--kin-chart-5)', 'var(--kin-chart-6)', 'var(--kin-chart-7)', 'var(--kin-chart-8)'];

function Breakdown({ title, rows, toneFor, testId }: { title: string; rows: { key: string; count: number }[]; toneFor: (k: string, i: number) => string; testId: string }) {
  const total = rows.reduce((s, r) => s + r.count, 0);
  return <div data-testid={testId}>
    <div className="flex items-baseline justify-between mb-1"><h4 className="text-[11px] uppercase tracking-wider text-ink-muted">{title}</h4><span className="text-[10px] font-mono text-ink-muted">of sample</span></div>
    {!rows.length ? <p className="text-[11px] text-ink-muted">No values in sample.</p> : <>
      <div className="flex h-1.5 rounded-full overflow-hidden bg-surface-raised mb-1.5" aria-hidden>
        {rows.map((r, i) => <div key={r.key} style={{ width: `${(r.count / total) * 100}%`, background: toneFor(r.key, i) }} />)}
      </div>
      <ul className="grid grid-cols-2 gap-x-3 gap-y-0.5">
        {rows.map((r, i) => <li key={r.key} className="flex items-center gap-1.5 text-[11px] min-w-0">
          <span className="h-2 w-2 rounded-[2px] shrink-0" style={{ background: toneFor(r.key, i) }} />
          <span className="truncate text-ink">{r.key || 'none'}</span>
          <span className="ml-auto font-mono tabular-nums text-ink-muted">{fullNumber(r.count)}</span>
        </li>)}
      </ul>
    </>}
  </div>;
}

function SampleTable({ items }: { items: PartnerSample[] }) {
  return <div className="max-h-[340px] overflow-auto border border-line rounded-sm" data-testid="table-sample">
    <table className="w-full text-[11px]">
      <thead className="sticky top-0 bg-surface-raised text-ink-muted"><tr className="text-left"><th className="px-2 h-7 font-medium">Partner</th><th className="px-2 font-medium">Contact</th><th className="px-2 font-medium">Status</th></tr></thead>
      <tbody>{items.map(p => <tr key={p.id} className="border-t border-line h-8" data-testid={`row-sample-${p.id}`}>
        <td className="px-2"><span className="text-ink">{[p.first_name, p.last_name].filter(Boolean).join(' ') || 'Unnamed partner'}</span><span className="block font-mono text-[10px] text-ink-muted">#{p.id}</span></td>
        <td className="px-2 text-ink-muted truncate max-w-[140px]" title={p.email ?? ''}>{p.email || p.phone || '—'}<span className="block text-[10px]">{(p.source_keys ?? []).join(', ')}</span></td>
        <td className="px-2"><span className="inline-flex items-center gap-1 font-mono text-[10px] uppercase"><span className="h-1.5 w-1.5 rounded-full" style={{ background: STATUS_TONE[p.donor_status ?? ''] ?? 'var(--kin-chart-8)' }} />{p.donor_status ?? '—'}</span></td>
      </tr>)}</tbody>
    </table>
  </div>;
}

export function LivePanel({ idle, preview, sample, history, lastCount, segmentId, canExport = false }: { idle: Idle; preview: Q<SegmentPreview>; sample: Q<SegmentSample>; history?: SegmentHistory[]; lastCount?: number | null; segmentId?: number; canExport?: boolean }) {
  const count = preview.data?.count;
  const vsSaved = count != null && lastCount != null ? count - lastCount : null;
  const timeout = preview.isError && /timed out/i.test(errText(preview.error));
  return <aside className="flex flex-col gap-3 min-w-0" aria-label="Live audience preview" aria-live="polite">
    <section className="rounded-sm border border-line bg-surface p-3">
      <div className="flex items-center justify-between">
        <h3 className="text-[11px] uppercase tracking-wider text-ink-muted flex items-center gap-1.5"><span className={`h-1.5 w-1.5 rounded-full ${preview.isFetching || idle.pending ? 'bg-warn animate-pulse' : count != null ? 'bg-ok' : 'bg-ink-muted'}`} />Live count</h3>
        {preview.data && <span className="text-[10px] font-mono text-ink-muted flex items-center gap-1"><Clock size={10} />{preview.data.took_ms.toFixed(0)} ms</span>}
      </div>
      <div className="mt-1 min-h-[52px]" data-testid="text-live-count">
        {idle.reason === 'empty' ? <p className="text-xs text-ink-muted pt-2">Add a rule to see how many partners match. Counts only run against a real definition.</p>
          : idle.reason === 'invalid' ? <p className="text-xs text-warn pt-2 flex items-start gap-1.5"><AlertTriangle size={13} className="mt-0.5 shrink-0" />Fix the highlighted rules to resume counting.</p>
          : idle.reason === 'loading-fields' ? <Skeleton className="h-9 w-40 bg-surface-raised" />
          : preview.isError ? <div className="text-xs pt-1">
              <p className="text-danger flex items-start gap-1.5"><AlertTriangle size={13} className="mt-0.5 shrink-0" />{errText(preview.error)}</p>
              {timeout && <p className="text-ink-muted mt-1">Narrow a gift or event window, or add a selective rule first.</p>}
              <button type="button" onClick={() => preview.refetch()} data-testid="button-retry-count" className="mt-2 inline-flex items-center gap-1 text-signal hover:underline"><RefreshCw size={11} /> Retry count</button>
            </div>
          : count == null ? <Skeleton className="h-9 w-40 bg-surface-raised" />
          : <div className={`transition-opacity ${preview.isFetching || idle.pending ? 'opacity-50' : ''}`}>
              <span className="font-mono text-[34px] leading-none tabular-nums text-ink" title={fullNumber(count)}>{fullNumber(count)}</span>
              <span className="ml-2 text-xs text-ink-muted">partners</span>
              {vsSaved != null && <p className={`mt-1 text-[11px] font-mono ${vsSaved > 0 ? 'text-ok' : vsSaved < 0 ? 'text-danger' : 'text-ink-muted'}`}>{signed(vsSaved)} vs last refresh ({abbreviate(lastCount)})</p>}
            </div>}
      </div>
      {history && segmentId != null && <div className="mt-2 pt-2 border-t border-line"><div className="text-[10px] uppercase tracking-wider text-ink-muted mb-1">Refresh history</div><HistoryView segmentId={segmentId} items={history} canExport={canExport} variant="sparkline" /></div>}
    </section>

    <section className="rounded-sm border border-line bg-surface p-3 flex flex-col gap-3">
      <div className="flex items-center justify-between"><h3 className="text-[11px] uppercase tracking-wider text-ink-muted flex items-center gap-1.5"><Users size={12} /> Sample of 25</h3>
        {sample.data && <button type="button" onClick={() => sample.refetch()} className="text-[10px] text-ink-muted hover:text-signal inline-flex items-center gap-1" data-testid="button-refresh-sample"><RefreshCw size={10} className={sample.isFetching ? 'animate-spin' : ''} /> resample</button>}</div>
      {idle.reason ? <p className="text-[11px] text-ink-muted">A sample appears once the definition counts.</p>
        : sample.isError ? <div className="text-[11px]"><p className="text-danger">{errText(sample.error)}</p><button type="button" onClick={() => sample.refetch()} className="mt-1 text-signal hover:underline">Retry sample</button></div>
        : !sample.data ? <div className="flex flex-col gap-1.5">{Array.from({ length: 6 }, (_, i) => <Skeleton key={i} className="h-6 w-full bg-surface-raised" />)}</div>
        : <>
          <Breakdown title="By partner status" rows={sample.data.breakdowns.status} toneFor={k => STATUS_TONE[k] ?? 'var(--kin-chart-8)'} testId="breakdown-status" />
          <Breakdown title="By source" rows={sample.data.breakdowns.source} toneFor={(_, i) => PALETTE[i % PALETTE.length]} testId="breakdown-source" />
          {sample.data.items.length ? <SampleTable items={sample.data.items} /> : <p className="text-[11px] text-ink-muted">No partners match — loosen a rule to see examples.</p>}
          <p className="text-[10px] text-ink-muted">Contact fields are masked by the API according to your role.</p>
        </>}
    </section>
  </aside>;
}
