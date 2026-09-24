import { PieChart } from 'lucide-react';
import { Button } from '@/components/ui/button';

export default function Segments() {
  return (
    <div className="h-full flex flex-col items-center justify-center space-y-4 animate-in fade-in duration-500">
      <div className="h-16 w-16 rounded-full bg-primary/10 flex items-center justify-center">
        <PieChart className="h-8 w-8 text-primary" />
      </div>
      <div className="text-center">
        <h2 className="text-xl font-semibold">No Segments Created</h2>
        <p className="text-muted-foreground mt-1 max-w-sm">
          Define rules to group your profiles into actionable audiences for targeting.
        </p>
      </div>
      <Button variant="outline" className="mt-4" disabled>Not available yet</Button>
    </div>
  );
}
