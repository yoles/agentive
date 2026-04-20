import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createRouter, RouterProvider } from "@tanstack/react-router";
import { ErrorBoundary } from "@/app/ErrorBoundary";
import { Providers } from "@/app/providers";
import { routeTree } from "@/app/routeTree.gen";
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
