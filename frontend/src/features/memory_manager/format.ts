/**
 * Presentation helpers for the namespaces listing — Story 3.2 AC3.
 *
 * Pure functions, no React — testable without rendering a component.
 */

import type { NamespaceListItem } from "./types";

const SECONDS_PER_MINUTE = 60;
const SECONDS_PER_HOUR = 3_600;
const SECONDS_PER_DAY = 86_400;

/** `604_800` → `"7 jours"`, `null` → `"illimité"`.
 *
 * The API accepts any `default_ttl_seconds >= 0` (a namespace can legitimately
 * be created with a sub-day TTL), so anything below one day gets its own
 * unit instead of rounding down to "0 jour" (code review Story 3.2, P5). */
export function formatRetention(seconds: number | null): string {
  if (seconds === null) return "illimité";
  if (seconds < SECONDS_PER_MINUTE) {
    return `${seconds} seconde${seconds > 1 ? "s" : ""}`;
  }
  if (seconds < SECONDS_PER_HOUR) {
    const minutes = Math.round(seconds / SECONDS_PER_MINUTE);
    return `${minutes} minute${minutes > 1 ? "s" : ""}`;
  }
  if (seconds < SECONDS_PER_DAY) {
    const hours = Math.round(seconds / SECONDS_PER_HOUR);
    return `${hours} heure${hours > 1 ? "s" : ""}`;
  }
  const days = Math.round(seconds / SECONDS_PER_DAY);
  return `${days} jour${days > 1 ? "s" : ""}`;
}

export type NamespaceGroup = {
  // `null` means "no department" — kept as `null`, never collapsed into a
  // display string here, so a real department cannot collide with the
  // "no department" bucket by being named the same thing (code review
  // Story 3.2, P9). Use `departmentLabel` to render it.
  department: string | null;
  types: { type: string; namespaces: NamespaceListItem[] }[];
};

/** `null` → `"(sans département)"`, anything else unchanged. The one place

 * that turns "no department" into display text — keeping the conversion
 * out of `groupByDepartmentThenType` is what prevents a real department
 * literally named `"(sans département)"` (or an empty string) from
 * merging into the "no department" group (code review Story 3.2, P9). */
export function departmentLabel(department: string | null): string {
  return department ?? "(sans département)";
}

/** Groups namespaces by department, then by type within each department —
 * a frontend presentation detail (Story 3.2 AC3), the API stays a flat
 * list. Departments/types are sorted alphabetically for a stable render;
 * "no department" always sorts last. Namespaces within a type are
 * sorted by name too — the API's own order is stable but insertion order
 * within a group isn't a meaningful sort key for an admin table (code
 * review Story 3.2, P6). */
export function groupByDepartmentThenType(namespaces: NamespaceListItem[]): NamespaceGroup[] {
  const byDepartment = new Map<string | null, NamespaceListItem[]>();
  for (const ns of namespaces) {
    // A blank/whitespace-only department is rejected on write since the
    // P4 fix, but pre-existing rows may still carry one — fold it into
    // "no department" rather than a separate, invisible-looking group.
    const key = ns.department?.trim() ? ns.department : null;
    const bucket = byDepartment.get(key);
    if (bucket) bucket.push(ns);
    else byDepartment.set(key, [ns]);
  }

  const departments = [...byDepartment.keys()].sort((a, b) => {
    if (a === null) return 1;
    if (b === null) return -1;
    return a.localeCompare(b);
  });

  return departments.map((department) => {
    const nsInDept = byDepartment.get(department) ?? [];
    const byType = new Map<string, NamespaceListItem[]>();
    for (const ns of nsInDept) {
      const bucket = byType.get(ns.type);
      if (bucket) bucket.push(ns);
      else byType.set(ns.type, [ns]);
    }
    const types = [...byType.keys()]
      .sort((a, b) => a.localeCompare(b))
      .map((type) => ({
        type,
        namespaces: [...(byType.get(type) ?? [])].sort((a, b) => a.name.localeCompare(b.name)),
      }));
    return { department, types };
  });
}
