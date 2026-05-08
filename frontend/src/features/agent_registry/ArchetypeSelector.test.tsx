/**
 * `ArchetypeSelector` tests — Story 2.1 T5.5.
 *
 * Verifies :
 *  - 8 cards rendered with display_name + description.
 *  - click selection invokes onChange.
 *  - keyboard selection (Enter / Space) invokes onChange.
 *  - selected card has the `ring-2 ring-primary` class.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ArchetypeSelector } from "./ArchetypeSelector";
import type { ArchetypeSummary } from "./types";

const FIXTURE_ARCHETYPES: ArchetypeSummary[] = [
  {
    id: "orchestrateur",
    display_name: "Orchestrateur",
    icon_name: "compass",
    description: "Coordonne plusieurs agents.",
    default_role: "orchestrator",
  },
  {
    id: "chercheur",
    display_name: "Chercheur",
    icon_name: "search",
    description: "Explore les sources.",
    default_role: "researcher",
  },
  {
    id: "analyste",
    display_name: "Analyste",
    icon_name: "bar-chart",
    description: "Synthétise les findings.",
    default_role: "analyst",
  },
  {
    id: "producteur",
    display_name: "Producteur",
    icon_name: "wrench",
    description: "Génère le livrable.",
    default_role: "producer",
  },
  {
    id: "stratege",
    display_name: "Stratège",
    icon_name: "target",
    description: "Définit la direction.",
    default_role: "strategist",
  },
  {
    id: "controleur",
    display_name: "Contrôleur",
    icon_name: "shield-check",
    description: "Review un livrable.",
    default_role: "controller",
  },
  {
    id: "veilleur",
    display_name: "Veilleur",
    icon_name: "eye",
    description: "Surveille un signal.",
    default_role: "watcher",
  },
  {
    id: "communicateur",
    display_name: "Communicateur",
    icon_name: "message-square",
    description: "Adapte un message.",
    default_role: "communicator",
  },
];

describe("ArchetypeSelector", () => {
  afterEach(cleanup);

  it("renders the 8 archetypes as radiogroup options", () => {
    render(
      <ArchetypeSelector
        archetypes={FIXTURE_ARCHETYPES}
        value={null}
        onChange={() => {}}
      />,
    );
    expect(screen.getByRole("radiogroup")).toBeInTheDocument();
    expect(screen.getAllByRole("radio")).toHaveLength(8);
    expect(screen.getByText("Orchestrateur")).toBeInTheDocument();
    expect(screen.getByText("Communicateur")).toBeInTheDocument();
  });

  it("invokes onChange with the archetype id when a card is clicked", () => {
    const onChange = vi.fn();
    render(
      <ArchetypeSelector
        archetypes={FIXTURE_ARCHETYPES}
        value={null}
        onChange={onChange}
      />,
    );
    const producerCard = screen.getByTestId("archetype-card-producteur");
    fireEvent.click(producerCard);
    expect(onChange).toHaveBeenCalledWith("producteur");
  });

  it("invokes onChange when Enter or Space is pressed on a focused card", () => {
    const onChange = vi.fn();
    render(
      <ArchetypeSelector
        archetypes={FIXTURE_ARCHETYPES}
        value={null}
        onChange={onChange}
      />,
    );
    const card = screen.getByTestId("archetype-card-controleur");
    fireEvent.keyDown(card, { key: "Enter" });
    expect(onChange).toHaveBeenLastCalledWith("controleur");

    fireEvent.keyDown(card, { key: " " });
    expect(onChange).toHaveBeenCalledTimes(2);
    expect(onChange).toHaveBeenLastCalledWith("controleur");
  });

  it("highlights the selected card with ring-primary", () => {
    render(
      <ArchetypeSelector
        archetypes={FIXTURE_ARCHETYPES}
        value="analyste"
        onChange={() => {}}
      />,
    );
    const selectedCard = screen.getByTestId("archetype-card-analyste");
    expect(selectedCard.className).toContain("ring-primary");
    expect(selectedCard).toHaveAttribute("aria-checked", "true");

    const unselectedCard = screen.getByTestId("archetype-card-producteur");
    expect(unselectedCard.className).not.toContain("ring-primary");
    expect(unselectedCard).toHaveAttribute("aria-checked", "false");
  });

  it("does not invoke onChange when disabled", () => {
    const onChange = vi.fn();
    render(
      <ArchetypeSelector
        archetypes={FIXTURE_ARCHETYPES}
        value={null}
        onChange={onChange}
        disabled
      />,
    );
    fireEvent.click(screen.getByTestId("archetype-card-producteur"));
    fireEvent.keyDown(screen.getByTestId("archetype-card-producteur"), {
      key: "Enter",
    });
    expect(onChange).not.toHaveBeenCalled();
  });
});
