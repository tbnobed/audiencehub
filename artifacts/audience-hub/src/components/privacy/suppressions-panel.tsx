import { useState } from 'react';
import { RefreshCw, ShieldOff, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { useSuppressions, useSuppressionActions } from '@/hooks/use-privacy';
import type { PrivacyIdentifier, Suppression } from '@/lib/privacy';
import { errMsg, fmtTime, identifierError, identifierPayload, shortHash } from '@/lib/privacy-ui';
import { IdentifierFields } from './identifier-fields';

export function SuppressionsPanel() {
  const list = useSuppressions();
  const { add, remove } = useSuppressionActions();
  const [ident, setIdent] = useState<PrivacyIdentifier>({ type: 'email', value: '' });
  const [touched, setTouched] = useState(false);
  const [target, setTarget] = useState<Suppression | null>(null);
  const err = touched ? identifierError(ident) : null;

  const submit = () => {
    setTouched(true);
    if (identifierError(ident)) return;
    add.mutate(identifierPayload(ident), { onSuccess: () => { setIdent(i => ({ ...i, value: '' })); setTouched(false); } });
  };

  return <div className="space-y-4">
    <section className="rounded-md border border-line bg-surface">
      <div className="border-b border-line px-5 py-3"><h2 className="kin-heading text-ink">Add suppression</h2><p className="mt-1 text-xs text-ink-muted">The value is normalized and stored as a hash. Email suppressions set email consent to opted out and exclude the partner from email activations only; imports are not rejected.</p></div>
      <form className="p-5 space-y-3" onSubmit={e => { e.preventDefault(); submit(); }}>
        <IdentifierFields prefix="suppression" value={ident} onChange={v => { add.reset(); setIdent(v); }} error={err} />
        <div className="flex items-center gap-3">
          <Button data-testid="button-add-suppression" type="submit" size="sm" disabled={add.isPending} className="bg-signal text-on-signal hover:bg-signal/90">{add.isPending ? 'Adding…' : 'Add suppression'}</Button>
          {add.isSuccess && <span role="status" data-testid="status-suppression-added" className="text-xs text-ok">Suppression added. Value cleared.</span>}
        </div>
        {add.isError && <p role="alert" className="text-xs text-danger">Suppression was not added: {errMsg(add.error)}</p>}
      </form>
    </section>

    <section className="rounded-md border border-line bg-surface overflow-hidden">
      <div className="border-b border-line px-5 py-3 flex items-center justify-between"><h2 className="kin-heading text-ink">Suppression list</h2><span className="font-mono text-[11px] text-ink-muted" data-testid="text-suppression-count">{list.data ? `${list.data.items.length} hashes` : ''}</span></div>
      {list.isLoading ? <div className="p-5 space-y-2">{[0, 1, 2].map(i => <div key={i} className="h-5 rounded-sm bg-surface-raised animate-pulse" />)}</div>
        : list.isError ? <div role="alert" className="p-5 text-sm"><p className="text-danger">Suppressions could not be loaded.</p><p className="text-xs text-ink-muted mt-1">{errMsg(list.error)}</p><Button variant="outline" size="sm" className="mt-3" onClick={() => list.refetch()}><RefreshCw size={13} className="mr-2" />Retry</Button></div>
        : !list.data?.items.length ? <div className="p-8 text-center"><ShieldOff size={20} className="mx-auto text-ink-muted mb-2" /><p className="text-sm text-ink">No suppressions yet</p><p className="text-xs text-ink-muted mt-1">Bounces, complaints, manual entries, and completed deletions appear here.</p></div>
        : <table className="w-full text-xs"><thead className="bg-surface-raised text-[11px] text-ink-muted"><tr><th className="text-left font-medium px-4 py-2">Type</th><th className="text-left font-medium px-4 py-2">Hash</th><th className="text-left font-medium px-4 py-2">Reason</th><th className="text-left font-medium px-4 py-2">Created</th><th className="px-4 py-2" /></tr></thead>
          <tbody>{list.data.items.map(s => { const tomb = s.reason === 'deletion_request'; return <tr key={s.id} data-testid={`row-suppression-${s.id}`} className="border-t border-line">
            <td className="px-4 py-2 uppercase font-mono text-[11px]">{s.type}</td>
            <td className="px-4 py-2 font-mono text-ink-muted" title={s.value_hash}>{shortHash(s.value_hash)}</td>
            <td className="px-4 py-2"><span className={`inline-block px-1.5 py-0.5 rounded-sm border font-mono text-[10px] ${tomb ? 'border-danger/30 text-danger bg-danger/5' : 'border-line text-ink-muted'}`}>{s.reason}</span></td>
            <td className="px-4 py-2 text-ink-muted font-mono">{fmtTime(s.created_at)}</td>
            <td className="px-4 py-2 text-right"><Button data-testid={`button-remove-suppression-${s.id}`} size="sm" variant="ghost" disabled={tomb || remove.isPending} title={tomb ? 'Deletion tombstones cannot be removed.' : undefined} onClick={() => { remove.reset(); setTarget(s); }}><Trash2 size={13} className="mr-1" />{tomb ? 'Locked' : 'Remove'}</Button></td>
          </tr>; })}</tbody></table>}
      {remove.isError && <p role="alert" className="px-5 py-3 text-xs text-danger border-t border-line">Suppression was not removed: {errMsg(remove.error)}</p>}
    </section>

    <AlertDialog open={!!target} onOpenChange={o => !o && setTarget(null)}>
      <AlertDialogContent>
        <AlertDialogHeader><AlertDialogTitle>Remove suppression {target && shortHash(target.value_hash)}?</AlertDialogTitle>
          <AlertDialogDescription asChild><div className="space-y-2 text-sm"><p>Hash <span className="font-mono break-all">{target?.value_hash}</span> ({target?.type}, {target?.reason}) will be removed.</p><p className="text-warn">Removing a suppression does NOT opt the partner back in. Their consent stays opted out until an authorized opt-in is recorded.</p></div></AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter><AlertDialogCancel data-testid="button-cancel-remove-suppression">Cancel</AlertDialogCancel><AlertDialogAction data-testid="button-confirm-remove-suppression" className="bg-danger text-white hover:bg-danger/90" onClick={() => { if (target) remove.mutate(target.id); setTarget(null); }}>Remove suppression</AlertDialogAction></AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div>;
}
