import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import jsxA11y from "eslint-plugin-jsx-a11y";
import boundaries from "eslint-plugin-boundaries";
import tseslint from "typescript-eslint";
import { defineConfig, globalIgnores } from "eslint/config";

export default defineConfig([
  globalIgnores(["dist", "src/app/routeTree.gen.ts"]),
  {
    files: ["**/*.{ts,tsx}"],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
      jsxA11y.flatConfigs.recommended,
    ],
    plugins: {
      boundaries,
    },
    settings: {
      // Feature-based isolation — enforced by `eslint-plugin-boundaries`.
      // Source : _bmad-output/planning-artifacts/architecture.md § "Architectural
      // Boundaries — Feature-Based Rules".
      "boundaries/elements": [
        { type: "app", pattern: "src/app/**" },
        { type: "feature", pattern: "src/features/*/**", capture: ["feature"] },
        { type: "shared", pattern: "src/shared/**" },
        { type: "styles", pattern: "src/styles/**" },
      ],
      "boundaries/ignore": ["**/*.test.{ts,tsx}"],
      // Required so `boundaries/dependencies` can resolve `@/...` aliases (vite-tsconfig-paths)
      // and bare-relative imports back to their element type. Without this, the rule
      // is silent on violations because imports can't be mapped to elements.
      "import/resolver": {
        typescript: {
          alwaysTryTypes: true,
          project: "./tsconfig.json",
        },
      },
    },
    languageOptions: {
      // Match TS target (ES2023) — allows top-level await, class fields, `.at()`.
      ecmaVersion: 2023,
      globals: globals.browser,
    },
    rules: {
      // ─── Fast-refresh allowlist ───
      // `Route` = TanStack Router file-route export (data, not a component).
      // `*Variants` = shadcn/ui cva variants co-located with their component
      // (canonical pattern preserved when running `npx shadcn add`).
      "react-refresh/only-export-components": [
        "error",
        {
          allowExportNames: [
            "Route",
            "buttonVariants",
            "badgeVariants",
            "tabsListVariants",
          ],
        },
      ],
      // ─── Layered architecture: app → features → shared ───
      // Migrated to v6 `boundaries/dependencies` (renamed from `element-types`)
      // with object-based selectors. Cf. v5→v6 migration guide.
      "boundaries/dependencies": [
        "error",
        {
          default: "disallow",
          rules: [
            // `app` can import from anywhere (bootstrap layer).
            {
              from: { type: "app" },
              allow: [
                { to: { type: "app" } },
                { to: { type: "feature" } },
                { to: { type: "shared" } },
                { to: { type: "styles" } },
              ],
            },
            // `features` can import `shared`, `styles`, and OWN feature files
            // (barrel `index.ts` reexports). NEVER another feature.
            {
              from: { type: "feature" },
              allow: [
                {
                  to: {
                    type: "feature",
                    captured: { feature: "{{ from.captured.feature }}" },
                  },
                },
                { to: { type: "shared" } },
                { to: { type: "styles" } },
              ],
            },
            // `shared` is a leaf — only other `shared` modules.
            {
              from: { type: "shared" },
              allow: [{ to: { type: "shared" } }],
            },
          ],
        },
      ],
      // ─── Accessibility (WCAG 2.1 AA — UX-DR37 to UX-DR44) ───
      "jsx-a11y/no-autofocus": "warn",
      "jsx-a11y/click-events-have-key-events": "error",
      "jsx-a11y/no-static-element-interactions": "error",
    },
  },
]);
