import { useState } from 'react';
import { 
  Activity, AlertTriangle, ShieldX, Database, GitMerge, FileWarning, 
  CheckCircle2, XCircle, ArrowRight
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { useDataHealth, useApproveBlocklist, useUnblockBlocklist } from '@/hooks/use-data-health';
import { useAuth } from '@/hooks/use-auth';
import { format } from 'date-fns';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts';
import { toast } from '@/hooks/use-toast';
import { useNavigate } from 'react-router-dom';

export default function DataHealth() {
  const { data, isLoading } = useDataHealth();
  const approveBlocklist = useApproveBlocklist();
  const unblockBlocklist = useUnblockBlocklist();
  const { user } = useAuth();
  const navigate = useNavigate();

  const handleApprove = async (id: number) => {
    try {
      await approveBlocklist.mutateAsync(id);
      toast({ title: 'Blocklist approved', description: 'The identifier will remain blocked permanently.' });
    } catch (e: any) {
      toast({ title: 'Failed to approve', description: e.message, variant: 'destructive' });
    }
  };

  const handleUnblock = async (id: number) => {
    try {
      await unblockBlocklist.mutateAsync(id);
      toast({ title: 'Identifier unblocked', description: 'The identifier has been removed from the blocklist.' });
    } catch (e: any) {
      toast({ title: 'Failed to unblock', description: e.message, variant: 'destructive' });
    }
  };

  if (isLoading) {
    return (
      <div className="p-8 space-y-6 max-w-[1400px] mx-auto">
        <div className="grid grid-cols-1 md:grid-cols-4 gap-6">
          <Skeleton className="h-32" />
          <Skeleton className="h-32" />
          <Skeleton className="h-32" />
          <Skeleton className="h-32" />
        </div>
        <Skeleton className="h-[300px]" />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <Skeleton className="h-[400px]" />
          <Skeleton className="h-[400px]" />
        </div>
      </div>
    );
  }

  if (!data) return null;

  return (
    <div className="h-full flex flex-col space-y-6 p-8 animate-in fade-in duration-500 max-w-[1400px] mx-auto">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Data Health</h1>
        <p className="text-muted-foreground text-sm mt-1">
          Monitor identity resolution engine, data anomalies, and import quality.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Pending Resolution</CardTitle>
            <Activity className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-bold font-mono tracking-tight">{data.pending_count.toLocaleString()}</div>
            <p className="text-xs text-muted-foreground mt-1">Source records awaiting stitch</p>
          </CardContent>
        </Card>
        
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Import Rejections</CardTitle>
            <FileWarning className="h-4 w-4 text-amber-500" />
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-bold font-mono tracking-tight text-amber-500">{data.rejected_rows.toLocaleString()}</div>
            <p className="text-xs text-muted-foreground mt-1">Total malformed rows skipped</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Blocklist Hits</CardTitle>
            <ShieldX className="h-4 w-4 text-destructive" />
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-bold font-mono tracking-tight text-destructive">{data.blocklist_hits.toLocaleString()}</div>
            <p className="text-xs text-muted-foreground mt-1">Records linked to bad identifiers</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Auto-Blocked Pending</CardTitle>
            <AlertTriangle className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-bold font-mono tracking-tight">{data.auto_blocklisted.length}</div>
            <p className="text-xs text-muted-foreground mt-1">Requiring admin approval</p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center text-base"><GitMerge className="h-4 w-4 mr-2 text-primary" /> Profile Merges (30 Days)</CardTitle>
          <CardDescription>Daily volume of profiles collapsed via deterministic stitching.</CardDescription>
        </CardHeader>
        <CardContent className="px-0 pb-0">
          <div className="h-[250px] w-full p-4 pt-0">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data.merges_per_day} margin={{ top: 10, right: 10, left: -20, bottom: 0 }}>
                <XAxis 
                  dataKey="day" 
                  tickFormatter={(val) => format(new Date(val), 'MMM d')} 
                  tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis 
                  tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }}
                  axisLine={false}
                  tickLine={false}
                />
                <Tooltip 
                  cursor={{ fill: 'hsl(var(--muted)/0.3)' }}
                  contentStyle={{ backgroundColor: 'hsl(var(--card))', border: '1px solid hsl(var(--border))', borderRadius: '4px', fontSize: '12px' }}
                  labelFormatter={(val) => format(new Date(val), 'MMM d, yyyy')}
                />
                <Bar dataKey="count" fill="hsl(var(--primary))" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <Card className="flex flex-col">
          <CardHeader>
            <CardTitle className="flex items-center text-base"><ShieldX className="h-4 w-4 mr-2 text-destructive" /> Auto-Blocklist Queue</CardTitle>
            <CardDescription>Identifiers flagged for high cardinality (e.g. dummy emails like test@test.com mapping to many profiles).</CardDescription>
          </CardHeader>
          <CardContent className="p-0 flex-1 overflow-auto max-h-[400px]">
            <Table>
              <TableHeader className="bg-muted/30 sticky top-0">
                <TableRow>
                  <TableHead>Identifier</TableHead>
                  <TableHead>Flagged</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.auto_blocklisted.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={3} className="text-center py-8 text-muted-foreground">
                      No identifiers pending approval.
                    </TableCell>
                  </TableRow>
                ) : (
                  data.auto_blocklisted.map((item) => (
                    <TableRow key={item.id}>
                      <TableCell>
                        <div className="flex flex-col">
                          <span className="font-mono text-xs truncate max-w-[200px]" title={item.value}>{item.value}</span>
                          <span className="text-[10px] text-muted-foreground uppercase">{item.type} • {item.reason}</span>
                        </div>
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {format(new Date(item.created_at), 'MMM d, yy')}
                      </TableCell>
                      <TableCell className="text-right">
                        {user?.role === 'admin' ? (
                          <div className="flex items-center justify-end gap-1">
                            <Button 
                              variant="ghost" 
                              size="sm"
                              className="h-7 px-2 text-emerald-500 hover:text-emerald-600 hover:bg-emerald-500/10"
                              onClick={() => handleApprove(item.id)}
                            >
                              <CheckCircle2 className="h-3 w-3 mr-1" /> Approve
                            </Button>
                            <Button 
                              variant="ghost" 
                              size="sm"
                              className="h-7 px-2 text-destructive hover:text-destructive hover:bg-destructive/10"
                              onClick={() => handleUnblock(item.id)}
                            >
                              <XCircle className="h-3 w-3 mr-1" /> Unblock
                            </Button>
                          </div>
                        ) : (
                          <span className="text-[10px] text-muted-foreground italic">Admin required</span>
                        )}
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </CardContent>
        </Card>

        <Card className="flex flex-col">
          <CardHeader>
            <CardTitle className="flex items-center text-base"><Database className="h-4 w-4 mr-2" /> Recent Imports</CardTitle>
            <CardDescription>Latest data ingestion jobs and their rejection counts.</CardDescription>
          </CardHeader>
          <CardContent className="p-0 flex-1 overflow-auto max-h-[400px]">
            <Table>
              <TableHeader className="bg-muted/30 sticky top-0">
                <TableRow>
                  <TableHead>Job / File</TableHead>
                  <TableHead className="text-right">Rows (OK/Err)</TableHead>
                  <TableHead></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.recent_imports.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={3} className="text-center py-8 text-muted-foreground">
                      No recent imports.
                    </TableCell>
                  </TableRow>
                ) : (
                  data.recent_imports.map((imp) => (
                    <TableRow key={imp.id}>
                      <TableCell>
                        <div className="flex flex-col">
                          <span className="font-medium text-xs flex items-center">
                            {imp.filename}
                            <Badge variant="outline" className="ml-2 text-[9px] h-4 py-0 uppercase">
                              {imp.status}
                            </Badge>
                          </span>
                          <span className="text-[10px] text-muted-foreground mt-0.5">
                            {imp.source_key} • {format(new Date(imp.created_at), 'MMM d HH:mm')}
                          </span>
                        </div>
                      </TableCell>
                      <TableCell className="text-right font-mono text-xs">
                        <span className="text-emerald-500">{imp.rows_ok}</span>
                        <span className="text-muted-foreground mx-1">/</span>
                        <span className={imp.rows_rejected > 0 ? "text-amber-500" : "text-muted-foreground"}>
                          {imp.rows_rejected}
                        </span>
                      </TableCell>
                      <TableCell className="text-right">
                        <Button variant="ghost" size="icon" className="h-6 w-6" onClick={() => navigate('/imports')}>
                          <ArrowRight className="h-3 w-3" />
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}