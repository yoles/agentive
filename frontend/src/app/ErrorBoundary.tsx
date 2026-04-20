/**
 * Root ErrorBoundary — prevents the whole app from unmounting on a render error.
 *
 * React 19 no longer guarantees that `getDerivedStateFromError` + `componentDidCatch`
 * capture async / effect errors — for those we rely on `onUncaughtError` and
 * `onCaughtError` options in `createRoot` (see `main.tsx`). This boundary catches
 * synchronous render errors and shows a recovery UI.
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

type Props = {
  children: ReactNode;
};

type State = {
  error: Error | null;
};

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // In dev, bubble to console with full stack. In prod, later stories will
    // forward to structlog via the correlation_id channel (Story 1.9 + Sentry
    // if plugged in).
    // eslint-disable-next-line no-console
    console.error("ErrorBoundary caught", error, info.componentStack);
  }

  handleReset = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    if (this.state.error !== null) {
      return (
        <div
          role="alert"
          className="flex min-h-screen flex-col items-center justify-center gap-4 p-8 text-foreground"
        >
          <h1 className="text-3xl font-semibold">Something went wrong</h1>
          <p className="max-w-prose text-center text-sm text-muted-foreground">
            The application encountered an unexpected error. Try reloading — if
            it persists, contact the maintainer with the correlation ID from
            your last successful request.
          </p>
          <pre className="max-w-prose overflow-auto rounded-md border border-border bg-card p-3 font-mono text-xs text-muted-foreground">
            {this.state.error.message}
          </pre>
          <button
            type="button"
            onClick={this.handleReset}
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
          >
            Dismiss &amp; retry
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
