import { RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { useAuth } from '@/hooks/use-auth';
import { ACTIVE_JOB_STATUSES, useSourceJobs } from '@/hooks/use-source-jobs';

const fmt = (v: string | null) => (v ? new Date(v).toLocaleString() : '—');
const cls = (s: string) => s === 'completed' ? 'bg-ok/15 text-ok' : s === 'failed' || s === 'dead' ? 'bg-destructive/15 text-destructive' : ACTIVE_JOB_STATUSES.includes(s) ? 'bg-signal-soft text-primary' : 'bg-muted text-muted-foreground';

export function SourceJobsPanel() {
  const { user } = useAuth();
  const isAdmin = user?.role === 'admin';
  const jobs = useSourceJobs(isAdmin);
  if (!isAdmin) return null;
  const items = jobs.data?.items ?? [];
  return (
    <section className="space-y-2 rounded-md border bg-card p-4" data-testid="panel-source-jobs">
      <div className="flex items-center justify-between">
        <h2 className="kin-heading">Source pulls</h2>
        {items.some(i => ACTIVE_JOB_STATUSES.includes(i.status)) && <span className="text-[11px] text-muted-foreground" data-testid="status-source-jobs-polling">Refreshing every 3s</span>}
      </div>
      {jobs.isLoading ? <div className="h-12 animate-pulse rounded-md bg-muted" data-testid="source-jobs-loading" /> : jobs.error ? (
        <div role="alert" className="flex items-center gap-2 text-xs text-destructive" data-testid="text-source-jobs-error">
          Unable to load source pulls: {jobs.error.message}
          <Button size="sm" variant="ghost" onClick={() => jobs.refetch()} data-testid="button-retry-source-jobs"><RefreshCw className="mr-1 h-3 w-3" /> Retry</Button>
        </div>
      ) : items.length === 0 ? (
        <p className="rounded-md border border-dashed p-3 text-center text-xs text-muted-foreground" data-testid="empty-source-jobs">No source pulls yet.</p>
      ) : (
        <div className="divide-y rounded-md border">
          {items.map(j => {
            const c = j.counts;
            const active = ACTIVE_JOB_STATUSES.includes(j.status);
            const registered = c.imports_total > 0;
            const pct = registered ? Math.min(100, (c.imports_completed / c.imports_total) * 100) : 0;
            return (
              <div key={j.job_id} className="space-y-1.5 px-3 py-2 text-xs" data-testid={`row-source-job-${j.job_id}`}>
                <div className="flex flex-wrap items-center gap-3">
                  <span className="font-mono">job #{j.job_id}</span>
                  <span className="font-medium">{j.source_name}</span>
                  <span className={`rounded px-1.5 text-[10px] uppercase ${cls(j.status)}`} data-testid={`status-source-job-${j.job_id}`}>{j.status}</span>
                  {j.phase && <span className="text-muted-foreground" data-testid={`text-source-job-phase-${j.job_id}`}>{j.phase}</span>}
                  <span className="ml-auto text-muted-foreground">{fmt(j.finished_at ?? j.started_at ?? j.created_at)}</span>
                </div>
                {active && (registered ? <Progress value={pct} className="h-1.5" /> : <div className="h-1.5 animate-pulse rounded bg-muted" />)}
                <div className="flex flex-wrap gap-x-4 text-[11px] text-muted-foreground" data-testid={`counts-source-job-${j.job_id}`}>
                  {!registered && j.rows_read !== null && <span>collected {j.rows_read.toLocaleString()} rows (imports not registered yet)</span>}
                  {registered && <span>imports {c.imports_completed.toLocaleString()}/{c.imports_total.toLocaleString()}</span>}
                  {(registered || c.rows_total > 0) && <>
                    <span>rows {c.rows_total.toLocaleString()}</span>
                    <span className="text-ok">ok {c.rows_ok.toLocaleString()}</span>
                    <span className={c.rows_rejected ? 'text-destructive' : ''}>rejected {c.rows_rejected.toLocaleString()}</span>
                  </>}
                </div>
                {j.error && <p role="alert" className="text-[11px] text-destructive" data-testid={`error-source-job-${j.job_id}`}>{j.error}</p>}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
