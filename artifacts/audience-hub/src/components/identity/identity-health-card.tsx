import { Fingerprint, RefreshCw } from 'lucide-react';
import { Link } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { useIdentityHealth } from '@/hooks/use-identity';
import { coveragePercent, errorMessage } from '@/lib/identity';
import { MasterIdChip } from '@/components/profiles/master-id-chip';

export function IdentityHealthCard() {
  const { data, isLoading, error, refetch, isFetching } = useIdentityHealth();
  if (isLoading) return <Skeleton className="h-[260px]" data-testid="skeleton-identity-health" />;
  if (error || !data) return (
    <Card data-testid="card-identity-health-error"><CardContent className="py-6 flex items-center justify-between gap-3 text-sm">
      <span className="text-muted-foreground">Identity health unavailable: {errorMessage(error)}</span>
      <Button size="sm" variant="outline" data-testid="button-retry-identity-health" onClick={() => refetch()}><RefreshCw className="h-3 w-3 mr-1" /> Retry</Button>
    </CardContent></Card>
  );
  const stats: [string, number, string][] = [
    ['Profiles', data.profiles, ''],
    ['Missing Master ID', data.missing_master_ids, data.missing_master_ids > 0 ? 'text-warn' : ''],
    ['Conflicts', data.conflicts, data.conflicts > 0 ? 'text-warn' : ''],
    ['Hash-only', data.hash_only_profiles, ''],
    ['Duplicate HubSpot', data.duplicate_hubspot_profiles, data.duplicate_hubspot_profiles > 0 ? 'text-danger' : ''],
  ];
  return (
    <Card data-testid="card-identity-health">
      <CardHeader className="flex flex-row items-start justify-between">
        <div>
          <CardTitle className="flex items-center text-base"><Fingerprint className="h-4 w-4 mr-2 text-primary" /> Master identity</CardTitle>
          <CardDescription>Key coverage, unresolved conflicts, hash-only profiles and HubSpot duplicates.</CardDescription>
        </div>
        <Button size="icon" variant="ghost" className="h-7 w-7" disabled={isFetching} data-testid="button-refresh-identity-health" onClick={() => refetch()}><RefreshCw className={`h-3 w-3 ${isFetching ? 'animate-spin' : ''}`} /></Button>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-2 md:grid-cols-5 gap-px bg-line border border-line rounded overflow-hidden">
          {stats.map(([label, value, tone]) => <div key={label} className="bg-surface px-3 py-2">
            <div className="text-[10px] uppercase tracking-wider text-ink-muted">{label}</div>
            <div className={`font-mono text-lg ${tone}`} data-testid={`value-identity-${label.toLowerCase().replaceAll(' ', '-')}`}>{value.toLocaleString()}</div>
          </div>)}
        </div>
        <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
          <div>
            <div className="text-xs font-medium mb-2">Key coverage</div>
            {data.key_coverage.length === 0 ? <p className="text-xs text-muted-foreground">No identity keys recorded yet.</p> :
              <div className="space-y-1.5">{data.key_coverage.map(k => {
                const pct = data.profiles ? Math.min(100, (k.profiles / data.profiles) * 100) : 0;
                return <div key={k.key_type} data-testid={`row-coverage-${k.key_type}`}>
                  <div className="flex justify-between text-[11px]"><span className="font-mono">{k.key_type}</span><span className="font-mono text-ink-muted">{k.profiles.toLocaleString()} · {coveragePercent(k.profiles, data.profiles)}</span></div>
                  <div className="h-1 bg-surface-raised rounded"><div className="h-1 bg-signal rounded origin-left" style={{ transform: `scaleX(${pct / 100})` }} /></div>
                </div>;
              })}</div>}
          </div>
          <div className="xl:col-span-2 grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <div className="text-xs font-medium mb-2">Duplicate HubSpot records <span className="text-ink-muted font-normal">(first 100)</span></div>
              <div className="border border-line rounded max-h-[220px] overflow-auto"><Table><TableHeader className="bg-muted/30 sticky top-0"><TableRow><TableHead>Master ID</TableHead><TableHead>HubSpot IDs</TableHead></TableRow></TableHeader><TableBody>
                {data.duplicate_hubspot.length === 0 ? <TableRow><TableCell colSpan={2} className="text-center py-6 text-xs text-muted-foreground">No master identity holds more than one HubSpot record.</TableCell></TableRow> :
                  data.duplicate_hubspot.map(d => <TableRow key={d.master_id} data-testid={`row-duplicate-hubspot-${d.master_id}`}>
                    <TableCell><MasterIdChip masterId={d.master_id} testId={`button-copy-dup-${d.master_id}`} /></TableCell>
                    <TableCell className="font-mono text-[11px]">{d.hubspot_record_ids.join(', ')}</TableCell>
                  </TableRow>)}
              </TableBody></Table></div>
            </div>
            <div>
              <div className="text-xs font-medium mb-2">Conflicts awaiting review <span className="text-ink-muted font-normal">(latest 100)</span></div>
              <div className="border border-line rounded max-h-[220px] overflow-auto"><Table><TableHeader className="bg-muted/30 sticky top-0"><TableRow><TableHead>Reason</TableHead><TableHead>Master ID</TableHead></TableRow></TableHeader><TableBody>
                {data.review_items.length === 0 ? <TableRow><TableCell colSpan={2} className="text-center py-6 text-xs text-muted-foreground">No identity conflicts.</TableCell></TableRow> :
                  data.review_items.map(r => <TableRow key={r.id} data-testid={`row-identity-review-${r.id}`}>
                    <TableCell className="text-xs">{r.reason.replaceAll('_', ' ')}</TableCell>
                    <TableCell>{r.master_id ? <Link className="font-mono text-[11px] text-signal hover:underline" to={`/profiles?search=${encodeURIComponent(r.master_id)}`}>{r.master_id}</Link> : <span className="text-xs text-muted-foreground">—</span>}</TableCell>
                  </TableRow>)}
              </TableBody></Table></div>
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
