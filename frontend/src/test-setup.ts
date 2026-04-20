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
