import { useState, useEffect, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '@/hooks/use-auth';
import { Button } from '@/components/ui/button';
import { Search, LogOut } from 'lucide-react';
import { JobsIcon } from '@/components/icons/KinshipIcons';
import { JobActivityDrawer } from './job-activity-drawer';
import { useQuery } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';
import { useTheme } from '@/lib/theme';
import { ThemeToggle } from '@/components/theme/theme-toggle';

function initials(name: string) {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map(p => p[0]!.toUpperCase()).join('') || '?';
}

export function TopBar() {
  const { user, logout } = useAuth();
  const { theme, toggleTheme } = useTheme();
  const navigate = useNavigate();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [showUserMenu, setShowUserMenu] = useState(false);
  const [term, setTerm] = useState('');
  const { data: jobs } = useQuery<{ items: { status: string }[] }>({
    queryKey: ['jobs', 'indicator'],
    queryFn: () => fetchApi('/api/admin/jobs'),
    enabled: user?.role === 'admin',
    refetchInterval: 5000,
  });
  const runningCount = jobs?.items.filter(job => job.status === 'running').length ?? 0;

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { setDrawerOpen(false); setShowUserMenu(false); }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  if (!user) return null;

  const devEnv = user.app_env === 'development';
  const devAuth = user.auth_mode === 'dev';
  const devLabel = devEnv && devAuth ? 'DEV · DEV AUTH' : devEnv ? 'DEV' : devAuth ? 'DEV AUTH' : null;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const q = term.trim();
    navigate(q ? `/profiles?${new URLSearchParams({ search: q })}` : '/profiles');
  };

  return (
    <>
      <header className="h-14 border-b border-line bg-surface flex items-center justify-between gap-3 px-4 md:px-6 shrink-0">
        <form role="search" onSubmit={submit} className="relative flex-1 max-w-[440px] min-w-0">
          <Search size={16} strokeWidth={1.75} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-muted pointer-events-none" />
          <label htmlFor="global-search" className="sr-only">Search partners</label>
          <input
            id="global-search"
            type="search"
            value={term}
            onChange={e => setTerm(e.target.value)}
            placeholder="Search partners by name, email or phone"
            className="w-full h-9 rounded-md border border-line bg-ground pl-9 pr-9 text-[13px] text-ink placeholder:text-ink-muted outline-none focus:border-signal focus-visible:ring-2 focus-visible:ring-ring/40"
          />
          <kbd aria-hidden className="absolute right-2 top-1/2 -translate-y-1/2 hidden sm:inline-flex h-5 min-w-5 items-center justify-center rounded border border-line bg-surface-raised px-1 font-mono text-[11px] text-ink-muted">/</kbd>
        </form>
        <div className="flex items-center gap-2 md:gap-3">
          {devLabel && (
            <span className="hidden sm:inline-flex items-center rounded border border-warn/50 bg-warn/10 px-2 py-0.5 font-mono text-[11px] text-warn whitespace-nowrap" title={`Environment: ${user.app_env}; auth: ${user.auth_mode}`}>{devLabel}</span>
          )}
          <ThemeToggle theme={theme} onToggle={toggleTheme} />
          <Button variant="ghost" size="icon" onClick={() => setDrawerOpen(true)} aria-label={`Job activity${user.role === 'admin' ? `: ${runningCount} running` : ''}`} aria-expanded={drawerOpen} className="group">
            <JobsIcon size={16} strokeWidth={1.75} className={drawerOpen || runningCount > 0 ? 'text-signal group-hover:text-ink' : 'text-ink-muted group-hover:text-ink'} />
          </Button>
          <div className="relative">
            <button type="button" aria-haspopup="menu" aria-expanded={showUserMenu} onClick={() => setShowUserMenu(!showUserMenu)} className="flex items-center gap-2 rounded-md px-1.5 py-1 hover:bg-surface-raised outline-none focus-visible:ring-2 focus-visible:ring-ring">
              <span className="h-7 w-7 rounded-full bg-signal-soft text-signal flex items-center justify-center text-[11px] font-semibold" aria-hidden>{initials(user.name)}</span>
              <span className="hidden md:flex flex-col items-start text-left">
                <span className="text-xs font-medium leading-none text-ink">{user.name}</span>
                <span className="text-[10px] text-ink-muted leading-none mt-1 capitalize">{user.role}</span>
              </span>
            </button>
            {showUserMenu && (
              <div role="menu" className="absolute right-0 mt-1 w-52 bg-popover border border-line rounded-md shadow-md py-1 z-50">
                <div className="px-4 py-2 border-b border-line"><p className="text-sm font-medium truncate">{user.email}</p></div>
                <button role="menuitem" onClick={() => logout()} className="w-full text-left px-4 py-2 text-sm text-danger hover:bg-surface-raised flex items-center">
                  <LogOut size={16} strokeWidth={1.75} className="mr-2" /> Sign out
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
