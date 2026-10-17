export type TimelineEvent = Record<string, unknown> & { occurred_at?: unknown; type?: unknown };
export type TimelineDay = { day: string; events: TimelineEvent[] };

const DAY = /^(\d{4}-\d{2}-\d{2})/;
export const UNDATED = 'undated';

/** Groups events by calendar day (from the ISO prefix, no TZ shifting), newest day and newest event first. */
export function groupEventsByDay(events: TimelineEvent[], type?: string): TimelineDay[] {
  const filtered = type && type !== 'all' ? events.filter(e => String(e.type ?? '') === type) : events;
  const stamp = (e: TimelineEvent) => (typeof e.occurred_at === 'string' ? e.occurred_at : '');
  const sorted = [...filtered].sort((a, b) => stamp(b).localeCompare(stamp(a)));
  const groups = new Map<string, TimelineEvent[]>();
  for (const event of sorted) {
    const key = DAY.exec(stamp(event))?.[1] ?? UNDATED;
    groups.set(key, [...(groups.get(key) ?? []), event]);
  }
  const days = [...groups.entries()].map(([day, list]) => ({ day, events: list }));
  return [...days.filter(d => d.day !== UNDATED), ...days.filter(d => d.day === UNDATED)];
}

export function eventTypes(events: TimelineEvent[]): string[] {
  return [...new Set(events.map(e => (typeof e.type === 'string' ? e.type : '')).filter(Boolean))].sort();
}
