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
    },
    languageOptions: {
      // Match TS target (ES2023) — allows top-level await, class fields, `.at()`.
      ecmaVersion: 2023,
      globals: globals.browser,
    },
    rules: {
      // ─── Fast-refresh allowlist ───
      // `Route` = TanStack Router file-route export (data, not a component).
      // `buttonVariants` = shadcn/ui cva variants co-located with the component.
      "react-refresh/only-export-components": [
        "error",
        { allowExportNames: ["Route", "buttonVariants"] },
      ],
      // ─── Layered architecture: app → features → shared ───
      "boundaries/element-types": [
        "error",
        {
          default: "disallow",
          rules: [
            // `app` can import from anywhere (bootstrap layer).
            { from: "app", allow: ["app", "feature", "shared", "styles"] },
            // `features` can import `shared` — NEVER another feature.
            { from: "feature", allow: ["shared", "styles"] },
            // `shared` is a leaf — only other `shared` modules.
            { from: "shared", allow: ["shared"] },
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
