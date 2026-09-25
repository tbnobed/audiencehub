import { useEffect, useState } from 'react';
import { RefreshCw, Settings2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useAuth } from '@/hooks/use-auth';
import { useAdminSettings, useUpdateAdminSettings, type DashboardSettingsPatch } from '@/hooks/use-dashboards';
import { validateCustomRange, type DashboardDefaultRange } from '@/lib/date-range';

const months = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
];

export default function Admin() {
  const { user } = useAuth();
  const settings = useAdminSettings();
  const updateSettings = useUpdateAdminSettings();
  const [month, setMonth] = useState('');
  const [preset, setPreset] = useState<DashboardDefaultRange['dashboard_default_preset']>('90d');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');

  useEffect(() => {
    if (settings.data) {
      setMonth(String(settings.data.fiscal_year_start_month));
      setPreset(settings.data.dashboard_default_preset);
      setFrom(settings.data.dashboard_default_from ?? '');
      setTo(settings.data.dashboard_default_to ?? '');
    }
  }, [settings.data]);

  const customError = preset === 'custom' ? validateCustomRange(from, to) : null;
  const dirty = !!settings.data && (
    Number(month) !== settings.data.fiscal_year_start_month
    || preset !== settings.data.dashboard_default_preset
    || (preset === 'custom' && (from !== settings.data.dashboard_default_from || to !== settings.data.dashboard_default_to))
  );
  const edit = () => updateSettings.reset();

  if (user?.role !== 'admin') {
    return <div role="alert" className="border border-danger/30 bg-danger/5 rounded-md p-6 text-sm text-ink">Administrator access is required to manage settings.</div>;
  }

  const save = () => {
    if (!month || customError || !settings.data) return;
    const datesChanged = preset !== settings.data.dashboard_default_preset
      || (preset === 'custom' && (from !== settings.data.dashboard_default_from || to !== settings.data.dashboard_default_to));
    const patch: DashboardSettingsPatch = datesChanged ? {
      fiscal_year_start_month: Number(month),
      dashboard_default_preset: preset,
      dashboard_default_from: preset === 'custom' ? from : null,
      dashboard_default_to: preset === 'custom' ? to : null,
    } : { fiscal_year_start_month: Number(month) };
    updateSettings.mutate(patch);
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
        <div className="p-5">
          <label className="flex flex-col gap-1.5 text-xs text-ink-muted" htmlFor="fiscal-year-start-month">Fiscal year starts in
            <select id="fiscal-year-start-month" data-testid="fiscal-year-start-month" value={month} onChange={event => { edit(); setMonth(event.target.value); }} className="min-w-48 rounded-sm border border-line-strong bg-surface-raised px-3 py-2 text-sm text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-signal focus-visible:ring-offset-2 focus-visible:ring-offset-ground">
              {months.map((name, index) => <option key={name} value={index + 1}>{name}</option>)}
            </select>
          </label>
        </div>
        <div className="border-t border-line px-5 py-4"><h2 className="kin-heading text-ink">Default dashboard date range</h2><p className="mt-1 text-xs text-ink-muted">Used when a dashboard link does not specify a date range. Explicit date selections in links always take priority.</p></div>
        <div className="p-5 space-y-4">
          <fieldset className="space-y-2 text-sm text-ink">
            <legend className="text-xs text-ink-muted mb-2">Default range</legend>
            <label className="flex items-center gap-2"><input type="radio" name="dashboard-default-preset" checked={preset === '90d'} onChange={() => { edit(); setPreset('90d'); }} /> Last 90 days</label>
            <label className="flex items-center gap-2"><input type="radio" name="dashboard-default-preset" checked={preset === 'custom'} onChange={() => { edit(); setPreset('custom'); }} /> Custom date range</label>
          </fieldset>
          {preset === 'custom' && <div className="space-y-3">
            <div className="flex flex-wrap gap-3">
              <label className="flex flex-col gap-1.5 text-xs text-ink-muted">From
                <input data-testid="default-range-from" type="date" min="1900-01-01" max="2999-12-31" value={from} onChange={event => { edit(); setFrom(event.target.value); }} className="rounded-sm border border-line-strong bg-surface-raised px-3 py-2 text-sm text-ink" />
              </label>
              <label className="flex flex-col gap-1.5 text-xs text-ink-muted">To
                <input data-testid="default-range-to" type="date" min="1900-01-01" max="2999-12-31" value={to} onChange={event => { edit(); setTo(event.target.value); }} className="rounded-sm border border-line-strong bg-surface-raised px-3 py-2 text-sm text-ink" />
              </label>
            </div>
            <Button type="button" variant="outline" size="sm" onClick={() => { edit(); setFrom('2020-01-01'); setTo('2024-12-31'); }}>Use seeded dates (Jan 1, 2020 – Dec 31, 2024)</Button>
            {customError && <p role="alert" className="text-xs text-danger">{customError}</p>}
          </div>}
          <div className="flex items-center gap-3">
            <Button type="button" data-testid="save-settings" disabled={!month || !dirty || !!customError || updateSettings.isPending} onClick={save} className="bg-signal text-on-signal hover:bg-signal/90">{updateSettings.isPending ? 'Saving…' : 'Save settings'}</Button>
            {updateSettings.isSuccess && <span role="status" className="text-xs text-ok">Settings saved.</span>}
          </div>
          {updateSettings.isError && <p role="alert" className="text-xs text-danger">Settings were not saved: {updateSettings.error instanceof Error ? updateSettings.error.message : 'The request could not be completed.'}</p>}
        </div>
      </section>}
  </div>;
}