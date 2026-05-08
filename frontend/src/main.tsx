import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createRouter, RouterProvider } from "@tanstack/react-router";
import { ErrorBoundary } from "@/app/ErrorBoundary";
import { Providers } from "@/app/providers";
import { routeTree } from "@/app/routeTree.gen";

// Self-hosted Geist Sans + Mono (UX-DR4). `@fontsource` bundles woff2 files
// into dist/assets/ at build time and declares @font-face with
// `font-display: swap` — no FOUT on reload, no Google Fonts CDN dependency.
import "@fontsource/geist-sans/400.css";
import "@fontsource/geist-sans/500.css";
import "@fontsource/geist-sans/600.css";
import "@fontsource/geist-sans/700.css";
import "@fontsource/geist-mono/400.css";
import "@fontsource/geist-mono/500.css";

import "@/styles/globals.css";

const router = createRouter({
  routeTree,
  // `intent` preload is nice for perceived perf, but combined with a 0 staleTime
  // it floods the backend on every pointer hover. We keep `intent` but use a
  // sensible staleTime (30s) so repeated hovers don't re-fetch.
  defaultPreload: "intent",
  defaultPreloadStaleTime: 30_000,
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Root element #root not found");
}

createRoot(rootElement, {
  // React 19 callbacks for errors outside the boundary
  // (async / effect / event handlers). Logged for now; Story 1.9 wires them
  // into structlog + Sentry.
  onUncaughtError: (error, errorInfo) => {
    console.error("[uncaught]", error, errorInfo);
  },
  onCaughtError: (error, errorInfo) => {
    console.error("[caught]", error, errorInfo);
  },
}).render(
  <StrictMode>
    <ErrorBoundary>
      <Providers>
        <RouterProvider router={router} />
      </Providers>
    </ErrorBoundary>
  </StrictMode>,
);
