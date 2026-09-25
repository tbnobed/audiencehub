import { useQuery } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';
import { useAuth } from '@/hooks/use-auth';

export type ShellActiveImport = {
  id: number;
  name: string;
  done: number;
  total: number;
  percent: number | null;
  rows_per_second: number | null;
  speed_basis: 'committed_rows_since_import_started';
  href: string;
};
export type ShellPayload = {
  imports_running: number | null;
  active_import: ShellActiveImport | null;
  open_issues: number;
  issue_counts: { unresolved: number; review: number };
};

export function useShell() {
  const { user } = useAuth();
  return useQuery({
    queryKey: ['shell'],
    queryFn: () => fetchApi('/api/shell') as Promise<ShellPayload>,
    enabled: !!user,
    refetchInterval: 5000,
    staleTime: 4000,
  });
}
