import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/chat/")({
  component: ChatPage,
});

function ChatPage() {
  return (
    <section aria-labelledby="chat-heading" className="mx-auto flex max-w-3xl flex-col gap-6">
      <header>
        <h1 id="chat-heading" className="text-3xl font-semibold tracking-tight">
          Chat
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Interaction conversationnelle avec les agents — streaming SSE, slash commands, validation inline.
          {" "}
          <span className="font-mono text-xs">[implémenté Epic 6]</span>
        </p>
      </header>

      <div className="rounded-lg border border-border bg-card p-6">
        <p className="text-sm text-muted-foreground">
          Décris ta tâche, ou utilise une slash command.
        </p>
      </div>
    </section>
  );
}
