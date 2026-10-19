import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { Archive, Copy, MoreHorizontal, Pencil, Plus, RefreshCw, Target } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { Skeleton } from '@/components/ui/skeleton';
import { Checkbox } from '@/components/ui/checkbox';
import { useSegmentActions, useSegments } from '@/hooks/use-segments';
import { toast } from '@/hooks/use-toast';
import type { Segment } from '@/lib/segments';
import { fullNumber, relativeTime, signed } from './model';
import { NativeSelect } from './rule-editor';
import { ArchiveDialog, StatusPill, errorMessage, toastError, useDuplicate, useSegmentRole } from './shared';

export function SegmentList() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const status = params.get('status') ?? '';
  const owner = params.get('owner') ?? '';
  const { canEdit } = useSegmentRole();
  const listQ = useSegments(status, owner);
  const allQ = useSegments('', '');
  const { materialize, bulk } = useSegmentActions();
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const duplicate = useDuplicate();
  const [archiving, setArchiving] = useState<Segment | null>(null);
  const owners = useMemo(() => {
    const m = new Map<number, string>();
    for (const s of allQ.data?.items ?? []) if (s.created_by != null) m.set(s.created_by, s.owner ?? `User #${s.created_by}`);
    return [...m.entries()].sort((a, b) => a[1].localeCompare(b[1]));
  }, [allQ.data]);
  const setFilter = (k: string, v: string) => { const n = new URLSearchParams(params); if (v) n.set(k, v); else n.delete(k); setParams(n, { replace: true }); };
  const items = listQ.data?.items ?? [];
  useEffect(() => { setSelected(new Set()); }, [status, owner]);
  useEffect(() => {
    if (!listQ.data) return;
    const selectable = new Set(listQ.data.items.filter(s => s.status !== 'archived').map(s => s.id));
    setSelected(previous => new Set([...previous].filter(id => selectable.has(id))));
  }, [listQ.data]);
  const selectable = items.filter(s => s.status !== 'archived');
  const selection = selectable.filter(s => selected.has(s.id));
  const allSelected = selectable.length > 0 && selection.length === selectable.length;
  const toggle = (id: number, checked: boolean) => setSelected(previous => {
    const next = new Set(previous);
    if (checked) next.add(id); else next.delete(id);
    return next;
  });
  const runBulk = (action: 'activate' | 'pause' | 'refresh') => bulk.mutate(
    { ids: selection.map(s => s.id), action },
    { onSuccess: result => {
      setSelected(new Set());
      toast({ title: action === 'refresh' ? 'Refresh queued' : action === 'activate' ? 'Audiences activated' : 'Audiences paused',
        description: `${result.ids.length} audiences${action === 'pause' ? ' · automatic refresh paused; saved counts retained.' : ` · ${result.job_ids.length} refresh jobs queued or already pending.`}` });
    }, onError: error => toastError('Bulk action failed', error) },
  );
  const totals = useMemo(() => ({ active: (allQ.data?.items ?? []).filter(s => s.status === 'active').length, draft: (allQ.data?.items ?? []).filter(s => s.status === 'draft').length }), [allQ.data]);
  const refresh = (s: Segment) => materialize.mutate(s.id, { onSuccess: r => toast({ title: 'Refresh queued', description: `${s.name} · job #${r.job_id}` }), onError: e => toastError('Refresh failed', e) });

  return <div className="flex flex-col gap-3 min-w-0" data-testid="page-segments">
    <header className="flex flex-wrap items-end gap-3">
      <div className="mr-auto">
        <h1 className="kin-title">Partner audiences</h1>
        <p className="text-xs text-ink-muted mt-0.5">Rule-defined groups of partners, refreshed on schedule and before every activation. <span className="font-mono">{totals.active}</span> active · <span className="font-mono">{totals.draft}</span> drafts</p>
      </div>
      <NativeSelect aria-label="Filter by status" data-testid="select-filter-status" value={status} onChange={e => setFilter('status', e.target.value)}>
        <option value="">All statuses</option><option value="active">Active</option><option value="draft">Draft</option><option value="archived">Archived</option>
      </NativeSelect>
      <NativeSelect aria-label="Filter by owner" data-testid="select-filter-owner" value={owner} onChange={e => setFilter('owner', e.target.value)}>
        <option value="">All owners</option>{owners.map(([id, name]) => <option key={id} value={String(id)}>{name}</option>)}
      </NativeSelect>
      {canEdit && <Button size="sm" className="h-8 text-xs" asChild data-testid="button-new-segment"><Link to="/segments/new"><Plus size={13} className="mr-1" /> New audience</Link></Button>}
    </header>

    {canEdit && selection.length > 0 && <div className="flex flex-wrap items-center gap-2 rounded-sm border border-line bg-surface px-3 py-2" role="region" aria-label="Selected audience actions">
      <span className="text-xs mr-auto">{selection.length} selected</span>
      <Button size="sm" variant="outline" disabled={bulk.isPending} onClick={() => runBulk('activate')} data-testid="button-bulk-activate">Activate</Button>
      <Button size="sm" variant="outline" disabled={bulk.isPending} onClick={() => runBulk('pause')} data-testid="button-bulk-pause">Pause</Button>
      <Button size="sm" variant="outline" disabled={bulk.isPending || selection.some(s => s.status !== 'active')} title={selection.some(s => s.status !== 'active') ? 'Activate selected audiences before refreshing' : undefined} onClick={() => runBulk('refresh')} data-testid="button-bulk-refresh"><RefreshCw size={12} className="mr-1" />Refresh now</Button>
      <Button size="sm" variant="ghost" disabled={bulk.isPending} onClick={() => setSelected(new Set())}>Clear selection</Button>
    </div>}
    <div className="rounded-sm border border-line bg-surface overflow-x-auto" aria-busy={bulk.isPending}>
      <table className="w-full text-xs min-w-[860px]" data-testid="table-segments">
        <thead className="sticky top-0 bg-surface-raised text-ink-muted text-[11px]"><tr className="text-left h-8">
          {canEdit && <th className="px-2 w-8"><Checkbox aria-label="Select all visible non-archived audiences" checked={allSelected ? true : selection.length ? 'indeterminate' : false} disabled={!selectable.length || bulk.isPending} onCheckedChange={checked => setSelected(checked === true ? new Set(selectable.map(s => s.id)) : new Set())} data-testid="checkbox-select-all-audiences" /></th>}
          <th className="px-3 font-medium">Audience</th><th className="px-2 font-medium">Status</th><th className="px-2 font-medium text-right">Partners</th><th className="px-2 font-medium text-right">Change</th>
          <th className="px-2 font-medium">Last refreshed</th><th className="px-2 font-medium">Owner</th><th className="px-2 font-medium text-right">Activations</th><th className="w-10" />
        </tr></thead>
        <tbody>
          {listQ.isLoading ? Array.from({ length: 8 }, (_, i) => <tr key={i} className="border-t border-line h-8">{Array.from({ length: canEdit ? 9 : 8 }, (_, j) => <td key={j} className="px-2"><Skeleton className="h-3.5 w-full bg-surface-raised" /></td>)}</tr>)
            : listQ.isError ? <tr><td colSpan={canEdit ? 9 : 8} className="h-40 text-center"><p className="text-danger">Unable to load audiences</p><p className="text-ink-muted mt-1">{errorMessage(listQ.error)}</p><Button size="sm" variant="outline" className="mt-3 h-7 text-xs" onClick={() => listQ.refetch()} data-testid="button-retry-segments"><RefreshCw size={12} className="mr-1" /> Retry</Button></td></tr>
            : !items.length ? <tr><td colSpan={canEdit ? 9 : 8} className="h-48 text-center" data-testid="empty-segments">
                <Target size={22} className="mx-auto text-ink-muted mb-2" />
                <p className="text-sm text-ink">{status || owner ? 'No audiences match these filters' : 'No partner audiences yet'}</p>
                <p className="text-ink-muted mt-1">{status || owner ? 'Clear a filter to see more.' : canEdit ? 'Create an audience to define who receives an activation.' : 'An analyst can create the first audience.'}</p>
                {canEdit && !status && !owner && <Button size="sm" className="mt-3 h-7 text-xs" asChild><Link to="/segments/new"><Plus size={12} className="mr-1" /> New audience</Link></Button>}
              </td></tr>
            : items.map(s => <tr key={s.id} data-testid={`row-segment-${s.id}`} className="border-t border-line h-9 hover:bg-surface-raised cursor-pointer" onClick={() => navigate(`/segments/${s.id}`)}>
                {canEdit && <td className="px-2" onClick={e => e.stopPropagation()}><Checkbox aria-label={`Select ${s.name}`} checked={selected.has(s.id)} disabled={s.status === 'archived' || bulk.isPending} onCheckedChange={checked => toggle(s.id, checked === true)} data-testid={`checkbox-audience-${s.id}`} /></td>}
                <td className="px-3 max-w-[320px]"><Link to={`/segments/${s.id}`} onClick={e => e.stopPropagation()} className="text-ink font-medium hover:text-signal" data-testid={`link-segment-${s.id}`}>{s.name}</Link>
                  {s.description && <span className="block text-[11px] text-ink-muted truncate">{s.description}</span>}</td>
                <td className="px-2"><StatusPill status={s.status} /></td>
                <td className="px-2 text-right font-mono tabular-nums" data-testid={`text-count-${s.id}`}>{fullNumber(s.last_count)}</td>
                <td className={`px-2 text-right font-mono tabular-nums ${s.change == null ? 'text-ink-muted' : s.change > 0 ? 'text-ok' : s.change < 0 ? 'text-danger' : 'text-ink-muted'}`}>{s.change == null ? '—' : signed(s.change)}</td>
                <td className="px-2 text-ink-muted" title={s.last_materialized_at ?? ''}>{relativeTime(s.last_materialized_at)}</td>
                <td className="px-2 text-ink-muted">{s.owner ?? '—'}</td>
                <td className="px-2 text-right font-mono tabular-nums">{s.activation_count ? <span className="text-signal">{s.activation_count}</span> : <span className="text-ink-muted">0</span>}</td>
                <td className="px-1" onClick={e => e.stopPropagation()}>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild><button type="button" aria-label={`Actions for ${s.name}`} data-testid={`button-actions-${s.id}`} className="h-6 w-6 inline-flex items-center justify-center rounded-sm text-ink-muted hover:text-ink hover:bg-surface"><MoreHorizontal size={14} /></button></DropdownMenuTrigger>
                    <DropdownMenuContent align="end" className="text-xs">
                      <DropdownMenuItem onSelect={() => navigate(`/segments/${s.id}`)}>Open details</DropdownMenuItem>
                      {canEdit && s.status !== 'archived' && <DropdownMenuItem onSelect={() => navigate(`/segments/${s.id}/edit`)} data-testid={`menu-edit-${s.id}`}><Pencil size={12} className="mr-2" /> Edit rules</DropdownMenuItem>}
                      {canEdit && <DropdownMenuItem disabled={duplicate.pending} onSelect={() => duplicate.run(s)} data-testid={`menu-duplicate-${s.id}`}><Copy size={12} className="mr-2" /> Duplicate</DropdownMenuItem>}
                      {canEdit && s.status === 'active' && <DropdownMenuItem onSelect={() => refresh(s)} data-testid={`menu-refresh-${s.id}`}><RefreshCw size={12} className="mr-2" /> Refresh now</DropdownMenuItem>}
                      {canEdit && s.status !== 'archived' && <><DropdownMenuSeparator /><DropdownMenuItem className="text-danger focus:text-danger" onSelect={() => setArchiving(s)} data-testid={`menu-archive-${s.id}`}><Archive size={12} className="mr-2" /> Archive…</DropdownMenuItem></>}
                    </DropdownMenuContent>
                  </DropdownMenu>
                </td>
              </tr>)}
        </tbody>
      </table>
    </div>
    <ArchiveDialog segment={archiving} open={!!archiving} onOpenChange={o => { if (!o) setArchiving(null); }} />
  </div>;
}
