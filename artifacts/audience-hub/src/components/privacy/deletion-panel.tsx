import { useState } from 'react';
import { AlertTriangle, CheckCircle2, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { useAuth } from '@/hooks/use-auth';
import { useDeletionActions, useDeletionRequests } from '@/hooks/use-privacy';
import type { DeletionPreview, DeletionRequest, PrivacyIdentifier } from '@/lib/privacy';
import { approveState, countEntries, errMsg, fmtTime, identifierError, identifierPayload, isActiveDeletion, totalCount } from '@/lib/privacy-ui';
import { IdentifierFields } from './identifier-fields';

const statusTone: Record<string, string> = { pending: 'text-warn border-warn/30 bg-warn/10', queued: 'text-signal border-signal/30 bg-signal-soft', running: 'text-signal border-signal/30 bg-signal-soft', completed: 'text-ok border-ok/30 bg-ok/10', failed: 'text-danger border-danger/30 bg-danger/10' };

function Counts({ counts, testid }: { counts: Record<string, number>; testid: string }) {
  const rows = countEntries(counts);
  if (!rows.length) return <span className="text-ink-muted">—</span>;
  return <dl data-testid={testid} className="grid grid-cols-[repeat(auto-fill,minmax(130px,1fr))] gap-px bg-line border border-line rounded-sm overflow-hidden">{rows.map(([k, n]) => <div key={k} className="bg-surface px-2 py-1.5"><dt className="text-[10px] uppercase tracking-wide text-ink-muted">{k.replace(/_/g, ' ')}</dt><dd className="font-mono text-sm tabular-nums">{n.toLocaleString()}</dd></div>)}</dl>;
}

export function DeletionPanel() {
  const { user } = useAuth();
  const list = useDeletionRequests();
  const { preview, create, approve } = useDeletionActions();
  const [ident, setIdent] = useState<PrivacyIdentifier>({ type: 'email', value: '' });
  const [touched, setTouched] = useState(false);
  const [shown, setShown] = useState<DeletionPreview | null>(null);
  const [confirmCreate, setConfirmCreate] = useState(false);
  const [toApprove, setToApprove] = useState<DeletionRequest | null>(null);
  const err = touched ? identifierError(ident) : null;
  const keepDays = list.data?.backup_keep_days ?? shown?.backup_keep_days;
  const twoPerson = !!list.data?.two_person;

  const change = (v: PrivacyIdentifier) => { setIdent(v); setShown(null); preview.reset(); create.reset(); };
  const runPreview = () => { setTouched(true); if (identifierError(ident)) return; preview.mutate(identifierPayload(ident), { onSuccess: p => setShown(p) }); };
  const runCreate = () => create.mutate(identifierPayload(ident), { onSuccess: () => { setIdent(i => ({ type: i.type, value: '' })); setShown(null); setTouched(false); } });

  return <div className="space-y-4">
    <div role="note" data-testid="text-deletion-notice" className="rounded-md border border-warn/40 bg-warn/5 p-4 text-xs text-ink space-y-1.5">
      <p className="flex items-center gap-2 font-medium"><AlertTriangle size={14} className="text-warn" /> Deletion is irreversible.</p>
      <p>Backups keep deleted data until they age out ({keepDays != null ? <span className="font-mono">{keepDays} days</span> : 'retention loading'}). Files already downloaded or exported cannot be recalled.</p>
      <p>Gift records needed for tax receipting must be handled in your system of record, not here.</p>
      <p>Retained upload and error files from affected sources will also be removed, including historical versions that may contain other partners. Other partners’ database records remain intact.</p>
    </div>

    <section className="rounded-md border border-line bg-surface">
      <div className="border-b border-line px-5 py-3"><h2 className="kin-heading text-ink">New deletion request</h2><p className="mt-1 text-xs text-ink-muted">Preview shows counts only, including merged-away partners. Creating a request does not delete anything; it must be approved{twoPerson ? ' by a second admin' : ''}.</p></div>
      <form className="p-5 space-y-3" onSubmit={e => { e.preventDefault(); runPreview(); }}>
        <IdentifierFields prefix="deletion" value={ident} onChange={change} error={err} />
        <Button data-testid="button-preview-deletion" type="submit" size="sm" variant="outline" disabled={preview.isPending}>{preview.isPending ? 'Previewing…' : 'Preview counts'}</Button>
        {preview.isError && <p role="alert" className="text-xs text-danger">Preview failed: {errMsg(preview.error)}</p>}
        {shown && <div data-testid="panel-deletion-preview" className="space-y-3 border-t border-line pt-3">
          <p className="text-sm"><span data-testid="text-preview-partners" className="font-mono">{shown.matched_partners}</span> matching partner{shown.matched_partners === 1 ? '' : 's'} · <span className="font-mono">{totalCount(shown.counts).toLocaleString()}</span> rows</p>
          <Counts counts={shown.counts} testid="list-preview-counts" />
          <Button data-testid="button-create-deletion" type="button" size="sm" disabled={create.isPending || shown.matched_partners === 0} onClick={() => setConfirmCreate(true)} className="bg-danger text-white hover:bg-danger/90">{create.isPending ? 'Creating…' : 'Create deletion request'}</Button>
          {shown.matched_partners === 0 && <p className="text-xs text-ink-muted">No partners match. Nothing to request.</p>}
        </div>}
        {create.isSuccess && <p role="status" data-testid="status-deletion-created" className="text-xs text-ok">Request #{create.data.id} created and awaiting approval. Identifier cleared.</p>}
        {create.isError && <p role="alert" className="text-xs text-danger">Request was not created: {errMsg(create.error)}</p>}
      </form>
    </section>

    <section className="rounded-md border border-line bg-surface overflow-hidden">
      <div className="border-b border-line px-5 py-3 flex items-center justify-between"><h2 className="kin-heading text-ink">Requests</h2><span className="font-mono text-[11px] text-ink-muted">{twoPerson ? 'Two-person approval on' : 'Single-admin approval'}</span></div>
      {list.isLoading ? <div className="p-5 space-y-2">{[0, 1, 2].map(i => <div key={i} className="h-5 rounded-sm bg-surface-raised animate-pulse" />)}</div>
        : list.isError ? <div role="alert" className="p-5 text-sm"><p className="text-danger">Deletion requests could not be loaded.</p><p className="text-xs text-ink-muted mt-1">{errMsg(list.error)}</p><Button variant="outline" size="sm" className="mt-3" onClick={() => list.refetch()}><RefreshCw size={13} className="mr-2" />Retry</Button></div>
        : !list.data?.items.length ? <div className="p-8 text-center"><CheckCircle2 size={20} className="mx-auto text-ink-muted mb-2" /><p className="text-sm">No deletion requests</p><p className="text-xs text-ink-muted mt-1">Requests and their job status will be tracked here.</p></div>
        : <ul className="divide-y divide-line">{list.data.items.map(r => { const a = approveState(r, user?.id, twoPerson); return <li key={r.id} data-testid={`row-deletion-${r.id}`} className="px-5 py-3 space-y-2 text-xs">
          <div className="flex flex-wrap items-center gap-3">
            <span className="font-mono text-ink">#{r.id}</span>
            <span data-testid={`status-deletion-${r.id}`} className={`px-1.5 py-0.5 rounded-sm border font-mono text-[10px] uppercase ${statusTone[r.status] ?? 'border-line text-ink-muted'}`}>{r.status}{isActiveDeletion(r.status) && <span className="ml-1 animate-pulse">·</span>}</span>
            <span className="text-ink-muted">Requested by <span className="font-mono">{r.requested_by ?? '—'}</span>{r.requested_by === user?.id && ' (you)'} · {fmtTime(r.created_at)}</span>
            <span className="text-ink-muted">Approved by <span className="font-mono">{r.approved_by ?? '—'}</span> · {fmtTime(r.approved_at)}</span>
            <span className="text-ink-muted">Executed {fmtTime(r.executed_at)}</span>
            <span className="ml-auto" />
            {r.status === 'pending' && <Button data-testid={`button-approve-deletion-${r.id}`} size="sm" disabled={!a.allowed || approve.isPending} onClick={() => { approve.reset(); setToApprove(r); }} className="bg-danger text-white hover:bg-danger/90">Approve and delete</Button>}
          </div>
          {a.reason && r.status === 'pending' && <p data-testid={`text-approve-reason-${r.id}`} className="text-ink-muted">{a.reason}</p>}
          {isActiveDeletion(r.status) && <p className="text-signal">Deletion job in progress. Data is not deleted until this shows completed.</p>}
          {r.status === 'completed' && <Counts counts={r.result_counts} testid={`list-result-counts-${r.id}`} />}
          {r.error && <p role="alert" className="text-danger font-mono">{r.error}</p>}
        </li>; })}</ul>}
      {approve.isError && <p role="alert" className="px-5 py-3 text-xs text-danger border-t border-line">Approval failed: {errMsg(approve.error)}</p>}
      {approve.isSuccess && <p role="status" className="px-5 py-3 text-xs text-ok border-t border-line">Approved. Job #{approve.data.job_id} queued; status updates below.</p>}
    </section>

    <AlertDialog open={confirmCreate} onOpenChange={setConfirmCreate}>
      <AlertDialogContent><AlertDialogHeader><AlertDialogTitle>Create deletion request?</AlertDialogTitle><AlertDialogDescription>This records a request for {shown?.matched_partners} partner{shown?.matched_partners === 1 ? '' : 's'} ({totalCount(shown?.counts).toLocaleString()} rows). Nothing is deleted until it is approved.</AlertDialogDescription></AlertDialogHeader>
        <AlertDialogFooter><AlertDialogCancel>Cancel</AlertDialogCancel><AlertDialogAction data-testid="button-confirm-create-deletion" onClick={runCreate}>Create request</AlertDialogAction></AlertDialogFooter></AlertDialogContent>
    </AlertDialog>
    <AlertDialog open={!!toApprove} onOpenChange={o => !o && setToApprove(null)}>
      <AlertDialogContent><AlertDialogHeader><AlertDialogTitle>Approve deletion request #{toApprove?.id}?</AlertDialogTitle>
        <AlertDialogDescription asChild><div className="space-y-2 text-sm"><p>A job will permanently delete all partner data matched by request #{toApprove?.id} and add deletion suppressions for their emails and phones. This cannot be undone.</p>{toApprove && countEntries(toApprove.result_counts).length > 0 && <Counts counts={toApprove.result_counts} testid="list-approve-counts" />}<p className="text-warn">Backups keep deleted data for {keepDays ?? '—'} days.</p></div></AlertDialogDescription></AlertDialogHeader>
        <AlertDialogFooter><AlertDialogCancel>Cancel</AlertDialogCancel><AlertDialogAction data-testid="button-confirm-approve-deletion" className="bg-danger text-white hover:bg-danger/90" onClick={() => { if (toApprove) approve.mutate(toApprove.id); setToApprove(null); }}>Permanently delete request #{toApprove?.id}</AlertDialogAction></AlertDialogFooter></AlertDialogContent>
    </AlertDialog>
  </div>;
}
