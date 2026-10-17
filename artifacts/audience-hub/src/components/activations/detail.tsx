import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ArrowLeft, Play, Pencil, Pause, PlayCircle, Download, RotateCcw, ChevronDown, ChevronRight, FileWarning } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { toast } from '@/hooks/use-toast';
import { useActivation, useActivationActions, useActivationRuns, activationDownloadUrl } from '@/hooks/use-activations';
import type { Activation, ActivationRun } from '@/lib/activations';
import { DEACTIVATE_COPY, plural, describeSchedule, elapsedMs, fmtDuration, fmtInt, isAdsType, isLive, runPhase, toggleActiveBody } from './logic';
import { ConfirmDialog, Funnel, RunStatus, Section, SkeletonRows, TypeBadge, apiToast, when } from './shared';

/** fetchApi requests root-relative /api paths without the app base, so the anchor uses the identical URL (same origin, cookie auth). */
export const downloadHref = (runId: number) => activationDownloadUrl(runId);

export function useRunNow() {
  const { run } = useActivationActions();
  const watching = useRef<number | null>(null);
  const trigger = (a: Pick<Activation, 'id' | 'name'>) => run.mutate(a.id, {
    onSuccess: r => { watching.current = r.run_id; toast({ title: `Run #${r.run_id} queued`, description: `${a.name} is queued (job ${r.job_id}). Counts appear when the worker finishes.` }); },
    onError: e => apiToast('Run failed to queue', e),
  });
  return { trigger, pending: run.isPending, watching };
}

export function ActivationDetail({ id }: { id: number }) {
  const nav = useNavigate();
  const q = useActivation(id);
  const runs = useActivationRuns(id);
  const { save, archive } = useActivationActions();
  const { trigger, pending, watching } = useRunNow();
  const [confirm, setConfirm] = useState<null | 'run' | 'pause' | 'archive'>(null);

  // Announce completion of a run this user queued — queued and complete are distinct events.
  useEffect(() => {
    const r = runs.data?.items.find(x => x.id === watching.current);
    if (r && !isLive(r)) { watching.current = null; toast({ title: `Run #${r.id} ${r.status}`, description: runPhase(r.status) === 'failed' ? r.error ?? 'See run history.' : `${plural(r.profiles_sent, 'partner')} sent.`, variant: runPhase(r.status) === 'failed' ? 'destructive' : undefined }); }
  }, [runs.data, watching]);

  if (q.isLoading) return <div className="space-y-2">{[0, 1, 2].map(i => <div key={i} className="h-10 bg-surface-raised animate-pulse rounded-sm" />)}</div>;
  if (q.isError || !q.data) return <div className="p-8 text-center"><p className="text-danger mb-2">Couldn't load activation #{id}{q.error ? `: ${(q.error as Error).message}` : ''}</p><div className="flex justify-center gap-2"><Button size="sm" variant="outline" onClick={() => q.refetch()} data-testid="button-retry-activation"><RotateCcw size={12} /> Retry</Button><Link to="/activations" className="text-signal text-[12px] self-center" data-testid="link-back-list">Back to activations</Link></div></div>;
  const a = q.data;
  const items = runs.data?.items ?? [];
  const live = items.some(isLive) || isLive(a.last_run);
  const latest = items[0] ?? a.last_run;

  return <div className="space-y-3">
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <Link to="/activations" className="text-ink-muted hover:text-ink inline-flex items-center gap-1 text-[12px]" data-testid="link-back"><ArrowLeft size={13} /> Activations</Link>
        <h1 className="kin-title truncate" data-testid="text-activation-name">{a.name} <span className="font-mono text-[12px] text-ink-muted">#{a.id}</span></h1>
        <div className="flex items-center gap-2 text-[12px] text-ink-muted flex-wrap mt-0.5">
          <Link to={`/segments/${a.segment_id}`} className="text-signal" data-testid="link-segment">{a.segment_name}</Link><ChevronRight size={11} />
          <span className="text-ink">{a.destination_name}</span><TypeBadge type={a.destination_type} />
          <span className={a.is_active ? 'text-ok' : 'text-warn'} data-testid="text-active-state">{a.is_active ? 'active' : 'paused'}</span>
        </div>
      </div>
      <div className="flex gap-1.5 shrink-0">
        <Button size="sm" className="h-7" disabled={pending || live} onClick={() => setConfirm('run')} data-testid="button-run-now"><Play size={12} /> {live ? 'Run in progress' : 'Run now'}</Button>
        <Button size="sm" variant="outline" className="h-7" onClick={() => nav(`/activations/${a.id}/edit`)} data-testid="button-edit-activation"><Pencil size={12} /> Edit</Button>
        {a.is_active
          ? <Button size="sm" variant="outline" className="h-7 text-danger" onClick={() => setConfirm('archive')} data-testid="button-deactivate-activation"><Pause size={12} /> {DEACTIVATE_COPY.button}</Button>
          : <Button size="sm" variant="outline" className="h-7" onClick={() => setConfirm('pause')} data-testid="button-resume-activation"><PlayCircle size={12} /> Resume</Button>}
      </div>
    </div>

    <div className="grid grid-cols-4 gap-2">
      {[['Schedule', describeSchedule(a.schedule, a.timezone)], ['Next run', a.is_active && a.next_run_at ? when(a.next_run_at) : a.is_active ? 'manual only' : 'paused'], ['Consent', a.required_consent ?? 'none'], ['Columns', Object.keys(a.field_mapping).join(', ') || '—']].map(([k, v]) =>
        <div key={k} className="border border-line bg-surface rounded-sm px-3 py-2 min-w-0"><div className="text-[10px] uppercase tracking-wider text-ink-muted">{k}</div><div className="text-[12px] font-mono truncate" title={v} data-testid={`text-${k.toLowerCase().replace(' ', '-')}`}>{v}</div></div>)}
    </div>
    {isAdsType(a.destination_type) && <p className="text-[12px] text-ink-muted border border-line rounded-sm p-2 bg-surface">Produces a normalized, SHA-256 hashed CSV. Download it from a finished run and upload it manually in the ad platform — this console has no connection to Google or Meta.</p>}

    {latest && <Section title={`Latest run #${latest.id}`} aside={<RunStatus status={latest.status} />} testId="section-latest-run"><Funnel run={latest} /></Section>}

    <Section title="Run history" aside={<span className="text-[11px] text-ink-muted">{live ? 'polling every 3s' : `${items.length} runs`}</span>} testId="section-run-history">
      <RunHistory runs={runs} onRerun={() => setConfirm('run')} rerunDisabled={pending || live} />
    </Section>

    <ConfirmDialog open={confirm === 'run'} onOpenChange={o => !o && setConfirm(null)} testId="run-activation" confirm="Queue run"
      title={`Run "${a.name}" now?`} body={<>Re-materializes <strong>{a.segment_name}</strong> and sends eligible members to <strong>{a.destination_name}</strong>{a.destination_type === 'webhook' ? ' over the network' : ' as a file'}. Consent ({a.required_consent ?? 'none'}) and suppressions apply.</>}
      onConfirm={() => trigger(a)} />
    <ConfirmDialog open={confirm === 'pause'} onOpenChange={o => !o && setConfirm(null)} testId="toggle-activation" confirm={a.is_active ? 'Pause' : 'Resume'}
      title={`${a.is_active ? 'Pause' : 'Resume'} "${a.name}"?`} body={a.is_active ? 'Scheduled runs stop until resumed. Manual runs remain possible.' : `Scheduled runs resume: ${describeSchedule(a.schedule, a.timezone)}.`}
      onConfirm={() => save.mutate(toggleActiveBody(a, !a.is_active), { onSuccess: () => toast({ title: `${a.name} ${a.is_active ? 'paused' : 'resumed'}` }), onError: e => apiToast('Update failed', e) })} />
    <ConfirmDialog open={confirm === 'archive'} onOpenChange={o => !o && setConfirm(null)} testId="deactivate-activation" danger confirm={DEACTIVATE_COPY.button}
      title={DEACTIVATE_COPY.title(a.name)} body={DEACTIVATE_COPY.body}
      onConfirm={() => archive.mutate(a.id, { onSuccess: () => toast({ title: DEACTIVATE_COPY.done(a.name) }), onError: e => apiToast('Deactivate failed', e) })} />
  </div>;
}

function RunHistory({ runs, onRerun, rerunDisabled }: { runs: ReturnType<typeof useActivationRuns>; onRerun: () => void; rerunDisabled: boolean }) {
  const [open, setOpen] = useState<number | null>(null);
  const items = runs.data?.items ?? [];
  return <div className="overflow-x-auto -m-3">
    <table className="w-full text-[12px]">
      <thead className="bg-surface-raised text-ink-muted text-[10px] uppercase tracking-wider"><tr className="h-8 text-left">
        <th className="px-3 w-6" /><th className="px-2">Run</th><th className="px-2">Status</th><th className="px-2">Trigger</th><th className="px-2">Started</th><th className="px-2">Elapsed</th><th className="px-2 min-w-[340px]">Funnel</th><th className="px-2">File</th><th className="px-2" />
      </tr></thead>
      <tbody>
        {runs.isLoading ? <SkeletonRows cols={9} rows={4} />
          : runs.isError ? <tr><td colSpan={9} className="p-4 text-center text-danger">Couldn't load runs. <button className="underline" onClick={() => runs.refetch()} data-testid="button-retry-runs">Retry</button></td></tr>
          : !items.length ? <tr><td colSpan={9} className="p-6 text-center text-ink-muted">No runs yet. Click Run now to send the first batch, or wait for the schedule.</td></tr>
          : items.map(r => <RunRow key={r.id} r={r} open={open === r.id} toggle={() => setOpen(o => (o === r.id ? null : r.id))} onRerun={onRerun} rerunDisabled={rerunDisabled} />)}
      </tbody>
    </table>
  </div>;
}

function RunRow({ r, open, toggle, onRerun, rerunDisabled }: { r: ActivationRun; open: boolean; toggle: () => void; onRerun: () => void; rerunDisabled: boolean }) {
  const failed = runPhase(r.status) === 'failed';
  return <>
    <tr className="h-10 border-t border-line hover:bg-surface-raised/50" data-testid={`row-run-${r.id}`}>
      <td className="px-3">{r.error && <button onClick={toggle} aria-expanded={open} aria-label="Show error" data-testid={`button-expand-run-${r.id}`}>{open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}</button>}</td>
      <td className="px-2 font-mono">#{r.id}</td>
      <td className="px-2"><RunStatus status={r.status} testId={`status-run-${r.id}`} /></td>
      <td className="px-2 text-ink-muted">{r.triggered_by}</td>
      <td className="px-2 font-mono text-[11px] text-ink-muted">{when(r.started_at ?? r.created_at)}</td>
      <td className="px-2 font-mono tabular-nums">{fmtDuration(elapsedMs(r))}</td>
      <td className="px-2 py-1">{isLive(r) && !r.profiles_selected ? <span className="text-ink-muted text-[11px]">{runPhase(r.status) === 'queued' ? 'Waiting for worker…' : 'Materializing…'}</span> : <Funnel run={r} compact />}</td>
      <td className="px-2">{r.revoked ? <span className="text-ink-muted text-[11px]" title="File expired or revoked">expired</span>
        : r.downloadable ? <a href={downloadHref(r.id)} className="text-signal inline-flex items-center gap-1 text-[11px]" data-testid={`link-download-run-${r.id}`}><Download size={11} /> CSV</a> : <span className="text-ink-muted">—</span>}</td>
      <td className="px-2">{(failed || !isLive(r)) && <Button size="sm" variant="ghost" className="h-6 px-2 text-[11px]" disabled={rerunDisabled} onClick={onRerun} data-testid={`button-rerun-${r.id}`}><RotateCcw size={11} /> Re-run</Button>}</td>
    </tr>
    {open && r.error && <tr className="border-t border-line"><td /><td colSpan={8} className="px-2 py-2"><pre className="font-mono text-[11px] text-danger whitespace-pre-wrap flex gap-2"><FileWarning size={12} className="shrink-0 mt-0.5" />{r.error}</pre></td></tr>}
  </>;
}
