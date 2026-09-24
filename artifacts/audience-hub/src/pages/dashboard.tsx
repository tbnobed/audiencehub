export default function Dashboard() {
  return (
    <div className="space-y-6 animate-in fade-in duration-500">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Dashboard</h1>
        <p className="text-muted-foreground mt-1">Overview of your audience data.</p>
      </div>
      
      <div className="rounded-lg border bg-card p-12 text-center shadow-sm">
        <h3 className="text-lg font-medium">No audience data yet</h3>
        <p className="text-sm text-muted-foreground mt-2">Dashboard metrics are not available yet. This area only includes system operations and the foundation.</p>
      </div>
    </div>
  );
}
