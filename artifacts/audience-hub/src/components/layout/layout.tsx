import { ReactNode } from 'react';
import { useAuth } from '@/hooks/use-auth';
import { Sidebar } from './sidebar';
import { TopBar } from './topbar';
import { useShortcuts } from '@/hooks/use-shortcuts';

export function Layout({ children }: { children: ReactNode }) {
  useShortcuts();
  const { user, isLoading } = useAuth();

  if (isLoading) {
    return <div className="min-h-screen bg-background flex items-center justify-center text-muted-foreground">Loading...</div>;
  }

  if (!user) {
    return <>{children}</>; // Render login screen if no user
  }

  return (
    <div className="flex h-screen overflow-hidden bg-background">
      <Sidebar />
      <div className="flex flex-col flex-1 overflow-hidden">
        <TopBar />
        <main className="flex-1 overflow-y-auto p-6 bg-background">
          {children}
        </main>
      </div>
    </div>
  );
}
