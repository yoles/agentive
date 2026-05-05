import { createRootRoute } from "@tanstack/react-router";
import { AppLayout } from "@/shared/components/layouts/AppLayout";
import { ModeToggle } from "@/features/theme";

export const Route = createRootRoute({
  component: () => (
    <AppLayout
      sidebarFooterSlot={<ModeToggle />}
      sidebarFooterSlotTooltip="Basculer le thème"
    />
  ),
});
