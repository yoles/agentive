import "@testing-library/jest-dom/vitest";

/**
 * Mock `window.matchMedia` (absent de jsdom) pour permettre à next-themes
 * de fonctionner dans les tests.
 */
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});

/**
 * Mock `ResizeObserver` (absent de jsdom) — requis par les primitives Radix
 * qui mesurent leur taille (Checkbox, Slider, ScrollArea…) via
 * `@radix-ui/react-use-size`. Sans ce stub, tout test qui interagit avec
 * l'une de ces primitives (ex. cliquer une Checkbox) lève
 * `ReferenceError: ResizeObserver is not defined` (fix-batch Story 2.7,
 * 2026-08-31 — première fois qu'un test clique effectivement une Checkbox
 * Radix dans ce repo).
 */
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
Object.defineProperty(window, "ResizeObserver", {
  writable: true,
  value: ResizeObserverStub,
});
