import { Home, RefreshCw, Smartphone } from 'lucide-react';
import { Link } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { useAuth } from '@/hooks/use-auth';
import { useProfileGraph } from '@/hooks/use-identity';
import { displayDate } from '@/lib/profile-display';
import { errorMessage } from '@/lib/identity';

export function HouseholdDevicePanel({ profileId }: { profileId: number }) {
  const { user } = useAuth();
  const allowed = user?.role === 'admin' || user?.role === 'analyst';
  const { data, isLoading, error, refetch } = useProfileGraph(profileId, allowed);
  if (!allowed) return null;
  if (isLoading) return <Skeleton className="h-40" />;
  if (error || !data) return <Card><CardContent className="py-6 flex items-center justify-between text-sm"><span className="text-muted-foreground">Address, household and device evidence unavailable: {errorMessage(error)}</span><Button size="sm" variant="outline" onClick={() => refetch()}><RefreshCw className="h-3 w-3 mr-1" />Retry</Button></CardContent></Card>;
  const a = data.address;
  const line = a ? [[a.number, a.street, a.unit].filter(Boolean).join(' '), a.city, [a.state, [a.zip5, a.zip4].filter(Boolean).join('-')].filter(Boolean).join(' ')].filter(Boolean).join(', ') : '';
  return <div className="grid grid-cols-1 md:grid-cols-2 gap-6" data-testid="panel-household-device">
    <Card>
      <CardHeader><CardTitle className="flex items-center text-base"><Home className="h-4 w-4 mr-2" /> Address and household</CardTitle><CardDescription>Standardized address evidence used to group household members.</CardDescription></CardHeader>
      <CardContent className="space-y-3 text-sm">
        {a ? <dl className="grid grid-cols-[110px_1fr] gap-y-1 text-xs">
          <dt className="text-muted-foreground">Standardized</dt><dd>{line || '—'}</dd>
          <dt className="text-muted-foreground">Address key</dt><dd className="font-mono break-all">{a.normalized_key ?? '—'}</dd>
          <dt className="text-muted-foreground">Parser</dt><dd className="font-mono">{a.parser ?? '—'}</dd>
          <dt className="text-muted-foreground">Vendor</dt><dd className="font-mono">{a.vendor ?? '—'}{a.vendor_result ? ` · ${typeof a.vendor_result === 'string' ? a.vendor_result : JSON.stringify(a.vendor_result)}` : ''}</dd>
        </dl> : <p className="text-xs text-muted-foreground">No standardized address on file.</p>}
        {data.household ? <Table><TableHeader className="bg-muted/30"><TableRow><TableHead>Household {String(data.household.id)} members</TableHead><TableHead className="text-right">Master ID</TableHead></TableRow></TableHeader><TableBody>
          {data.household.members.map(m => <TableRow key={m.id}><TableCell><Link className="hover:text-signal" to={`/profiles/${m.id}`}>{[m.first_name, m.last_name].filter(Boolean).join(' ') || `Profile ${m.id}`}</Link></TableCell><TableCell className="text-right font-mono text-[11px]">{m.master_id ?? '—'}</TableCell></TableRow>)}
        </TableBody></Table> : <p className="text-xs text-muted-foreground">Not part of a household.</p>}
      </CardContent>
    </Card>
    <Card>
      <CardHeader><CardTitle className="flex items-center text-base"><Smartphone className="h-4 w-4 mr-2" /> Devices</CardTitle><CardDescription>Device keys expire automatically.</CardDescription></CardHeader>
      <CardContent className="p-0 max-h-[320px] overflow-auto"><Table><TableHeader className="bg-muted/30"><TableRow><TableHead>Type</TableHead><TableHead>Device</TableHead><TableHead>Last seen</TableHead><TableHead className="text-right">Expires</TableHead></TableRow></TableHeader><TableBody>
        {data.devices.length === 0 ? <TableRow><TableCell colSpan={4} className="text-center py-6 text-muted-foreground">No device keys.</TableCell></TableRow>
          : data.devices.map((d, i) => <TableRow key={i}><TableCell className="font-mono text-[11px] uppercase">{d.key_type}</TableCell><TableCell className="font-mono text-[11px] break-all">{d.device_id}</TableCell><TableCell className="text-xs text-muted-foreground">{displayDate(d.last_seen_at, 'MMM d, yy')}</TableCell><TableCell className="text-right text-xs text-muted-foreground">{displayDate(d.expires_at, 'MMM d, yy')}</TableCell></TableRow>)}
      </TableBody></Table></CardContent>
    </Card>
  </div>;
}
