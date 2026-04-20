import { createRootRoute } from "@tanstack/react-router";
import { AppLayout } from "@/shared/components/layouts/AppLayout";

export const Route = createRootRoute({
  component: AppLayout,
});
