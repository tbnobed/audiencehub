import { useMemo, useState } from 'react';
import { Activity } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { displayDate, displayValue } from '@/lib/profile-display';
import { eventTypes, groupEventsByDay, UNDATED, type TimelineEvent } from '@/lib/activity-timeline';

export function ActivityTimeline({ events }: { events: TimelineEvent[] }) {
  const [type, setType] = useState('all');
  const types = useMemo(() => eventTypes(events), [events]);
  const days = useMemo(() => groupEventsByDay(events, type), [events, type]);

  if (!events.length) {
    return <div className="border border-line rounded-md bg-surface py-12 text-center text-ink-muted text-sm">
      <Activity size={20} className="mx-auto mb-2" />No recent activity recorded.
    </div>;
  }

  return <div className="border border-line rounded-md bg-surface" data-testid="activity-timeline">
    <div className="flex flex-wrap items-center gap-1.5 border-b border-line px-3 py-2" role="group" aria-label="Filter by event type">
      {['all', ...types].map(t => <button key={t} type="button" data-testid={`filter-event-type-${t}`} aria-pressed={type === t} onClick={() => setType(t)}
        className={`px-2 py-0.5 rounded-sm border font-mono text-[10px] uppercase tracking-wide transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-signal ${type === t ? 'bg-signal-soft text-signal border-signal/40' : 'border-line text-ink-muted hover:text-ink'}`}>
        {t === 'all' ? `All (${events.length})` : t.replaceAll('_', ' ')}
      </button>)}
    </div>
    <ol className="divide-y divide-line">
      {days.map(({ day, events: list }) => <li key={day} data-testid={`timeline-day-${day}`}>
        <h3 className="sticky top-0 z-10 bg-surface-raised px-4 py-1.5 text-[11px] font-mono uppercase tracking-wider text-ink-muted flex justify-between">
          <span>{day === UNDATED ? 'Undated' : displayDate(day, 'EEEE, MMM d, yyyy')}</span><span>{list.length} event{list.length === 1 ? '' : 's'}</span>
        </h3>
        <ol className="relative ml-6 border-l border-line">
          {list.map((event, i) => {
            const props = event.properties && typeof event.properties === 'object' ? event.properties as Record<string, unknown> : null;
            return <li key={String(event.id ?? i)} className="relative pl-4 pr-4 py-2.5 text-xs">
              <span aria-hidden className="absolute -left-[4.5px] top-[15px] h-2 w-2 rounded-full bg-signal ring-2 ring-surface" />
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-ink-muted tabular-nums w-16">{displayDate(event.occurred_at, 'HH:mm:ss')}</span>
                <Badge variant="outline" className="font-mono text-[10px] uppercase">{displayValue(event.type)}</Badge>
                <span className="font-medium text-ink">{displayValue(event.name)}</span>
                <Badge variant="secondary" className="font-mono text-[10px] ml-auto">{displayValue(event.source_key)}</Badge>
              </div>
              {props && Object.keys(props).length > 0 && <details className="mt-1 ml-[72px] text-[10px]"><summary className="cursor-pointer text-primary w-fit">Properties</summary><pre className="mt-1 p-2 bg-ground border border-line rounded max-w-full overflow-auto">{JSON.stringify(props, null, 2)}</pre></details>}
            </li>;
          })}
        </ol>
      </li>)}
    </ol>
  </div>;
}
