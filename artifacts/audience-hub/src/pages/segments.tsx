import { Link, useLocation, useParams } from 'react-router-dom';
import { SegmentBuilder } from '@/components/segments/builder';
import { SegmentDetail } from '@/components/segments/detail';
import { SegmentList } from '@/components/segments/list';

/** Routes: /segments (list), /segments/new (builder), /segments/:id (detail), /segments/:id/edit (builder). */
export default function Segments() {
  const { id } = useParams();
  const { pathname } = useLocation();
  if (pathname.replace(/\/$/, '').endsWith('/segments/new')) return <SegmentBuilder key="new" />;
  if (!id) return <SegmentList />;
  const num = Number(id);
  if (!Number.isSafeInteger(num) || num < 1) return <div className="p-6 text-sm text-ink-muted" data-testid="error-segment-id">Audience not found. <Link to="/segments" className="text-signal">Back to audiences</Link></div>;
  if (pathname.replace(/\/$/, '').endsWith('/edit')) return <SegmentBuilder key={`edit-${num}`} id={num} />;
  return <SegmentDetail key={num} id={num} />;
}
