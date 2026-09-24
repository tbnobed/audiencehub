import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchApi } from '@/lib/api';

export type ProfileItem = {
  id: number;
  first_name: string | null;
  last_name: string | null;
  email: string | null;
  phone: string | null;
  city: string | null;
  region: string | null;
  country: string | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  donor_status: string;
};

export type ProfileDetail = ProfileItem & {
  merged_into_id: number | null;
  address1: string | null;
  postal_code: string | null;
  traits: Record<string, any>;
  gifts: any[];
  events: any[];
  identifiers: any[];
  source_records: any[];
  merges: any[];
  enrichment: any[];
  consents: any[];
};

export function useProfiles(params: {
  search?: string;
  source_id?: string;
  has_email?: string;
  has_phone?: string;
  donor_status?: string;
  page?: number;
  page_size?: number;
}) {
  return useQuery({
    queryKey: ['profiles', params],
    queryFn: async () => {
      const searchParams = new URLSearchParams();
      if (params.search) searchParams.set('search', params.search);
      if (params.source_id && params.source_id !== 'all') searchParams.set('source_id', params.source_id);
      if (params.has_email && params.has_email !== 'all') searchParams.set('has_email', params.has_email === 'true' ? 'true' : 'false');
      if (params.has_phone && params.has_phone !== 'all') searchParams.set('has_phone', params.has_phone === 'true' ? 'true' : 'false');
      if (params.donor_status && params.donor_status !== 'all') searchParams.set('donor_status', params.donor_status);
      if (params.page) searchParams.set('page', params.page.toString());
      if (params.page_size) searchParams.set('page_size', params.page_size.toString());
      
      const queryStr = searchParams.toString();
      return fetchApi(`/api/profiles${queryStr ? `?${queryStr}` : ''}`) as Promise<{
        items: ProfileItem[];
        total: number;
        page: number;
        page_size: number;
      }>;
    },
  });
}

export function useProfile(id: number) {
  return useQuery({
    queryKey: ['profiles', id],
    queryFn: () => fetchApi(`/api/profiles/${id}`) as Promise<ProfileDetail>,
    enabled: !!id,
  });
}
