import { useState, useEffect } from 'react';
import { useAuth } from '@/hooks/use-auth';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Search, User as UserIcon, LogOut } from 'lucide-react';
import { JobsIcon } from '@/components/icons/KinshipIcons';
import { JobActivityDrawer } from './job-activity-drawer';
import { useQuery } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';

export function TopBar() {
  const { user, logout } = useAuth();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [showUserMenu, setShowUserMenu] = useState(false);
  const { data: jobs } = useQuery<{ items: { status: string }[] }>({
    queryKey: ['jobs', 'indicator'],
    queryFn: () => fetchApi('/api/admin/jobs'),
    enabled: user?.role === 'admin',
    refetchInterval: 5000,
  });
  const runningCount = jobs?.items.filter(job => job.status === 'running').length ?? 0;

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setDrawerOpen(false);
        setShowUserMenu(false);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  if (!user) return null;

  return (
    <>
      <header className="h-14 border-b border-border bg-card flex items-center justify-between px-6 shrink-0">
        <div className="flex items-center flex-1">
          <div className="relative w-96 max-w-md hidden md:block">
            <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input 
              id="global-search"
              type="search" 
              placeholder="Search... (Press '/' to focus)" 
              className="pl-9 bg-background/50 border-input h-9" 
            />
          </div>
        </div>
        <div className="flex items-center space-x-4">
          {user.app_env === 'development' && (
            <Badge variant="outline" className="font-mono bg-amber-500/10 text-amber-500 border-amber-500/50">
              DEV
            </Badge>
          )}
          {user.auth_mode === 'dev' && (
            <Badge variant="outline" className="font-mono bg-amber-500/10 text-amber-500 border-amber-500/50">
              DEV AUTH
            </Badge>
          )}
          
          <Button variant="ghost" size="icon" onClick={() => setDrawerOpen(true)} aria-label={`Job activity: ${runningCount} running`} aria-expanded={drawerOpen} className="group">
            <JobsIcon size={16} strokeWidth={1.75} className={drawerOpen || runningCount > 0 ? 'text-signal group-hover:text-ink group-focus-visible:text-ink' : 'text-ink-muted group-hover:text-ink group-focus-visible:text-ink'} />
          </Button>
          {user.role === 'admin' && <span className="font-mono text-xs text-muted-foreground" aria-live="polite">{runningCount} running</span>}

          <div className="relative">
            <Button 
              variant="ghost" 
              className="flex items-center gap-2 pl-2 pr-3"
              onClick={() => setShowUserMenu(!showUserMenu)}
            >
              <div className="h-7 w-7 rounded-full bg-primary/10 flex items-center justify-center">
                <UserIcon className="h-4 w-4 text-primary" />
              </div>
              <div className="flex flex-col items-start text-left">
                <span className="text-xs font-medium leading-none">{user.name}</span>
                <span className="text-[10px] text-muted-foreground leading-none mt-1 capitalize">{user.role}</span>
              </div>
            </Button>
            
            {showUserMenu && (
              <div className="absolute right-0 mt-1 w-48 bg-popover border border-popover-border rounded-md shadow-md py-1 z-50">
                <div className="px-4 py-2 border-b border-popover-border">
                  <p className="text-sm font-medium">{user.email}</p>
                </div>
                <button 
                  onClick={() => logout()}
                  className="w-full text-left px-4 py-2 text-sm text-destructive hover:bg-muted transition-colors flex items-center"
                >
                  <LogOut className="mr-2 h-4 w-4" />
                  Sign out
                </button>
              </div>
            )}
          </div>
        </div>
      </header>
      
      <JobActivityDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} />
    </>
  );
}
