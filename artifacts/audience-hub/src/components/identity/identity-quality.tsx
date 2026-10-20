import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { RefreshCw, Play, FlaskConical, TrendingUp } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { useAuth } from '@/hooks/use-auth';
import { toast } from '@/hooks/use-toast';
import { errorMessage } from '@/lib/identity';
import {
  useGraphPolicy, useUpdateGraphPolicy, useGraphRuns, useStartGraphRun, useQualityItems, useSampleQuality,
  useQualityVerdict, useGraphTrends, type GraphPolicy, type Verdict, type QualityItem,
} from '@/hooks/use-identity';

const field = 'rounded-sm border border-line-strong bg-surface-raised px-3 py-2 text-sm text-ink w-full';
const when = (v: string | null) => (v ? new Date(v).toLocaleString() : '—');
const pct = (n: number) => `${(n * 100).toFixed(1)}%`;

function ErrorBox({ what, error, retry }: { what: string; error: unknown; retry: () => void }) {
  return <div role="alert" className="border border-danger/30 bg-danger/5 rounded-md p-4 text-sm flex items-center justify-between gap-3"><span>{what} could not be loaded: {errorMessage(error)}</span><Button size="sm" variant="outline" onClick={retry}><RefreshCw size={13} className="mr-2" />Retry</Button></div>;
}

export function IdentityQualityPanel() {
  return <div className="space-y-5" data-testid="panel-identity-quality"><PolicyCard /><RunsCard /><ReviewCard /></div>;
}

function PolicyCard() {
  const q = useGraphPolicy();
  const save = useUpdateGraphPolicy();
  const [f, setF] = useState<GraphPolicy | null>(null);
  useEffect(() => { if (q.data) setF(q.data); }, [q.data]);
  if (q.isLoading) return <Skeleton className="h-56" />;
  if (q.isError) return <ErrorBox what="Graph settings" error={q.error} retry={() => q.refetch()} />;
  if (!f || !q.data) return null;
  const set = <K extends keyof GraphPolicy>(k: K, v: GraphPolicy[K]) => { save.reset(); setF({ ...f, [k]: v }); };
  const num = (k: keyof GraphPolicy, v: string) => set(k, (v === '' ? NaN : Number(v)) as never);
  const err = !(f.review_threshold > 0 && f.review_threshold < 1 && f.auto_threshold > 0 && f.auto_threshold < 1) ? 'Thresholds must be greater than 0 and less than 1.'
    : f.auto_threshold <= f.review_threshold ? 'Auto-link threshold must be greater than the review threshold.'
    : !(Number.isInteger(f.device_expiry_days) && f.device_expiry_days >= 1 && f.device_expiry_days <= 3650) ? 'Device expiry must be a whole number from 1 to 3650 days.'
    : !(Number.isInteger(f.schedule_hour) && f.schedule_hour >= 0 && f.schedule_hour <= 23) ? 'Schedule hour must be 0 to 23.' : null;
  const dirty = JSON.stringify(f) !== JSON.stringify(q.data);
  return <Card><CardHeader><CardTitle className="text-base">Identity graph policy</CardTitle><CardDescription>Probabilistic linking is reversible: every automatic merge can be reviewed and unmerged.</CardDescription></CardHeader>
    <CardContent className="space-y-4">
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" data-testid="checkbox-graph-enabled" checked={f.enabled} onChange={e => set('enabled', e.target.checked)} /> Graph linking enabled</label>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        <label className="text-xs text-ink-muted space-y-1 block">Review threshold<input data-testid="input-review-threshold" className={field} type="number" step="0.01" min="0" max="1" value={Number.isNaN(f.review_threshold) ? '' : f.review_threshold} onChange={e => num('review_threshold', e.target.value)} /></label>
        <label className="text-xs text-ink-muted space-y-1 block">Auto-link threshold<input data-testid="input-auto-threshold" className={field} type="number" step="0.01" min="0" max="1" value={Number.isNaN(f.auto_threshold) ? '' : f.auto_threshold} onChange={e => num('auto_threshold', e.target.value)} /></label>
        <label className="text-xs text-ink-muted space-y-1 block">Device expiry (days)<input data-testid="input-device-expiry" className={field} type="number" min="1" step="1" value={Number.isNaN(f.device_expiry_days) ? '' : f.device_expiry_days} onChange={e => num('device_expiry_days', e.target.value)} /></label>
        <label className="text-xs text-ink-muted space-y-1 block">Nightly run hour (0-23)<input data-testid="input-schedule-hour" className={field} type="number" min="0" max="23" step="1" value={Number.isNaN(f.schedule_hour) ? '' : f.schedule_hour} onChange={e => num('schedule_hour', e.target.value)} /></label>
      </div>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" data-testid="checkbox-household-surname" checked={f.household_surname} onChange={e => set('household_surname', e.target.checked)} /> Require matching surname for household grouping</label>
      {err && <p role="alert" className="text-xs text-danger">{err}</p>}
      <div className="flex items-center gap-3">
        <Button data-testid="button-save-graph-policy" disabled={!dirty || !!err || save.isPending} className="bg-signal text-on-signal hover:bg-signal/90" onClick={() => save.mutate(f, { onSuccess: () => toast({ title: 'Graph policy saved' }) })}>{save.isPending ? 'Saving…' : 'Save policy'}</Button>
        {save.isError && <span role="alert" className="text-xs text-danger">Not saved: {errorMessage(save.error)}</span>}
      </div>
    </CardContent></Card>;
}

function RunsCard() {
  const q = useGraphRuns();
  const start = useStartGraphRun();
  const go = (dry: boolean) => start.mutate(dry, { onSuccess: r => toast({ title: dry ? 'Dry run queued' : 'Graph run queued', description: `Job ${r.job_id}` }), onError: e => toast({ title: 'Run not started', description: errorMessage(e), variant: 'destructive' }) });
  return <Card><CardHeader className="flex flex-row items-start justify-between gap-3"><div><CardTitle className="text-base">Graph runs</CardTitle><CardDescription>A dry run counts candidates without changing any profile.</CardDescription></div>
    <div className="flex gap-2 shrink-0"><Button size="sm" variant="outline" data-testid="button-dry-run" disabled={start.isPending} onClick={() => go(true)}><FlaskConical size={13} className="mr-1" />Dry run</Button><Button size="sm" data-testid="button-run-graph" disabled={start.isPending} onClick={() => go(false)} className="bg-signal text-on-signal hover:bg-signal/90"><Play size={13} className="mr-1" />Run now</Button></div></CardHeader>
    <CardContent className="p-0 overflow-x-auto">{q.isLoading ? <Skeleton className="h-32 m-4" /> : q.isError ? <div className="p-4"><ErrorBox what="Runs" error={q.error} retry={() => q.refetch()} /></div> :
      <Table><TableHeader className="bg-muted/30"><TableRow><TableHead>Started</TableHead><TableHead>Mode</TableHead><TableHead>Status</TableHead><TableHead className="text-right">Auto</TableHead><TableHead className="text-right">Review</TableHead><TableHead className="text-right">Rejected</TableHead><TableHead className="text-right">Conflicts</TableHead><TableHead className="text-right">Linked</TableHead></TableRow></TableHeader><TableBody>
        {!q.data?.items.length ? <TableRow><TableCell colSpan={8} className="text-center py-8 text-muted-foreground">No runs yet. Start with a dry run.</TableCell></TableRow>
          : q.data.items.map(r => <TableRow key={r.id}><TableCell className="text-xs">{when(r.started_at)}</TableCell><TableCell className="text-xs">{r.dry_run ? 'Dry run' : 'Live'}</TableCell><TableCell><Badge variant="outline" className="text-[10px] uppercase">{r.status}</Badge>{r.error && <div className="text-[10px] text-danger mt-1 max-w-[220px]">{r.error}</div>}</TableCell>
            {(['auto', 'review', 'rejected', 'conflicts', 'linked'] as const).map(k => <TableCell key={k} className="text-right font-mono text-xs">{r.counts?.[k] ?? '—'}</TableCell>)}</TableRow>)}
      </TableBody></Table>}</CardContent></Card>;
}

function ReviewCard() {
  const q = useQualityItems();
  const sample = useSampleQuality();
  const [count, setCount] = useState('25');
  const n = Number(count);
  const valid = Number.isInteger(n) && n >= 1 && n <= 1000;
  return <Card><CardHeader className="flex flex-row items-start justify-between gap-3"><div><CardTitle className="text-base">Pair review</CardTitle><CardDescription>Match merges an eligible review pair after server conflict checks. Not a match reverses a probabilistic merge.</CardDescription></div>
    <div className="flex items-end gap-2 shrink-0"><label className="text-xs text-ink-muted">Sample size<input data-testid="input-sample-count" type="number" min="1" max="1000" value={count} onChange={e => setCount(e.target.value)} className={`${field} w-24 py-1`} /></label>
      <Button size="sm" variant="outline" data-testid="button-sample" disabled={!valid || sample.isPending} onClick={() => sample.mutate(n, { onSuccess: r => toast({ title: `${r.sampled} pairs sampled for review` }), onError: e => toast({ title: 'Sampling failed', description: errorMessage(e), variant: 'destructive' }) })}>{sample.isPending ? 'Sampling…' : 'Sample pairs'}</Button></div></CardHeader>
    <CardContent className="p-0 overflow-x-auto">{q.isLoading ? <Skeleton className="h-40 m-4" /> : q.isError ? <div className="p-4"><ErrorBox what="Review queue" error={q.error} retry={() => q.refetch()} /></div> :
      <Table><TableHeader className="bg-muted/30"><TableRow><TableHead>Pair / evidence</TableHead><TableHead>Confidence / rule</TableHead><TableHead>Status</TableHead><TableHead>Verdict</TableHead></TableRow></TableHeader><TableBody>
        {!q.data?.items.length ? <TableRow><TableCell colSpan={4} className="text-center py-8 text-muted-foreground">Nothing to review. Sample linked pairs to audit precision.</TableCell></TableRow>
          : q.data.items.map(i => <QualityRow key={i.id} item={i} />)}
      </TableBody></Table>}</CardContent></Card>;
}

function QualityRow({ item: i }: { item: QualityItem }) {
  const v = useQualityVerdict();
  const [note, setNote] = useState('');
  const reviewed = !!i.verdict && i.verdict !== 'unsure' || i.status === 'reviewed';
  const send = (verdict: Verdict) => v.mutate({ id: i.id, verdict, note }, { onSuccess: () => { setNote(''); toast({ title: 'Verdict saved' }); } });
  return <TableRow data-testid={`row-quality-${i.id}`}>
    <TableCell className="text-xs"><Link className="hover:text-signal" to={`/profiles/${i.left_id}`}>{i.left_name || `Profile ${i.left_id}`}</Link><span className="text-ink-muted"> / </span><Link className="hover:text-signal" to={`/profiles/${i.right_id}`}>{i.right_name || `Profile ${i.right_id}`}</Link><div className="font-mono text-[10px] text-ink-muted">{i.left_master_id ?? '—'} / {i.right_master_id ?? '—'}</div><div className="font-mono text-[10px] text-ink-muted" data-testid={`text-evidence-${i.id}`}>Pair {i.left_id}:{i.right_id}{i.left_source_record_id != null || i.right_source_record_id != null ? ` · source records ${i.left_source_record_id ?? '—'}:${i.right_source_record_id ?? '—'}` : ''}{i.evidence_record_ids?.length ? ` · evidence ${i.evidence_record_ids.join(', ')}` : ''}</div></TableCell>
    <TableCell className="text-xs"><span className="font-mono">{pct(i.confidence)}</span><div className="text-[10px] text-ink-muted">{i.rule}</div></TableCell>
    <TableCell className="text-xs"><Badge variant="outline" className="text-[10px] uppercase">{i.status}</Badge>{i.sampled && <div className="text-[10px] text-ink-muted mt-1">sampled</div>}{i.merge_id && <div className="text-[10px] text-ink-muted mt-1">merge #{i.merge_id}</div>}</TableCell>
    <TableCell><div className="flex flex-wrap items-center gap-1">
      {i.verdict && <span className="text-[10px] uppercase text-ink-muted mr-1">{i.verdict.replace('_', ' ')}</span>}
      <input disabled={reviewed} aria-label="Reviewer note" placeholder="Note" value={note} onChange={e => { v.reset(); setNote(e.target.value); }} className="rounded-sm border border-line-strong bg-surface-raised px-2 py-1 text-xs w-28" />
      <Button size="sm" variant="outline" className="h-7 text-xs" disabled={v.isPending || reviewed} data-testid={`button-match-${i.id}`} onClick={() => send('match')}>Match</Button>
      <Button size="sm" variant="outline" className="h-7 text-xs" disabled={v.isPending || reviewed} data-testid={`button-not-match-${i.id}`} onClick={() => send('not_match')}>Not match</Button>
      <Button size="sm" variant="ghost" className="h-7 text-xs" disabled={v.isPending || reviewed} data-testid={`button-unsure-${i.id}`} onClick={() => send('unsure')}>Unsure</Button>
      {v.isError && <p role="alert" className="w-full text-[10px] text-danger">{errorMessage(v.error)}</p>}
    </div></TableCell></TableRow>;
}

export function IdentityTrendsCard() {
  const { user } = useAuth();
  const q = useGraphTrends({ enabled: user?.role !== 'viewer' });
  if (user?.role === 'viewer') return null;
  return <Card><CardHeader><CardTitle className="flex items-center text-base"><TrendingUp className="h-4 w-4 mr-2 text-primary" /> Identity quality by source</CardTitle><CardDescription>Daily candidates, links and match rate (linked of candidates) and sample precision (correct of sampled reviews only, not all links).</CardDescription></CardHeader>
    <CardContent className="p-0 max-h-[360px] overflow-auto">{q.isLoading ? <Skeleton className="h-40 m-4" /> : q.isError ? <div className="p-4"><ErrorBox what="Trends" error={q.error} retry={() => q.refetch()} /></div> :
      <Table><caption className="sr-only">Daily identity graph history per source</caption><TableHeader className="bg-muted/30 sticky top-0"><TableRow><TableHead>Day</TableHead><TableHead>Source</TableHead><TableHead className="text-right">Candidates</TableHead><TableHead className="text-right">Linked</TableHead><TableHead className="text-right">Match rate</TableHead><TableHead className="text-right">Review</TableHead><TableHead className="text-right">Queue open</TableHead><TableHead className="text-right">Sampled reviews</TableHead><TableHead className="text-right">Sample precision</TableHead></TableRow></TableHeader><TableBody>
        {!q.data?.items.length ? <TableRow><TableCell colSpan={9} className="text-center py-8 text-muted-foreground">No graph history yet. Trends appear after the first live run.</TableCell></TableRow>
          : q.data.items.map((t, i) => <TableRow key={i}><TableCell className="text-xs">{t.day}</TableCell><TableCell className="text-xs">{t.source_name ?? 'All sources'}</TableCell><TableCell className="text-right font-mono text-xs">{t.candidates}</TableCell><TableCell className="text-right font-mono text-xs">{t.linked}</TableCell><TableCell className="text-right font-mono text-xs">{t.candidates ? pct(t.linked / t.candidates) : '—'}</TableCell><TableCell className="text-right font-mono text-xs">{t.review}</TableCell><TableCell className="text-right font-mono text-xs">{t.queue_open ?? '—'}</TableCell><TableCell className="text-right font-mono text-xs">{t.reviewed}</TableCell><TableCell className="text-right font-mono text-xs">{t.reviewed ? pct(t.correct / t.reviewed) : '—'}</TableCell></TableRow>)}
      </TableBody></Table>}</CardContent></Card>;
}
