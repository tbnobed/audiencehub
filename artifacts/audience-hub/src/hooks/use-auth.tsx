import { createContext, useContext, type ReactNode } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchApi, setCsrfToken } from '@/lib/api';

export type Role = 'admin' | 'analyst' | 'viewer';

export interface User {
  id: number;
  email: string;
  name: string;
  role: Role;
  csrf_token: string;
  auth_mode: string;
  app_env: string;
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

  const { data: user, isLoading } = useQuery<User>({
    queryKey: ['me'],
    queryFn: async () => {
      const data = await fetchApi('/api/me');
      setCsrfToken(data.csrf_token);
      return data;
    },
    retry: false,
    staleTime: Infinity, // Avoid re-fetching too frequently
  });

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
    onSuccess: () => {
      setCsrfToken(null);
      queryClient.setQueryData(['me'], null);
      queryClient.clear();
    },
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
