import { useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Archive, ArrowLeft, Copy, Lock, Pencil, RefreshCw, Send } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useSegment, useSegmentActivity, useSegmentFields, useSegmentHistory, useSegmentMembers, useSegments } from '@/hooks/use-segments';
import type { Segment } from '@/lib/segments';
import { HistoryView } from './history-chart';
import { STATUS_TONE } from './live-panel';
import { SCHEDULES, abbreviate, countRules, fullNumber, relativeTime, signed, summarize } from './model';
import { ArchiveDialog, StatusPill, errorMessage, useDuplicate, useRefresh, useSegmentRole } from './shared';

const when = (iso: string) => new Date(iso).toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' });
function Tile({ label, value, sub, tone, testId }: { label: string; value: string; sub?: string; tone?: string; testId: string }) {
  return <div className="rounded-sm border border-line bg-surface px-3 py-2 min-w-0">
    <div className="text-[10px] uppercase tracking-wider text-ink-muted">{label}</div>
    <div className={`font-mono text-xl tabular-nums leading-7 ${tone ?? 'text-ink'}`} data-testid={testId}>{value}</div>
    {sub && <div className="text-[11px] text-ink-muted truncate">{sub}</div>}
  </div>;
}
function Retry({ what, error, onRetry }: { what: string; error: unknown; onRetry: () => unknown }) {
  return <div className="p-6 text-center text-xs"><p className="text-danger">Unable to load {what}</p><p className="text-ink-muted mt-1">{errorMessage(error)}</p><Button size="sm" variant="outline" className="mt-3 h-7 text-xs" onClick={() => onRetry()}><RefreshCw size={12} className="mr-1" /> Retry</Button></div>;
}
const rowsSkeleton = (cols: number) => Array.from({ length: 6 }, (_, i) => <tr key={i} className="border-t border-line h-8">{Array.from({ length: cols }, (_, j) => <td key={j} className="px-2"><Skeleton className="h-3.5 bg-surface-raised" /></td>)}</tr>);

function Members({ id }: { id: number }) {
  const [stack, setStack] = useState<string[]>(['']);
  const cursor = stack[stack.length - 1];
  const q = useSegmentMembers(id, cursor);
  const page = stack.length;
  return <div className="rounded-sm border border-line bg-surface">
    <div className="overflow-x-auto"><table className="w-full text-xs min-w-[640px]" data-testid="table-members">
      <thead className="bg-surface-raised text-ink-muted text-[11px]"><tr className="text-left h-8"><th className="px-3 font-medium">Partner</th><th className="px-2 font-medium">Email</th><th className="px-2 font-medium">Phone</th><th className="px-2 font-medium">Status</th><th className="px-2 font-medium">Sources</th></tr></thead>
      <tbody>{q.isLoading ? rowsSkeleton(5)
        : q.isError ? <tr><td colSpan={5}><Retry what="members" error={q.error} onRetry={q.refetch} /></td></tr>
        : !q.data?.items.length ? <tr><td colSpan={5} className="h-32 text-center text-ink-muted" data-testid="empty-members">{page > 1 ? 'No more members.' : 'No materialized members yet. Activate the audience or run a refresh to populate membership.'}</td></tr>
        : q.data.items.map(p => <tr key={p.id} className="border-t border-line h-8" data-testid={`row-member-${p.id}`}>
          <td className="px-3"><Link to={`/profiles/${p.id}`} className="text-ink hover:text-signal">{[p.first_name, p.last_name].filter(Boolean).join(' ') || 'Unnamed partner'}</Link><span className="ml-2 font-mono text-[10px] text-ink-muted">#{p.id}</span></td>
          <td className="px-2 text-ink-muted">{p.email ?? '—'}</td><td className="px-2 text-ink-muted">{p.phone ?? '—'}</td>
          <td className="px-2"><span className="inline-flex items-center gap-1 font-mono text-[10px] uppercase"><span className="h-1.5 w-1.5 rounded-full" style={{ background: STATUS_TONE[p.donor_status ?? ''] ?? 'var(--kin-chart-8)' }} />{p.donor_status ?? '—'}</span></td>
          <td className="px-2 text-ink-muted">{(p.source_keys ?? []).join(', ') || '—'}</td>
        </tr>)}</tbody>
    </table></div>
    <div className="flex items-center justify-end gap-2 border-t border-line px-3 h-9 text-[11px] text-ink-muted">
      <span className="font-mono">Page {page}</span>
      <Button size="sm" variant="outline" className="h-6 text-[11px]" disabled={page === 1} onClick={() => setStack(s => s.slice(0, -1))} data-testid="button-members-prev">Previous</Button>
      <Button size="sm" variant="outline" className="h-6 text-[11px]" disabled={!q.data?.next_cursor} onClick={() => q.data?.next_cursor && setStack(s => [...s, q.data!.next_cursor!])} data-testid="button-members-next">Next</Button>
    </div>
  </div>;
}

export function SegmentDetail({ id }: { id: number }) {
  const navigate = useNavigate();
  const { canEdit } = useSegmentRole();
  const segQ = useSegment(id);
  const histQ = useSegmentHistory(id);
  const actQ = useSegmentActivity(id);
  const fieldsQ = useSegmentFields();
  const allQ = useSegments('', '');
  const duplicate = useDuplicate();
  const [archiveOpen, setArchiveOpen] = useState(false);
  const s = segQ.data;
  const { start, pending, starting } = useRefresh(s, () => { segQ.refetch(); histQ.refetch(); });
  const summary = useMemo(() => s && fieldsQ.data ? summarize(s.definition, fieldsQ.data.fields, sid => allQ.data?.items.find(x => x.id === sid)?.name) : '', [s, fieldsQ.data, allQ.data]);
  const dependents = useMemo(() => (allQ.data?.items ?? []).filter(o => o.id !== id && JSON.stringify(o.definition).includes(`"segment_id":${id}`)), [allQ.data, id]);
  const history = histQ.data?.items ?? [];

  if (segQ.isLoading) return <div className="flex flex-col gap-3" data-testid="skeleton-detail"><Skeleton className="h-8 w-72 bg-surface-raised" /><div className="grid grid-cols-4 gap-2">{Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-16 bg-surface-raised" />)}</div><Skeleton className="h-64 bg-surface-raised" /></div>;
  if (segQ.isError || !s) return <div data-testid="error-segment"><Retry what="this audience" error={segQ.error} onRetry={segQ.refetch} /><div className="text-center"><Link to="/segments" className="text-xs text-signal">Back to audiences</Link></div></div>;
  const schedule = s.refresh_schedule ? SCHEDULES.find(x => x.value === s.refresh_schedule)?.label ?? s.refresh_schedule : 'Manual only';

  return <div className="flex flex-col gap-3 min-w-0 pb-8" data-testid="page-segment-detail">
    <header className="flex flex-wrap items-start gap-3">
      <Link to="/segments" className="mt-1 text-ink-muted hover:text-ink" aria-label="Back to audiences" data-testid="link-back"><ArrowLeft size={16} /></Link>
      <div className="flex-1 min-w-[240px]">
        <div className="flex items-center gap-2 flex-wrap"><h1 className="kin-title" data-testid="text-segment-name">{s.name}</h1><StatusPill status={s.status} testId="status-segment" /><span className="font-mono text-[11px] text-ink-muted">#{s.id}</span>
          {pending && <span className="inline-flex items-center gap-1 text-[11px] text-warn font-mono" data-testid="status-refresh-pending"><RefreshCw size={11} className="animate-spin" /> refreshing · job #{pending.job}</span>}</div>
        <p className="text-xs text-ink-muted mt-0.5">{s.description || 'No description.'} <span className="mx-1">·</span> Owner {s.owner ?? '—'} <span className="mx-1">·</span> {schedule} <span className="mx-1">·</span> updated {relativeTime(s.updated_at)}</p>
      </div>
      <div className="flex flex-wrap gap-2">
        {canEdit && s.status === 'active' && <Button size="sm" variant="outline" className="h-8 text-xs" disabled={starting || !!pending} onClick={start} data-testid="button-refresh"><RefreshCw size={12} className={`mr-1 ${pending ? 'animate-spin' : ''}`} />{pending ? 'Refreshing…' : 'Refresh now'}</Button>}
        {canEdit && <Button size="sm" variant="outline" className="h-8 text-xs" disabled={duplicate.pending} onClick={() => duplicate.run(s)} data-testid="button-duplicate"><Copy size={12} className="mr-1" /> Duplicate</Button>}
        {canEdit && s.status !== 'archived' && <Button size="sm" variant="outline" className="h-8 text-xs text-danger hover:text-danger" onClick={() => setArchiveOpen(true)} data-testid="button-archive"><Archive size={12} className="mr-1" /> Archive</Button>}
        <Button size="sm" className="h-8 text-xs" asChild data-testid="button-edit"><Link to={`/segments/${s.id}/edit`}>{canEdit && s.status !== 'archived' ? <><Pencil size={12} className="mr-1" /> Edit rules</> : 'View rules'}</Link></Button>
      </div>
    </header>

    <div className="grid grid-cols-2 xl:grid-cols-4 gap-2">
      <Tile label="Partners" value={abbreviate(s.last_count)} sub={s.last_count != null ? `${fullNumber(s.last_count)} at last refresh` : 'Not materialized yet'} testId="text-detail-count" />
      <Tile label="Change since last refresh" value={signed(s.change)} tone={s.change == null ? 'text-ink-muted' : s.change > 0 ? 'text-ok' : s.change < 0 ? 'text-danger' : undefined} sub={history.length ? `${fullNumber(history[history.length - 1]?.added)} added · ${fullNumber(history[history.length - 1]?.removed)} removed` : undefined} testId="text-detail-change" />
      <Tile label="Last refreshed" value={relativeTime(s.last_materialized_at)} sub={s.last_materialized_at ? when(s.last_materialized_at) : 'Runs on activate, schedule, and before activations'} testId="text-detail-refreshed" />
      <Tile label="Used by" value={`${s.activation_count}`} sub={`activation${s.activation_count === 1 ? '' : 's'}${dependents.length ? ` · ${dependents.length} audience${dependents.length === 1 ? '' : 's'}` : ''}`} testId="text-detail-activations" />
    </div>

    <section className="rounded-sm border border-line bg-surface px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-ink-muted mb-0.5">Definition · {countRules(s.definition.root)} rules</div>
      {summary ? <p className="text-[13px] leading-5" data-testid="text-summary">{summary}</p> : <Skeleton className="h-4 w-2/3 bg-surface-raised" />}
    </section>

    <Tabs defaultValue="history">
      <TabsList className="h-8 bg-surface border border-line">
        <TabsTrigger value="history" className="text-xs h-6" data-testid="tab-history">Count history</TabsTrigger>
        <TabsTrigger value="members" className="text-xs h-6" data-testid="tab-members">Members</TabsTrigger>
        <TabsTrigger value="activations" className="text-xs h-6" data-testid="tab-activations">Activations <span className="ml-1 font-mono text-[10px] text-ink-muted">{actQ.data?.activations.length ?? s.activation_count}</span></TabsTrigger>
        <TabsTrigger value="audit" className="text-xs h-6" data-testid="tab-audit">Audit trail</TabsTrigger>
      </TabsList>
      <TabsContent value="history" className="mt-2">
        <div className="rounded-sm border border-line bg-surface p-3">
          <div className="flex items-baseline justify-between mb-2"><h3 className="kin-heading">Partners per refresh</h3><span className="text-[11px] text-ink-muted">line: total · bars: added / removed</span></div>
          {histQ.isLoading ? <Skeleton className="h-64 bg-surface-raised" /> : histQ.isError ? <Retry what="count history" error={histQ.error} onRetry={histQ.refetch} />
            : <>
              <HistoryView segmentId={s.id} items={history} canExport={canEdit} variant="chart" />
              {!history.length && <div className="h-40 flex items-center justify-center text-xs text-ink-muted text-center" data-testid="empty-history">No refreshes recorded. {s.status === 'active' ? 'Run Refresh now to record the first count.' : 'Activate this audience to start recording counts.'}</div>}
            </>}
        </div>
      </TabsContent>
      <TabsContent value="members" className="mt-2">
        {canEdit ? <Members id={id} /> : <div className="rounded-sm border border-line bg-surface p-6 text-center text-xs text-ink-muted" data-testid="members-restricted"><Lock size={16} className="mx-auto mb-2" />Member lists are available to analysts and admins.</div>}
      </TabsContent>
      <TabsContent value="activations" className="mt-2">
        <ActivityTable segment={s} kind="activations" q={actQ} dependents={dependents} />
      </TabsContent>
      <TabsContent value="audit" className="mt-2">
        <ActivityTable segment={s} kind="audit" q={actQ} dependents={[]} />
      </TabsContent>
    </Tabs>
    <ArchiveDialog segment={s} open={archiveOpen} onOpenChange={setArchiveOpen} onDone={() => navigate('/segments')} />
  </div>;
}

type ActQ = ReturnType<typeof useSegmentActivity>;
function ActivityTable({ segment, kind, q, dependents }: { segment: Segment; kind: 'activations' | 'audit'; q: ActQ; dependents: Segment[] }) {
  if (q.isLoading) return <div className="rounded-sm border border-line bg-surface"><table className="w-full"><tbody>{rowsSkeleton(3)}</tbody></table></div>;
  if (q.isError) return <div className="rounded-sm border border-line bg-surface"><Retry what={kind === 'audit' ? 'the audit trail' : 'linked activations'} error={q.error} onRetry={q.refetch} /></div>;
  if (kind === 'activations') {
    const rows = q.data?.activations ?? [];
    return <div className="flex flex-col gap-2">
      <div className="rounded-sm border border-line bg-surface"><table className="w-full text-xs" data-testid="table-activations">
        <thead className="bg-surface-raised text-ink-muted text-[11px]"><tr className="text-left h-8"><th className="px-3 font-medium">Activation</th><th className="px-2 font-medium">Status</th><th className="px-2 font-medium w-24" /></tr></thead>
        <tbody>{!rows.length ? <tr><td colSpan={3} className="h-28 text-center text-ink-muted" data-testid="empty-activations">No activations send “{segment.name}” anywhere yet. Create one from Activations to sync it to a destination.</td></tr>
          : rows.map(a => <tr key={a.id} className="border-t border-line h-8" data-testid={`row-activation-${a.id}`}><td className="px-3"><span className="text-ink">{a.name}</span><span className="ml-2 font-mono text-[10px] text-ink-muted">#{a.id}</span></td><td className="px-2"><StatusPill status={a.status} /></td><td className="px-2 text-right"><Link to={`/activations/${a.id}`} className="text-signal text-[11px] inline-flex items-center gap-1"><Send size={11} /> Open</Link></td></tr>)}</tbody>
      </table></div>
      {dependents.length > 0 && <div className="rounded-sm border border-line bg-surface px-3 py-2 text-xs"><span className="text-ink-muted">Referenced by audiences: </span>{dependents.map((d, i) => <span key={d.id}>{i > 0 && ', '}<Link to={`/segments/${d.id}`} className="text-signal">{d.name}</Link></span>)}</div>}
    </div>;
  }
  const rows = q.data?.audit ?? [];
  return <div className="rounded-sm border border-line bg-surface"><table className="w-full text-xs" data-testid="table-audit">
    <thead className="bg-surface-raised text-ink-muted text-[11px]"><tr className="text-left h-8"><th className="px-3 font-medium">When</th><th className="px-2 font-medium">Action</th><th className="px-2 font-medium">User</th></tr></thead>
    <tbody>{!rows.length ? <tr><td colSpan={3} className="h-28 text-center text-ink-muted" data-testid="empty-audit">No audit entries for this audience.</td></tr>
      : rows.map((a, i) => <tr key={i} className="border-t border-line h-8" data-testid={`row-audit-${i}`}><td className="px-3 font-mono text-ink-muted tabular-nums">{when(a.created_at)}</td><td className="px-2"><span className="font-mono text-[11px] text-ink">{a.action}</span></td><td className="px-2 text-ink-muted font-mono">{a.user_id == null ? 'system' : `user #${a.user_id}`}</td></tr>)}</tbody>
  </table></div>;
}
