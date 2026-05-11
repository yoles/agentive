import { createFileRoute, Link } from "@tanstack/react-router";

export const Route = createFileRoute("/config/playground/")({
  component: PlaygroundIndex,
});

export function PlaygroundIndex() {
  return (
    <section className="container mx-auto flex max-w-3xl flex-col gap-4 py-6">
      <h1 className="text-3xl font-semibold tracking-tight">Playground</h1>
      <p className="text-sm text-muted-foreground">
        Choisissez un agent-template à tester depuis la liste de configuration.
      </p>
      <Link
        to="/config"
        className="text-sm text-primary underline-offset-4 hover:underline"
      >
        ← Configuration
      </Link>
    </section>
  );
}
