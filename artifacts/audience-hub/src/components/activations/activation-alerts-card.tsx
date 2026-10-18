import { Link } from 'react-router-dom';
import { AlertOctagon, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { useActivationAlerts } from '@/hooks/use-activations';
import { when } from './shared';

/** Durable failed-run alerts for Data Health. Analyst+ only (endpoint enforces). */
export function ActivationAlertsCard() {
  const q = useActivationAlerts();
  const items = q.data?.items ?? [];
  return <Card data-testid="card-activation-alerts">
    <CardHeader className="flex flex-row items-start justify-between">
      <div>
        <CardTitle className="flex items-center text-base gap-2"><AlertOctagon className="h-4 w-4 text-destructive" /> Delivery failures
          {items.length > 0 && <span className="inline-flex items-center h-5 px-1.5 rounded-sm bg-destructive/15 text-destructive font-mono text-[11px]" data-testid="badge-activation-failures">{items.length}{items.length >= 100 ? '+' : ''}</span>}
        </CardTitle>
        <CardDescription>The latest failed activation runs (up to 100), newest first.</CardDescription>
      </div>
      <Button size="icon" variant="ghost" className="h-7 w-7" disabled={q.isFetching} onClick={() => q.refetch()} data-testid="button-refresh-activation-alerts"><RefreshCw className={`h-3 w-3 ${q.isFetching ? 'animate-spin' : ''}`} /></Button>
    </CardHeader>
    <CardContent className="p-0 max-h-[300px] overflow-auto">
      {q.isLoading ? <div className="p-4 space-y-2"><Skeleton className="h-6" /><Skeleton className="h-6" /></div>
        : q.isError ? <div className="p-4 flex items-center justify-between text-sm"><span className="text-muted-foreground">Couldn't load delivery alerts: {(q.error as Error).message}</span><Button size="sm" variant="outline" onClick={() => q.refetch()} data-testid="button-retry-activation-alerts">Retry</Button></div>
        : !items.length ? <p className="p-6 text-center text-sm text-muted-foreground" data-testid="text-no-activation-alerts">No failed deliveries.</p>
        : <table className="w-full text-[12px]"><tbody>{items.map(a => <tr key={a.id} className="border-t border-line align-top" data-testid={`row-activation-alert-${a.run_id}`}>
          <td className="px-3 py-1.5 font-mono text-[11px] text-ink-muted whitespace-nowrap">{when(a.created_at)}</td>
          <td className="px-2 py-1.5"><Link to={`/activations/${a.activation_id}`} className="text-signal hover:underline font-mono text-[11px]" data-testid={`link-alert-activation-${a.run_id}`}>activation #{a.activation_id} · run #{a.run_id}</Link>
            <div className="text-danger text-[11px] break-words">{a.message}</div></td>
        </tr>)}</tbody></table>}
    </CardContent>
  </Card>;
}
