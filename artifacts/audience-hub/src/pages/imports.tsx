import { useState, useEffect } from 'react';
import { FileUp, Database, ArrowRight, CheckCircle2, AlertTriangle, FileDown, RefreshCw, Eye, Settings2, PlayCircle, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@/components/ui/dialog';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Label } from '@/components/ui/label';
import { Input } from '@/components/ui/input';
import { Progress } from '@/components/ui/progress';
import { useImports, useCreateImport, useImport, useImportPreview, useUpdateMapping, useValidateImport, useRunImport, downloadImportErrors, ImportJob } from '@/hooks/use-imports';
import { useSources } from '@/hooks/use-sources';
import { toast } from '@/hooks/use-toast';
import { format } from 'date-fns';
import { useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';

export default function Imports() {
  const { data: importsData, isLoading: isLoadingImports } = useImports();
  const imports = importsData?.items || [];
  
  const [activeJobId, setActiveJobId] = useState<number | null>(null);
  const [isWizardOpen, setIsWizardOpen] = useState(false);

  const startNewImport = () => {
    setActiveJobId(null);
    setIsWizardOpen(true);
  };

  const openJob = (id: number) => {
    setActiveJobId(id);
    setIsWizardOpen(true);
  };

  return (
    <div className="h-full flex flex-col space-y-6 p-8 animate-in fade-in duration-500 max-w-6xl mx-auto">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Data Imports</h1>
          <p className="text-muted-foreground text-sm mt-1">
            Manual and scheduled batch ingestion jobs.
          </p>
        </div>
        <Button onClick={startNewImport}>
          <FileUp className="mr-2 h-4 w-4" /> New Import
        </Button>
      </div>

      <div className="border rounded-md bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>File</TableHead>
              <TableHead>Source</TableHead>
              <TableHead>Type</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="text-right">Rows (OK / Err / Warn)</TableHead>
              <TableHead className="w-[120px] text-right">Date</TableHead>
              <TableHead className="w-[80px] text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoadingImports ? (
              <TableRow>
                <TableCell colSpan={7} className="h-24 text-center">Loading imports...</TableCell>
              </TableRow>
            ) : imports.length === 0 ? (
              <TableRow>
                <TableCell colSpan={7} className="h-24 text-center text-muted-foreground">
                  No imports recorded yet.
                </TableCell>
              </TableRow>
            ) : (
              imports.map(job => (
                <TableRow key={job.id}>
                  <TableCell className="font-medium">
                    <div className="flex items-center gap-2">
                      <FileUp className="h-4 w-4 text-muted-foreground" />
                      {job.filename}
                    </div>
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">{job.source_id}</TableCell>
                  <TableCell>
                    <Badge variant="outline" className="text-[10px] uppercase font-mono">{job.record_type}</Badge>
                  </TableCell>
                  <TableCell>
                    <StatusBadge status={job.status} />
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs">
                    {job.rows_total > 0 ? (
                      <>
                        <span className="text-emerald-500">{job.rows_ok}</span>
                        <span className="text-muted-foreground mx-1">/</span>
                        <span className={job.rows_rejected > 0 ? "text-destructive" : "text-muted-foreground"}>
                          {job.rows_rejected}
                        </span>
                        <span className="text-muted-foreground mx-1">/</span>
                        <span className={job.warning_count ? "text-amber-500" : "text-muted-foreground"}>
                          {job.warning_count || 0}
                        </span>
                        {job.status === 'mapped' && (
                          <span className="block text-[10px] text-muted-foreground">validation sample · first 5,000</span>
                        )}
                      </>
                    ) : (
                      <span className="text-muted-foreground">-</span>
                    )}
                  </TableCell>
                  <TableCell className="text-right text-xs text-muted-foreground">
                    {format(new Date(job.created_at), 'MMM d, HH:mm')}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button variant="ghost" size="icon" onClick={() => openJob(job.id)}>
                      <Eye className="h-4 w-4" />
                    </Button>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>

      <ImportWizardDialog 
        open={isWizardOpen} 
        onOpenChange={setIsWizardOpen} 
        jobId={activeJobId} 
        onJobIdChange={setActiveJobId} 
      />
    </div>
  );
}

function StatusBadge({ status }: { status: ImportJob['status'] }) {
  switch (status) {
    case 'completed': return <Badge className="bg-emerald-500/10 text-emerald-500 border-emerald-500/20">Completed</Badge>;
    case 'failed': return <Badge variant="destructive">Failed</Badge>;
    case 'running': return <Badge className="bg-blue-500/10 text-blue-500 border-blue-500/20">Running</Badge>;
    case 'mapped': return <Badge className="bg-primary/10 text-primary border-primary/20">Mapped</Badge>;
    case 'uploaded': return <Badge variant="outline">Uploaded</Badge>;
    default: return <Badge variant="secondary" className="capitalize">{status}</Badge>;
  }
}

function ImportWizardDialog({ 
  open, 
  onOpenChange, 
  jobId, 
  onJobIdChange 
}: { 
  open: boolean; 
  onOpenChange: (open: boolean) => void;
  jobId: number | null;
  onJobIdChange: (id: number | null) => void;
}) {
  const { data: job, isLoading: isJobLoading } = useImport(jobId || 0);
  const queryClient = useQueryClient();

  const handleOpenChange = (isOpen: boolean) => {
    onOpenChange(isOpen);
    if (!isOpen) {
      queryClient.invalidateQueries({ queryKey: ['imports'] });
    }
  };
  
  // Decide which step to show based on job status
  const currentStep = !jobId ? 'upload' : job?.status;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-[700px] p-0 gap-0 overflow-hidden flex flex-col max-h-[90vh]">
        <div className="p-6 border-b border-border flex items-center justify-between shrink-0 bg-muted/30">
          <div>
            <DialogTitle className="text-lg">Data Import Wizard</DialogTitle>
            <DialogDescription className="mt-1">
              {!jobId ? 'Select source and upload file' : `Job ID: ${jobId}`}
            </DialogDescription>
          </div>
          {job && <StatusBadge status={job.status} />}
        </div>
        
        <div className="p-6 overflow-y-auto flex-1">
          {currentStep === 'upload' && (
            <UploadStep onJobCreated={(id) => onJobIdChange(id)} />
          )}
          {currentStep === 'uploaded' && job && (
            <MappingStep job={job} />
          )}
          {currentStep === 'mapped' && job && (
            <ReadyStep job={job} />
          )}
          {currentStep === 'running' && job && (
            <div className="flex flex-col items-center justify-center py-12 space-y-4">
              <RefreshCw className="h-8 w-8 text-primary animate-spin" />
              <div className="text-center w-full max-w-sm">
                <h3 className="font-medium text-lg">Import Running</h3>
                <p className="text-muted-foreground text-sm mb-4">
                  {job.progress?.message || 'Ingesting validated records into the hub...'}
                </p>
                {job.progress?.total ? (
                  <>
                    <Progress value={Math.min(100, ((job.progress.done || 0) / job.progress.total) * 100)} className="h-2" />
                    <div className="flex justify-between text-xs text-muted-foreground mt-2 font-mono">
                      <span>{job.progress.done || 0} processed</span>
                      <span>{job.progress.total} total</span>
                    </div>
                  </>
                ) : (
                  <p className="text-xs text-muted-foreground">Waiting for progress update…</p>
                )}
              </div>
            </div>
          )}
          {currentStep === 'completed' && job && (
            <div className="py-8 flex flex-col items-center justify-center">
              <CheckCircle2 className="h-16 w-16 text-emerald-500 mb-4" />
              <h3 className="font-semibold text-xl mb-1">Import Completed</h3>
              <p className="text-muted-foreground text-sm mb-6 text-center">
                Successfully processed {job.rows_total} records.
              </p>
              <div className="grid grid-cols-3 gap-4 w-full max-w-md">
                <div className="bg-muted p-4 rounded-md text-center border border-transparent">
                  <div className="text-2xl font-mono font-medium text-emerald-500">{job.rows_ok}</div>
                  <div className="text-xs text-muted-foreground mt-1 uppercase tracking-wider">Accepted</div>
                </div>
                <div className={`p-4 rounded-md text-center border ${job.warning_count ? 'bg-amber-500/10 border-amber-500/20' : 'bg-muted border-transparent'}`}>
                  <div className={`text-2xl font-mono font-medium ${job.warning_count ? 'text-amber-500' : 'text-muted-foreground'}`}>{job.warning_count || 0}</div>
                  <div className="text-xs text-muted-foreground mt-1 uppercase tracking-wider">Warnings</div>
                </div>
                <div className={`p-4 rounded-md text-center border ${job.rows_rejected > 0 ? 'bg-destructive/10 border-destructive/20' : 'bg-muted border-transparent'}`}>
                  <div className={`text-2xl font-mono font-medium ${job.rows_rejected > 0 ? 'text-destructive' : 'text-muted-foreground'}`}>{job.rows_rejected}</div>
                  <div className="text-xs text-muted-foreground mt-1 uppercase tracking-wider">Rejected</div>
                </div>
              </div>
              {hasErrorReport(job) && (
                <Button variant="outline" className="mt-6" onClick={() => downloadImportErrors(job.id)}>
                  <FileDown className="mr-2 h-4 w-4" /> Download Error Report
                </Button>
              )}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function UploadStep({ onJobCreated }: { onJobCreated: (id: number) => void }) {
  const { data: sourcesData } = useSources();
  const sources = sourcesData?.items || [];
  const createImport = useCreateImport();
  
  const [sourceId, setSourceId] = useState('');
  const [recordType, setRecordType] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [showDuplicateConfirm, setShowDuplicateConfirm] = useState(false);

  const selectedSource = sources.find(s => s.id.toString() === sourceId);
  const recordTypes = selectedSource?.record_types || [];

  const handleUpload = async (confirm = false) => {
    if (!sourceId || !recordType || !file) return;
    try {
      const res = await createImport.mutateAsync({ 
        source_id: parseInt(sourceId, 10), 
        record_type: recordType, 
        file,
        confirm_duplicate: confirm
      });
      onJobCreated(res.id);
    } catch (e: any) {
      if (e.message?.includes('duplicate') || e.message?.includes('409') || e.message?.toLowerCase().includes('already exists')) {
        setShowDuplicateConfirm(true);
      } else {
        toast({ title: 'Upload failed', description: e.message, variant: 'destructive' });
      }
    }
  };

  if (showDuplicateConfirm) {
    return (
      <div className="space-y-6 py-4 flex flex-col items-center text-center">
        <AlertTriangle className="h-12 w-12 text-amber-500 mb-2" />
        <h3 className="text-lg font-semibold">Duplicate File Detected</h3>
        <p className="text-sm text-muted-foreground max-w-md">
          A file with this name has already been uploaded for this source. Are you sure you want to upload it again?
        </p>
        <div className="flex gap-4 pt-4">
          <Button variant="outline" onClick={() => setShowDuplicateConfirm(false)}>Cancel</Button>
          <Button onClick={() => handleUpload(true)} disabled={createImport.isPending}>
            {createImport.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            Yes, Upload Anyway
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <Label>Data Source</Label>
        <Select value={sourceId} onValueChange={setSourceId}>
          <SelectTrigger>
            <SelectValue placeholder="Select a connected source" />
          </SelectTrigger>
          <SelectContent>
            {sources.filter(s => s.is_active).map(s => (
              <SelectItem key={s.id} value={s.id.toString()}>{s.name} ({s.key})</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="space-y-2">
        <Label>Record Type</Label>
        <Select value={recordType} onValueChange={setRecordType} disabled={!sourceId}>
          <SelectTrigger>
            <SelectValue placeholder="Select target record type" />
          </SelectTrigger>
          <SelectContent>
            {recordTypes.map(rt => (
              <SelectItem key={rt} value={rt}>{rt}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="space-y-2">
        <Label>CSV File</Label>
        <div className="border-2 border-dashed border-border rounded-md p-8 text-center bg-muted/20 transition-colors hover:bg-muted/40">
          <Input 
            type="file" 
            accept=".csv" 
            onChange={(e) => setFile(e.target.files?.[0] || null)} 
            className="hidden" 
            id="file-upload" 
          />
          <Label htmlFor="file-upload" className="cursor-pointer flex flex-col items-center justify-center gap-2">
            <FileUp className="h-8 w-8 text-muted-foreground" />
            {file ? (
              <span className="font-medium text-primary">{file.name}</span>
            ) : (
              <span className="text-muted-foreground">Click to browse or drag and drop</span>
            )}
            <span className="text-xs text-muted-foreground">CSV up to 50MB</span>
          </Label>
        </div>
      </div>

      <div className="flex justify-end pt-4">
        <Button onClick={() => handleUpload()} disabled={!sourceId || !recordType || !file || createImport.isPending}>
          {createImport.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
          Upload & Continue
        </Button>
      </div>
    </div>
  );
}

function MappingStep({ job }: { job: ImportJob }) {
  const { data: preview, isLoading } = useImportPreview(job.id);
  const updateMapping = useUpdateMapping();
  const validateImport = useValidateImport();
  
  const [mapping, setMapping] = useState<Record<string, string>>({});

  // Initialize mapping when preview loads
  useEffect(() => {
    if (preview?.suggested_mapping && Object.keys(mapping).length === 0) {
      setMapping(preview.suggested_mapping);
    }
  }, [preview, mapping]);

  if (isLoading || !preview) return <div className="py-12 text-center text-muted-foreground">Analyzing columns...</div>;

  const handleNext = async () => {
    try {
      await updateMapping.mutateAsync({ id: job.id, mapping });
      await validateImport.mutateAsync(job.id);
    } catch (e: any) {
      toast({ title: 'Failed to save mapping or validate import', description: e.message, variant: 'destructive' });
    }
  };

  return (
    <div className="space-y-6">
      <div className="bg-muted p-4 rounded-md">
        <h4 className="text-sm font-medium mb-2 flex items-center gap-2">
          <Settings2 className="h-4 w-4" /> Column Mapping
        </h4>
        <p className="text-xs text-muted-foreground mb-4">
          Map your CSV columns to the target schema fields. Unmapped columns will be stored in unstructured attributes if supported.
        </p>
        
        <div className="grid grid-cols-2 gap-4 max-h-[300px] overflow-y-auto pr-2">
          {preview.headers.map((header) => (
            <div key={header} className="flex items-center gap-3 bg-background p-2 rounded border border-border">
              <div className="flex-1 font-mono text-xs truncate" title={header}>{header}</div>
              <ArrowRight className="h-3 w-3 text-muted-foreground shrink-0" />
              <Input 
                value={mapping[header] || ''} 
                onChange={(e) => setMapping(prev => ({ ...prev, [header]: e.target.value }))}
                className="flex-1 h-7 text-xs font-mono"
                placeholder="Field name..."
              />
            </div>
          ))}
        </div>
      </div>

      <div className="space-y-2">
        <Label>Data Preview (First 3 rows)</Label>
        <div className="border rounded-md overflow-x-auto max-w-full">
          <Table className="text-xs">
            <TableHeader>
              <TableRow>
                {preview.headers.map(h => <TableHead key={h} className="whitespace-nowrap font-mono">{h}</TableHead>)}
              </TableRow>
            </TableHeader>
            <TableBody>
              {preview.rows.slice(0, 3).map((row, i) => (
                <TableRow key={i}>
                  {preview.headers.map(h => (
                    <TableCell key={h} className="whitespace-nowrap max-w-[200px] truncate" title={String(row[h] || '')}>
                      {String(row[h] || '')}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </div>

      <div className="flex justify-end pt-4">
        <Button onClick={handleNext} disabled={updateMapping.isPending || validateImport.isPending}>
          {(updateMapping.isPending || validateImport.isPending) && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
          Save Mapping &amp; Validate
        </Button>
      </div>
    </div>
  );
}

function ReadyStep({ job }: { job: ImportJob }) {
  const runImport = useRunImport();

  const handleRun = async () => {
    try {
      await runImport.mutateAsync(job.id);
    } catch (e: any) {
      toast({ title: 'Failed to start run', description: e.message, variant: 'destructive' });
    }
  };

  const validationErrors = getValidationErrors(job);
  const hasErrors = job.rows_rejected > 0 || validationErrors.length > 0;
  const isValid = job.validation?.valid ?? job.is_valid ?? job.valid ?? (job.rows_rejected === 0 && validationErrors.length === 0);
  const validationScope = job.validation?.scope ?? 5000;

  return (
    <div className="space-y-6 py-4">
      <div className="text-center mb-8">
        <h3 className="text-lg font-semibold mb-2">Validation Complete</h3>
        <p className="text-sm text-muted-foreground">
          Dry-run validation checks up to the first {validationScope.toLocaleString()} rows. Counts below describe that validation scope, not necessarily the full file.
        </p>
      </div>

      <div className="grid grid-cols-3 gap-4 w-full max-w-lg mx-auto">
        <div className="bg-emerald-500/10 border border-emerald-500/20 p-4 rounded-lg flex flex-col items-center text-center">
          <div className="text-3xl font-mono font-medium text-emerald-500 mb-1">{job.rows_ok}</div>
          <div className="text-xs font-semibold text-emerald-600 uppercase tracking-wider">Valid</div>
          <div className="text-[10px] text-muted-foreground mt-2">In scope</div>
        </div>

        <div className={`p-4 rounded-lg flex flex-col items-center text-center border ${job.warning_count ? 'bg-amber-500/10 border-amber-500/20' : 'bg-muted/50 border-transparent'}`}>
          <div className={`text-3xl font-mono font-medium mb-1 ${job.warning_count ? 'text-amber-500' : 'text-muted-foreground'}`}>{job.warning_count || 0}</div>
          <div className={`text-xs font-semibold uppercase tracking-wider ${job.warning_count ? 'text-amber-600' : 'text-muted-foreground'}`}>Warnings</div>
          <div className="text-[10px] text-muted-foreground mt-2">In scope</div>
        </div>

        <div className={`p-4 rounded-lg flex flex-col items-center text-center border ${hasErrors ? 'bg-destructive/10 border-destructive/20' : 'bg-muted/50 border-transparent'}`}>
          <div className={`text-3xl font-mono font-medium mb-1 ${hasErrors ? 'text-destructive' : 'text-muted-foreground'}`}>{job.rows_rejected}</div>
          <div className={`text-xs font-semibold uppercase tracking-wider ${hasErrors ? 'text-destructive' : 'text-muted-foreground'}`}>Rejected</div>
          <div className="text-[10px] text-muted-foreground mt-2">In scope</div>
        </div>
      </div>

      {(hasErrors || (job.warning_count && job.warning_count > 0)) && (
        <div className={`${hasErrors ? 'bg-destructive/10 text-destructive' : 'bg-amber-500/10 text-amber-600'} p-4 rounded-md text-sm flex gap-3 max-w-lg mx-auto mt-4`}>
          <AlertTriangle className="h-5 w-5 shrink-0" />
          <div>
            <p>{job.rows_rejected} rows rejected, {job.warning_count || 0} warnings in validation scope.</p>
            {validationErrors.length > 0 && (
              <ul className="mt-2 list-disc space-y-1 pl-5 text-xs">
                {validationErrors.slice(0, 10).map((error, index) => (
                  <li key={index}>{formatValidationError(error)}</li>
                ))}
                {validationErrors.length > 10 && <li>And {validationErrors.length - 10} more validation issues.</li>}
              </ul>
            )}
            {job.warning_counts && Object.keys(job.warning_counts).length > 0 && (
               <div className="mt-2 text-xs">
                 <p className="font-semibold mb-1">Warning summary:</p>
                 <ul className="list-disc pl-5">
                   {Object.entries(job.warning_counts).filter(([_, v]) => v > 0).map(([k, v]) => (
                     <li key={k}>{v} {k.replace('_', ' ')}</li>
                   ))}
                 </ul>
               </div>
            )}
          </div>
        </div>
      )}

      <div className="flex justify-center pt-8 gap-4">
        <Button size="lg" onClick={handleRun} disabled={runImport.isPending || !isValid} className="w-full max-w-xs">
          {runImport.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <PlayCircle className="mr-2 h-5 w-5" />}
          {!isValid ? 'Resolve Validation Issues' : 'Run Import'}
        </Button>
      </div>
    </div>
  );
}

function getValidationErrors(job: ImportJob) {
  return job.validation_errors ?? job.validation?.errors ?? job.errors ?? [];
}

function formatValidationError(error: string | { row?: number; field?: string; message?: string }) {
  if (typeof error === 'string') return error;
  const location = [error.row ? `Row ${error.row}` : '', error.field].filter(Boolean).join(' · ');
  return [location, error.message || 'Validation failed'].filter(Boolean).join(': ');
}

function hasErrorReport(job: ImportJob) {
  if (job.status !== 'completed') return false;
  const explicitAvailability = job.error_report_available ?? job.has_error_report ?? job.report_available;
  if (explicitAvailability !== undefined) return explicitAvailability;
  return Boolean(
    job.error_report_url ||
    job.report_url ||
    (typeof job.error_report === 'string' && job.error_report) ||
    (typeof job.error_report === 'object' && job.error_report && Object.keys(job.error_report).length > 0) ||
    (typeof job.report === 'string' && job.report) ||
    (typeof job.report === 'object' && job.report && Object.keys(job.report).length > 0)
  );
}
