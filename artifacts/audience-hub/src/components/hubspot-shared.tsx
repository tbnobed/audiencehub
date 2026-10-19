import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { AlertTriangle, CheckCircle2 } from 'lucide-react';
import type { HsSchedule, HsTest } from '@/hooks/use-hubspot';

const sel = 'h-8 w-full rounded-md border border-input bg-background px-2 text-xs';

export function ScheduleEditor({ value, onChange, testId }: { value: HsSchedule; onChange: (s: HsSchedule) => void; testId: string }) {
  const set = (p: Partial<HsSchedule>) => onChange({ ...value, ...p, timezone: 'America/New_York' });
  return <div className="flex flex-wrap items-end gap-2">
    <div className="space-y-1"><Label className="text-[11px]">Schedule</Label>
      <select className={sel} value={value.frequency} data-testid={`select-${testId}-frequency`} onChange={e => set({ frequency: e.target.value as HsSchedule['frequency'] })}>
        {['manual', 'hourly', 'daily', 'monthly'].map(f => <option key={f} value={f}>{f}</option>)}</select></div>
    {value.frequency === 'monthly' && <div className="space-y-1 w-16"><Label className="text-[11px]">Day</Label><Input type="number" min={1} max={28} className="h-8 text-xs" value={value.day ?? 1} data-testid={`input-${testId}-day`} onChange={e => set({ day: Number(e.target.value) })} /></div>}
    {(value.frequency === 'daily' || value.frequency === 'monthly') && <div className="space-y-1 w-16"><Label className="text-[11px]">Hour</Label><Input type="number" min={0} max={23} className="h-8 text-xs" value={value.hour ?? 0} data-testid={`input-${testId}-hour`} onChange={e => set({ hour: Number(e.target.value) })} /></div>}
    {value.frequency !== 'manual' && <div className="space-y-1 w-16"><Label className="text-[11px]">Min</Label><Input type="number" min={0} max={59} className="h-8 text-xs" value={value.minute ?? 0} data-testid={`input-${testId}-minute`} onChange={e => set({ minute: Number(e.target.value) })} /></div>}
    <span className="pb-2 text-[10px] font-mono text-muted-foreground">America/New_York</span>
  </div>;
}

export function TestResult({ r, testId }: { r: HsTest; testId: string }) {
  const missing = Object.entries(r.missing_scopes ?? {}).filter(([, v]) => v.length);
  return <div className="space-y-2 rounded-md border p-3 text-xs" data-testid={testId}>
    <div className="flex items-center gap-2"><CheckCircle2 className="h-4 w-4 text-ok" /> Connected to hub <span className="font-mono">{String(r.account?.hub_id)}</span>{r.account?.app_id !== undefined && <span className="text-muted-foreground">app {String(r.account.app_id)}</span>}</div>
    {missing.length === 0 ? <p className="text-ok" data-testid={`${testId}-scopes-ok`}>All required scopes are granted.</p> : <div role="alert" className="space-y-1 rounded border border-warn/40 bg-warn/10 p-2" data-testid={`${testId}-missing`}>
      <p className="flex items-center gap-1.5 font-medium"><AlertTriangle className="h-3.5 w-3.5 text-warn" /> Missing scopes</p>
      {missing.map(([feature, scopes]) => <p key={feature}><span className="font-medium">{feature}:</span> <span className="font-mono">{scopes.join(', ')}</span></p>)}
    </div>}
    <details><summary className="cursor-pointer text-muted-foreground">Granted scopes ({r.scopes?.length ?? 0})</summary><p className="mt-1 break-words font-mono text-[11px]">{(r.scopes ?? []).join(', ') || 'none'}</p></details>
  </div>;
}
