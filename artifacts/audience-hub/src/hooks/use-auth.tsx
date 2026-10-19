import { createContext, useContext, useCallback, useEffect, type ReactNode } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { ApiError, API_TIMEOUT_MS, fetchApi, setCsrfToken } from '@/lib/api';

export type Role = 'admin' | 'analyst' | 'viewer';

export interface User {
  id: number;
  email: string;
  name: string;
  role: Role;
  csrf_token: string;
  auth_mode: string;
  app_env: string;
  mfa_required?: boolean;
  mfa_enabled?: boolean;
}

interface AuthContextType {
  user: User | null;
  isLoading: boolean;
  login: (role: Role, name: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();

  const { data: user, isLoading } = useQuery<User | null>({
    queryKey: ['me'],
    queryFn: async ({ signal }) => {
      try {
        const data = await fetchApi('/api/me', { signal, timeoutMs: API_TIMEOUT_MS });
        setCsrfToken(data.csrf_token);
        return data;
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          setCsrfToken(null);
          return null;
        }
        throw error;
      }
    },
    retry: false,
    staleTime: 60_000,
    refetchInterval: query => query.state.data && !(query.state.data.mfa_required && !query.state.data.mfa_enabled) ? 60_000 : false,
    refetchIntervalInBackground: false,
    // The enrollment gate owns its completion transition: a background probe
    // must not unmount one-time recovery codes before the user saves them.
    refetchOnWindowFocus: query => !!query.state.data && !(query.state.data.mfa_required && !query.state.data.mfa_enabled),
    refetchOnReconnect: query => !!query.state.data && !(query.state.data.mfa_required && !query.state.data.mfa_enabled),
  });
  const resetSession = useCallback(() => {
    setCsrfToken(null);
    void queryClient.cancelQueries();
    // Preserve the observed session query. Removing it during its own request
    // can recreate it and trap the router in its initial loading state.
    queryClient.removeQueries({ predicate: query => query.queryKey[0] !== 'me' });
    queryClient.getMutationCache().clear();
    queryClient.setQueryData(['me'], null);
  }, [queryClient]);
  useEffect(() => {
    window.addEventListener('kinship-session-expired', resetSession);
    return () => window.removeEventListener('kinship-session-expired', resetSession);
  }, [resetSession]);

  const loginMutation = useMutation({
    mutationFn: async ({ role, name }: { role: Role; name: string }) => {
      await fetchApi('/auth/dev-login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ role, name }),
      });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['me'] });
    },
  });

  const logoutMutation = useMutation({
    mutationFn: async () => {
      await fetchApi('/auth/logout', { method: 'POST' });
    },
    onSuccess: resetSession,
  });

  return (
    <AuthContext.Provider
      value={{
        user: user || null,
        isLoading,
        login: async (role, name) => {
          await loginMutation.mutateAsync({ role, name });
        },
        logout: async () => {
          await logoutMutation.mutateAsync();
        },
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
