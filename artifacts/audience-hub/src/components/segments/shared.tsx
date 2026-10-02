import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { useAuth } from '@/hooks/use-auth';
import { useSegmentActions } from '@/hooks/use-segments';
import { toast } from '@/hooks/use-toast';
import type { Segment } from '@/lib/segments';

export function useSegmentRole() {
  const { user } = useAuth();
  const role = user?.role ?? 'viewer';
  return { role, canEdit: role === 'admin' || role === 'analyst', isAdmin: role === 'admin', userId: user?.id };
}

export const errorMessage = (e: unknown) => e instanceof Error ? e.message : 'Something went wrong.';
export function toastError(title: string, e: unknown) { toast({ variant: 'destructive', title, description: errorMessage(e) }); }

const STATUS_CLASS: Record<string, string> = {
  active: 'text-ok bg-ok/10 border-ok/25', draft: 'text-warn bg-warn/10 border-warn/25', archived: 'text-ink-muted bg-surface-raised border-line',
};
export function StatusPill({ status, testId }: { status: string; testId?: string }) {
  return <span data-testid={testId} className={`inline-flex items-center gap-1 px-1.5 h-5 rounded-sm border font-mono text-[10px] uppercase tracking-wider ${STATUS_CLASS[status] ?? STATUS_CLASS.archived}`}>
    <span className={`h-1.5 w-1.5 rounded-full ${status === 'active' ? 'bg-ok' : status === 'draft' ? 'bg-warn' : 'bg-ink-muted'}`} />{status}
  </span>;
}

export function ArchiveDialog({ segment, open, onOpenChange, onDone }: { segment: Segment | null; open: boolean; onOpenChange: (o: boolean) => void; onDone?: () => void }) {
  const { archive } = useSegmentActions();
  return <AlertDialog open={open} onOpenChange={onOpenChange}>
    <AlertDialogContent className="border-line bg-surface">
      <AlertDialogHeader>
        <AlertDialogTitle>Archive “{segment?.name}”?</AlertDialogTitle>
        <AlertDialogDescription className="text-ink-muted">
          The audience stops refreshing and can no longer be referenced by new rules.
          {segment && segment.activation_count > 0 && <> It is used by <b className="text-warn">{segment.activation_count} activation{segment.activation_count === 1 ? '' : 's'}</b>; the API will refuse if any still depend on it.</>}
        </AlertDialogDescription>
      </AlertDialogHeader>
      <AlertDialogFooter>
        <AlertDialogCancel data-testid="button-cancel-archive">Keep it</AlertDialogCancel>
        <AlertDialogAction data-testid="button-confirm-archive" className="bg-danger text-foreground hover:bg-danger/90" disabled={archive.isPending}
          onClick={e => { e.preventDefault(); if (!segment) return; archive.mutate(segment.id, {
            onSuccess: () => { toast({ title: 'Audience archived', description: segment.name }); onOpenChange(false); onDone?.(); },
            onError: err => toastError('Archive failed', err),
          }); }}>{archive.isPending ? 'Archiving…' : `Archive ${segment?.name ?? ''}`}</AlertDialogAction>
      </AlertDialogFooter>
    </AlertDialogContent>
  </AlertDialog>;
}

/** Duplicate = save without id, as a draft, then open the copy in the builder. */
export function useDuplicate() {
  const { save } = useSegmentActions();
  const navigate = useNavigate();
  const run = (s: Segment) => save.mutate({ name: `${s.name} (copy)`.slice(0, 200), description: s.description, definition: s.definition, status: 'draft', refresh_schedule: s.refresh_schedule }, {
    onSuccess: copy => { toast({ title: 'Audience duplicated', description: `${copy.name} saved as draft.` }); navigate(`/segments/${copy.id}/edit`); },
    onError: e => toastError('Duplicate failed', e),
  });
  return { run, pending: save.isPending };
}

/** Manual refresh: queue materialization, then poll until last_materialized_at moves (or 3 min). */
export function useRefresh(segment: Segment | undefined, refetch: () => unknown) {
  const { materialize } = useSegmentActions();
  const [pending, setPending] = useState<{ job: number; since: string | null; started: number } | null>(null);
  const refetchRef = useRef(refetch);
  refetchRef.current = refetch;
  useEffect(() => {
    if (!pending) return;
    if (segment && segment.last_materialized_at !== pending.since) { setPending(null); toast({ title: 'Refresh complete', description: `${segment.name}: ${segment.last_count?.toLocaleString('en-US') ?? '—'} partners.` }); return; }
    if (Date.now() - pending.started > 180000) { setPending(null); toast({ title: 'Refresh still running', description: 'Check the job drawer for progress.' }); return; }
    const t = setTimeout(() => refetchRef.current(), 3000);
    return () => clearTimeout(t);
  }, [pending, segment]);
  const start = () => segment && materialize.mutate(segment.id, {
    onSuccess: r => { setPending({ job: r.job_id, since: segment.last_materialized_at, started: Date.now() }); toast({ title: 'Refresh queued', description: `Job #${r.job_id}` }); },
    onError: e => toastError('Refresh failed', e),
  });
  return { start, pending, starting: materialize.isPending };
}

/** Warn before leaving with unsaved edits: tab close/reload and in-app link clicks. */
export function useUnsavedGuard(dirty: boolean) {
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  useEffect(() => {
    const beforeUnload = (e: BeforeUnloadEvent) => { if (dirtyRef.current) { e.preventDefault(); e.returnValue = ''; } };
    const onClick = (e: MouseEvent) => {
      if (!dirtyRef.current || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey) return;
      const a = (e.target as HTMLElement | null)?.closest?.('a[href]') as HTMLAnchorElement | null;
      if (!a || a.target === '_blank' || a.dataset.unsavedOk) return;
      if (!window.confirm('Discard unsaved changes to this audience?')) { e.preventDefault(); e.stopPropagation(); }
    };
    window.addEventListener('beforeunload', beforeUnload);
    document.addEventListener('click', onClick, true);
    return () => { window.removeEventListener('beforeunload', beforeUnload); document.removeEventListener('click', onClick, true); };
  }, []);
  return (proceed: () => void) => { if (!dirtyRef.current || window.confirm('Discard unsaved changes to this audience?')) { dirtyRef.current = false; proceed(); } };
}
