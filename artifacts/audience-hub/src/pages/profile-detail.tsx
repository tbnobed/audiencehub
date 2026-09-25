import { useParams, useNavigate, useLocation } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';
import { useProfile } from '@/hooks/use-profiles';
import { 
  ArrowLeft, Mail, Phone, MapPin, Calendar, CreditCard, Activity, Link2, Database, ShieldAlert,
  Merge, Gift, LayoutDashboard, Fingerprint, Coins, Shield, Copy, Check
} from 'lucide-react';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { displayValue, displayDate, displayAmount, displayNumber, displayJoined, records, record } from '@/lib/profile-display';
import { ProfileSectionBoundary } from '@/components/ProfileSectionBoundary';
import { Skeleton } from '@/components/ui/skeleton';

export default function ProfileDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const back = (location.state as { from?: string } | null)?.from || '/profiles';
  const [copied, setCopied] = useState(false);
  const { data: rawProfile, isLoading, error, refetch } = useProfile(Number(id));
  const { data: catalog } = useQuery({
    queryKey: ['traits-catalog'],
    queryFn: () => fetchApi('/api/traits') as Promise<{ items: { key: string; label: string; description: string }[] }>,
    staleTime: 300000,
  });

  if (isLoading) {
    return (
      <div className="p-8 space-y-6 max-w-[1400px] mx-auto animate-in fade-in duration-500">
        <Skeleton className="h-10 w-[200px]" />
        <Skeleton className="h-32 w-full" />
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-[400px] w-full" />
      </div>
    );
  }

  if (error || !rawProfile) {
    return (
      <div className="p-8 flex flex-col items-center justify-center h-full text-center">
        <div className="h-16 w-16 rounded-full bg-destructive/10 flex items-center justify-center mb-4">
          <ShieldAlert className="h-8 w-8 text-destructive" />
        </div>
        <h2 className="kin-heading mb-2">Profile Not Found</h2>
        <p className="text-muted-foreground mb-6 max-w-md">
          This profile might have been deleted or merged into another record.
        </p>
        <Button variant="outline" onClick={() => navigate(back)}>
          <ArrowLeft className="mr-2 h-4 w-4" /> Back to Profiles
        </Button>
        {error && <Button variant="outline" onClick={() => refetch()}>Retry</Button>}
      </div>
    );
  }

  const profile = {
    ...rawProfile,
    traits: record(rawProfile.traits),
    gifts: records(rawProfile.gifts),
    events: records(rawProfile.events),
    identifiers: records(rawProfile.identifiers),
    source_records: records(rawProfile.source_records),
    merges: records(rawProfile.merges),
    enrichment: records(rawProfile.enrichment),
    consents: records(rawProfile.consents),
  };
  const name = displayJoined([profile.first_name, profile.last_name], ' ');
  const boundary = { route: location.pathname, profileId: id || '', onRetry: () => { void refetch(); } };

  return (
    <div className="h-full flex flex-col space-y-4 pb-8 animate-in fade-in duration-300 max-w-[1680px] mx-auto">
      <ProfileSectionBoundary {...boundary} section="Profile header">{() => <div className="flex items-center gap-4">
        <Button variant="ghost" size="icon" data-testid="button-back-profiles" onClick={() => navigate(back)}>
          <ArrowLeft className="h-5 w-5" />
        </Button>
        <div>
          <div className="flex items-center gap-3">
            <h1 className="kin-title">{name}</h1>
            <Badge variant="outline" className={`font-mono text-xs uppercase ${
              profile.donor_status === 'active' ? 'bg-primary/10 text-primary border-primary/20' :
              profile.donor_status === 'lapsed' ? 'bg-amber-500/10 text-amber-500 border-amber-500/20' : ''
            }`}>
              {displayValue(profile.donor_status)}
            </Badge>
            <button type="button" data-testid="button-copy-profile-id" className="font-mono text-[11px] text-muted-foreground border border-border rounded px-2 py-1 flex items-center gap-1 hover:text-primary" title="Copy profile ID" onClick={async () => { await navigator.clipboard.writeText(String(profile.id)); setCopied(true); setTimeout(() => setCopied(false), 2000); }}>ID: {profile.id} {copied ? <Check size={12} /> : <Copy size={12} />}</button>
          </div>
          <div className="flex items-center gap-4 mt-2 text-sm text-muted-foreground">
            <span className="flex items-center"><Mail className="h-3 w-3 mr-1" /> {displayValue(profile.email)}</span>
            <span className="flex items-center"><Phone className="h-3 w-3 mr-1" /> {displayValue(profile.phone)}</span>
            {(profile.city || profile.region || profile.country) && (
              <span className="flex items-center">
                <MapPin className="h-3 w-3 mr-1" /> 
                {displayJoined([profile.city, profile.region, profile.country])}
              </span>
            )}
          </div>
        </div>
      </div>}</ProfileSectionBoundary>
      <ProfileSectionBoundary {...boundary} section="Profile statistics">{() => <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        {[
          ['Donor status', displayValue(profile.donor_status).replaceAll('_', ' ')],
          ['RFM score', displayValue(profile.traits.rfm_score)],
          ['Lifetime giving', displayAmount(profile.traits.ltv_total)],
          ['Last gift', displayDate(profile.traits.last_gift_date)],
        ].map(([label, value]) => <div key={label} className="border border-line bg-surface rounded px-3 py-3"><div className="text-[10px] uppercase tracking-wider text-ink-muted">{label}</div><div className="font-mono text-lg text-ink mt-1" data-testid={`value-${label.toLowerCase().replaceAll(' ', '-')}`}>{value}</div></div>)}
      </div>}</ProfileSectionBoundary>

      <Tabs defaultValue="overview" className="flex-1 flex flex-col">
        <TabsList className="w-full justify-start border-b rounded-none h-12 bg-transparent p-0">
          <TabsTrigger value="overview" className="data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none h-12 px-6">
            <LayoutDashboard className="h-4 w-4 mr-2" /> Overview
          </TabsTrigger>
          <TabsTrigger value="gifts" className="data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none h-12 px-6">
            <Gift className="h-4 w-4 mr-2" /> Gifts <Badge variant="secondary" className="ml-2 bg-muted">{profile.gifts.length}</Badge>
          </TabsTrigger>
          <TabsTrigger value="activity" className="data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none h-12 px-6">
            <Activity className="h-4 w-4 mr-2" /> Activity <Badge variant="secondary" className="ml-2 bg-muted">{profile.events.length}</Badge>
          </TabsTrigger>
          <TabsTrigger value="identity" className="data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none h-12 px-6">
            <Fingerprint className="h-4 w-4 mr-2" /> Identity
          </TabsTrigger>
          <TabsTrigger value="enrichment" className="data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none h-12 px-6">
            <Coins className="h-4 w-4 mr-2" /> Enrichment
          </TabsTrigger>
          <TabsTrigger value="consents" className="data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none h-12 px-6">
            <Shield className="h-4 w-4 mr-2" /> Consents
          </TabsTrigger>
        </TabsList>

        <div className="flex-1 mt-4">
          <TabsContent value="overview" className="m-0 space-y-6">
            <ProfileSectionBoundary {...boundary} section="Overview">{() => <>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm font-medium text-muted-foreground">Lifetime Value</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="text-3xl font-bold font-mono tracking-tight">
                    {displayAmount(profile.traits.ltv_total)}
                  </div>
                  <p className="text-xs text-muted-foreground mt-1">Across {displayNumber(profile.traits.gift_count_total)} gifts</p>
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm font-medium text-muted-foreground">Last Gift</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="text-3xl font-bold font-mono tracking-tight">
                    {displayDate(profile.traits.last_gift_date)}
                  </div>
                  <p className="text-xs text-muted-foreground mt-1">
                    {displayNumber(profile.traits.days_since_last_gift)} days ago
                  </p>
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm font-medium text-muted-foreground">RFM Score</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="text-3xl font-bold font-mono tracking-tight text-primary">
                    {displayValue(profile.traits.rfm_score)}
                  </div>
                  <p className="text-xs text-muted-foreground mt-1">
                    R:{displayValue(profile.traits.rfm_recency)} F:{displayValue(profile.traits.rfm_frequency)} M:{displayValue(profile.traits.rfm_monetary)}
                  </p>
                </CardContent>
              </Card>
            </div>
            
            <ProfileSectionBoundary {...boundary} section="Profile details panel">{() => <Card>
              <CardHeader>
                <CardTitle>Profile Details</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-6 text-sm">
                  <div>
                    <span className="text-muted-foreground block text-xs mb-1">First Seen</span>
                    <span className="font-medium">{displayDate(profile.first_seen_at, 'MMM d, yyyy HH:mm')}</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground block text-xs mb-1">Last Seen</span>
                    <span className="font-medium">{displayDate(profile.last_seen_at, 'MMM d, yyyy HH:mm')}</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground block text-xs mb-1">12m Gift Value</span>
                    <span className="font-mono font-medium">{displayAmount(profile.traits.gift_amount_12m)}</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground block text-xs mb-1">12m Gift Count</span>
                    <span className="font-medium">{displayNumber(profile.traits.gift_count_12m)}</span>
                  </div>
                  <div className="col-span-2">
                    <span className="text-muted-foreground block text-xs mb-1">Address</span>
                    <span className="font-medium">
                      {displayJoined([profile.address1, profile.city, profile.region, profile.postal_code, profile.country])}
                    </span>
                  </div>
                  <div className="col-span-2">
                    <span className="text-muted-foreground block text-xs mb-1">Known Sources</span>
                    <div className="flex gap-2 mt-1 flex-wrap">
                      {displayValue(profile.traits.source_keys)}
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>}</ProfileSectionBoundary>
            <ProfileSectionBoundary {...boundary} section="Computed traits panel">{() => <Card>
              <CardHeader className="pb-3"><CardTitle className="text-sm">Computed traits</CardTitle><CardDescription>Current values from the trait engine. Hover a label for its definition.</CardDescription></CardHeader>
              <CardContent className="grid grid-cols-2 lg:grid-cols-4 gap-px bg-line p-px">
                {records(catalog?.items).map((item, i) => <div key={i} className="bg-surface p-3 min-w-0" title={displayValue(item.description)}><div className="text-[10px] uppercase tracking-wide text-ink-muted truncate">{displayValue(item.label)}</div><div className="font-mono text-xs mt-1 break-words">{displayValue(profile.traits[displayValue(item.key)])}</div></div>)}
                {!catalog?.items?.length && <div className="bg-surface p-3 text-xs text-ink-muted">Trait definitions unavailable.</div>}
              </CardContent>
            </Card>}</ProfileSectionBoundary>
            <ProfileSectionBoundary {...boundary} section="Consent summary panel">{() => <Card>
              <CardHeader className="pb-3"><CardTitle className="text-sm">Consent by channel</CardTitle><CardDescription>Recorded communication preferences and provenance.</CardDescription></CardHeader>
              <CardContent className="p-0"><Table><TableHeader><TableRow><TableHead>Channel</TableHead><TableHead>Status</TableHead><TableHead>Source</TableHead><TableHead className="text-right">Captured</TableHead></TableRow></TableHeader><TableBody>{profile.consents.length ? profile.consents.map((c, i) => <TableRow key={i}><TableCell className="uppercase text-xs">{displayValue(c.channel)}</TableCell><TableCell className="text-xs">{displayValue(c.status)}</TableCell><TableCell className="font-mono text-xs">{displayValue(c.source_key)}</TableCell><TableCell className="text-right text-xs">{displayDate(c.captured_at)}</TableCell></TableRow>) : <TableRow><TableCell colSpan={4} className="text-center text-xs text-muted-foreground py-6">No consent records yet.</TableCell></TableRow>}</TableBody></Table></CardContent>
            </Card>}</ProfileSectionBoundary>
            </>}</ProfileSectionBoundary>
          </TabsContent>

          <TabsContent value="gifts" className="m-0 h-full">
            <ProfileSectionBoundary {...boundary} section="Gifts">{() =>
            <Card className="h-full flex flex-col">
              <div className="overflow-auto flex-1 p-0">
                <Table>
                  <TableHeader className="bg-muted/50 sticky top-0">
                    <TableRow>
                      <TableHead>Date</TableHead>
                      <TableHead className="text-right">Amount</TableHead>
                      <TableHead>Fund</TableHead>
                      <TableHead>Campaign / Appeal</TableHead>
                      <TableHead>Channel</TableHead>
                      <TableHead>Source</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {profile.gifts.length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={6} className="text-center py-12 text-muted-foreground">
                          No gifts recorded for this profile.
                        </TableCell>
                      </TableRow>
                    ) : (
                      profile.gifts.map((gift) => (
                        <TableRow key={gift.id}>
                          <TableCell className="font-medium">
                            {displayDate(gift.gift_date)}
                          </TableCell>
                          <TableCell className="text-right font-mono font-medium text-emerald-500">
                            {displayAmount(gift.amount, gift.currency ?? 'USD')}
                            {gift.is_recurring === true && <Badge variant="outline" className="ml-2 text-[9px] uppercase">Recurring</Badge>}
                          </TableCell>
                          <TableCell className="text-xs">{displayValue(gift.fund)}</TableCell>
                          <TableCell className="text-xs text-muted-foreground">
                            {displayJoined([gift.campaign, gift.appeal_code], ' / ')}
                          </TableCell>
                          <TableCell className="text-xs uppercase">{displayValue(gift.channel)}</TableCell>
                          <TableCell>
                            <Badge variant="secondary" className="font-mono text-[10px]">{displayValue(gift.source_key)}</Badge>
                          </TableCell>
                        </TableRow>
                      ))
                    )}
                  </TableBody>
                </Table>
              </div>
            </Card>}</ProfileSectionBoundary>
          </TabsContent>

          <TabsContent value="activity" className="m-0 h-full">
            <ProfileSectionBoundary {...boundary} section="Activity">{() =>
            <Card className="h-full flex flex-col">
              <div className="overflow-auto flex-1 p-0">
                <Table>
                  <TableHeader className="bg-muted/50 sticky top-0">
                    <TableRow>
                      <TableHead className="w-[180px]">Time</TableHead>
                      <TableHead>Event Type</TableHead>
                      <TableHead>Name</TableHead>
                      <TableHead>Source</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {profile.events.length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={4} className="text-center py-12 text-muted-foreground">
                          No recent activity recorded.
                        </TableCell>
                      </TableRow>
                    ) : (
                      profile.events.map((event) => (
                        <TableRow key={event.id}>
                          <TableCell className="text-xs text-muted-foreground">
                            {displayDate(event.occurred_at, 'MMM d, yyyy HH:mm:ss')}
                          </TableCell>
                          <TableCell>
                            <Badge variant="outline" className="font-mono text-[10px] uppercase">{displayValue(event.type)}</Badge>
                          </TableCell>
                          <TableCell className="font-medium text-sm">{displayValue(event.name)}</TableCell>
                          <TableCell>
                            <Badge variant="secondary" className="font-mono text-[10px]">{displayValue(event.source_key)}</Badge>
                            {event.properties && Object.keys(event.properties).length > 0 && <details className="mt-1 text-[10px]"><summary className="cursor-pointer text-primary">Properties</summary><pre className="mt-1 p-2 bg-ground border border-line rounded max-w-[300px] overflow-auto">{JSON.stringify(event.properties, null, 2)}</pre></details>}
                          </TableCell>
                        </TableRow>
                      ))
                    )}
                  </TableBody>
                </Table>
              </div>
            </Card>}</ProfileSectionBoundary>
          </TabsContent>

          <TabsContent value="identity" className="m-0 space-y-6">
            <ProfileSectionBoundary {...boundary} section="Identity">{() => <>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center text-base"><Link2 className="h-4 w-4 mr-2" /> Current Identifiers</CardTitle>
                  <CardDescription>Keys used to stitch this profile.</CardDescription>
                </CardHeader>
                <CardContent className="p-0">
                  <Table>
                    <TableHeader className="bg-muted/30">
                      <TableRow>
                        <TableHead>Type</TableHead>
                        <TableHead>Value</TableHead>
                        <TableHead className="text-right">First Seen</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {profile.identifiers.length === 0 ? (
                        <TableRow>
                          <TableCell colSpan={3} className="text-center py-6 text-muted-foreground">No extra identifiers.</TableCell>
                        </TableRow>
                      ) : (
                        profile.identifiers.map((ident) => (
                          <TableRow key={ident.id}>
                            <TableCell className="text-xs font-mono uppercase">{displayValue(ident.type)}</TableCell>
                            <TableCell className="font-medium text-sm">{displayValue(ident.value)}</TableCell>
                            <TableCell className="text-right text-xs text-muted-foreground">
                              {displayDate(ident.first_seen_at, 'MMM d, yy')}
                            </TableCell>
                          </TableRow>
                        ))
                      )}
                    </TableBody>
                  </Table>
                </CardContent>
              </Card>
              
              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center text-base"><Merge className="h-4 w-4 mr-2" /> Merge History</CardTitle>
                  <CardDescription>Profiles folded into this one.</CardDescription>
                </CardHeader>
                <CardContent className="p-0">
                  <Table>
                    <TableHeader className="bg-muted/30">
                      <TableRow>
                        <TableHead>Time</TableHead>
                        <TableHead>Merged From</TableHead>
                        <TableHead>Reason</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {profile.merges.length === 0 ? (
                        <TableRow>
                          <TableCell colSpan={3} className="text-center py-6 text-muted-foreground">No merge history.</TableCell>
                        </TableRow>
                      ) : (
                        profile.merges.map((merge) => (
                          <TableRow key={merge.id}>
                            <TableCell className="text-xs text-muted-foreground">
                              {displayDate(merge.merged_at, 'MMM d, yy HH:mm')}
                            </TableCell>
                            <TableCell className="font-mono text-xs">
                              ID: {displayValue(merge.loser_id)}
                            </TableCell>
                            <TableCell className="text-xs capitalize">{displayValue(merge.reason).replaceAll('_', ' ')}</TableCell>
                          </TableRow>
                        ))
                      )}
                    </TableBody>
                  </Table>
                </CardContent>
              </Card>
            </div>
            
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center text-base"><Database className="h-4 w-4 mr-2" /> Source Records</CardTitle>
                <CardDescription>The raw data components that make up this profile.</CardDescription>
              </CardHeader>
              <CardContent className="p-0">
                <Table>
                  <TableHeader className="bg-muted/30">
                    <TableRow>
                      <TableHead>Source</TableHead>
                      <TableHead>External ID</TableHead>
                      <TableHead>Name Provided</TableHead>
                      <TableHead>Contact Provided</TableHead>
                      <TableHead className="text-right">Last Updated</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {profile.source_records.length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={5} className="text-center py-8 text-muted-foreground">No source records found.</TableCell>
                      </TableRow>
                    ) : (
                      profile.source_records.map((sr) => (
                        <TableRow key={sr.id}>
                          <TableCell>
                            <Badge variant="secondary" className="font-mono text-[10px]">{displayValue(sr.source_key)}</Badge>
                          </TableCell>
                          <TableCell className="font-mono text-xs text-muted-foreground">{displayValue(sr.external_id)}</TableCell>
                          <TableCell className="text-sm">
                            {displayJoined([sr.first_name, sr.last_name], ' ')}
                          </TableCell>
                          <TableCell className="text-xs">
                            <div className="flex flex-col">
                              <span>{displayValue(sr.email)}</span>
                              <span className="text-muted-foreground">{displayValue(sr.phone)}</span>
                              {sr.attributes && Object.keys(sr.attributes).length > 0 && <details className="mt-1 text-[10px]"><summary className="cursor-pointer text-primary">Raw attributes</summary><pre className="mt-1 p-2 bg-ground border border-line rounded max-w-[300px] overflow-auto">{JSON.stringify(sr.attributes, null, 2)}</pre></details>}
                            </div>
                          </TableCell>
                          <TableCell className="text-right text-xs text-muted-foreground">
                            {displayDate(sr.updated_at)}
                          </TableCell>
                        </TableRow>
                      ))
                    )}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>
            </>}</ProfileSectionBoundary>
          </TabsContent>

          <TabsContent value="enrichment" className="m-0">
            <ProfileSectionBoundary {...boundary} section="Enrichment">{() =>
            <Card>
              <CardHeader>
                <CardTitle>Enrichment Data</CardTitle>
                <CardDescription>Third-party attributes appended to this profile.</CardDescription>
              </CardHeader>
              <CardContent className="p-0">
                <Table>
                  <TableHeader className="bg-muted/30">
                    <TableRow>
                      <TableHead>Provider</TableHead>
                      <TableHead>Attribute Key</TableHead>
                      <TableHead>Value</TableHead>
                      <TableHead className="text-right">License expires</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {profile.enrichment.length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={4} className="text-center py-12 text-muted-foreground">
                          No third-party enrichment data attached.
                        </TableCell>
                      </TableRow>
                    ) : (
                      profile.enrichment.map((enr, i) => (
                        <TableRow key={i}>
                          <TableCell>
                            <Badge variant="outline" className="font-mono text-[10px]">{displayValue(enr.source_key)}</Badge>
                          </TableCell>
                          <TableCell className="font-medium text-sm">{displayValue(enr.attribute_key)}</TableCell>
                          <TableCell className="font-mono text-xs">
                            {enr.value_text != null ? displayValue(enr.value_text) :
                             enr.value_num != null ? displayNumber(enr.value_num) :
                             enr.value_bool != null ? displayValue(enr.value_bool) :
                             displayDate(enr.value_date)}
                          </TableCell>
                          <TableCell className="text-right text-xs text-muted-foreground">
                            {displayDate(enr.license_expires_at, 'MMM d, yy')}
                          </TableCell>
                        </TableRow>
                      ))
                    )}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>}</ProfileSectionBoundary>
          </TabsContent>
          
          <TabsContent value="consents" className="m-0">
            <ProfileSectionBoundary {...boundary} section="Consents">{() =>
            <Card>
              <CardHeader>
                <CardTitle>Consent Records</CardTitle>
                <CardDescription>Communication preferences and opt-ins.</CardDescription>
              </CardHeader>
              <CardContent className="p-0">
                <Table>
                  <TableHeader className="bg-muted/30">
                    <TableRow>
                      <TableHead>Channel</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>Source</TableHead>
                      <TableHead className="text-right">Captured At</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {profile.consents.length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={4} className="text-center py-12 text-muted-foreground">
                          No explicit consent records.
                        </TableCell>
                      </TableRow>
                    ) : (
                      profile.consents.map((consent, i) => (
                        <TableRow key={i}>
                          <TableCell className="font-medium uppercase text-xs">{displayValue(consent.channel)}</TableCell>
                          <TableCell>
                            <Badge variant="outline" className={`font-mono text-[10px] ${
                              consent.status === 'opt_in' ? 'text-emerald-500 border-emerald-500/20 bg-emerald-500/10' :
                              consent.status === 'opt_out' ? 'text-destructive border-destructive/20 bg-destructive/10' : ''
                            }`}>
                              {displayValue(consent.status)}
                            </Badge>
                          </TableCell>
                          <TableCell>
                            <Badge variant="secondary" className="font-mono text-[10px]">{displayValue(consent.source_key)}</Badge>
                          </TableCell>
                          <TableCell className="text-right text-xs text-muted-foreground">
                            {displayDate(consent.captured_at, 'MMM d, yy HH:mm')}
                          </TableCell>
                        </TableRow>
                      ))
                    )}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>}</ProfileSectionBoundary>
          </TabsContent>
        </div>
      </Tabs>
    </div>
  );
}