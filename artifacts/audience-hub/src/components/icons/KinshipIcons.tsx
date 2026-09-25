import type { SVGProps } from "react";

/** Kinship navigation icons. 24px grid, 1.75 stroke, currentColor. Same API shape as lucide-react. */
export type IconProps = SVGProps<SVGSVGElement> & { size?: number | string; strokeWidth?: number | string };

function Base({ size = 16, strokeWidth = 1.75, children, ...rest }: IconProps & { children: React.ReactNode }) {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" {...rest}>
      {children}
    </svg>
  );
}

export const DashboardIcon = (p: IconProps) => <Base {...p}><rect x="3" y="3" width="8" height="10" rx="2" /><rect x="13" y="3" width="8" height="6" rx="2" /><rect x="13" y="11" width="8" height="10" rx="2" /><rect x="3" y="15" width="8" height="6" rx="2" /></Base>;
export const ProfilesIcon = (p: IconProps) => <Base {...p}><circle cx="9" cy="8" r="3.5" /><path d="M3 20.5c0-3.6 2.7-6.3 6-6.3s6 2.7 6 6.3" /><path d="M12.4 7.2l4.6-2" /><circle cx="19" cy="4.4" r="1.9" fill="currentColor" stroke="none" /></Base>;
export const SegmentsIcon = (p: IconProps) => <Base {...p}><circle cx="9" cy="12" r="6" /><circle cx="15" cy="12" r="6" /><path d="M12 6.8a6 6 0 0 1 0 10.4a6 6 0 0 1 0-10.4z" fill="currentColor" stroke="none" /></Base>;
export const ActivationsIcon = (p: IconProps) => <Base {...p}><circle cx="4.5" cy="12" r="2.3" fill="currentColor" stroke="none" /><path d="M7 12h10.5" /><path d="M6.7 10.6L16 5.2" /><path d="M6.7 13.4L16 18.8" /><circle cx="19.3" cy="12" r="1.8" /><circle cx="18" cy="4" r="1.8" /><circle cx="18" cy="20" r="1.8" /></Base>;
export const ImportsIcon = (p: IconProps) => <Base {...p}><path d="M12 3v9.5" /><path d="M8.2 9l3.8 3.8L15.8 9" /><path d="M3.5 14.5v3.5a2.5 2.5 0 0 0 2.5 2.5h12a2.5 2.5 0 0 0 2.5-2.5v-3.5" /><path d="M3.5 14.5h4.2l1.3 2h6l1.3-2h4.2" /></Base>;
export const DataHealthIcon = (p: IconProps) => <Base {...p}><path d="M12 20.3s-7.8-4.6-8.8-10A4.6 4.6 0 0 1 12 7.4a4.6 4.6 0 0 1 8.8 2.9c-1 5.4-8.8 10-8.8 10z" /><path d="M3.8 12.6h4l1.6-2.8 2.4 5 1.7-2.2h6.7" /></Base>;
export const SourcesIcon = (p: IconProps) => <Base {...p}><ellipse cx="9.5" cy="5.5" rx="6.5" ry="2.5" /><path d="M3 5.5v13c0 1.4 2.9 2.5 6.5 2.5s6.5-1.1 6.5-2.5v-13" /><path d="M3 12c0 1.4 2.9 2.5 6.5 2.5s6.5-1.1 6.5-2.5" /><path d="M16 12h3" /><circle cx="20.6" cy="12" r="1.9" fill="currentColor" stroke="none" /></Base>;
export const AdminIcon = (p: IconProps) => <Base {...p}><path d="M12 3l7.5 2.8v5.6c0 4.7-3.2 8.2-7.5 9.6c-4.3-1.4-7.5-4.9-7.5-9.6V5.8z" /><circle cx="12" cy="10.6" r="2" fill="currentColor" stroke="none" /><path d="M12 12.4v3.8" /></Base>;
export const SystemIcon = (p: IconProps) => <Base {...p}><rect x="3" y="3.5" width="18" height="7.5" rx="2" /><rect x="3" y="13" width="18" height="7.5" rx="2" /><circle cx="7" cy="7.25" r="1.3" fill="currentColor" stroke="none" /><circle cx="7" cy="16.75" r="1.3" fill="currentColor" stroke="none" /><path d="M11 7.25h6.5" /><path d="M11 16.75h6.5" /></Base>;
export const JobsIcon = (p: IconProps) => <Base {...p}><path d="M20 12a8 8 0 1 1-2.3-5.6" /><path d="M18.2 2.9v3.9h-3.9" /><circle cx="12" cy="12" r="2.4" fill="currentColor" stroke="none" /></Base>;

export const navIcons = {
  "dashboard": DashboardIcon,
  "profiles": ProfilesIcon,
  "segments": SegmentsIcon,
  "activations": ActivationsIcon,
  "imports": ImportsIcon,
  "data-health": DataHealthIcon,
  "sources": SourcesIcon,
  "admin": AdminIcon,
  "system": SystemIcon,
  "jobs": JobsIcon,
} as const;
