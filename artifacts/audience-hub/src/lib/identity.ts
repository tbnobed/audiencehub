export type MergeHistoryItem = {
  merge_id: number;
  survivor_id: string | null;
  retired_id: string | null;
  merged_at: string | null;
  undone_at: string | null;
  reversible: boolean | null;
};

export type UnmergeState = { allowed: boolean; reason: string };

/** Client hint only; the server enforces provenance and ordering. */
export function unmergeState(item: MergeHistoryItem, isAdmin: boolean): UnmergeState {
  if (item.undone_at) return { allowed: false, reason: 'Already reversed.' };
  if (!item.reversible) return { allowed: false, reason: 'No reversible provenance recorded (legacy merge or later dependent merge).' };
  if (!isAdmin) return { allowed: false, reason: 'Admin role required to reverse merges.' };
  return { allowed: true, reason: '' };
}

export function coveragePercent(covered: number, total: number): string {
  if (!total || total <= 0) return '—';
  return `${((covered / total) * 100).toFixed(1)}%`;
}

export function errorMessage(e: unknown): string {
  if (e instanceof Error && e.message) return e.message;
  return 'Request failed. Try again.';
}
