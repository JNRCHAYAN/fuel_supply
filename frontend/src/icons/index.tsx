import type { SVGProps } from 'react'

export interface IconProps extends SVGProps<SVGSVGElement> {
  size?: number
  /** Set when the icon is the only label for a control. */
  title?: string
}

/**
 * One wrapper for the whole set, so stroke width, cap style and sizing stay
 * consistent. Icons are decorative by default and hidden from assistive
 * technology; a control that is icon-only supplies its own aria-label.
 */
function Icon({ size = 16, title, children, ...rest }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden={title ? undefined : true}
      role={title ? 'img' : undefined}
      focusable="false"
      {...rest}
    >
      {title ? <title>{title}</title> : null}
      {children}
    </svg>
  )
}

const paths = {
  CheckCircle: <><circle cx="12" cy="12" r="9" /><path d="m8.5 12.5 2.5 2.5 4.5-5" /></>,
  AlertTriangle: <><path d="M10.3 3.9 2.6 17.2A2 2 0 0 0 4.3 20h15.4a2 2 0 0 0 1.7-2.8L13.7 3.9a2 2 0 0 0-3.4 0Z" /><path d="M12 9v4" /><path d="M12 17h.01" /></>,
  XOctagon: <><path d="M7.9 2h8.2L22 7.9v8.2L16.1 22H7.9L2 16.1V7.9Z" /><path d="m15 9-6 6" /><path d="m9 9 6 6" /></>,
  Clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  Fuel: <><path d="M3 22h12" /><path d="M5 22V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v17" /><path d="M5 12h8" /><path d="M17 8h2a2 2 0 0 1 2 2v6a1.5 1.5 0 0 0 3 0v-7l-3-4" /></>,
  Depot: <><path d="M3 21V9l9-6 9 6v12" /><path d="M9 21v-7h6v7" /><path d="M3 21h18" /></>,
  Station: <><path d="M12 21s7-5.6 7-11a7 7 0 1 0-14 0c0 5.4 7 11 7 11Z" /><circle cx="12" cy="10" r="2.5" /></>,
  Route: <><circle cx="6" cy="19" r="2.5" /><circle cx="18" cy="5" r="2.5" /><path d="M8.5 19H14a4 4 0 0 0 0-8H9a4 4 0 0 1 0-8h0" /></>,
  Activity: <path d="M3 12h4l3-8 4 16 3-8h4" />,
  Shield: <><path d="M12 22s8-3.5 8-10V5l-8-3-8 3v7c0 6.5 8 10 8 10Z" /></>,
  Gauge: <><path d="M12 21a9 9 0 1 0-9-9" /><path d="m12 12 4-4" /><circle cx="12" cy="12" r="1.5" /></>,
  Scroll: <><path d="M6 4h11a2 2 0 0 1 2 2v12a2 2 0 0 0 2 2H8a2 2 0 0 1-2-2Z" /><path d="M6 4a2 2 0 0 0-2 2v2h2" /><path d="M9 9h6" /><path d="M9 13h6" /></>,
  Play: <path d="M7 4.5v15l12-7.5Z" />,
  Pause: <><path d="M9 4.5v15" /><path d="M15 4.5v15" /></>,
  StepForward: <><path d="M5 4.5v15l10-7.5Z" /><path d="M19 4.5v15" /></>,
  RotateCcw: <><path d="M3 12a9 9 0 1 0 2.6-6.4" /><path d="M3 4v5h5" /></>,
  Sun: <><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></>,
  Moon: <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z" />,
  Wifi: <><path d="M2.5 9a15 15 0 0 1 19 0" /><path d="M5.5 12.5a11 11 0 0 1 13 0" /><path d="M8.5 16a7 7 0 0 1 7 0" /><path d="M12 19.5h.01" /></>,
  WifiOff: <><path d="m2 2 20 20" /><path d="M8.5 16a7 7 0 0 1 7 0" /><path d="M5.5 12.5a11 11 0 0 1 4-2.4" /><path d="M14.5 10.1a11 11 0 0 1 5 2.4" /><path d="M2.5 9a15 15 0 0 1 6-3.7" /><path d="M15.5 5.4A15 15 0 0 1 21.5 9" /><path d="M12 19.5h.01" /></>,
  ChevronRight: <path d="m9 5 7 7-7 7" />,
  Search: <><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></>,
  Plus: <><path d="M12 5v14" /><path d="M5 12h14" /></>,
  X: <><path d="m6 6 12 12" /><path d="m18 6-12 12" /></>,
  ArrowRight: <><path d="M4 12h16" /><path d="m14 6 6 6-6 6" /></>,
} as const

export type IconName = keyof typeof paths

export const CheckCircle = (p: IconProps) => <Icon {...p}>{paths.CheckCircle}</Icon>
export const AlertTriangle = (p: IconProps) => <Icon {...p}>{paths.AlertTriangle}</Icon>
export const XOctagon = (p: IconProps) => <Icon {...p}>{paths.XOctagon}</Icon>
export const Clock = (p: IconProps) => <Icon {...p}>{paths.Clock}</Icon>
export const Fuel = (p: IconProps) => <Icon {...p}>{paths.Fuel}</Icon>
export const Depot = (p: IconProps) => <Icon {...p}>{paths.Depot}</Icon>
export const Station = (p: IconProps) => <Icon {...p}>{paths.Station}</Icon>
export const Route = (p: IconProps) => <Icon {...p}>{paths.Route}</Icon>
export const Activity = (p: IconProps) => <Icon {...p}>{paths.Activity}</Icon>
export const Shield = (p: IconProps) => <Icon {...p}>{paths.Shield}</Icon>
export const Gauge = (p: IconProps) => <Icon {...p}>{paths.Gauge}</Icon>
export const Scroll = (p: IconProps) => <Icon {...p}>{paths.Scroll}</Icon>
export const Play = (p: IconProps) => <Icon {...p}>{paths.Play}</Icon>
export const Pause = (p: IconProps) => <Icon {...p}>{paths.Pause}</Icon>
export const StepForward = (p: IconProps) => <Icon {...p}>{paths.StepForward}</Icon>
export const RotateCcw = (p: IconProps) => <Icon {...p}>{paths.RotateCcw}</Icon>
export const Sun = (p: IconProps) => <Icon {...p}>{paths.Sun}</Icon>
export const Moon = (p: IconProps) => <Icon {...p}>{paths.Moon}</Icon>
export const Wifi = (p: IconProps) => <Icon {...p}>{paths.Wifi}</Icon>
export const WifiOff = (p: IconProps) => <Icon {...p}>{paths.WifiOff}</Icon>
export const ChevronRight = (p: IconProps) => <Icon {...p}>{paths.ChevronRight}</Icon>
export const Search = (p: IconProps) => <Icon {...p}>{paths.Search}</Icon>
export const Plus = (p: IconProps) => <Icon {...p}>{paths.Plus}</Icon>
export const X = (p: IconProps) => <Icon {...p}>{paths.X}</Icon>
export const ArrowRight = (p: IconProps) => <Icon {...p}>{paths.ArrowRight}</Icon>
