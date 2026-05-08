/**
 * Icon mapping — `icon_name` (YAML slug) → lucide-react component.
 *
 * Tree-shakeable: explicit imports per icon (vs. dynamic `lucide-react/dynamicIconImports`).
 * Adding a new archetype: add its lucide-react icon import + entry below.
 */

import {
  BarChart,
  Compass,
  Eye,
  type LucideIcon,
  MessageSquare,
  Search,
  ShieldCheck,
  Target,
  Wrench,
} from "lucide-react";

const ICONS: Record<string, LucideIcon> = {
  compass: Compass,
  search: Search,
  "bar-chart": BarChart,
  wrench: Wrench,
  target: Target,
  "shield-check": ShieldCheck,
  eye: Eye,
  "message-square": MessageSquare,
};

/** Resolve an `icon_name` slug to a lucide-react component. Falls back to
 *  ``Compass`` if the slug is unknown — better than a missing icon. */
export function getArchetypeIcon(iconName: string): LucideIcon {
  return ICONS[iconName] ?? Compass;
}
