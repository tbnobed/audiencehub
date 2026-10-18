import { useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { useCanonicalSchema } from '@/hooks/use-profiles';
import { groupCanonical } from '@/lib/canonical-display';

export function CanonicalAttributes({ values, hideSensitive }: { values: Record<string, unknown> | undefined; hideSensitive: boolean }) {
  const { data, isLoading, error, refetch } = useCanonicalSchema();
  const [showEmpty, setShowEmpty] = useState(false);
  if (isLoading) return <Skeleton className="h-40 w-full" />;
  if (error || !data) return <Card><CardContent className="py-4 flex items-center justify-between text-xs text-muted-foreground" data-testid="canonical-error">
    Imported attribute catalog unavailable.
    <Button size="sm" variant="outline" data-testid="button-retry-canonical" onClick={() => refetch()}><RefreshCw className="h-3 w-3 mr-1" />Retry</Button>
  </CardContent></Card>;
  const groups = groupCanonical(data.fields, values ?? {}, { hideSensitive });
  const presentCount = groups.reduce((n, g) => n + g.rows.filter(r => r.present).length, 0);
  return (
    <Card data-testid="card-canonical-attributes">
      <CardHeader className="pb-3 flex flex-row items-start justify-between gap-3">
        <div>
          <CardTitle className="text-sm">Imported partner attributes</CardTitle>
          <CardDescription>First-party values resolved by source priority. Imported model scores are shown with their model dates and are separate from the computed RFM above.</CardDescription>
        </div>
        <Button size="sm" variant="ghost" className="h-7 text-xs shrink-0" data-testid="button-toggle-empty-canonical" onClick={() => setShowEmpty(v => !v)}>{showEmpty ? 'Hide empty' : 'Show empty'}</Button>
      </CardHeader>
      <CardContent className="space-y-4">
        {presentCount === 0 && !showEmpty && <p className="text-xs text-muted-foreground" data-testid="text-canonical-empty">No imported attributes recorded for this partner.</p>}
        {groups.map(g => {
          const rows = showEmpty ? g.rows : g.rows.filter(r => r.present);
          if (!rows.length) return null;
          return <section key={g.group} data-testid={`group-canonical-${g.group.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`}>
            <div className="flex items-baseline justify-between border-b border-line pb-1 mb-1">
              <h3 className="text-[11px] uppercase tracking-wider text-ink-muted font-medium">{g.group}</h3>
              {g.modelDates.length > 0 && <div className="flex flex-wrap gap-x-3 text-[10px] text-ink-muted font-mono">{g.modelDates.map(d => <span key={d.key}>{d.label}: <span className="text-ink">{d.value}</span></span>)}</div>}
            </div>
            <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-x-6 gap-y-2">
              {rows.map(r => <div key={r.key} className="min-w-0" data-testid={`canonical-${r.key}`}>
                <div className="text-[10px] uppercase tracking-wide text-ink-muted truncate" title={r.key}>{r.label}</div>
                <div className={`font-mono text-xs break-words ${r.present ? 'text-ink' : 'text-ink-muted'}`}>{r.value}</div>
                {r.modelDate && r.present && <div className="text-[10px] text-ink-muted">model {r.modelDate}</div>}
              </div>)}
            </div>
          </section>;
        })}
      </CardContent>
    </Card>
  );
}
