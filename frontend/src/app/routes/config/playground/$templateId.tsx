import { createFileRoute } from "@tanstack/react-router";
import { PlaygroundPage } from "@/features/playground";

export const Route = createFileRoute("/config/playground/$templateId")({
  component: PlaygroundRouteComponent,
});

export function PlaygroundRouteComponent() {
  const { templateId } = Route.useParams();
  return <PlaygroundPage templateId={templateId} />;
}
