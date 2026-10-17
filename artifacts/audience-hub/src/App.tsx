import { lazy, Suspense } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ErrorBoundary } from '@/components/error-boundary';
import { Toaster } from '@/components/ui/toaster';
import { TooltipProvider } from '@/components/ui/tooltip';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { AuthProvider } from '@/hooks/use-auth';
import { useAuth } from '@/hooks/use-auth';
import { Layout } from '@/components/layout/layout';

const Login = lazy(() => import('@/pages/login'));
const Dashboard = lazy(() => import('@/pages/dashboard'));
const Profiles = lazy(() => import('@/pages/profiles'));
const ProfileDetail = lazy(() => import('@/pages/profile-detail'));
const Segments = lazy(() => import('@/pages/segments'));
const Activations = lazy(() => import('@/pages/activations'));
const Imports = lazy(() => import('@/pages/imports'));
const Sources = lazy(() => import('@/pages/sources'));
const DataHealth = lazy(() => import('@/pages/data-health'));
const Admin = lazy(() => import('@/pages/admin'));
const System = lazy(() => import('@/pages/system'));
const NotFound = lazy(() => import('@/pages/not-found'));

const routeLoading = <div role="status" className="p-8 text-muted-foreground">Loading page…</div>;

const queryClient = new QueryClient();

function Router() {
  const { user, isLoading } = useAuth();
  if (isLoading) return <div className="min-h-screen bg-background text-foreground p-8">Loading console…</div>;
  if (!user) return <Suspense fallback={routeLoading}><Login /></Suspense>;
  return (
    <ErrorBoundary>
      <Layout>
        <Suspense fallback={routeLoading}>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/profiles" element={<Profiles />} />
          <Route path="/profiles/:id" element={<ProfileDetail />} />
          <Route path="/data-health" element={<DataHealth />} />
          <Route path="/segments" element={<Segments />} />
          <Route path="/segments/new" element={<Segments />} />
          <Route path="/segments/:id" element={<Segments />} />
          <Route path="/segments/:id/edit" element={<Segments />} />
          <Route path="/activations" element={user.role === 'viewer' ? <Navigate to="/" /> : <Activations />} />
          <Route path="/activations/new" element={user.role === 'viewer' ? <Navigate to="/" /> : <Activations />} />
          <Route path="/activations/:id" element={user.role === 'viewer' ? <Navigate to="/" /> : <Activations />} />
          <Route path="/activations/:id/edit" element={user.role === 'viewer' ? <Navigate to="/" /> : <Activations />} />
          <Route path="/imports" element={user.role === 'viewer' ? <Navigate to="/" /> : <Imports />} />
          <Route path="/sources" element={user.role === 'viewer' ? <Navigate to="/" /> : <Sources />} />
          <Route path="/admin" element={user.role === 'admin' ? <Admin /> : <Navigate to="/" />} />
          <Route path="/system" element={user.role === 'admin' ? <System /> : <Navigate to="/" />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
        </Suspense>
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
