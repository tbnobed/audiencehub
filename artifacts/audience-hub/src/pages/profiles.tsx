import { useState, useEffect } from 'react';
import { Search, Filter, Mail, Phone, MapPin, Loader2, Users } from 'lucide-react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { useProfiles } from '@/hooks/use-profiles';
import { useSources } from '@/hooks/use-sources';
import { format } from 'date-fns';

export default function Profiles() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { data: sourcesData } = useSources();
  const sources = sourcesData?.items || [];

  const [searchInput, setSearchInput] = useState(searchParams.get('search') || '');
  const [debouncedSearch, setDebouncedSearch] = useState(searchInput);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(searchInput), 500);
    return () => clearTimeout(timer);
  }, [searchInput]);

  useEffect(() => {
    if (debouncedSearch !== (searchParams.get('search') || '')) {
      const newParams = new URLSearchParams(searchParams);
      if (debouncedSearch) {
        newParams.set('search', debouncedSearch);
        newParams.set('page', '1');
      } else {
        newParams.delete('search');
      }
      setSearchParams(newParams, { replace: true });
    }
  }, [debouncedSearch, searchParams, setSearchParams]);

  const updateFilter = (key: string, value: string) => {
    const newParams = new URLSearchParams(searchParams);
    if (value && value !== 'all') {
      newParams.set(key, value);
    } else {
      newParams.delete(key);
    }
    newParams.set('page', '1');
    setSearchParams(newParams, { replace: true });
  };

  const page = parseInt(searchParams.get('page') || '1', 10);
  const pageSize = 50;

  const { data, isLoading } = useProfiles({
    search: debouncedSearch,
    source_id: searchParams.get('source_id') || undefined,
    has_email: searchParams.get('has_email') || undefined,
    has_phone: searchParams.get('has_phone') || undefined,
    donor_status: searchParams.get('donor_status') || undefined,
    page,
    page_size: pageSize,
  });

  return (
    <div className="h-full flex flex-col space-y-6 p-8 animate-in fade-in duration-500 max-w-[1400px] mx-auto">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Identity & Profiles</h1>
        <p className="text-muted-foreground text-sm mt-1">
          Unified customer profiles, identity resolution, and master records.
        </p>
      </div>

      <div className="flex flex-col md:flex-row gap-4">
        <div className="relative flex-1">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            type="search"
            placeholder="Search profiles by name, email, phone..."
            className="pl-9"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
          />
        </div>
        <div className="flex flex-wrap gap-2">
          <Select value={searchParams.get('donor_status') || 'all'} onValueChange={(v) => updateFilter('donor_status', v)}>
            <SelectTrigger className="w-[160px]">
              <SelectValue placeholder="Donor Status" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Any Status</SelectItem>
              <SelectItem value="prospect">Prospect</SelectItem>
              <SelectItem value="active">Active Donor</SelectItem>
              <SelectItem value="lapsed">Lapsed Donor</SelectItem>
            </SelectContent>
          </Select>

          <Select value={searchParams.get('source_id') || 'all'} onValueChange={(v) => updateFilter('source_id', v)}>
            <SelectTrigger className="w-[160px]">
              <SelectValue placeholder="Data Source" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Any Source</SelectItem>
              {sources.map(s => (
                <SelectItem key={s.id} value={s.id.toString()}>{s.name}</SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={searchParams.get('has_email') || 'all'} onValueChange={(v) => updateFilter('has_email', v)}>
            <SelectTrigger className="w-[140px]">
              <SelectValue placeholder="Email Status" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Email: Any</SelectItem>
              <SelectItem value="true">Has Email</SelectItem>
              <SelectItem value="false">No Email</SelectItem>
            </SelectContent>
          </Select>

          <Select value={searchParams.get('has_phone') || 'all'} onValueChange={(v) => updateFilter('has_phone', v)}>
            <SelectTrigger className="w-[140px]">
              <SelectValue placeholder="Phone Status" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Phone: Any</SelectItem>
              <SelectItem value="true">Has Phone</SelectItem>
              <SelectItem value="false">No Phone</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      <div className="border rounded-md bg-card flex-1 flex flex-col">
        <div className="overflow-auto flex-1">
          <Table>
            <TableHeader className="bg-muted/50 sticky top-0 z-10">
              <TableRow>
                <TableHead className="w-[80px]">ID</TableHead>
                <TableHead>Name</TableHead>
                <TableHead>Contact</TableHead>
                <TableHead>Location</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Last Seen</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {isLoading ? (
                <TableRow>
                  <TableCell colSpan={6} className="h-64 text-center">
                    <div className="flex flex-col items-center justify-center text-muted-foreground">
                      <Loader2 className="h-8 w-8 animate-spin mb-4" />
                      Loading profiles...
                    </div>
                  </TableCell>
                </TableRow>
              ) : data?.items.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6} className="h-64 text-center text-muted-foreground">
                    <div className="flex flex-col items-center justify-center">
                      <Users className="h-10 w-10 text-muted-foreground/30 mb-4" />
                      No profiles match the current filters.
                    </div>
                  </TableCell>
                </TableRow>
              ) : (
                data?.items.map(profile => (
                  <TableRow
                    key={profile.id}
                    className="cursor-pointer hover:bg-muted/50 transition-colors"
                    onClick={() => navigate(`/profiles/${profile.id}`)}
                  >
                    <TableCell className="font-mono text-xs text-muted-foreground">{profile.id}</TableCell>
                    <TableCell className="font-medium">
                      {profile.first_name || profile.last_name
                        ? `${profile.first_name || ''} ${profile.last_name || ''}`.trim()
                        : <span className="text-muted-foreground italic">Unnamed Profile</span>}
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-col gap-1">
                        {profile.email && (
                          <div className="flex items-center text-xs text-muted-foreground">
                            <Mail className="h-3 w-3 mr-1.5" />
                            {profile.email}
                          </div>
                        )}
                        {profile.phone && (
                          <div className="flex items-center text-xs text-muted-foreground">
                            <Phone className="h-3 w-3 mr-1.5" />
                            {profile.phone}
                          </div>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center text-xs text-muted-foreground">
                        {profile.city || profile.region || profile.country ? (
                          <>
                            <MapPin className="h-3 w-3 mr-1.5" />
                            {[profile.city, profile.region, profile.country].filter(Boolean).join(', ')}
                          </>
                        ) : '-'}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline" className={`text-[10px] uppercase font-mono ${
                        profile.donor_status === 'active' ? 'bg-primary/10 text-primary border-primary/20' :
                        profile.donor_status === 'lapsed' ? 'bg-amber-500/10 text-amber-500 border-amber-500/20' : ''
                      }`}>
                        {profile.donor_status}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right text-xs text-muted-foreground">
                      {profile.last_seen_at ? format(new Date(profile.last_seen_at), 'MMM d, yyyy') : '-'}
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </div>

        {data && data.total > 0 && (
          <div className="border-t p-4 flex items-center justify-between text-sm bg-muted/20">
            <div className="text-muted-foreground">
              Showing <span className="font-medium text-foreground">{(page - 1) * pageSize + 1}</span> to <span className="font-medium text-foreground">{Math.min(page * pageSize, data.total)}</span> of <span className="font-medium text-foreground">{data.total}</span> profiles
            </div>
            <div className="flex items-center space-x-2">
              <Button
                variant="outline"
                size="sm"
                disabled={page === 1}
                onClick={() => updateFilter('page', (page - 1).toString())}
              >
                Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={page * pageSize >= data.total}
                onClick={() => updateFilter('page', (page + 1).toString())}
              >
                Next
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}