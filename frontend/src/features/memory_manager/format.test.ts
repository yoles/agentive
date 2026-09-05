/**
 * Pure-function tests — namespaces listing presentation helpers (Story 3.2 AC3).
 */

import { describe, expect, it } from "vitest";
import { departmentLabel, formatRetention, groupByDepartmentThenType } from "./format";
import type { NamespaceListItem } from "./types";

function makeNamespace(overrides: Partial<NamespaceListItem>): NamespaceListItem {
  return {
    namespace_id: "11111111-1111-1111-1111-111111111111",
    name: "ns",
    type: "metier",
    department: null,
    project: null,
    retention_policy: { default_ttl_seconds: null, archive_after_seconds: null },
    retention_policy_valid: true,
    embedding_backend: "cloud",
    chunk_count: 0,
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

describe("formatRetention", () => {
  it("returns 'illimité' for null", () => {
    expect(formatRetention(null)).toBe("illimité");
  });

  it("formats 604800 seconds as '7 jours'", () => {
    expect(formatRetention(604_800)).toBe("7 jours");
  });

  it("formats 86400 seconds as '1 jour' (singular)", () => {
    expect(formatRetention(86_400)).toBe("1 jour");
  });

  it("formats 31536000 seconds as '365 jours'", () => {
    expect(formatRetention(31_536_000)).toBe("365 jours");
  });

  // Code review Story 3.2, P5 — every sub-day TTL used to round down to
  // "0 jour", indistinguishable from a misconfigured namespace.
  it("formats 0 seconds as '0 seconde'", () => {
    expect(formatRetention(0)).toBe("0 seconde");
  });

  it("formats 1 second as '1 seconde' (singular)", () => {
    expect(formatRetention(1)).toBe("1 seconde");
  });

  it("formats 45 seconds as '45 secondes'", () => {
    expect(formatRetention(45)).toBe("45 secondes");
  });

  it("formats 120 seconds as '2 minutes'", () => {
    expect(formatRetention(120)).toBe("2 minutes");
  });

  it("formats 3600 seconds as '1 heure'", () => {
    expect(formatRetention(3_600)).toBe("1 heure");
  });

  it("formats 7200 seconds as '2 heures'", () => {
    expect(formatRetention(7_200)).toBe("2 heures");
  });
});

describe("departmentLabel", () => {
  it("returns '(sans département)' for null", () => {
    expect(departmentLabel(null)).toBe("(sans département)");
  });

  it("passes a real department through unchanged", () => {
    expect(departmentLabel("Dev")).toBe("Dev");
  });
});

describe("groupByDepartmentThenType", () => {
  it("groups namespaces by department then type", () => {
    const namespaces = [
      makeNamespace({ name: "a", department: "Dev", type: "metier" }),
      makeNamespace({ name: "b", department: "Dev", type: "contextuelle" }),
      makeNamespace({ name: "c", department: "Design-UX", type: "metier" }),
    ];

    const groups = groupByDepartmentThenType(namespaces);

    expect(groups.map((g) => g.department)).toEqual(["Design-UX", "Dev"]);
    const dev = groups.find((g) => g.department === "Dev");
    expect(dev?.types.map((t) => t.type)).toEqual(["contextuelle", "metier"]);
  });

  it("sorts namespaces with no department last", () => {
    const namespaces = [
      makeNamespace({ name: "shared", department: null }),
      makeNamespace({ name: "dept", department: "Dev" }),
    ];

    const groups = groupByDepartmentThenType(namespaces);

    expect(groups.map((g) => g.department)).toEqual(["Dev", null]);
    expect(groups.map((g) => departmentLabel(g.department))).toEqual([
      "Dev",
      "(sans département)",
    ]);
  });

  // Code review Story 3.2, P9 — the "no department" bucket used to be a
  // literal string key, colliding with a real department of that name or
  // with a blank string. `department` now stays `null` for "no
  // department", so neither can merge into it.
  it("does not merge a real department literally named '(sans département)' into the no-department group", () => {
    const namespaces = [
      makeNamespace({ name: "shared", department: null }),
      makeNamespace({ name: "literal", department: "(sans département)" }),
    ];

    const groups = groupByDepartmentThenType(namespaces);

    expect(groups).toHaveLength(2);
    const literalGroup = groups.find((g) => g.department === "(sans département)");
    const noDeptGroup = groups.find((g) => g.department === null);
    expect(literalGroup?.types.flatMap((t) => t.namespaces).map((ns) => ns.name)).toEqual([
      "literal",
    ]);
    expect(noDeptGroup?.types.flatMap((t) => t.namespaces).map((ns) => ns.name)).toEqual([
      "shared",
    ]);
  });

  it("folds a blank department into the no-department group", () => {
    const namespaces = [
      makeNamespace({ name: "blank", department: "   " }),
      makeNamespace({ name: "none", department: null }),
    ];

    const groups = groupByDepartmentThenType(namespaces);

    expect(groups).toHaveLength(1);
    expect(groups[0]?.department).toBeNull();
    expect(groups[0]?.types.flatMap((t) => t.namespaces).map((ns) => ns.name)).toEqual([
      "blank",
      "none",
    ]);
  });

  // Code review Story 3.2, P6 — the API's own order is now stable
  // (created_at, id), but that's not a meaningful sort key for an admin
  // table; namespaces within a group should sort by name.
  it("sorts namespaces by name within a department/type group", () => {
    const namespaces = [
      makeNamespace({ name: "zebra", department: "Dev", type: "metier" }),
      makeNamespace({ name: "alpha", department: "Dev", type: "metier" }),
      makeNamespace({ name: "mid", department: "Dev", type: "metier" }),
    ];

    const groups = groupByDepartmentThenType(namespaces);

    const dev = groups.find((g) => g.department === "Dev");
    const metier = dev?.types.find((t) => t.type === "metier");
    expect(metier?.namespaces.map((ns) => ns.name)).toEqual(["alpha", "mid", "zebra"]);
  });
});
