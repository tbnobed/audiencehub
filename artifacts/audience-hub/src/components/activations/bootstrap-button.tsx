import { useState } from 'react';
import { Layers } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { toast } from '@/hooks/use-toast';
import { useBootstrapActivations } from '@/hooks/use-activations';
import { ConfirmDialog, apiToast } from './shared';

/** Admin-only: creates the reference deliveries (all disabled) without touching existing configuration. */
export const REFERENCE_PRESETS = [
  { name: 'CustomerPulse – All TBN Reference', schedule: 'Monthly, day 2' },
  { name: 'CustomerPulse – RFM Donor Reference', schedule: 'Monthly, day 1' },
  { name: 'Trilogy – RFM Donor Reference', schedule: 'Monthly, day 29' },
  { name: 'Identity Crosswalk – Daily', schedule: 'Daily' },
];

export function BootstrapReferencesButton() {
  const boot = useBootstrapActivations();
  const [open, setOpen] = useState(false);
  return <>
    <Button size="sm" variant="outline" className="h-7" onClick={() => setOpen(true)} disabled={boot.isPending} data-testid="button-bootstrap-references"><Layers size={13} /> Reference presets</Button>
    <ConfirmDialog open={open} onOpenChange={setOpen} testId="bootstrap-references" title="Set up reference deliveries?" confirm={boot.isPending ? 'Creating…' : 'Create disabled presets'}
      onConfirm={() => boot.mutate(undefined, {
        onSuccess: r => {
          setOpen(false);
          toast({ title: r.created.length ? `Created ${r.created.length} disabled activation${r.created.length === 1 ? '' : 's'}` : 'All reference presets already exist',
            description: r.unavailable.length ? `Missing audiences, skipped: ${r.unavailable.join(', ')}. Create them, then run setup again.` : 'Add SFTP credentials and an approved purpose for the crosswalk, then enable each one.' });
        },
        onError: e => { setOpen(false); apiToast('Preset setup failed', e); },
      })}
      body={`Adds ${REFERENCE_PRESETS.map(p => `${p.name} (${p.schedule})`).join('; ')}, plus their SFTP destinations. Everything is created disabled with no credentials; activations that already exist by name are left unchanged.`} />
  </>;
}
