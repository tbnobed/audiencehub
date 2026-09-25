import { useEffect, useState } from 'react';
import { ArrowRight, Search, Users, RefreshCw } from 'lucide-react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Skeleton } from '@/components/ui/skeleton';
import { useProfiles } from '@/hooks/use-profiles';
import { useSources } from '@/hooks/use-sources';

const statusStyle: Record<string, string> = {
  prospect: 'text-ink-muted bg-surface-raised border-line',
  new: 'text-signal bg-signal-soft border-signal/25',
  active: 'text-ok bg-ok/10 border-ok/25',
  reactivated: 'text-ok bg-ok/10 border-ok/25',
  lapsing: 'text-warn bg-warn/10 border-warn/25',
  lapsed: 'text-danger bg-danger/10 border-danger/25',
};
const date = (value: unknown) => value ? new Date(String(value)).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }) : '—';
const currency = (value: unknown) => value == null ? '—' : new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(Number(value));

export default function Profiles() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = useState(params.get('search') || '');
  const [debounced, setDebounced] = useState(search);
  const urlSearch = params.get('search') || '';
  useEffect(() => { setSearch(urlSearch); setDebounced(urlSearch); }, [urlSearch]);
  useEffect(() => { const timer = setTimeout(() => setDebounced(search), 350); return () => clearTimeout(timer); }, [search]);
  useEffect(() => {
    if (debounced === (params.get('search') || '')) return;
    const next = new URLSearchParams(params);
    if (debounced) next.set('search', debounced); else next.delete('search');
    next.set('page', '1');
    setParams(next, { replace: true });
  }, [debounced, params, setParams]);
  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value === 'all' || !value) next.delete(key); else next.set(key, value);
    if (key !== 'page') next.set('page', '1');
    setParams(next);
  };
  const page = Math.max(1, Number(params.get('page')) || 1);
  const pageSize = 50;
  const { data, isLoading, isError, error, refetch } = useProfiles({
    search: params.get('search') || undefined,
    source_id: params.get('source_id') || undefined,
    has_email: params.get('has_email') || undefined,
    donor_status: params.get('donor_status') || undefined,
    page, page_size: pageSize,
  });
  const { data: sourceData } = useSources();

  return <div className="max-w-[1680px] mx-auto space-y-4 pb-8 animate-in fade-in duration-300">
    <header className="flex justify-between items-end">
      <div><p className="text-[10px] font-mono uppercase tracking-[.16em] text-signal mb-1">Audience / Identity</p><h1 className="kin-title">Profiles</h1><p className="text-xs text-ink-muted mt-1">Unified people and their giving history.</p></div>
      <div data-testid="text-profile-count" className="text-xs text-ink-muted font-mono">{data ? `${data.total.toLocaleString()} profiles` : '— profiles'}</div>
    </header>
    <div className="bg-surface border border-line rounded-md p-3 flex flex-wrap gap-2 items-center">
      <div className="relative flex-1 min-w-[240px]"><Search className="absolute left-2.5 top-2.5 h-4 w-4 text-ink-muted" /><Input data-testid="input-profile-search" type="search" value={search} onChange={e => setSearch(e.target.value)} placeholder="Search name, email, or phone" className="pl-9 h-9 bg-ground border-line-strong text-xs" /></div>
      <Select value={params.get('donor_status') || 'all'} onValueChange={v => update('donor_status', v)}><SelectTrigger data-testid="select-donor-status" className="w-[155px] h-9 text-xs"><SelectValue placeholder="Donor status" /></SelectTrigger><SelectContent><SelectItem value="all">All statuses</SelectItem>{['prospect', 'new', 'active', 'reactivated', 'lapsing', 'lapsed'].map(s => <SelectItem key={s} value={s} className="capitalize">{s}</SelectItem>)}</SelectContent></Select>
      <Select value={params.get('source_id') || 'all'} onValueChange={v => update('source_id', v)}><SelectTrigger data-testid="select-profile-source" className="w-[145px] h-9 text-xs"><SelectValue placeholder="Source" /></SelectTrigger><SelectContent><SelectItem value="all">All sources</SelectItem>{(sourceData?.items || []).map(s => <SelectItem key={s.id} value={String(s.id)}>{s.name}</SelectItem>)}</SelectContent></Select>
      <Select value={params.get('has_email') || 'all'} onValueChange={v => update('has_email', v)}><SelectTrigger data-testid="select-has-email" className="w-[140px] h-9 text-xs"><SelectValue placeholder="Email" /></SelectTrigger><SelectContent><SelectItem value="all">Any email</SelectItem><SelectItem value="true">Has email</SelectItem><SelectItem value="false">No email</SelectItem></SelectContent></Select>
      {(params.get('search') || params.get('source_id') || params.get('donor_status') || params.get('has_email')) && <Button variant="ghost" size="sm" onClick={() => { setSearch(''); setParams({}); }}>Clear filters</Button>}
    </div>
    <div className="border border-line rounded-md bg-surface overflow-hidden">
      <div className="overflow-auto"><Table><TableHeader className="bg-surface-raised"><TableRow className="border-line hover:bg-transparent"><TableHead className="text-[11px]">Profile</TableHead><TableHead className="text-[11px]">Contact</TableHead><TableHead className="text-[11px]">Location</TableHead><TableHead className="text-[11px]">Donor status</TableHead><TableHead className="text-[11px] text-right">Lifetime giving</TableHead><TableHead className="text-[11px] text-right">Last gift</TableHead><TableHead className="text-[11px]">Sources</TableHead><TableHead className="w-8" /></TableRow></TableHeader><TableBody>
        {isLoading ? Array.from({ length: 9 }, (_, i) => <TableRow key={i} className="border-line">{Array.from({ length: 8 }, (_, j) => <TableCell key={j}><Skeleton className="h-4 w-full bg-surface-raised" /></TableCell>)}</TableRow>)
        : isError ? <TableRow><TableCell colSpan={8} className="h-44 text-center"><p className="text-danger text-sm">Unable to load profiles</p><p className="text-ink-muted text-xs mt-1">{error instanceof Error ? error.message : 'Please try again.'}</p><Button variant="outline" size="sm" className="mt-3" onClick={() => refetch()}><RefreshCw size={13} className="mr-2" /> Retry</Button></TableCell></TableRow>
        : !data?.items.length ? <TableRow><TableCell colSpan={8} className="h-44 text-center"><Users size={22} className="mx-auto text-ink-muted mb-2" /><p className="text-sm">No matching profiles</p><p className="text-xs text-ink-muted mt-1">Adjust the filters or import audience records.</p></TableCell></TableRow>
        : data.items.map(p => {
          const traits = (p as typeof p & { traits?: Record<string, unknown> }).traits;
          const name = [p.first_name, p.last_name].filter(Boolean).join(' ') || 'Unnamed profile';
          const sourceKeys = Array.isArray(traits?.source_keys) ? traits.source_keys as string[] : [];
          return <TableRow key={p.id} data-testid={`row-profile-${p.id}`} className="cursor-pointer border-line hover:bg-surface-raised text-xs" onClick={() => navigate(`/profiles/${p.id}`, { state: { from: `/profiles${params.toString() ? `?${params}` : ''}` } })}>
            <TableCell className="font-medium text-ink">{name}<span className="block text-[10px] font-mono text-ink-muted mt-0.5">#{p.id}</span></TableCell>
            <TableCell><span className="block text-ink-muted">{p.email || '—'}</span><span className="block text-ink-muted mt-0.5">{p.phone || ''}</span></TableCell>
            <TableCell className="text-ink-muted">{[p.city, p.region].filter(Boolean).join(', ') || '—'}</TableCell>
            <TableCell><span className={`inline-block px-2 py-0.5 rounded-sm border uppercase tracking-wide font-mono text-[10px] ${statusStyle[p.donor_status] || statusStyle.prospect}`}>{p.donor_status}</span></TableCell>
            <TableCell className="text-right font-mono tabular-nums">{currency(traits?.ltv_total)}</TableCell>
            <TableCell className="text-right text-ink-muted font-mono">{date(traits?.last_gift_date)}</TableCell>
            <TableCell><div className="flex flex-wrap gap-1">{sourceKeys.length ? sourceKeys.slice(0, 3).map(s => <span key={s} className="text-[10px] font-mono px-1.5 py-0.5 border border-line rounded-sm text-ink-muted">{s}</span>) : <span className="text-ink-muted">—</span>}{sourceKeys.length > 3 && <span className="text-ink-muted">+{sourceKeys.length - 3}</span>}</div></TableCell><TableCell><ArrowRight size={13} className="text-ink-muted" /></TableCell>
          </TableRow>;
        })}
      </TableBody></Table></div>
      {data && data.total > 0 && <footer className="border-t border-line px-4 py-2 flex justify-between items-center text-[11px] text-ink-muted font-mono"><span>Showing {(page - 1) * pageSize + 1}–{Math.min(page * pageSize, data.total)} of {data.total.toLocaleString()}</span><div className="flex gap-2"><Button data-testid="button-previous-profiles" size="sm" variant="outline" disabled={page <= 1} onClick={() => update('page', String(page - 1))}>Previous</Button><Button data-testid="button-next-profiles" size="sm" variant="outline" disabled={page * pageSize >= data.total} onClick={() => update('page', String(page + 1))}>Next</Button></div></footer>}
    </div>
  </div>;
}