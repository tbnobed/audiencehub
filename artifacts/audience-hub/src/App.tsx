import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ErrorBoundary } from '@/components/error-boundary';
import { Toaster } from '@/components/ui/toaster';
import { TooltipProvider } from '@/components/ui/tooltip';
import NotFound from '@/pages/not-found';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { AuthProvider } from '@/hooks/use-auth';
import { useAuth } from '@/hooks/use-auth';
import { Layout } from '@/components/layout/layout';

import Login from '@/pages/login';
import Dashboard from '@/pages/dashboard';
import Profiles from '@/pages/profiles';
import ProfileDetail from '@/pages/profile-detail';
import Segments from '@/pages/segments';
import Activations from '@/pages/activations';
import Imports from '@/pages/imports';
import Sources from '@/pages/sources';
import DataHealth from '@/pages/data-health';
import Admin from '@/pages/admin';
import System from '@/pages/system';

const queryClient = new QueryClient();

function Router() {
  const { user, isLoading } = useAuth();
  if (isLoading) return <div className="min-h-screen bg-background text-foreground p-8">Loading console…</div>;
  if (!user) return <Login />;
  return (
    <ErrorBoundary>
      <Layout>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/profiles" element={<Profiles />} />
          <Route path="/profiles/:id" element={<ProfileDetail />} />
          <Route path="/data-health" element={<DataHealth />} />
          <Route path="/segments" element={<Segments />} />
          <Route path="/activations" element={user.role === 'viewer' ? <Navigate to="/" /> : <Activations />} />
          <Route path="/imports" element={user.role === 'viewer' ? <Navigate to="/" /> : <Imports />} />
          <Route path="/sources" element={user.role === 'viewer' ? <Navigate to="/" /> : <Sources />} />
          <Route path="/admin" element={user.role === 'admin' ? <Admin /> : <Navigate to="/" />} />
          <Route path="/system" element={user.role === 'admin' ? <System /> : <Navigate to="/" />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </Layout>
    </ErrorBoundary>
  );
}

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <AuthProvider>
          <BrowserRouter basename={import.meta.env.BASE_URL}>
            <Router />
          </BrowserRouter>
          <Toaster />
        </AuthProvider>
      </TooltipProvider>
    </QueryClientProvider>
  );
}

export default App;
