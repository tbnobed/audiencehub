import { UserCog } from 'lucide-react';
import { PasswordSection, TotpSection } from '@/components/account/security-panel';

export default function Account() {
  return <div className="max-w-[760px] mx-auto space-y-5 pb-8">
    <header><div className="flex items-center gap-2 text-[10px] uppercase tracking-[.16em] font-mono text-signal mb-1"><UserCog size={13} /> Account</div>
      <h1 className="kin-title text-ink">Security</h1><p className="text-xs text-ink-muted mt-1">Manage your password and two-factor authentication.</p></header>
    <PasswordSection /><TotpSection />
  </div>;
}
