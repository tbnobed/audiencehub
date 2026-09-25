import { Link, useLocation } from 'react-router-dom';
import { useState } from 'react';
import { useAuth } from '@/hooks/use-auth';
import { cn } from '@/lib/utils';
import { brandAsset, useTheme } from '@/lib/theme';
import { PanelLeftClose, PanelLeftOpen } from 'lucide-react';
import {
  DashboardIcon, ProfilesIcon, SegmentsIcon, ActivationsIcon, ImportsIcon,
  DataHealthIcon, SourcesIcon, AdminIcon, SystemIcon,
} from '@/components/icons/KinshipIcons';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';

export function Sidebar() {
  const [collapsed, setCollapsed] = useState(false);
  const location = useLocation().pathname;
  const { user } = useAuth();
  const { theme } = useTheme();
  
  if (!user) return null;

  const role = user.role;

  const navItems = [
    { name: 'Dashboard', path: '/', icon: DashboardIcon, roles: ['viewer', 'analyst', 'admin'] },
    { name: 'Profiles', path: '/profiles', icon: ProfilesIcon, roles: ['viewer', 'analyst', 'admin'] },
    { name: 'Segments', path: '/segments', icon: SegmentsIcon, roles: ['viewer', 'analyst', 'admin'] },
    { name: 'Activations', path: '/activations', icon: ActivationsIcon, roles: ['analyst', 'admin'] },
    { name: 'Imports', path: '/imports', icon: ImportsIcon, roles: ['analyst', 'admin'] },
    { name: 'Data Health', path: '/data-health', icon: DataHealthIcon, roles: ['analyst', 'admin', 'viewer'] },
    { name: 'Sources', path: '/sources', icon: SourcesIcon, roles: ['analyst', 'admin'] },
    { name: 'Admin', path: '/admin', icon: AdminIcon, roles: ['admin'] },
    { name: 'System', path: '/system', icon: SystemIcon, roles: ['admin'] },
  ];

  const visibleItems = navItems.filter(item => item.roles.includes(role));

  return (
    <div className={cn("bg-sidebar border-r border-sidebar-border flex flex-col shrink-0 transition-[width]", collapsed ? "w-14" : "w-[200px]")}>
      <div className={cn("h-14 flex items-center px-4 border-b border-sidebar-border", collapsed && "justify-center")}>
        <Link to="/" aria-label="Kinship home">
          <img
            src={brandAsset(collapsed ? 'mark' : 'wordmark', theme)}
            alt="Kinship"
            className="h-6 w-auto max-w-full"
            height={24}
          />
        </Link>
      </div>
      <nav className={cn("flex-1 py-4 space-y-1", collapsed ? "px-2" : "px-3")}>
        {visibleItems.map(item => {
          const isActive = location === item.path || (item.path !== '/' && location.startsWith(`${item.path}/`));
          const link = (
            <Link
              to={item.path}
              aria-label={item.name}
              aria-current={isActive ? 'page' : undefined}
              className={cn(
                "flex items-center py-2 text-sm font-medium rounded-md transition-colors",
                collapsed ? "justify-center px-2" : "px-3",
                isActive
                  ? "bg-signal-soft text-signal hover:text-ink focus-visible:text-ink"
                  : "text-ink-muted hover:bg-sidebar-accent/50 hover:text-ink focus-visible:text-ink"
              )}
            >
              <item.icon size={16} strokeWidth={1.75} className={cn("shrink-0", !collapsed && "mr-3")} />
              {!collapsed && item.name}
            </Link>
          );
          return (
            <div key={item.path}>
              {collapsed ? (
                <Tooltip>
                  <TooltipTrigger asChild>{link}</TooltipTrigger>
                  <TooltipContent side="right" className="bg-surface-raised text-ink border border-line">{item.name}</TooltipContent>
                </Tooltip>
              ) : link}
            </div>
          );
        })}
      </nav>
      <button type="button" title={collapsed ? "Expand sidebar" : "Collapse sidebar"} aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"} aria-expanded={!collapsed} onClick={() => setCollapsed(!collapsed)} className="m-2 flex justify-center p-2 rounded-md text-sidebar-foreground hover:text-primary">
        {collapsed ? <PanelLeftOpen className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
      </button>
    </div>
  );
}
