import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';
import { useAuth } from '@/hooks/use-auth';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { 
  Table, 
  TableBody, 
  TableCell, 
  TableHead, 
  TableHeader, 
  TableRow 
} from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { RefreshCw, Play, Loader2 } from 'lucide-react';
import { useToast } from '@/hooks/use-toast';
import { Navigate } from 'react-router-dom';

interface SystemStats {
  counts: Record<string, number>;
  database_size: string;
  migration: string;
  scheduled_runs: { task: string; window_key: string; job_id: number }[];
}

interface Job {
  id: number;
  type: string;
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
  created_at: string;
  attempts: number;
  max_attempts: number;
  error: string | null;
  progress: { done?: number; total?: number; message?: string };
}

export default function System() {
  const { user } = useAuth();
  const { toast } = useToast();
  const queryClient = useQueryClient();

  const { data: stats, isLoading: statsLoading } = useQuery<SystemStats>({
    queryKey: ['system', 'stats'],
    queryFn: () => fetchApi('/api/admin/system'),
    refetchInterval: 10000,
    enabled: user?.role === 'admin',
  });

  const { data: jobResponse, isLoading: jobsLoading } = useQuery<{ items: Job[] }>({
    queryKey: ['jobs', 'list'],
    queryFn: () => fetchApi('/api/admin/jobs'),
    refetchInterval: 5000,
    enabled: user?.role === 'admin',
  });
  const jobs = jobResponse?.items ?? [];

  const noopMutation = useMutation({
    mutationFn: () => fetchApi('/api/admin/jobs/noop', { method: 'POST' }),
    onSuccess: () => {
      toast({ title: 'No-op job triggered successfully.' });
      queryClient.invalidateQueries({ queryKey: ['jobs'] });
      queryClient.invalidateQueries({ queryKey: ['system'] });
    },
    onError: (err: any) => toast({ title: 'Failed to trigger job', description: err.message, variant: 'destructive' }),
  });

  const retryMutation = useMutation({
    mutationFn: (id: number) => fetchApi(`/api/admin/jobs/${id}/retry`, { method: 'POST' }),
    onSuccess: () => {
      toast({ title: 'Job retry initiated.' });
      queryClient.invalidateQueries({ queryKey: ['jobs'] });
      queryClient.invalidateQueries({ queryKey: ['system'] });
    },
    onError: (err: any) => toast({ title: 'Failed to retry job', description: err.message, variant: 'destructive' }),
  });

  if (user?.role !== 'admin') return <Navigate to="/" />;
  return (
    <div className="space-y-6 animate-in fade-in duration-500 max-w-6xl mx-auto">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="kin-title">System Status</h1>
          <p className="text-muted-foreground mt-1">Platform health and worker queues.</p>
        </div>
        <div className="flex space-x-2">
          <Button 
            onClick={() => noopMutation.mutate()} 
            disabled={noopMutation.isPending}
            variant="outline"
          >
            {noopMutation.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}
            Trigger Test Job
          </Button>
          <Button 
            onClick={() => {
              queryClient.invalidateQueries({ queryKey: ['system'] });
              queryClient.invalidateQueries({ queryKey: ['jobs'] });
            }}
            variant="outline" size="icon"
          >
            <RefreshCw className="h-4 w-4" />
          </Button>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2 space-y-0">
            <CardTitle className="text-sm font-medium">Pending Jobs</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold font-mono text-primary">
              {statsLoading ? '-' : stats?.counts.queued ?? 0}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2 space-y-0">
            <CardTitle className="text-sm font-medium">Running Jobs</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold font-mono text-primary">
              {statsLoading ? '-' : stats?.counts.running ?? 0}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2 space-y-0">
            <CardTitle className="text-sm font-medium">Failed Jobs</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold font-mono text-destructive">
              {statsLoading ? '-' : stats?.counts.failed ?? 0}
            </div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Worker Queue</CardTitle>
        </CardHeader>
        <CardContent>
          {jobsLoading ? (
            <div className="py-12 flex justify-center">
              <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[100px]">ID</TableHead>
                  <TableHead>Name</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {jobs.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={5} className="text-center py-8 text-muted-foreground">
                      No jobs in the queue.
                    </TableCell>
                  </TableRow>
                ) : (
                  jobs.map((job) => (
                    <TableRow key={job.id}>
                       <TableCell className="font-mono text-xs text-muted-foreground">{job.id}</TableCell>
                       <TableCell className="font-medium">{job.type}</TableCell>
                      <TableCell>
                        <Badge 
                          variant={
                             job.status === 'succeeded' ? 'default' : 
                            job.status === 'failed' ? 'destructive' : 
                            'outline'
                          }
                          className="capitalize"
                        >
                          {job.status}
                        </Badge>
                      </TableCell>
                       <TableCell className="text-muted-foreground text-sm">
                         {new Date(job.created_at).toLocaleString()}
                         {job.progress?.total ? <span className="block">{job.progress.done ?? 0} / {job.progress.total}</span> : null}
                         {job.error ? <span className="block text-destructive">{job.error}</span> : null}
                      </TableCell>
                      <TableCell className="text-right">
                        {job.status === 'failed' && (
                          <Button 
                            variant="ghost" 
                            size="sm" 
                            onClick={() => retryMutation.mutate(job.id)}
                            disabled={retryMutation.isPending && retryMutation.variables === job.id}
                          >
                            {(retryMutation.isPending && retryMutation.variables === job.id) ? 
                              <Loader2 className="h-4 w-4 animate-spin" /> : 
                              <RefreshCw className="h-4 w-4 mr-2" />
                            }
                            Retry
                          </Button>
                        )}
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
