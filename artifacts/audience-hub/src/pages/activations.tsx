import { useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { Plus, Search, RotateCcw, ChevronRight, Send } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { useAuth } from '@/hooks/use-auth';
import { useActivations } from '@/hooks/use-activations';
import { DestinationsPanel } from '@/components/activations/destinations';
import { ActivationWizard } from '@/components/activations/wizard';
import { ActivationDetail } from '@/components/activations/detail';
import { RunStatus, SkeletonRows, TypeBadge, when } from '@/components/activations/shared';
import { can, describeSchedule, fmtInt } from '@/components/activations/logic';

export default function Activations() {
  const { user } = useAuth();
  const role = user?.role;
  const params = useParams();
  const { pathname } = useLocation();
  if (!can(role).viewActivations) return <p className="p-8 text-center text-ink-muted">Activations are available to analysts and admins.</p>;
  const id = params.id ? Number(params.id) : undefined;
  if (pathname.endsWith('/new')) return <ActivationWizard role={role!} />;
  if (id && pathname.endsWith('/edit')) return <ActivationWizard id={id} role={role!} />;
  if (id) return <ActivationDetail id={id} />;
  return <ActivationList isAdmin={role === 'admin'} />;
}

function ActivationList({ isAdmin }: { isAdmin: boolean }) {
  const nav = useNavigate();
  const q = useActivations();
  const [tab, setTab] = useState('activations');
  const [s, setS] = useState('');
  const items = (q.data?.items ?? []).filter(a => !s || `${a.name} ${a.segment_name} ${a.destination_name}`.toLowerCase().includes(s.toLowerCase()));
  return <div className="space-y-3">
    <div className="flex items-end justify-between">
      <div><h1 className="kin-title">Activations</h1><p className="text-[12px] text-ink-muted">Send active segments to partner destinations. Consent and suppressions apply on every run.</p></div>
      <Button size="sm" className="h-7" onClick={() => nav('/activations/new')} data-testid="button-new-activation"><Plus size={13} /> New activation</Button>
    </div>
    <Tabs value={tab} onValueChange={setTab}>
      <TabsList className="h-7"><TabsTrigger value="activations" className="text-xs h-6" data-testid="tab-activations">Activations <span className="ml-1 font-mono text-[10px] text-ink-muted">{q.data?.items.length ?? ''}</span></TabsTrigger><TabsTrigger value="destinations" className="text-xs h-6" data-testid="tab-destinations">Destinations</TabsTrigger></TabsList>
      <TabsContent value="activations" className="mt-2 space-y-2">
        <div className="relative w-72"><Search size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-ink-muted" /><Input className="h-7 pl-7 text-[12px]" placeholder="Filter by name, segment, destination" value={s} onChange={e => setS(e.target.value)} data-testid="input-filter-activations" /></div>
        <div className="border border-line rounded-sm bg-surface overflow-x-auto">
          <table className="w-full text-[12px]">
            <thead className="sticky top-0 bg-surface-raised text-ink-muted text-[10px] uppercase tracking-wider"><tr className="h-8 text-left">
              <th className="px-3">Activation</th><th className="px-2">Segment → Destination</th><th className="px-2">Schedule</th><th className="px-2">Last run</th><th className="px-2 text-right">Sent</th><th className="px-2">Next run</th><th className="px-2 w-6" />
            </tr></thead>
            <tbody>
              {q.isLoading ? <SkeletonRows cols={7} />
                : q.isError ? <tr><td colSpan={7} className="p-6 text-center"><p className="text-danger mb-2">Couldn't load activations: {(q.error as Error).message}</p><Button size="sm" variant="outline" onClick={() => q.refetch()} data-testid="button-retry-activations"><RotateCcw size={12} /> Retry</Button></td></tr>
                : !items.length ? <tr><td colSpan={7} className="p-10 text-center text-ink-muted"><Send size={18} className="mx-auto mb-2 text-signal" />{s ? `No activations match "${s}".` : <>No activations yet. Create one to send an active segment to a destination.{!isAdmin && ' You need at least one destination from an admin.'}</>}</td></tr>
                : items.map(a => <tr key={a.id} className="h-8 border-t border-line hover:bg-surface-raised/60 cursor-pointer" onClick={() => nav(`/activations/${a.id}`)} data-testid={`row-activation-${a.id}`}>
                  <td className="px-3"><Link to={`/activations/${a.id}`} onClick={e => e.stopPropagation()} className="text-ink hover:text-signal" data-testid={`link-activation-${a.id}`}>{a.name}</Link>{!a.is_active && <span className="ml-2 text-[10px] text-warn uppercase">paused</span>}</td>
                  <td className="px-2"><span className="inline-flex items-center gap-1.5 max-w-[340px]"><span className="truncate">{a.segment_name}</span><ChevronRight size={11} className="text-ink-muted shrink-0" /><span className="truncate">{a.destination_name}</span><TypeBadge type={a.destination_type} /></span></td>
                  <td className="px-2 text-ink-muted truncate max-w-[220px]">{a.schedule ? describeSchedule(a.schedule, a.timezone) : 'Manual'}</td>
                  <td className="px-2"><span className="inline-flex items-center gap-2"><RunStatus status={a.last_run?.status} testId={`status-activation-${a.id}`} /><span className="font-mono text-[11px] text-ink-muted">{when(a.last_run?.finished_at ?? a.last_run?.created_at)}</span></span></td>
                  <td className="px-2 text-right font-mono tabular-nums">{fmtInt(a.last_run?.profiles_sent)}</td>
                  <td className="px-2 font-mono text-[11px] text-ink-muted">{a.is_active ? (a.next_run_at ? when(a.next_run_at) : '—') : 'paused'}</td>
                  <td className="px-2"><ChevronRight size={12} className="text-ink-muted" /></td>
                </tr>)}
            </tbody>
          </table>
        </div>
      </TabsContent>
      <TabsContent value="destinations" className="mt-2"><DestinationsPanel isAdmin={isAdmin} /></TabsContent>
    </Tabs>
  </div>;
}
