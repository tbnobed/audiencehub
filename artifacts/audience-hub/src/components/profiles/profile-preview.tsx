import { Link } from 'react-router-dom';
import { ArrowUpRight, Mail, MapPin, Phone, RefreshCw, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { MasterIdChip } from '@/components/profiles/master-id-chip';
import { useProfile } from '@/hooks/use-profiles';
import { displayAmount, displayDate, displayJoined, displayNumber, displayValue, record, records } from '@/lib/profile-display';

/**
 * Read-only preview of a profile. Renders only what the API returned — contact
 * fields arrive already masked for viewer roles; identifiers are never shown here.
 */
export function ProfilePreview({ id, from, onClose }: { id: number; from: string; onClose: () => void }) {
  const { data, isLoading, isError, error, refetch } = useProfile(id);
  const traits = record(data?.traits);
  const gifts = records(data?.gifts).slice(0, 4);

  return <div className="flex flex-col h-full min-h-0" data-testid="panel-profile-preview" aria-live="polite">
    <div className="flex shrink-0 items-start justify-between gap-2 border-b border-line px-4 py-3">
      <div className="min-w-0">
        <p className="text-[10px] font-mono uppercase tracking-[.16em] text-signal">Profile preview</p>
        {data && <MasterIdChip masterId={data.master_id} testId="button-copy-preview-master-id" />}
        {isLoading ? <Skeleton className="h-5 w-40 mt-1 bg-surface-raised" />
          : <h2 className="kin-heading truncate" data-testid="text-preview-name">{data ? displayJoined([data.first_name, data.last_name], ' ') : 'Profile'}</h2>}
      </div>
      <Button variant="ghost" size="icon" className="h-7 w-7 shrink-0" aria-label="Close preview" data-testid="button-close-preview" onClick={onClose}><X size={14} /></Button>
    </div>
    <div className="shrink-0 border-b border-line p-3">
      <Button asChild size="sm" className="w-full"><Link to={`/profiles/${id}`} state={{ from }} data-testid="link-open-full-profile">Open full profile <ArrowUpRight size={13} className="ml-1.5" /></Link></Button>
    </div>
    <div className="min-h-0 flex-1 overflow-auto px-4 py-3 space-y-4 text-xs">
      {isLoading ? <div className="space-y-3">{Array.from({ length: 6 }, (_, i) => <Skeleton key={i} className="h-8 w-full bg-surface-raised" />)}</div>
      : isError || !data ? <div className="py-10 text-center" role="alert">
          <p className="text-danger text-sm">Unable to load preview</p>
          <p className="text-ink-muted mt-1">{error instanceof Error ? error.message : 'Profile may have been merged or removed.'}</p>
          <Button variant="outline" size="sm" className="mt-3" data-testid="button-retry-preview" onClick={() => refetch()}><RefreshCw size={13} className="mr-2" />Retry</Button>
        </div>
      : <>
        <div className="space-y-1.5 text-ink-muted">
          <div className="flex items-center gap-2"><Mail size={12} /><span data-testid="text-preview-email" className="truncate">{displayValue(data.email)}</span></div>
          <div className="flex items-center gap-2"><Phone size={12} /><span data-testid="text-preview-phone">{displayValue(data.phone)}</span></div>
          <div className="flex items-center gap-2"><MapPin size={12} /><span>{displayJoined([data.city, data.region, data.country])}</span></div>
        </div>
        <dl className="grid grid-cols-2 gap-px bg-line border border-line rounded-sm overflow-hidden">
          {([['Status', displayValue(data.donor_status)], ['RFM', displayValue(traits.rfm_score)], ['Lifetime', displayAmount(traits.ltv_total)], ['Gifts', displayNumber(traits.gift_count_total)], ['Last gift', displayDate(traits.last_gift_date)], ['Last seen', displayDate(data.last_seen_at)]] as const).map(([k, v]) =>
            <div key={k} className="bg-surface px-2.5 py-2"><dt className="text-[10px] uppercase tracking-wide text-ink-muted">{k}</dt><dd className="font-mono text-ink mt-0.5 capitalize">{v}</dd></div>)}
        </dl>
        <section>
          <h3 className="text-[10px] uppercase tracking-wider text-ink-muted mb-1.5">Recent gifts</h3>
          {gifts.length ? <ul className="divide-y divide-line border border-line rounded-sm">{gifts.map((g, i) => <li key={String(g.id ?? i)} className="flex justify-between px-2.5 py-1.5"><span className="text-ink-muted">{displayDate(g.gift_date)}</span><span className="font-mono text-ok">{displayAmount(g.amount, typeof g.currency === 'string' ? g.currency : 'USD')}</span></li>)}</ul>
            : <p className="text-ink-muted">No gifts recorded.</p>}
        </section>
        <section>
          <h3 className="text-[10px] uppercase tracking-wider text-ink-muted mb-1.5">Activity</h3>
          <p className="text-ink-muted">{records(data.events).length} events · {records(data.source_records).length} source records</p>
        </section>
      </>}
    </div>
  </div>;
}
