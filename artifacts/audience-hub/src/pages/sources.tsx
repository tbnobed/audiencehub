import { useState } from 'react';
import { Database, Plus, MoreVertical, Edit2, Trash2, ArrowUp, ArrowDown, KeySquare, Plug } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogDescription } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import { useSources, useCreateSource, useUpdateSource, useDeleteSource, Source } from '@/hooks/use-sources';
import { toast } from '@/hooks/use-toast';
import { useAuth } from '@/hooks/use-auth';
import { SourceEvents } from '@/components/source-events';
import { SourceConnection } from '@/components/source-connection';
import { HubspotSourceConnection } from '@/components/hubspot-source-connection';
import { MetaSourcePanel } from '@/components/meta-source-panel';
import { isPullKind } from '@/hooks/use-source-connection';

export default function Sources() {
  const { data, isLoading, error } = useSources();
  const { user } = useAuth();
  const isAdmin = user?.role === 'admin';
  const canReadEvents = isAdmin || user?.role === 'analyst';
  const [eventSource, setEventSource] = useState<Source | null>(null);
  const [connectionSource, setConnectionSource] = useState<Source | null>(null);
  const createSource = useCreateSource();
  const updateSource = useUpdateSource();
  const deleteSource = useDeleteSource();

  const [isFormOpen, setIsFormOpen] = useState(false);
  const [editingSource, setEditingSource] = useState<Source | null>(null);

  const [formData, setFormData] = useState({
    name: '',
    key: '',
    kind: 'csv',
    record_types: '',
    is_active: true,
    settings: '{}',
  });

  const sources = [...(data?.items || [])].sort((a, b) => a.priority - b.priority);

  const handleOpenForm = (source?: Source) => {
    if (source) {
      setEditingSource(source);
      setFormData({
        name: source.name,
        key: source.key,
        kind: source.kind,
        record_types: source.record_types.join(', '),
        is_active: source.is_active,
        settings: JSON.stringify(source.settings, null, 2),
      });
    } else {
      setEditingSource(null);
      setFormData({
        name: '',
        key: '',
        kind: 'csv',
        record_types: 'contact',
        is_active: true,
        settings: '{\n  \n}',
      });
    }
    setIsFormOpen(true);
  };

  const handleSave = async () => {
    try {
      let parsedSettings = {};
      if (formData.settings.trim()) {
        parsedSettings = JSON.parse(formData.settings);
      }

      const mutablePayload = {
        name: formData.name,
        record_types: formData.record_types.split(',').map(t => t.trim()).filter(Boolean),
        is_active: formData.is_active,
        settings: parsedSettings,
      };

      if (editingSource) {
        await updateSource.mutateAsync({ id: editingSource.id, data: mutablePayload });
        toast({ title: 'Source updated successfully' });
      } else {
        const payload = {
          ...mutablePayload,
          key: formData.key,
          kind: formData.kind,
        };
        await createSource.mutateAsync({ ...payload, priority: sources.length });
        toast({ title: 'Source created successfully' });
      }
      setIsFormOpen(false);
    } catch (e: any) {
      toast({ title: 'Error saving source', description: e.message, variant: 'destructive' });
    }
  };

  const handleDelete = async (id: number) => {
    if (!confirm('Are you sure you want to delete this source?')) return;
    try {
      await deleteSource.mutateAsync(id);
      toast({ title: 'Source deleted' });
    } catch (e: any) {
      toast({ title: 'Error deleting', description: e.message, variant: 'destructive' });
    }
  };

  const handleMove = async (index: number, direction: 'up' | 'down') => {
    if (direction === 'up' && index === 0) return;
    if (direction === 'down' && index === sources.length - 1) return;

    const swapIndex = direction === 'up' ? index - 1 : index + 1;
    const current = sources[index];
    const swap = sources[swapIndex];

    try {
      await Promise.all([
        updateSource.mutateAsync({ id: current.id, data: { priority: swap.priority } }),
        updateSource.mutateAsync({ id: swap.id, data: { priority: current.priority } })
      ]);
    } catch (e: any) {
      toast({ title: 'Error reordering', description: e.message, variant: 'destructive' });
    }
  };

  if (isLoading) {
    return <div className="p-8">Loading sources...</div>;
  }

  return (
    <div className="h-full flex flex-col space-y-6 p-8 animate-in fade-in duration-500 max-w-6xl mx-auto">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="kin-title">Data Sources</h1>
          <p className="text-muted-foreground text-sm mt-1">
            Manage ingest sources and priority ordering.
          </p>
        </div>
        {isAdmin && <Button onClick={() => handleOpenForm()}>
          <Plus className="mr-2 h-4 w-4" /> Add Source
        </Button>}
      </div>

      {error && <p role="alert" className="text-destructive">Unable to load sources: {error.message}</p>}
      <div className="border rounded-md bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-16">Pri</TableHead>
              <TableHead>Source</TableHead>
              <TableHead>Kind</TableHead>
              <TableHead>Record Types</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="w-[100px] text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {sources.length === 0 ? (
              <TableRow>
                <TableCell colSpan={6} className="h-24 text-center text-muted-foreground">
                  No sources configured.
                </TableCell>
              </TableRow>
            ) : (
              sources.map((source, idx) => (
                <TableRow key={source.id}>
                  <TableCell>
                    <div className="flex flex-col items-center space-y-1">
                      <button 
                        onClick={() => handleMove(idx, 'up')}
                        disabled={!isAdmin || idx === 0}
                        className="text-muted-foreground hover:text-foreground disabled:opacity-30"
                      >
                        <ArrowUp className="h-3 w-3" />
                      </button>
                      <span className="text-xs font-mono">{source.priority}</span>
                      <button 
                        onClick={() => handleMove(idx, 'down')}
                        disabled={!isAdmin || idx === sources.length - 1}
                        className="text-muted-foreground hover:text-foreground disabled:opacity-30"
                      >
                        <ArrowDown className="h-3 w-3" />
                      </button>
                    </div>
                  </TableCell>
                  <TableCell>
                    <div className="font-medium flex items-center gap-2">
                      <Database className="h-4 w-4 text-primary" />
                      {source.name}
                    </div>
                    <div className="text-xs text-muted-foreground font-mono mt-0.5">
                      {source.key}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge variant="secondary" className="font-mono text-[10px] uppercase">
                      {source.kind}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {source.record_types.map(rt => (
                        <Badge key={rt} variant="outline" className="text-[10px]">
                          {rt}
                        </Badge>
                      ))}
                    </div>
                  </TableCell>
                  <TableCell>
                    {source.is_active ? (
                      <Badge className="bg-emerald-500/10 text-emerald-500 border-emerald-500/20 hover:bg-emerald-500/20">Active</Badge>
                    ) : (
                      <Badge variant="secondary" className="text-muted-foreground">Inactive</Badge>
                    )}
                  </TableCell>
                  <TableCell className="text-right">
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button variant="ghost" className="h-8 w-8 p-0">
                          <span className="sr-only">Open menu</span>
                          <MoreVertical className="h-4 w-4" />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        {canReadEvents && source.kind === 'event_api' && <DropdownMenuItem onClick={() => setEventSource(source)}>
                          <KeySquare className="mr-2 h-4 w-4" /> SDK & live events
                        </DropdownMenuItem>}
                        {isAdmin && (isPullKind(source.kind) || source.kind === 'hubspot' || source.kind === 'meta_lead_ads') && <DropdownMenuItem data-testid={`menu-connection-${source.id}`} onClick={() => setConnectionSource(source)}>
                          <Plug className="mr-2 h-4 w-4" /> Connection
                        </DropdownMenuItem>}
                        {isAdmin && <DropdownMenuItem onClick={() => handleOpenForm(source)}>
                          <Edit2 className="mr-2 h-4 w-4" /> Edit
                        </DropdownMenuItem>}
                        {isAdmin && <DropdownMenuItem onClick={() => handleDelete(source.id)} className="text-destructive focus:text-destructive">
                          <Trash2 className="mr-2 h-4 w-4" /> Delete
                        </DropdownMenuItem>}
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>

      <Dialog open={isFormOpen} onOpenChange={setIsFormOpen}>
        <DialogContent className="sm:max-w-[500px]">
          <DialogHeader>
            <DialogTitle>{editingSource ? 'Edit Source' : 'New Source'}</DialogTitle>
            <DialogDescription>
              Configure connection details for this ingest source.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-4">
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label htmlFor="name">Display Name</Label>
                <Input 
                  id="name" 
                  value={formData.name} 
                  onChange={e => setFormData({ ...formData, name: e.target.value })} 
                  placeholder="e.g. Main S3 Bucket" 
                />
              </div>
              {!editingSource && (
                <div className="space-y-2">
                  <Label htmlFor="key">Source Key</Label>
                  <Input
                    id="key"
                    value={formData.key}
                    onChange={e => setFormData({ ...formData, key: e.target.value })}
                    placeholder="e.g. main_csv"
                    className="font-mono text-sm"
                  />
                </div>
              )}
            </div>
            
            <div className="grid grid-cols-2 gap-4">
              {!editingSource && (
                <div className="space-y-2">
                  <Label htmlFor="kind">Kind</Label>
                  <select
                    id="kind"
                    data-testid="select-source-kind"
                    value={formData.kind}
                    onChange={e => setFormData({ ...formData, kind: e.target.value, record_types: e.target.value === 'event_api' ? 'event' : e.target.value === 'meta_lead_ads' ? 'contact, consent' : e.target.value === 'hubspot' ? 'contact' : formData.record_types })}
                    className="h-9 w-full rounded-md border border-input bg-background px-2 text-sm"
                  >
                    <option value="csv">csv</option>
                    <option value="event_api">event_api</option>
                    <option value="sftp_jsonl">sftp_jsonl</option>
                    <option value="snowflake">snowflake</option>
                    <option value="zeta_exit">zeta_exit</option>
                    <option value="hubspot">hubspot</option>
                    <option value="meta_lead_ads">meta_lead_ads</option>
                  </select>
                  <p className="text-xs text-muted-foreground">csv: file upload. event_api: browser SDK (event records). sftp_jsonl, snowflake, zeta_exit, hubspot: scheduled pulls, configured after creation via Connection.</p>
                </div>
              )}
              <div className="space-y-2">
                <Label htmlFor="record_types">Record types (comma-separated)</Label>
                <Input 
                  id="record_types" 
                  value={formData.record_types} 
                  onChange={e => setFormData({ ...formData, record_types: e.target.value })} 
                  placeholder="e.g. contact, gift, event, resource"
                />
              </div>
            </div>

            <div className="space-y-2">
              <Label htmlFor="settings">Connection Settings (JSON)</Label>
              <Textarea 
                id="settings" 
                value={formData.settings} 
                onChange={e => setFormData({ ...formData, settings: e.target.value })} 
                className="font-mono text-xs h-32"
                placeholder='{"bucket": "..."}'
              />
            </div>

            <div className="flex items-center space-x-2 pt-2">
              <Switch 
                id="is_active" 
                checked={formData.is_active} 
                onCheckedChange={checked => setFormData({ ...formData, is_active: checked })} 
              />
              <Label htmlFor="is_active">Source is active</Label>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setIsFormOpen(false)}>Cancel</Button>
            <Button onClick={handleSave}>Save Source</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={!!eventSource} onOpenChange={open => { if (!open) setEventSource(null); }}>
        <DialogContent className="sm:max-w-3xl max-h-[90vh] overflow-auto">
          <DialogHeader>
            <DialogTitle>{eventSource?.name} · SDK & events</DialogTitle>
            <DialogDescription>Install browser tracking and inspect received events.</DialogDescription>
          </DialogHeader>
          {eventSource && <SourceEvents key={eventSource.id} source={sources.find(s => s.id === eventSource.id) || eventSource} />}
        </DialogContent>
      </Dialog>
      {isAdmin && <Dialog open={!!connectionSource} onOpenChange={open => { if (!open) setConnectionSource(null); }}>
        <DialogContent className="sm:max-w-3xl max-h-[90vh] overflow-auto">
          <DialogHeader>
            <DialogTitle>{connectionSource?.name} · Connection</DialogTitle>
            <DialogDescription>Credentials, feed mappings, schedules, dry run and manual pull.</DialogDescription>
          </DialogHeader>
          {connectionSource && connectionSource.kind === 'hubspot' && <HubspotSourceConnection key={connectionSource.id} source={sources.find(s => s.id === connectionSource.id) || connectionSource} />}
          {connectionSource && connectionSource.kind === 'meta_lead_ads' && <MetaSourcePanel key={connectionSource.id} source={sources.find(s => s.id === connectionSource.id) || connectionSource} />}
          {connectionSource && connectionSource.kind !== 'hubspot' && connectionSource.kind !== 'meta_lead_ads' && <SourceConnection key={connectionSource.id} source={sources.find(s => s.id === connectionSource.id) || connectionSource} />}
        </DialogContent>
      </Dialog>}
    </div>
  );
}
