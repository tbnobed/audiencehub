import { useQuery } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';
import { X, Loader2, CheckCircle2, AlertCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { useAuth } from '@/hooks/use-auth';

interface Job {
  id: number;
  type: string;
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
  created_at: string;
  progress: { done?: number; total?: number };
}

export function JobActivityDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { user } = useAuth();
  
  const { data, isLoading } = useQuery<{ items: Job[] }>({
    queryKey: ['jobs', 'activity'],
    queryFn: () => fetchApi('/api/admin/jobs'),
    refetchInterval: open ? 5000 : false,
    enabled: open && user?.role === 'admin',
  });
  const jobs = data?.items ?? [];

  return (
    <>
      {open && (
        <div 
          className="fixed inset-0 bg-background/80 backdrop-blur-sm z-40 transition-opacity"
          onClick={onClose}
        />
      )}
      <div 
        className={cn(
          "fixed top-0 right-0 h-full w-80 bg-card border-l border-border z-50 transform transition-transform duration-200 ease-in-out shadow-xl flex flex-col",
          open ? "translate-x-0" : "translate-x-full"
        )}
      >
        <div className="h-14 border-b border-border flex items-center justify-between px-4 shrink-0">
          <h2 className="font-semibold">Job Activity</h2>
          <Button variant="ghost" size="icon" onClick={onClose}>
            <X className="h-4 w-4" />
          </Button>
        </div>
        
        <div className="flex-1 overflow-y-auto p-4">
          {user?.role !== 'admin' ? (
            <div className="text-center text-sm text-muted-foreground mt-10">
              Only admins can view job activity.
            </div>
          ) : isLoading ? (
            <div className="flex justify-center mt-10">
              <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
            </div>
          ) : jobs.length === 0 ? (
            <div className="text-center text-sm text-muted-foreground mt-10">
              No recent jobs.
            </div>
          ) : (
            <div className="space-y-3">
              {jobs.map(job => (
                <div key={job.id} className="p-3 border border-border rounded-md bg-background/50 flex items-start space-x-3">
                   {job.status === 'succeeded' ? (
                    <CheckCircle2 className="h-4 w-4 text-green-500 mt-0.5 shrink-0" />
                  ) : job.status === 'failed' ? (
                    <AlertCircle className="h-4 w-4 text-destructive mt-0.5 shrink-0" />
                  ) : (
                    <Loader2 className="h-4 w-4 text-primary animate-spin mt-0.5 shrink-0" />
                  )}
                  <div className="min-w-0 flex-1">
                     <p className="text-sm font-medium truncate">{job.type} · #{job.id}</p>
                    <p className="text-xs text-muted-foreground capitalize mt-1">{job.status}</p>
                     {job.progress?.total ? <p className="text-xs text-muted-foreground">{job.progress.done ?? 0}/{job.progress.total}</p> : null}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </>
  );
}
