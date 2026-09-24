import { Activity } from 'lucide-react';
import { Button } from '@/components/ui/button';

export default function Activations() {
  return (
    <div className="h-full flex flex-col items-center justify-center space-y-4 animate-in fade-in duration-500">
      <div className="h-16 w-16 rounded-full bg-primary/10 flex items-center justify-center">
        <Activity className="h-8 w-8 text-primary" />
      </div>
      <div className="text-center">
        <h2 className="kin-heading">No Activations Running</h2>
        <p className="text-muted-foreground mt-1 max-w-sm">
          Sync your segments to external marketing and advertising destinations.
        </p>
      </div>
      <Button variant="outline" className="mt-4" disabled>Not available yet</Button>
    </div>
  );
}
