import type { ReactNode } from 'react';
import { FileSpreadsheet, Webhook, Megaphone, Target } from 'lucide-react';
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { toast } from '@/hooks/use-toast';
import type { ActivationRun, DestinationType } from '@/lib/activations';
import { runFunnel, runPhase, fmtInt, typeLabel } from './logic';

const PHASE: Record<string, string> = {
  queued: 'border-signal/40 text-signal bg-signal-soft',
  running: 'border-signal/40 text-signal bg-signal-soft',
  succeeded: 'border-ok/40 text-ok bg-ok/10',
  failed: 'border-danger/40 text-danger bg-danger/10',
  other: 'border-line text-ink-muted bg-surface-raised',
};
export function RunStatus({ status, testId }: { status: string | null | undefined; testId?: string }) {
  const p = status ? runPhase(status) : 'other';
  return <span data-testid={testId} className={`inline-flex items-center gap-1 px-1.5 h-5 rounded-sm border font-mono text-[10px] uppercase tracking-wider ${PHASE[p]}`}>
    <span className={`h-1.5 w-1.5 rounded-full ${p === 'succeeded' ? 'bg-ok' : p === 'failed' ? 'bg-danger' : p === 'other' ? 'bg-ink-muted' : 'bg-signal animate-pulse'}`} />{status ?? 'never run'}
  </span>;
}

const TYPE_ICON: Record<DestinationType, typeof Webhook> = { csv: FileSpreadsheet, webhook: Webhook, google_customer_match: Target, meta_custom_audience: Megaphone };
export function TypeBadge({ type }: { type: DestinationType }) {
  const Icon = TYPE_ICON[type] ?? FileSpreadsheet;
  return <span className="inline-flex items-center gap-1 px-1.5 h-5 rounded-sm border border-line bg-surface-raised text-[10px] font-mono uppercase tracking-wider text-ink-muted" data-testid={`badge-type-${type}`}><Icon size={11} />{typeLabel(type)}</span>;
}

/** Plain stage counts with proportional bars. Rendered as a list so screen readers get numbers, not shapes. */
export function Funnel({ run, compact = false }: { run: ActivationRun; compact?: boolean }) {
  const stages = runFunnel(run);
  const max = Math.max(1, stages[0].count);
  return <ol className={`grid ${compact ? 'grid-cols-5 gap-1' : 'grid-cols-5 gap-2'}`} aria-label={`Run ${run.id} funnel`} data-testid={`funnel-run-${run.id}`}>
    {stages.map((s, i) => <li key={s.key} className="min-w-0">
      <div className="text-[10px] uppercase tracking-wider text-ink-muted truncate">{i > 0 && <span aria-hidden className="mr-0.5">›</span>}{s.label}</div>
      <div className="font-mono tabular-nums text-ink text-[12px]" data-testid={`text-funnel-${s.key}-${run.id}`}>{fmtInt(s.count)}</div>
      <div className="h-1 bg-surface-raised rounded-sm overflow-hidden mt-0.5" aria-hidden><div className={`h-full origin-left ${s.key === 'sent' ? 'bg-ok' : 'bg-signal'}`} style={{ transform: `scaleX(${s.count / max})` }} /></div>
      {!compact && s.dropped > 0 && <div className="font-mono text-[10px] text-warn tabular-nums">−{fmtInt(s.dropped)}</div>}
    </li>)}
  </ol>;
}

export function ConfirmDialog({ open, onOpenChange, title, body, confirm, danger, onConfirm, testId }: { open: boolean; onOpenChange: (o: boolean) => void; title: string; body: ReactNode; confirm: string; danger?: boolean; onConfirm: () => void; testId: string }) {
  return <AlertDialog open={open} onOpenChange={onOpenChange}>
    <AlertDialogContent className="bg-surface border-line">
      <AlertDialogHeader><AlertDialogTitle className="kin-heading">{title}</AlertDialogTitle><AlertDialogDescription className="text-ink-muted text-[13px]">{body}</AlertDialogDescription></AlertDialogHeader>
      <AlertDialogFooter>
        <AlertDialogCancel data-testid={`button-cancel-${testId}`}>Cancel</AlertDialogCancel>
        <AlertDialogAction data-testid={`button-confirm-${testId}`} onClick={onConfirm} className={danger ? 'bg-danger text-ground hover:bg-danger/90' : ''}>{confirm}</AlertDialogAction>
      </AlertDialogFooter>
    </AlertDialogContent>
  </AlertDialog>;
}

export function apiToast(title: string, err: unknown) {
  const msg = err instanceof Error ? err.message : String(err);
  toast({ title, description: msg, variant: 'destructive' });
}

export function Section({ title, aside, children, testId }: { title: string; aside?: ReactNode; children: ReactNode; testId?: string }) {
  return <section className="border border-line bg-surface rounded-sm" data-testid={testId}>
    <header className="flex items-center justify-between h-9 px-3 border-b border-line"><h3 className="text-[11px] uppercase tracking-wider text-ink-muted font-semibold">{title}</h3>{aside}</header>
    <div className="p-3">{children}</div>
  </section>;
}

export function SkeletonRows({ cols, rows = 5 }: { cols: number; rows?: number }) {
  return <>{Array.from({ length: rows }, (_, i) => <tr key={i} className="h-8 border-t border-line">{Array.from({ length: cols }, (_, j) => <td key={j} className="px-3"><div className="h-2.5 rounded-sm bg-surface-raised animate-pulse" style={{ width: `${40 + ((i * 7 + j * 13) % 50)}%` }} /></td>)}</tr>)}</>;
}

export const when = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—');
