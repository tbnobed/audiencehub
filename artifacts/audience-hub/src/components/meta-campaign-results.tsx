import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Skeleton } from '@/components/ui/skeleton';
import { toast } from '@/hooks/use-toast';
import { useAuth } from '@/hooks/use-auth';
import { useSources } from '@/hooks/use-sources';
import { useCreateMetaAudience, useMetaSegmentResults, useRefreshMetaResults } from '@/hooks/use-meta';

const fmt = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : 'never');

export function MetaCampaignResults({ segmentId }: { segmentId: number }) {
  const { user } = useAuth();
  const isAdmin = user?.role === 'admin';
  const q = useMetaSegmentResults(segmentId);
  const sources = useSources();
  const create = useCreateMetaAudience(segmentId);
  const refresh = useRefreshMetaResults(segmentId);
  const [sourceId, setSourceId] = useState('');
  const [name, setName] = useState('');
  const metaSources = (sources.data?.items ?? []).filter(s => s.kind === 'meta_lead_ads');
  const err = (t: string) => (e: any) => toast({ title: t, description: e.message, variant: 'destructive' });
  const busy = q.data?.jobs.some(j => ['queued', 'running'].includes(j.status));

  return (
    <div className="flex flex-col gap-3" data-testid="panel-meta-results">
      <div className="rounded-sm border border-line bg-surface px-3 py-2 text-xs text-ink-muted">
        Viewing / VOD rules and attributes are blocked for every Meta export. The backend rejects audiences that use them.
      </div>
      {isAdmin && (
        <div className="rounded-sm border border-line bg-surface p-3 space-y-2">
          <h3 className="kin-heading">Create Meta custom audience</h3>
          <p className="text-[11px] text-ink-muted">Creates a Kinship-owned custom audience and a linked connected Meta destination. Requires a Meta source with an ads access token and ad account ID.</p>
          {!metaSources.length ? <p className="text-xs text-ink-muted" data-testid="empty-meta-sources">No Meta Lead Ads source configured.</p> : (
            <div className="flex flex-wrap items-end gap-2">
              <div className="space-y-1"><Label htmlFor="meta-aud-source">Source</Label>
                <select id="meta-aud-source" data-testid="select-meta-source" value={sourceId} onChange={e => setSourceId(e.target.value)} className="h-8 rounded-md border border-input bg-background px-2 text-xs">
                  <option value="">Select source</option>{metaSources.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></div>
              <div className="space-y-1"><Label htmlFor="meta-aud-name">Audience name</Label><Input id="meta-aud-name" data-testid="input-meta-audience-name" className="h-8 text-xs" value={name} onChange={e => setName(e.target.value)} /></div>
              <Button size="sm" className="h-8 text-xs" data-testid="button-meta-create-audience" disabled={!sourceId || !name.trim() || create.isPending}
                onClick={() => create.mutate({ source_id: Number(sourceId), name: name.trim() }, { onSuccess: r => { setName(''); toast({ title: 'Meta audience created', description: `Audience ${r.meta_audience_id}, destination #${r.destination_id}` }); }, onError: err('Could not create audience') })}>
                {create.isPending ? 'Creating...' : 'Create audience'}</Button>
            </div>)}
          {create.isError && <p role="alert" className="text-xs text-danger">{(create.error as Error).message}</p>}
        </div>
      )}
      {q.isLoading ? <Skeleton className="h-40 bg-surface-raised" /> : q.isError ? (
        <div className="p-4 text-center text-xs"><p className="text-danger">Unable to load Meta results</p><p className="text-ink-muted">{(q.error as Error).message}</p><Button size="sm" variant="outline" className="mt-2 h-7 text-xs" onClick={() => q.refetch()}>Retry</Button></div>
      ) : (<>
        <div className="rounded-sm border border-line bg-surface overflow-x-auto"><table className="w-full text-xs" data-testid="table-meta-audiences">
          <thead className="bg-surface-raised text-ink-muted text-[11px]"><tr className="text-left h-8"><th className="px-3 font-medium">Meta audience</th><th className="px-2 font-medium">Meta ID</th><th className="px-2 font-medium">Destination</th><th className="px-2 font-medium">Last refreshed</th></tr></thead>
          <tbody>{!q.data!.audiences.length ? <tr><td colSpan={4} className="h-20 text-center text-ink-muted">No Meta audiences linked to this segment.</td></tr>
            : q.data!.audiences.map(a => <tr key={a.id} className="border-t border-line h-8"><td className="px-3">{a.name}</td><td className="px-2 font-mono text-ink-muted">{a.meta_audience_id}</td><td className="px-2 font-mono text-ink-muted">#{a.destination_id}</td><td className="px-2 text-ink-muted">{fmt(a.last_refreshed_at)}</td></tr>)}</tbody>
        </table></div>
        <div className="flex items-center justify-between gap-2">
          <p className="text-[11px] text-ink-muted max-w-xl">Results are attributed per ad set. Audiences can overlap, so reach and conversions across ad sets are not additive.</p>
          {isAdmin && <Button size="sm" variant="outline" className="h-8 text-xs" data-testid="button-meta-refresh" disabled={refresh.isPending || busy}
            onClick={() => refresh.mutate(undefined, { onSuccess: r => toast({ title: 'Results refresh queued', description: `Job #${r.job_id}` }), onError: err('Refresh failed') })}>{busy ? 'Refreshing...' : 'Refresh results'}</Button>}
        </div>
        <div className="rounded-sm border border-line bg-surface overflow-x-auto"><table className="w-full text-xs min-w-[640px]" data-testid="table-meta-results">
          <thead className="bg-surface-raised text-ink-muted text-[11px]"><tr className="text-left h-8"><th className="px-3 font-medium">Ad set</th><th className="px-2 font-medium">Period</th><th className="px-2 font-medium text-right">Spend</th><th className="px-2 font-medium text-right">Reach</th><th className="px-2 font-medium text-right">Conversions</th><th className="px-2 font-medium">Attribution</th></tr></thead>
          <tbody>{!q.data!.items.length ? <tr><td colSpan={6} className="h-24 text-center text-ink-muted">No campaign results yet. Refresh results after an audience is used in an ad set.</td></tr>
            : q.data!.items.map((r, i) => <tr key={`${r.adset_id}-${r.date_start}-${i}`} className="border-t border-line h-8"><td className="px-3">{r.adset_name}<span className="ml-2 font-mono text-[10px] text-ink-muted">{r.adset_id}</span></td><td className="px-2 font-mono text-ink-muted">{r.date_start} to {r.date_stop}</td><td className="px-2 text-right font-mono tabular-nums">{Number(r.spend).toFixed(2)} {r.currency}</td><td className="px-2 text-right font-mono tabular-nums">{r.reach.toLocaleString()}</td><td className="px-2 text-right font-mono tabular-nums">{r.conversions.toLocaleString()}</td><td className="px-2 text-ink-muted">{r.attribution_note ?? 'Ad-set level'}</td></tr>)}</tbody>
        </table></div>
        {q.data!.jobs.length > 0 && <div className="rounded-sm border border-line bg-surface px-3 py-2 text-[11px] space-y-1" data-testid="list-meta-jobs">{q.data!.jobs.slice(0, 5).map(j => <div key={j.id} className="flex gap-2"><span className="font-mono">job #{j.id}</span><span className="font-mono uppercase">{j.status}</span><span className="text-ink-muted">{fmt(j.created_at)}</span>{j.error && <span className="text-danger">{j.error}</span>}</div>)}</div>}
      </>)}
    </div>
  );
}
