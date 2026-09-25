import { Link, useLocation } from 'react-router-dom';
import { useState } from 'react';
import { useAuth } from '@/hooks/use-auth';
import { useShell } from '@/hooks/use-shell';
import { cn } from '@/lib/utils';
import { brandAsset, useTheme } from '@/lib/theme';
import { PanelLeftClose, PanelLeftOpen } from 'lucide-react';
import {
  DashboardIcon, ProfilesIcon, SegmentsIcon, ActivationsIcon, ImportsIcon,
  DataHealthIcon, SourcesIcon, AdminIcon, SystemIcon,
} from '@/components/icons/KinshipIcons';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';

type Item = { name: string; path: string; icon: typeof DashboardIcon; roles: string[]; badge?: 'imports' | 'issues' };
const groups: { label: string; items: Item[] }[] = [
  { label: 'Insights', items: [
    { name: 'Dashboard', path: '/', icon: DashboardIcon, roles: ['viewer', 'analyst', 'admin'] },
    { name: 'Profiles', path: '/profiles', icon: ProfilesIcon, roles: ['viewer', 'analyst', 'admin'] },
    { name: 'Segments', path: '/segments', icon: SegmentsIcon, roles: ['viewer', 'analyst', 'admin'] },
  ] },
  { label: 'Operate', items: [
    { name: 'Activations', path: '/activations', icon: ActivationsIcon, roles: ['analyst', 'admin'] },
    { name: 'Imports', path: '/imports', icon: ImportsIcon, roles: ['analyst', 'admin'], badge: 'imports' },
    { name: 'Sources', path: '/sources', icon: SourcesIcon, roles: ['analyst', 'admin'] },
    { name: 'Data Health', path: '/data-health', icon: DataHealthIcon, roles: ['analyst', 'admin', 'viewer'], badge: 'issues' },
  ] },
  { label: 'Settings', items: [
    { name: 'Admin', path: '/admin', icon: AdminIcon, roles: ['admin'] },
    { name: 'System', path: '/system', icon: SystemIcon, roles: ['admin'] },
  ] },
];

const nf = new Intl.NumberFormat('en-US');

export function Sidebar() {
  const [collapsed, setCollapsed] = useState(() => typeof window !== 'undefined' && window.innerWidth < 768);
  const location = useLocation().pathname;
  const { user } = useAuth();
  const { theme } = useTheme();
  const shell = useShell().data;

  if (!user) return null;
  const role = user.role;
  const running = shell?.imports_running ?? 0;
  const issues = shell?.open_issues ?? 0;
  const active = shell?.active_import ?? null;

  return (
    <div className={cn("bg-sidebar border-r border-sidebar-border flex flex-col shrink-0 transition-[width]", collapsed ? "w-14" : "w-[216px]")}>
      <div className={cn("h-14 flex items-center px-4 border-b border-sidebar-border", collapsed && "justify-center")}>
        <Link to="/" aria-label="Kinship home">
          <img src={brandAsset(collapsed ? 'mark' : 'wordmark', theme)} alt="Kinship" className="h-6 w-auto max-w-full" height={24} />
        </Link>
      </div>
      <nav aria-label="Primary" className={cn("flex-1 overflow-y-auto py-3", collapsed ? "px-2" : "px-3")}>
        {groups.map(group => {
          const items = group.items.filter(item => item.roles.includes(role));
          if (!items.length) return null;
          return (
            <div key={group.label} className="mb-3">
              {collapsed
                ? <div className="mx-2 my-2 border-t border-line" aria-hidden />
                : <div className="px-3 pt-2 pb-1.5 text-[11px] font-medium uppercase tracking-[.08em] text-ink-muted">{group.label}</div>}
              <ul className="space-y-0.5">
                {items.map(item => {
                  const isActive = location === item.path || (item.path !== '/' && location.startsWith(`${item.path}/`));
                  const badge = item.badge === 'imports' && shell?.imports_running != null && running > 0
                    ? { text: `${nf.format(running)} running`, short: String(running), cls: 'bg-signal-soft text-signal', label: `${running} running` }
                    : item.badge === 'issues' && issues > 0
                      ? { text: nf.format(issues), short: issues > 99 ? '99+' : String(issues), cls: 'bg-danger/15 text-danger', label: `${issues} open issues` }
                      : null;
                  const link = (
                    <Link
                      to={item.path}
                      aria-label={badge ? `${item.name}, ${badge.label}` : item.name}
                      aria-current={isActive ? 'page' : undefined}
                      className={cn(
                        "relative flex items-center h-8 text-[13px] font-medium rounded-md transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring",
                        collapsed ? "justify-center px-2" : "px-3",
                        isActive ? "bg-signal-soft text-signal" : "text-ink-muted hover:bg-surface-raised hover:text-ink",
                      )}
                    >
                      <item.icon size={16} strokeWidth={1.75} className={cn("shrink-0", !collapsed && "mr-2.5")} />
                      {!collapsed && <span className="truncate">{item.name}</span>}
                      {badge && !collapsed && <span className={cn("ml-auto pl-2 shrink-0 rounded px-1.5 py-px text-[10px] font-mono tabular-nums leading-4", badge.cls)}>{badge.text}</span>}
                      {badge && collapsed && <span aria-hidden className={cn("absolute -top-0.5 -right-0.5 rounded px-1 text-[9px] font-mono leading-[14px]", badge.cls)}>{badge.short}</span>}
                    </Link>
                  );
                  return (
                    <li key={item.path}>
                      {collapsed ? (
                        <Tooltip>
                          <TooltipTrigger asChild>{link}</TooltipTrigger>
                          <TooltipContent side="right" className="bg-surface-raised text-ink border border-line">{item.name}{badge ? ` · ${badge.label}` : ''}</TooltipContent>
                        </Tooltip>
                      ) : link}
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })}
      </nav>
      {active && !collapsed && (
        <Link to={active.href} className="mx-3 mb-2 block rounded-md border border-line bg-surface-raised p-3 hover:border-line-strong focus-visible:ring-2 focus-visible:ring-ring outline-none" data-testid="sidebar-active-import">
          <div className="flex items-center justify-between gap-2 text-xs">
            <span className="truncate text-ink font-medium" title={active.name}>{active.name}</span>
            {active.percent != null && <span className="font-mono tabular-nums text-signal">{Math.round(active.percent)}%</span>}
          </div>
          <div className="mt-2 h-1 rounded-full bg-line overflow-hidden" role="progressbar" aria-label={`${active.name} progress`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={active.percent ?? undefined}>
            <div className="h-full bg-signal origin-left transition-transform duration-500" style={{ transform: `scaleX(${Math.min(100, active.percent ?? 0) / 100})` }} />
          </div>
          <div className="mt-2 text-[10px] font-mono tabular-nums text-ink-muted leading-4">
            {compactRows(active.done)} / {active.total ? compactRows(active.total) : '—'} rows
            {active.rows_per_second != null && <span title="Average committed rows per second since the import started, including pauses and retries"> · {nf.format(Math.round(active.rows_per_second))}/s avg</span>}
          </div>
        </Link>
      )}
      <button type="button" title={collapsed ? "Expand sidebar" : "Collapse sidebar"} aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"} aria-expanded={!collapsed} onClick={() => setCollapsed(!collapsed)} className="m-2 flex justify-center p-2 rounded-md text-ink-muted hover:text-signal">
        {collapsed ? <PanelLeftOpen size={16} strokeWidth={1.75} /> : <PanelLeftClose size={16} strokeWidth={1.75} />}
      </button>
    </div>
  );
}

function compactRows(n: number) {
  return n >= 100_000 ? new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 2 }).format(n) : nf.format(n);
}
