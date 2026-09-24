import { useEffect, useState } from 'react';
import { RefreshCw, Settings2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useAuth } from '@/hooks/use-auth';
import { useAdminSettings, useUpdateAdminSettings } from '@/hooks/use-dashboards';

const months = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
];

export default function Admin() {
  const { user } = useAuth();
  const settings = useAdminSettings();
  const updateSettings = useUpdateAdminSettings();
  const [month, setMonth] = useState('');

  useEffect(() => {
    if (settings.data) setMonth(String(settings.data.fiscal_year_start_month));
  }, [settings.data]);

  if (user?.role !== 'admin') {
    return <div role="alert" className="border border-danger/30 bg-danger/5 rounded-md p-6 text-sm text-ink">Administrator access is required to manage settings.</div>;
  }

  const save = () => {
    if (month) updateSettings.mutate(Number(month));
  };

  return <div className="max-w-[900px] mx-auto space-y-5 pb-8 animate-in fade-in duration-300">
    <header>
      <div className="flex items-center gap-2 text-[10px] uppercase tracking-[.16em] font-mono text-signal mb-1"><Settings2 size={13} /> Organization controls</div>
      <h1 className="kin-title text-ink">Admin Settings</h1>
      <p className="text-xs text-ink-muted mt-1">Configure organization-wide dashboard behavior.</p>
    </header>

    {settings.isLoading ? <div className="rounded-md border border-line bg-surface p-6 text-xs text-ink-muted">Loading settings…</div>
      : settings.isError ? <div role="alert" className="border border-danger/30 bg-danger/5 rounded-md p-5 text-sm text-ink"><p>Settings could not be loaded.</p><p className="text-xs text-ink-muted mt-1">{settings.error instanceof Error ? settings.error.message : 'The request could not be completed.'}</p><Button variant="outline" size="sm" className="mt-3" onClick={() => settings.refetch()}><RefreshCw size={13} className="mr-2" /> Retry</Button></div>
      : settings.data && <section className="rounded-md border border-line bg-surface">
        <div className="border-b border-line px-5 py-4"><h2 className="kin-heading text-ink">Fiscal year</h2><p className="mt-1 text-xs text-ink-muted">Choose the first month of your fiscal year. The Last FY dashboard preset uses this setting.</p></div>
        <div className="flex flex-col sm:flex-row sm:items-end gap-3 p-5">
          <label className="flex flex-col gap-1.5 text-xs text-ink-muted" htmlFor="fiscal-year-start-month">Fiscal year starts in
            <select id="fiscal-year-start-month" data-testid="fiscal-year-start-month" value={month} onChange={event => setMonth(event.target.value)} className="min-w-48 rounded-sm border border-line-strong bg-surface-raised px-3 py-2 text-sm text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-signal focus-visible:ring-offset-2 focus-visible:ring-offset-ground">
              {months.map((name, index) => <option key={name} value={index + 1}>{name}</option>)}
            </select>
          </label>
          <Button type="button" data-testid="save-settings" disabled={!month || Number(month) === settings.data.fiscal_year_start_month || updateSettings.isPending} onClick={save} className="bg-signal text-on-signal hover:bg-signal/90">{updateSettings.isPending ? 'Saving…' : 'Save settings'}</Button>
          {updateSettings.isSuccess && <span role="status" className="text-xs text-ok">Settings saved.</span>}
        </div>
        {updateSettings.isError && <p role="alert" className="mx-5 mb-5 text-xs text-danger">Settings were not saved: {updateSettings.error instanceof Error ? updateSettings.error.message : 'The request could not be completed.'}</p>}
      </section>}
  </div>;
}