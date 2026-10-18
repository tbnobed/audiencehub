import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { KeyRound, RefreshCw, Undo2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { useAuth } from '@/hooks/use-auth';
import { useMasterIdentity, useUnmerge, resolveMaster } from '@/hooks/use-identity';
import { errorMessage, unmergeState, type MergeHistoryItem } from '@/lib/identity';
import { displayDate } from '@/lib/profile-display';
import { toast } from '@/hooks/use-toast';

export function MasterIdentityPanel({ masterId, profileId }: { masterId: string | null | undefined; profileId: number }) {
  if (!masterId) return <Card><CardContent className="py-6 text-sm text-muted-foreground" data-testid="text-no-master-id">This profile has no Master ID yet. Merge history and reversal become available after master ID backfill.</CardContent></Card>;
  return <MasterIdentityBody masterId={masterId} profileId={profileId} />;
}

function MasterIdentityBody({ masterId, profileId }: { masterId: string; profileId: number }) {
  const { user } = useAuth();
  const navigate = useNavigate();
  const isAdmin = user?.role === 'admin';
  const { data, isLoading, error, refetch } = useMasterIdentity(masterId);
  const unmerge = useUnmerge();
  const [pending, setPending] = useState<MergeHistoryItem | null>(null);

  if (isLoading) return <Skeleton className="h-48" />;
  if (error || !data) return <Card><CardContent className="py-6 flex items-center justify-between text-sm"><span className="text-muted-foreground">Master identity unavailable: {errorMessage(error)}</span><Button size="sm" variant="outline" data-testid="button-retry-master" onClick={() => refetch()}><RefreshCw className="h-3 w-3 mr-1" />Retry</Button></CardContent></Card>;

  const confirm = async () => {
    if (!pending) return;
    try {
      const result = await unmerge.mutateAsync(pending.merge_id);
      toast({ title: 'Merge reversed', description: `Retained ${result.retained_master_id}; separated ${result.new_master_id}.` });
      setPending(null);
      // Navigate to the side this page no longer represents if it moved; numeric routes stay canonical.
      const retained = await resolveMaster(result.retained_master_id).catch(() => null);
      if (retained?.profile_id && retained.profile_id !== profileId) navigate(`/profiles/${retained.profile_id}`);
    } catch (e) {
      toast({ title: 'Unmerge refused', description: errorMessage(e), variant: 'destructive' });
    }
  };

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
      <Card>
        <CardHeader><CardTitle className="flex items-center text-base"><KeyRound className="h-4 w-4 mr-2" /> Master keys</CardTitle>
          <CardDescription>Cross-reference keys bound to {data.master_id}{data.requested_master_id !== data.master_id ? ` (resolved from ${data.requested_master_id})` : ''}.</CardDescription></CardHeader>
        <CardContent className="p-0 max-h-[320px] overflow-auto"><Table><TableHeader className="bg-muted/30"><TableRow><TableHead>Key</TableHead><TableHead>Value</TableHead><TableHead className="text-right">Last seen</TableHead></TableRow></TableHeader><TableBody>
          {data.keys.length === 0 ? <TableRow><TableCell colSpan={3} className="text-center py-6 text-muted-foreground">No keys recorded.</TableCell></TableRow> :
            data.keys.map((k, i) => <TableRow key={i}><TableCell className="font-mono text-[11px] uppercase">{k.key_type}</TableCell><TableCell className="font-mono text-[11px] break-all">{k.key_value}</TableCell><TableCell className="text-right text-xs text-muted-foreground">{displayDate(k.last_seen_at, 'MMM d, yy')}</TableCell></TableRow>)}
        </TableBody></Table></CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle className="flex items-center text-base"><Undo2 className="h-4 w-4 mr-2" /> Identity merge journal</CardTitle>
          <CardDescription>Reversal is admin only and re-checked by the server. Later dependent merges must be reversed first.</CardDescription></CardHeader>
        <CardContent className="p-0"><Table><TableHeader className="bg-muted/30"><TableRow><TableHead>Merged</TableHead><TableHead>Survivor / Retired</TableHead><TableHead className="text-right">Action</TableHead></TableRow></TableHeader><TableBody>
          {data.merge_history.length === 0 ? <TableRow><TableCell colSpan={3} className="text-center py-6 text-muted-foreground">No identity merges.</TableCell></TableRow> :
            data.merge_history.map(m => { const s = unmergeState(m, isAdmin); return <TableRow key={m.merge_id} data-testid={`row-merge-${m.merge_id}`}>
              <TableCell className="text-xs text-muted-foreground">{displayDate(m.merged_at, 'MMM d, yy HH:mm')}</TableCell>
              <TableCell className="font-mono text-[11px]"><div>{m.survivor_id ?? '—'}</div><div className="text-ink-muted">{m.retired_id ?? '—'}</div></TableCell>
              <TableCell className="text-right">{m.undone_at ? <Badge variant="outline" className="text-[10px]">Reversed</Badge> :
                <Button size="sm" variant="outline" className="h-7 text-xs" disabled={!s.allowed || unmerge.isPending} title={s.reason || 'Reverse this merge'} data-testid={`button-unmerge-${m.merge_id}`} onClick={() => setPending(m)}>Unmerge</Button>}
                {!s.allowed && !m.undone_at && <div className="text-[10px] text-muted-foreground mt-1 max-w-[180px] ml-auto" data-testid={`text-unmerge-reason-${m.merge_id}`}>{s.reason}</div>}
              </TableCell>
            </TableRow>; })}
        </TableBody></Table></CardContent>
      </Card>
      <AlertDialog open={!!pending} onOpenChange={o => { if (!o && !unmerge.isPending) setPending(null); }}>
        <AlertDialogContent>
          <AlertDialogHeader><AlertDialogTitle>Reverse merge #{pending?.merge_id}?</AlertDialogTitle>
            <AlertDialogDescription>This splits {pending?.retired_id} back out of {pending?.survivor_id}. Identifiers, source records, gifts and events return to their recorded sides; traits and audience membership are recomputed. Opt-outs are kept on both sides. The retired Master ID stays retired; the separated side receives a new Master ID. The server will refuse if source records changed since the merge.</AlertDialogDescription></AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={unmerge.isPending} data-testid="button-cancel-unmerge">Cancel</AlertDialogCancel>
            <AlertDialogAction className="bg-destructive text-ink hover:bg-destructive/90" disabled={unmerge.isPending} data-testid="button-confirm-unmerge" onClick={e => { e.preventDefault(); void confirm(); }}>{unmerge.isPending ? 'Reversing…' : 'Reverse merge'}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
