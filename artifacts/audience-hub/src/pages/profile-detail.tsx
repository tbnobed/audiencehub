import { useParams, useNavigate } from 'react-router-dom';
import { useProfile } from '@/hooks/use-profiles';
import { 
  ArrowLeft, Mail, Phone, MapPin, Calendar, CreditCard, Activity, Link2, Database, ShieldAlert,
  Merge, Gift, LayoutDashboard, Fingerprint, Coins, Shield
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { format } from 'date-fns';
import { Skeleton } from '@/components/ui/skeleton';

export default function ProfileDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data: profile, isLoading, error } = useProfile(Number(id));

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

  if (error || !profile) {
    return (
      <div className="p-8 flex flex-col items-center justify-center h-full text-center">
        <div className="h-16 w-16 rounded-full bg-destructive/10 flex items-center justify-center mb-4">
          <ShieldAlert className="h-8 w-8 text-destructive" />
        </div>
        <h2 className="text-xl font-semibold mb-2">Profile Not Found</h2>
        <p className="text-muted-foreground mb-6 max-w-md">
          This profile might have been deleted or merged into another record.
        </p>
        <Button onClick={() => navigate('/profiles')}>
          <ArrowLeft className="mr-2 h-4 w-4" /> Back to Profiles
        </Button>
      </div>
    );
  }

  const name = [profile.first_name, profile.last_name].filter(Boolean).join(' ') || 'Unnamed Profile';

  return (
    <div className="h-full flex flex-col space-y-6 p-8 animate-in fade-in duration-500 max-w-[1400px] mx-auto">
      <div className="flex items-center gap-4">
        <Button variant="ghost" size="icon" onClick={() => navigate('/profiles')}>
          <ArrowLeft className="h-5 w-5" />
        </Button>
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold tracking-tight">{name}</h1>
            <Badge variant="outline" className={`font-mono text-xs uppercase ${
              profile.donor_status === 'active' ? 'bg-primary/10 text-primary border-primary/20' :
              profile.donor_status === 'lapsed' ? 'bg-amber-500/10 text-amber-500 border-amber-500/20' : ''
            }`}>
              {profile.donor_status}
            </Badge>
            <Badge variant="secondary" className="font-mono text-xs text-muted-foreground">ID: {profile.id}</Badge>
          </div>
          <div className="flex items-center gap-4 mt-2 text-sm text-muted-foreground">
            {profile.email && <span className="flex items-center"><Mail className="h-3 w-3 mr-1" /> {profile.email}</span>}
            {profile.phone && <span className="flex items-center"><Phone className="h-3 w-3 mr-1" /> {profile.phone}</span>}
            {(profile.city || profile.region || profile.country) && (
              <span className="flex items-center">
                <MapPin className="h-3 w-3 mr-1" /> 
                {[profile.city, profile.region, profile.country].filter(Boolean).join(', ')}
              </span>
            )}
          </div>
        </div>
      </div>

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

        <div className="flex-1 mt-6">
          <TabsContent value="overview" className="m-0 space-y-6">
            <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm font-medium text-muted-foreground">Lifetime Value</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="text-3xl font-bold font-mono tracking-tight">
                    ${(profile.traits?.ltv_total || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  </div>
                  <p className="text-xs text-muted-foreground mt-1">Across {profile.traits?.gift_count_total || 0} gifts</p>
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm font-medium text-muted-foreground">Last Gift</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="text-3xl font-bold font-mono tracking-tight">
                    {profile.traits?.last_gift_date ? format(new Date(profile.traits.last_gift_date), 'MMM d, yyyy') : '-'}
                  </div>
                  <p className="text-xs text-muted-foreground mt-1">
                    {profile.traits?.days_since_last_gift !== undefined 
                      ? `${profile.traits.days_since_last_gift} days ago` 
                      : 'No gifts recorded'}
                  </p>
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm font-medium text-muted-foreground">RFM Score</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="text-3xl font-bold font-mono tracking-tight text-primary">
                    {profile.traits?.rfm_score || '-'}
                  </div>
                  <p className="text-xs text-muted-foreground mt-1">
                    R:{profile.traits?.rfm_recency || '-'} F:{profile.traits?.rfm_frequency || '-'} M:{profile.traits?.rfm_monetary || '-'}
                  </p>
                </CardContent>
              </Card>
            </div>
            
            <Card>
              <CardHeader>
                <CardTitle>Profile Details</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-6 text-sm">
                  <div>
                    <span className="text-muted-foreground block text-xs mb-1">First Seen</span>
                    <span className="font-medium">{profile.first_seen_at ? format(new Date(profile.first_seen_at), 'MMM d, yyyy HH:mm') : '-'}</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground block text-xs mb-1">Last Seen</span>
                    <span className="font-medium">{profile.last_seen_at ? format(new Date(profile.last_seen_at), 'MMM d, yyyy HH:mm') : '-'}</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground block text-xs mb-1">12m Gift Value</span>
                    <span className="font-mono font-medium">${(profile.traits?.gift_amount_12m || 0).toLocaleString()}</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground block text-xs mb-1">12m Gift Count</span>
                    <span className="font-medium">{profile.traits?.gift_count_12m || 0}</span>
                  </div>
                  <div className="col-span-2">
                    <span className="text-muted-foreground block text-xs mb-1">Address</span>
                    <span className="font-medium">
                      {[profile.address1, profile.city, profile.region, profile.postal_code, profile.country].filter(Boolean).join(', ') || '-'}
                    </span>
                  </div>
                  <div className="col-span-2">
                    <span className="text-muted-foreground block text-xs mb-1">Known Sources</span>
                    <div className="flex gap-2 mt-1 flex-wrap">
                      {profile.traits?.source_keys?.map((sk: string) => (
                        <Badge key={sk} variant="outline" className="font-mono text-[10px]">{sk}</Badge>
                      )) || '-'}
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="gifts" className="m-0 h-full">
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
                            {gift.gift_date ? format(new Date(gift.gift_date), 'MMM d, yyyy') : '-'}
                          </TableCell>
                          <TableCell className="text-right font-mono font-medium text-emerald-500">
                            {gift.amount ? `$${gift.amount.toLocaleString(undefined, {minimumFractionDigits: 2})}` : '-'}
                            {gift.is_recurring && <Badge variant="outline" className="ml-2 text-[9px] uppercase">Recurring</Badge>}
                          </TableCell>
                          <TableCell className="text-xs">{gift.fund || '-'}</TableCell>
                          <TableCell className="text-xs text-muted-foreground">
                            {[gift.campaign, gift.appeal_code].filter(Boolean).join(' / ') || '-'}
                          </TableCell>
                          <TableCell className="text-xs uppercase">{gift.channel || '-'}</TableCell>
                          <TableCell>
                            <Badge variant="secondary" className="font-mono text-[10px]">{gift.source_key}</Badge>
                          </TableCell>
                        </TableRow>
                      ))
                    )}
                  </TableBody>
                </Table>
              </div>
            </Card>
          </TabsContent>

          <TabsContent value="activity" className="m-0 h-full">
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
                            {event.occurred_at ? format(new Date(event.occurred_at), 'MMM d, yyyy HH:mm:ss') : '-'}
                          </TableCell>
                          <TableCell>
                            <Badge variant="outline" className="font-mono text-[10px] uppercase">{event.type}</Badge>
                          </TableCell>
                          <TableCell className="font-medium text-sm">{event.name}</TableCell>
                          <TableCell>
                            <Badge variant="secondary" className="font-mono text-[10px]">{event.source_key}</Badge>
                          </TableCell>
                        </TableRow>
                      ))
                    )}
                  </TableBody>
                </Table>
              </div>
            </Card>
          </TabsContent>

          <TabsContent value="identity" className="m-0 space-y-6">
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
                            <TableCell className="text-xs font-mono uppercase">{ident.type}</TableCell>
                            <TableCell className="font-medium text-sm">{ident.value}</TableCell>
                            <TableCell className="text-right text-xs text-muted-foreground">
                              {ident.first_seen_at ? format(new Date(ident.first_seen_at), 'MMM d, yy') : '-'}
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
                              {merge.merged_at ? format(new Date(merge.merged_at), 'MMM d, yy HH:mm') : '-'}
                            </TableCell>
                            <TableCell className="font-mono text-xs">
                              ID: {merge.loser_id}
                            </TableCell>
                            <TableCell className="text-xs capitalize">{merge.reason?.replace('_', ' ')}</TableCell>
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
                            <Badge variant="secondary" className="font-mono text-[10px]">{sr.source_key}</Badge>
                          </TableCell>
                          <TableCell className="font-mono text-xs text-muted-foreground">{sr.external_id || '-'}</TableCell>
                          <TableCell className="text-sm">
                            {[sr.first_name, sr.last_name].filter(Boolean).join(' ') || '-'}
                          </TableCell>
                          <TableCell className="text-xs">
                            <div className="flex flex-col">
                              {sr.email && <span>{sr.email}</span>}
                              {sr.phone && <span className="text-muted-foreground">{sr.phone}</span>}
                            </div>
                          </TableCell>
                          <TableCell className="text-right text-xs text-muted-foreground">
                            {sr.updated_at ? format(new Date(sr.updated_at), 'MMM d, yyyy') : '-'}
                          </TableCell>
                        </TableRow>
                      ))
                    )}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="enrichment" className="m-0">
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
                      <TableHead className="text-right">Updated At</TableHead>
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
                            <Badge variant="outline" className="font-mono text-[10px]">{enr.source_key}</Badge>
                          </TableCell>
                          <TableCell className="font-medium text-sm">{enr.attribute_key}</TableCell>
                          <TableCell className="font-mono text-xs">
                            {enr.value_text !== null ? enr.value_text :
                             enr.value_num !== null ? enr.value_num :
                             enr.value_bool !== null ? (enr.value_bool ? 'TRUE' : 'FALSE') :
                             enr.value_date !== null ? enr.value_date : '-'}
                          </TableCell>
                          <TableCell className="text-right text-xs text-muted-foreground">
                            {enr.imported_at ? format(new Date(enr.imported_at), 'MMM d, yy') : '-'}
                          </TableCell>
                        </TableRow>
                      ))
                    )}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>
          </TabsContent>
          
          <TabsContent value="consents" className="m-0">
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
                          <TableCell className="font-medium uppercase text-xs">{consent.channel}</TableCell>
                          <TableCell>
                            <Badge variant="outline" className={`font-mono text-[10px] ${
                              consent.status === 'opt_in' ? 'text-emerald-500 border-emerald-500/20 bg-emerald-500/10' :
                              consent.status === 'opt_out' ? 'text-destructive border-destructive/20 bg-destructive/10' : ''
                            }`}>
                              {consent.status}
                            </Badge>
                          </TableCell>
                          <TableCell>
                            <Badge variant="secondary" className="font-mono text-[10px]">{consent.source_key}</Badge>
                          </TableCell>
                          <TableCell className="text-right text-xs text-muted-foreground">
                            {consent.captured_at ? format(new Date(consent.captured_at), 'MMM d, yy HH:mm') : '-'}
                          </TableCell>
                        </TableRow>
                      ))
                    )}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>
          </TabsContent>
        </div>
      </Tabs>
    </div>
  );
}