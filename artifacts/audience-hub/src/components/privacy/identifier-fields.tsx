import type { PrivacyIdentifier } from '@/lib/privacy';

export function IdentifierFields({ prefix, value, onChange, error }: { prefix: string; value: PrivacyIdentifier; onChange: (v: PrivacyIdentifier) => void; error: string | null }) {
  const errId = `${prefix}-identifier-error`;
  return <div className="flex flex-wrap items-start gap-3">
    <label className="flex flex-col gap-1.5 text-xs text-ink-muted">Type
      <select data-testid={`select-${prefix}-type`} value={value.type} onChange={e => onChange({ type: e.target.value as PrivacyIdentifier['type'], value: '' })} className="rounded-sm border border-line-strong bg-surface-raised px-3 py-2 text-sm text-ink">
        <option value="email">Email</option><option value="phone">Phone</option>
      </select>
    </label>
    <label className="flex flex-1 min-w-[240px] flex-col gap-1.5 text-xs text-ink-muted">{value.type === 'email' ? 'Email address' : 'Phone number'}
      <input data-testid={`input-${prefix}-value`} autoComplete="off" spellCheck={false} type={value.type === 'email' ? 'email' : 'tel'} value={value.value} aria-invalid={!!error} aria-describedby={error ? errId : undefined} onChange={e => onChange({ ...value, value: e.target.value })} className="rounded-sm border border-line-strong bg-surface-raised px-3 py-2 text-sm text-ink font-mono" />
      {error && <span id={errId} role="alert" className="text-danger">{error}</span>}
    </label>
  </div>;
}
