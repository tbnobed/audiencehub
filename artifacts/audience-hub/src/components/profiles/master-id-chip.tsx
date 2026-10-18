import { useState } from 'react';
import { Check, Copy } from 'lucide-react';

/** Public Master ID chip. Renders nothing until the API supplies master_id. */
export function MasterIdChip({ masterId, testId = 'button-copy-master-id' }: { masterId?: string | null; testId?: string }) {
  const [copied, setCopied] = useState(false);
  if (!masterId) return null;
  return (
    <button type="button" data-testid={testId} title="Copy Master ID"
      className="font-mono text-[11px] text-signal border border-signal/30 bg-signal-soft rounded px-2 py-0.5 inline-flex items-center gap-1 hover:border-signal"
      onClick={async () => { try { await navigator.clipboard.writeText(masterId); setCopied(true); setTimeout(() => setCopied(false), 2000); } catch { /* clipboard unavailable */ } }}>
      {masterId} {copied ? <Check size={12} /> : <Copy size={12} />}
    </button>
  );
}
