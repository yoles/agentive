/**
 * useGlobalShortcuts — UX-DR38. Cmd+1..4 (Mac) / Ctrl+1..4 (Win/Linux)
 * navigate between the 4 main spaces.
 *
 * Mounted once at the layout root (`AppLayout`). Listener is attached to
 * `window` so it captures keystrokes regardless of focused element, but
 * we opt out under several conditions:
 *  - User is typing in an editable target (input/textarea/contenteditable,
 *    `<select>`, `role="textbox|combobox|searchbox"`, descending into open
 *    Shadow DOM roots so that web-component inputs are detected).
 *  - An open Radix dialog/popover/menu has focus → the shortcut would
 *    navigate "behind" the modal.
 *  - IME composition is active (`event.isComposing`).
 *  - `shiftKey`/`altKey` modifiers are present → reserved for browser/OS
 *    shortcuts (Cmd+Shift+1 = macOS screenshot, AltGr produces ctrlKey on
 *    Windows/Linux for legitimate text entry).
 *  - `event.repeat` is true → don't flood navigation when key is held.
 *
 * Layout-independent: matches on `event.code` (`Digit1`..`Digit4`) so that
 * AZERTY keyboards (where the digit row needs Shift) still trigger correctly.
 */

import { useNavigate } from "@tanstack/react-router";
import { useEffect } from "react";

const SHORTCUTS: Record<string, "/dashboard" | "/chat" | "/trace" | "/config"> =
  {
    Digit1: "/dashboard",
    Digit2: "/chat",
    Digit3: "/trace",
    Digit4: "/config",
  };

const EDITABLE_ROLES = new Set(["textbox", "combobox", "searchbox"]);

function getDeepActiveElement(): Element | null {
  let active: Element | null = document.activeElement;
  // Descend into open Shadow DOM roots so web-component inputs are detected.
  while (active?.shadowRoot?.activeElement) {
    active = active.shadowRoot.activeElement;
  }
  return active;
}

function isEditableTarget(el: Element | null): boolean {
  if (el === null) return false;
  if (el instanceof HTMLInputElement) return true;
  if (el instanceof HTMLTextAreaElement) return true;
  if (el instanceof HTMLSelectElement) return true;
  if (el instanceof HTMLElement && el.isContentEditable) return true;
  const role = el.getAttribute("role");
  if (role !== null && EDITABLE_ROLES.has(role)) return true;
  return false;
}

function isInsideOpenOverlay(el: Element | null): boolean {
  // Block when focus is inside an open Radix dialog / popover / menu.
  // Radix sets `data-state="open"` on portaled content elements.
  return el?.closest('[role="dialog"][data-state="open"]') !== null
    || el?.closest('[role="menu"][data-state="open"]') !== null
    || el?.closest('[role="listbox"][data-state="open"]') !== null;
}

export function useGlobalShortcuts(): void {
  const navigate = useNavigate();

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      // Hard guards (cheap checks first).
      if (event.repeat) return;
      if (event.isComposing) return;
      // Mac: metaKey ; Windows/Linux: ctrlKey. Accept either (UX-DR38).
      if (!(event.metaKey || event.ctrlKey)) return;
      // Reserve combinations involving shift/alt for OS/browser shortcuts.
      if (event.shiftKey || event.altKey) return;

      const target = SHORTCUTS[event.code];
      if (target === undefined) return;

      const active = getDeepActiveElement();
      if (isEditableTarget(active)) return;
      if (isInsideOpenOverlay(active)) return;

      event.preventDefault();
      // Fire-and-forget: navigation errors surface to TanStack Router's
      // own error boundary, not here.
      void navigate({ to: target });
    };

    window.addEventListener("keydown", handler);
    return () => {
      window.removeEventListener("keydown", handler);
    };
  }, [navigate]);
}
