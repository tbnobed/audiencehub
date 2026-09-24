import { Link, useLocation } from 'react-router-dom';
import { useState } from 'react';
import { useAuth } from '@/hooks/use-auth';
import { cn } from '@/lib/utils';
import { 
  LayoutDashboard, 
  Users, 
  PieChart, 
  Activity, 
  ArrowDownToLine, 
  Database,
  Settings,
  Server, PanelLeftClose, PanelLeftOpen
} from 'lucide-react';

export function Sidebar() {
  const [collapsed, setCollapsed] = useState(false);
  const location = useLocation().pathname;
  const { user } = useAuth();
  
  if (!user) return null;

  const role = user.role;

  const navItems = [
    { name: 'Dashboard', path: '/', icon: LayoutDashboard, roles: ['viewer', 'analyst', 'admin'] },
    { name: 'Profiles', path: '/profiles', icon: Users, roles: ['viewer', 'analyst', 'admin'] },
    { name: 'Segments', path: '/segments', icon: PieChart, roles: ['viewer', 'analyst', 'admin'] },
    { name: 'Activations', path: '/activations', icon: Activity, roles: ['analyst', 'admin'] },
    { name: 'Imports', path: '/imports', icon: ArrowDownToLine, roles: ['analyst', 'admin'] },
    { name: 'Data Health', path: '/data-health', icon: Server, roles: ['analyst', 'admin', 'viewer'] },
    { name: 'Sources', path: '/sources', icon: Database, roles: ['analyst', 'admin'] },
    { name: 'Admin', path: '/admin', icon: Settings, roles: ['admin'] },
    { name: 'System', path: '/system', icon: Server, roles: ['admin'] },
  ];

  const visibleItems = navItems.filter(item => item.roles.includes(role));

  return (
    <div className={cn("bg-sidebar border-r border-sidebar-border flex flex-col shrink-0 transition-[width]", collapsed ? "w-14" : "w-[200px]")}>
      <div className={cn("h-14 flex items-center px-4 border-b border-sidebar-border", collapsed && "justify-center")}>
        <Link to="/" aria-label="Kinship home">
          <img
            src={`${import.meta.env.BASE_URL}brand/${collapsed ? 'kinship-mark-dark.svg' : 'kinship-sidebar-wordmark.svg'}`}
            alt="Kinship"
            className="h-6 w-auto max-w-full"
            height={24}
          />
        </Link>
      </div>
      <nav className={cn("flex-1 py-4 space-y-1", collapsed ? "px-2" : "px-3")}>
        {visibleItems.map(item => {
          const isActive = location === item.path || (item.path !== '/' && location.startsWith(item.path));
          return (
            <Link 
              key={item.path} 
              to={item.path}
              title={collapsed ? item.name : undefined}
              aria-label={item.name}
              className={cn(
                "flex items-center py-2 text-sm font-medium rounded-md transition-colors",
                collapsed ? "justify-center px-2" : "px-3",
                isActive 
                  ? "bg-sidebar-accent text-sidebar-accent-foreground" 
                  : "text-sidebar-foreground hover:bg-sidebar-accent/50 hover:text-sidebar-accent-foreground"
              )}
            >
               <item.icon className={cn("h-4 w-4 shrink-0", !collapsed && "mr-3")} />
               {!collapsed && item.name}
            </Link>
          );
        })}
      </nav>
      <button type="button" title={collapsed ? "Expand sidebar" : "Collapse sidebar"} aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"} aria-expanded={!collapsed} onClick={() => setCollapsed(!collapsed)} className="m-2 flex justify-center p-2 rounded-md text-sidebar-foreground hover:text-primary">
        {collapsed ? <PanelLeftOpen className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
      </button>
    </div>
  );
}
