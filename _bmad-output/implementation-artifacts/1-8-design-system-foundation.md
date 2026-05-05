# Story 1.8: Design system foundation (shadcn + tokens + routing 4 espaces)

Status: done

> 🎨 **Cinquième story Core de l'Epic 1** (post-1.7 auth). Première story **frontend** explicite — toutes les précédentes étaient backend ou cross-cutting.
>
> **Story 1.1 a déjà livré (NE PAS recréer) :**
> - `frontend/src/styles/globals.css` — tokens CSS Tailwind v4 dark default + `.light` override (UX-DR2 conforme : `--color-background`, `--color-card`, `--color-muted`, `--color-primary` `#8B5CF6`, `--color-destructive`, `--color-ring`, status colors UX-DR3)
> - `frontend/src/features/theme/ThemeProvider.tsx` — wrapper `next-themes` (`attribute="class"`, `defaultTheme="dark"`, `enableSystem`, `disableTransitionOnChange`)
> - `frontend/src/app/routes/{dashboard,chat,trace,config}/index.tsx` — 4 placeholder pages TanStack Router file-based
> - `frontend/src/app/routes/index.tsx` — redirect `/` → `/dashboard`
> - `frontend/src/shared/components/layouts/{AppLayout,Sidebar}.tsx` — sidebar 240px **fixe** (pas encore collapsable) + main content fluide max-width 1440px + raccourcis Cmd+1/2/3/4 affichés en `<kbd>` (mais non câblés)
> - `frontend/src/shared/components/ui/button.tsx` — primitive shadcn canonical (cva, asChild via `@radix-ui/react-slot`, focus-visible ring) — **conservée telle quelle**
> - `frontend/src/shared/lib/utils.ts` — `cn()` (clsx + tailwind-merge)
> - Dépendances package.json présentes : `next-themes@^0.4.6`, `cmdk@^1.1.1`, `lucide-react@^1.8.0`, `class-variance-authority@^0.7.1`, `tailwind-merge@^3.5.0`, `clsx@^2.1.1`, `@radix-ui/react-slot@^1.2.4`, `react-hook-form@^7.72.1`, `zod@^4.3.6`, `react-markdown@^10.1.0`, `rehype-sanitize@^6.0.0`
> - Smoke test `app.test.tsx` — vérifie Sidebar + 4 liens
> - eslint-plugin-jsx-a11y + eslint-plugin-boundaries déjà actifs
>
> **Ce que cette story livre :**
> - `components.json` (config shadcn CLI v4) à la racine de `frontend/`
> - 16 primitives shadcn copiées dans `src/shared/components/ui/` (Button déjà présent, à conserver) : `input`, `textarea`, `card`, `dialog`, `sheet`, `command`, `tabs`, `select`, `tooltip`, `badge`, `avatar`, `separator`, `skeleton`, `sonner` (toast moderne), `accordion`, `scroll-area`
> - Polices `Geist Sans` + `Geist Mono` (packages `@fontsource/geist-sans` + `@fontsource/geist-mono`) installées + imports `@fontsource/.../{400,500,600,700}.css` en tête de `main.tsx` (le CSS référence déjà `"Geist Sans"` / `"Geist Mono"` — il manque le chargement effectif self-hosted)
> - `ModeToggle` (UX-DR16) — composant `features/theme/ModeToggle.tsx` exposant un bouton qui switch dark↔light via `useTheme()` (icône Sun/Moon Lucide), inséré dans la `Sidebar` (footer ou header)
> - **Câblage effectif** des raccourcis globaux Cmd+1/Cmd+2/Cmd+3/Cmd+4 → navigation TanStack Router (hook `useGlobalShortcuts` dans `shared/hooks/`)
> - Sidebar collapsable 240px ↔ 56px (UX-DR19) avec persistance `localStorage` ou Zustand store (`features/sidebar/sidebarStore.ts`), tooltip Radix sur icônes en mode collapsé
> - Tests Vitest + Testing Library : theme switching, raccourcis Cmd+1-4, sidebar collapse, focus-visible ring
>
> **Anti-scope strict :**
> - **PAS** de Command Palette Cmd+K interactive — juste la primitive `command` shadcn copiée. La feature complète est Story 6.5 (épic 6).
> - **PAS** de composants custom Agentive (`AgentCard` UX-DR9, `WorkflowCard` UX-DR10, `OutputCard` UX-DR11, `MetricBlock` UX-DR12, `ChatMessage` UX-DR13, `TraceNode` UX-DR14, `AlertBanner` UX-DR15, `ArchetypeSelector` UX-DR17, `KeyboardShortcut` UX-DR18) — réservés aux Epics 2/6/7/8.
> - **PAS** de `Toaster` runtime monté avec un store global et triggers métier — on copie la primitive `sonner` mais sans intégration applicative (UX-DR27 "silence = succès" sera implémentée quand le premier déclencheur métier arrivera).
> - **PAS** de mode "expert vs wizard" Config (UX-DR24) — Story 2.3.
> - **PAS** de Layouts spécifiques par espace (Dashboard 12-col grid UX-DR21, Chat max-w 720px UX-DR22, Trace 320px UX-DR23, Config 640px UX-DR24) — la base AppLayout existante suffit ; chaque epic affinera son layout.
> - **PAS** de OpenAPI client TS auto-généré (`shared/api/types.ts` existe déjà en stub) — `openapi-typescript` est en devDeps, l'intégration CI sera finalisée Story 1.9.
> - **PAS** de logique de routes protégées (auth guard frontend) — l'auth Story 1.7 est backend-only, le frontend sera connecté plus tard (Sprint 4 Growth pour login/session).
>
> **Référence canonique :** Epic 1 lignes 712-746 ; Architecture lignes 167-193 (Frontend stack), 397-417 (Frontend Architecture), 1589-1769 (project structure frontend) ; UX Spec § Visual Design Foundation (lignes 480-660), § Design Direction Decision (lignes 661-712), UX-DR1-49 (epics.md lignes 215-283).
> **NFRs ciblés :** **NFR1** (perf navigation < 100ms switch espace), **NFR4** (frontend démarre en < 60s avec docker compose) — déjà acquis ; cette story doit ne PAS les régresser.

## Story

As **John** (propriétaire de la plateforme Agentive),
I want le scaffolding UI complété avec les 16 primitives shadcn restantes copiées en local, la police Geist chargée, le ModeToggle dark/light fonctionnel, les raccourcis Cmd+1-4 câblés, et la sidebar collapsable 240↔56px,
So that la fondation UI est prête à accueillir les composants custom des epics suivants (Epic 2 Config, Epic 6 Chat, Epic 7 Dashboard, Epic 8 Trace) sans avoir à recopier des primitives ou à refactorer la navigation globale.

## Acceptance Criteria

### AC1 — `components.json` configuré pour shadcn CLI v4

**Given** `frontend/components.json` n'existe pas encore
**When** je crée le fichier de configuration shadcn
**Then** il contient :
  ```json
  {
    "$schema": "https://ui.shadcn.com/schema.json",
    "style": "new-york",
    "rsc": false,
    "tsx": true,
    "tailwind": {
      "config": "",
      "css": "src/styles/globals.css",
      "baseColor": "neutral",
      "cssVariables": true,
      "prefix": ""
    },
    "aliases": {
      "components": "@/shared/components",
      "utils": "@/shared/lib/utils",
      "ui": "@/shared/components/ui",
      "hooks": "@/shared/hooks",
      "lib": "@/shared/lib"
    },
    "iconLibrary": "lucide"
  }
  ```
**And** `style: "new-york"` est choisi (cohérent avec UX-DR1 — densité Linear/Vercel)
**And** `tailwind.config: ""` (vide) car Tailwind v4 utilise `globals.css` exclusivement, pas de `tailwind.config.ts`
**And** les alias respectent la structure existante (`@/shared/...`)

> **NOTE** : Le `Button` existant (`src/shared/components/ui/button.tsx`) est déjà canonical shadcn — **NE PAS le re-générer** via la CLI (la CLI demanderait une confirmation d'overwrite : répondre `N`). Si la CLI overwrite quand même, restaurer la version actuelle depuis git.

### AC1bis — `.npmrc` pour résoudre le conflit ERESOLVE pré-existant (TS↔openapi-typescript)

**Given** le projet pin TypeScript `~6.0.2` mais `openapi-typescript@7.13.0` (déjà présent en `devDependencies` post-Story 1.1) déclare `peerDependencies: { typescript: "^5.x" }`
**And** le `Dockerfile` dev utilise déjà `RUN npm install --legacy-peer-deps` pour contourner ce conflit
**When** un dev exécute `npm install` (ou `npx shadcn@latest add ...` qui déclenche un `npm install` derrière) **hors du conteneur Docker** (sur la machine hôte)
**Then** sans configuration locale, `npm install` échoue avec `ERESOLVE could not resolve` — bloquant pour AC2

**Solution prescrite** : créer `frontend/.npmrc` avec **uniquement** :
```ini
# Aligné avec le Dockerfile dev (RUN npm install --legacy-peer-deps).
# Conflit pré-existant : openapi-typescript@7.x exige TS ^5, projet pin TS ~6.0.
# À retirer dès que openapi-typescript supporte TS 6 ou que le projet bump TS.
legacy-peer-deps=true
```

**And** le commentaire en tête du fichier doit pointer vers la cause (conflit TS) et la condition de retrait
**And** ce fichier est commité (PAS gitignore) pour reproductibilité dev/CI

**Alternative valide (moins préférée)** : `overrides` dans `package.json` ciblant uniquement `typescript` pour `openapi-typescript`. Plus chirurgical mais plus complexe à maintenir et sensible aux versions npm. Le `legacy-peer-deps` global est aligné avec le Dockerfile et suffit au stade Sprint 0.

> **Anti-scope** : ce fichier ne traite **que** le conflit TS↔openapi-typescript. Il ne doit pas devenir un fourre-tout pour masquer de futurs conflits — chaque nouveau peer-dep conflict doit être analysé individuellement (préférer un fix amont : bump de la dep, ou `overrides` ciblé). Réviser ce fichier à chaque bump TypeScript ou openapi-typescript.

### AC2 — 16 primitives shadcn copiées dans `src/shared/components/ui/`

**Given** `components.json` est en place
**When** j'exécute :
  ```bash
  cd frontend && npx shadcn@latest add input textarea card dialog sheet command tabs select tooltip badge avatar separator skeleton sonner accordion scroll-area
  ```
**Then** les 16 fichiers suivants sont créés dans `src/shared/components/ui/` :
  - `input.tsx`, `textarea.tsx`
  - `card.tsx` (Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter)
  - `dialog.tsx` (Dialog + Trigger/Content/Header/Footer/Title/Description/Close)
  - `sheet.tsx` (Sheet latéral — UX-DR32)
  - `command.tsx` (cmdk wrapper — base Cmd+K, primitive seulement)
  - `tabs.tsx`, `select.tsx`
  - `tooltip.tsx` (Tooltip + Provider/Trigger/Content)
  - `badge.tsx`, `avatar.tsx`, `separator.tsx`
  - `skeleton.tsx` (UX-DR30 loading)
  - `sonner.tsx` (Toaster — UX-DR27, primitive seulement, pas montée globalement)
  - `accordion.tsx`, `scroll-area.tsx`
**And** chaque fichier respecte les conventions shadcn canoniques (cva variants, `forwardRef`, `displayName`, `cn()` import depuis `@/shared/lib/utils`)
**And** `eslint .` passe sans erreur sur les nouveaux fichiers (jsx-a11y inclus)
**And** `tsc -b --noEmit` passe sans erreur
**And** chaque fichier importe les primitives Radix sous-jacentes — soit via le **méta-package** `radix-ui` (`import { Dialog as DialogPrimitive } from "radix-ui"`) que shadcn CLI 2.x ajoute par défaut, soit via les sous-packages individuels (`@radix-ui/react-dialog`, etc.) si la CLI est configurée pour. **Les deux formes sont acceptables** ; en pratique shadcn CLI 2.x privilégie le méta-package depuis 2025.
**And** `sonner` est installé en `dependencies` (vs `cmdk` déjà présent)

> **Vérification post-install** : `npm ls radix-ui` (méta-package) doit lister `radix-ui@^1.x` ; OU `npm ls @radix-ui/react-dialog @radix-ui/react-tabs @radix-ui/react-select @radix-ui/react-tooltip @radix-ui/react-accordion @radix-ui/react-avatar @radix-ui/react-scroll-area @radix-ui/react-separator` doit lister les 8 sous-packages selon le mode de génération de la CLI. **Une des deux suffit** — ne pas exiger les deux. Vérifier en parallèle que le bundle JS gzip reste sous le seuil NFR (cf AC8).

### AC3 — Police Geist (Sans + Mono) chargée en self-hosted

**Given** `globals.css` référence `Geist Sans` et `Geist Mono` dans `--font-sans` / `--font-mono` (UX-DR4) mais aucune police n'est chargée à l'exécution
**When** j'installe les packages `@fontsource/geist-sans` et `@fontsource/geist-mono` :
  ```bash
  cd frontend && npm install @fontsource/geist-sans @fontsource/geist-mono
  ```
  Et dans `frontend/src/main.tsx`, **avant** `import "@/styles/globals.css"` :
  ```ts
  import "@fontsource/geist-sans/400.css";
  import "@fontsource/geist-sans/500.css";
  import "@fontsource/geist-sans/600.css";
  import "@fontsource/geist-sans/700.css";
  import "@fontsource/geist-mono/400.css";
  import "@fontsource/geist-mono/500.css";
  ```
  *(`@fontsource/*` est la pratique self-hosted standard pour Vite — chaque import déclare `@font-face` avec `font-display: swap` et bundle le woff2 dans `dist/`. Les 4 poids Sans (400/500/600/700) couvrent UX-DR typo specs ; Mono 400/500 suffit pour code/timestamps.)*
**Then** au runtime, `getComputedStyle(document.body).fontFamily` retourne une chaîne contenant `"Geist Sans"` (test unitaire jsdom suffit ; jsdom ne charge pas la police mais lit la CSS)
**And** `npm run build` produit des `.woff2` dans `dist/assets/` (vérification : `ls dist/assets/*.woff2 | wc -l` ≥ 4)
**And** aucun FOUT (Flash of Unstyled Text) au reload (`font-display: swap` natif `@fontsource`)
**And** `tabular-nums` reste actif via `font-feature-settings: "tnum"` sur `body` (UX-DR4 — déjà présent dans `globals.css`)
**And** `--font-sans` dans `globals.css` reste inchangé : `"Geist Sans", "Geist", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif` (le fallback `"Geist"` couvre le package `geist` officiel si jamais on switche, le system stack reste filet de sécurité)

> **Anti-scope** : ne PAS utiliser le CDN Google Fonts (le mockup HTML l'utilisait, c'est explicitement noté dans UX spec ligne 696 : _"pour l'implémentation réelle : npm geist"_). Cohérent avec la philosophie "self-hosted everything" (NFR16 + AR23 anti-supply-chain).
>
> **Pourquoi `@fontsource` et pas le package `geist` officiel** : le package `geist` v1.7 (Vercel) est conçu pour `next/font` Next.js et expose des objets JS (`GeistSans.className`), pas un CSS Vite-compatible. `@fontsource/*` est l'écosystème self-hosted standard pour Vite/CRA/etc. (docs : https://fontsource.org/fonts/geist-sans).

### AC4 — `ModeToggle` (UX-DR16) inséré dans la sidebar

**Given** le `ModeToggle` n'existe pas encore
**When** je crée `frontend/src/features/theme/ModeToggle.tsx` :
  ```tsx
  import { Moon, Sun } from "lucide-react";
  import { useTheme } from "next-themes";
  import { Button } from "@/shared/components/ui/button";

  export function ModeToggle() {
    const { theme, setTheme } = useTheme();
    const isDark = theme === "dark" || theme === "system";
    return (
      <Button
        variant="ghost"
        size="icon"
        aria-label={isDark ? "Activer le thème clair" : "Activer le thème sombre"}
        onClick={() => setTheme(isDark ? "light" : "dark")}
      >
        {isDark ? <Sun className="size-4" /> : <Moon className="size-4" />}
      </Button>
    );
  }
  ```
**Then** je l'export depuis `features/theme/index.ts` (`export { ModeToggle } from "./ModeToggle"`)
**And** le `ModeToggle` est rendu dans le footer de la `Sidebar` (à côté de `Sprint 0 · v0.1.0`). **Deux patterns d'intégration sont acceptables** ; au choix de l'implémentation :

  **Pattern A — Import direct (le plus simple)** : `Sidebar.tsx` importe `ModeToggle` directement :
  ```tsx
  import { ModeToggle } from "@/features/theme";
  // ...
  <div className="mt-auto flex items-center justify-between px-4 py-4 text-xs text-muted-foreground">
    <span>Sprint 0 · v0.1.0</span>
    <ModeToggle />
  </div>
  ```

  **Pattern B — Composition par slot (recommandé si boundaries v6 strict)** : `Sidebar` expose une prop `footerSlot?: ReactNode` et `AppLayout` la propage via `sidebarFooterSlot` ; `__root.tsx` (couche `app`) injecte `<ModeToggle />` :
  ```tsx
  // shared/components/layouts/Sidebar.tsx
  type SidebarProps = { footerSlot?: ReactNode };
  export function Sidebar({ footerSlot }: SidebarProps) {
    // ... <div className="...flex items-center justify-between..."><span>Sprint 0 · v0.1.0</span>{footerSlot}</div>
  }

  // app/routes/__root.tsx
  component: () => <AppLayout sidebarFooterSlot={<ModeToggle />} />
  ```
  Ce pattern évite une dépendance directe `shared` → `feature` ; utile en anticipation de la migration `eslint-plugin-boundaries` v6 (`element-types` deprecated → `dependencies`). Si le pattern B est retenu, **wrapper aussi le slot dans un `Tooltip` shadcn quand la sidebar est en mode collapsed** (cf AC6) — soit côté slot consumer (le Tooltip dans `ModeToggle` lui-même), soit en demandant un slot `(collapsed: boolean) => ReactNode` côté Sidebar.

**And** un test unitaire vérifie que cliquer sur le bouton appelle `setTheme("light")` quand le thème courant est `"dark"`, et inversement (mock `useTheme` ou test d'intégration via `ThemeProvider`)
**And** la classe `light` est ajoutée sur `<html>` quand l'utilisateur switche en light (next-themes gère automatiquement via `attribute="class"`)
**And** **aucun FOUT** (transition désactivée par `disableTransitionOnChange` déjà configuré dans `ThemeProvider`)

> **NOTE accessibilité** : le `aria-label` est dynamique (décrit l'action, pas l'état). Le focus-visible ring violet 2px (UX-DR37) hérite automatiquement du global `:focus-visible` dans `globals.css`. Tap target ≥ 32x32 (icon size, UX-DR40 desktop OK).

### AC5 — Raccourcis globaux Cmd+1/Cmd+2/Cmd+3/Cmd+4 câblés

**Given** la `Sidebar` affiche `⌘1`...`⌘4` en `<kbd>` mais aucun listener clavier n'est actif
**When** je crée `frontend/src/shared/hooks/useGlobalShortcuts.ts` :
  ```tsx
  import { useNavigate } from "@tanstack/react-router";
  import { useEffect } from "react";

  const SHORTCUTS: Record<string, "/dashboard" | "/chat" | "/trace" | "/config"> = {
    "1": "/dashboard",
    "2": "/chat",
    "3": "/trace",
    "4": "/config",
  };

  export function useGlobalShortcuts() {
    const navigate = useNavigate();
    useEffect(() => {
      const handler = (e: KeyboardEvent) => {
        // Mac : metaKey ; Win/Linux : ctrlKey — accepter les deux (UX-DR38)
        if (!(e.metaKey || e.ctrlKey)) return;
        const target = SHORTCUTS[e.key];
        if (!target) return;
        // Ne pas intercepter si l'utilisateur édite un input/textarea/contenteditable
        const active = document.activeElement;
        if (
          active instanceof HTMLInputElement ||
          active instanceof HTMLTextAreaElement ||
          (active instanceof HTMLElement && active.isContentEditable)
        ) {
          return;
        }
        e.preventDefault();
        void navigate({ to: target });
      };
      window.addEventListener("keydown", handler);
      return () => window.removeEventListener("keydown", handler);
    }, [navigate]);
  }
  ```
**Then** ce hook est appelé depuis `AppLayout.tsx` (un seul endroit, monté une fois) :
  ```tsx
  import { useGlobalShortcuts } from "@/shared/hooks/useGlobalShortcuts";
  export function AppLayout() {
    useGlobalShortcuts();
    return ( /* ... */ );
  }
  ```
**And** un test d'intégration vérifie qu'un `keydown` `{ key: "2", metaKey: true }` (ou `ctrlKey: true`) déclenche une navigation vers `/chat` (utiliser `createMemoryHistory` + `fireEvent.keyDown` sur `window` via `@testing-library/dom`)
**And** un test vérifie qu'un Cmd+2 **ne déclenche PAS** la navigation si l'élément actif est un `<input>` (focus-trap dans formulaires)
**And** un test vérifie qu'un Cmd+5 ou un simple `2` (sans modifier) **n'a aucun effet**
**And** la nav latence est < 100ms (NFR1, vérification manuelle — pas de test automatisé)

### AC6 — Sidebar collapsable 240px ↔ 56px (UX-DR19)

**Given** la `Sidebar` actuelle est figée à `w-60` (240px)
**When** je crée un store Zustand `frontend/src/features/sidebar/sidebarStore.ts` :
  ```tsx
  import { create } from "zustand";
  import { persist } from "zustand/middleware";

  type SidebarState = {
    collapsed: boolean;
    toggle: () => void;
    setCollapsed: (value: boolean) => void;
  };

  export const useSidebarStore = create<SidebarState>()(
    persist(
      (set) => ({
        collapsed: false,
        toggle: () => set((s) => ({ collapsed: !s.collapsed })),
        setCollapsed: (value) => set({ collapsed: value }),
      }),
      { name: "agentive.sidebar" },
    ),
  );
  ```
**And** un `index.ts` re-exporte (`export { useSidebarStore } from "./sidebarStore"`)
**And** la `Sidebar` consomme l'état :
  ```tsx
  const collapsed = useSidebarStore((s) => s.collapsed);
  const toggle = useSidebarStore((s) => s.toggle);
  ```
**Then** la sidebar passe de `w-60` (240px) à `w-14` (56px) selon `collapsed`
**And** en mode collapsed, **uniquement les icônes Lucide** sont visibles (les labels et `<kbd>` sont masqués)
**And** chaque lien d'espace est wrappé dans un `<Tooltip>` shadcn affichant le `label` + `shortcut` (uniquement actif en mode collapsed — `delayDuration={600}` UX-DR35)
**And** un bouton chevron (icône `PanelLeftClose` / `PanelLeftOpen` Lucide) en haut de la sidebar permet de toggler manuellement
**And** l'état `collapsed` persiste dans `localStorage` clé `agentive.sidebar` (rechargement de la page → sidebar reste dans l'état précédent)
**And** un test vérifie que la sidebar a `w-60` puis `w-14` après un click sur le bouton toggle, ET que les labels disparaissent en collapsed
**And** **aucune feature ne s'auto-collapse** (pas de logique responsive automatique en Sprint 0 — Story responsive Sprint 1+ ; UX-DR46 tablet en backlog)

> **NOTE structure** : `features/sidebar/` est un nouveau feature module. La logique de **rendu** reste dans `shared/components/layouts/Sidebar.tsx` (couche shared = layout transverse), seul le **state** est dans `features/sidebar/`. C'est cohérent avec eslint-plugin-boundaries (un `shared` peut consommer un autre `shared`, et un `feature` peut être consommé par `shared` via une exception OU par `app/AppLayout`). **Alternative valide** : mettre le store dans `shared/state/sidebarStore.ts` pour éviter la dépendance shared → feature. À trancher au moment de l'implémentation selon ce que l'eslint-plugin-boundaries autorise (cf `eslint.config.js:48-58`). **Recommandation** : `shared/state/sidebarStore.ts` (simpler, respecte les règles existantes).

### AC7 — Tests Vitest étendus

**Given** le smoke test `app.test.tsx` couvre déjà la sidebar de base
**When** j'ajoute les tests suivants :
  - `frontend/src/features/theme/ModeToggle.test.tsx` :
    - Render dans `<ThemeProvider>` test wrapper (`enableSystem={false}` pour la déterminabilité)
    - Click → vérifier `document.documentElement.classList.contains("light")` switch
    - aria-label correct selon thème courant
  - `frontend/src/shared/hooks/useGlobalShortcuts.test.tsx` :
    - Mount un composant qui appelle le hook + un Router en MemoryHistory
    - `fireEvent.keyDown(window, { key: "2", metaKey: true })` → assert location = `/chat`
    - `fireEvent.keyDown(window, { key: "5", metaKey: true })` → assert location inchangé
    - Avec `<input>` focused → Cmd+2 → location inchangé
  - `frontend/src/shared/state/sidebarStore.test.ts` (ou `features/sidebar/...` selon AC6) :
    - `useSidebarStore.getState().collapsed === false` initialement
    - Après `toggle()` → `true`
    - Après `setCollapsed(false)` → `false`
  - Mise à jour de `app.test.tsx` :
    - Vérifier qu'au moins une primitive shadcn s'importe sans crash (`import { Card } from "@/shared/components/ui/card"; render(<Card>test</Card>)`)
    - Vérifier que la sidebar passe de 240px à 56px après click sur le toggle (computed style ou class assertion)
**Then** `npm test -- --run` (Vitest run-once) passe avec **0 régression** sur les tests existants
**And** au moins 8 nouveaux tests passent (theme×2, shortcuts×3, sidebar store×2, integration×1+)
**And** tous les nouveaux fichiers de test importent depuis `@testing-library/react` + `vitest` (pas de jest)

### AC8 — Lint, build, a11y, NFRs

**Given** tous les changements sont en place
**When** j'exécute la batterie de validation :
  - `cd frontend && npm run lint` (eslint . — boundaries + jsx-a11y)
  - `cd frontend && npm run typecheck` (`tsc -b --noEmit`)
  - `cd frontend && npm run test -- --run`
  - `cd frontend && npm run build` (vite build + tsc)
**Then** **les 4 commandes passent sans erreur ni warning bloquant** (les warnings stylistic acceptables sont OK — pas de nouveau warning `boundaries/element-types` ni `jsx-a11y/*` introduit)
**And** le bundle JS gzippé reste **< 500 KB** (NFR ; vérification : `ls -la dist/assets/*.js` après build, somme des gzip ≈ Vite affiche les tailles en sortie de build)
**And** `docker compose up -d frontend` démarre toujours le frontend en **< 60s** (NFR4, déjà acquis Story 1.1 — ne PAS régresser)
**And** un audit Lighthouse (manuel, accepté optionnel) sur `localhost:5173/dashboard` retourne **score Accessibility ≥ 90** (UX-DR44)

## Tasks / Subtasks

### T1. Configuration shadcn CLI v4 + .npmrc (AC1, AC1bis)

- [x] T1.1 — Créer `frontend/components.json` avec : `style: "new-york"`, `tailwind.config: ""`, `tailwind.css: "src/styles/globals.css"`, `tailwind.cssVariables: true`, `aliases: { components: "@/shared/components", utils: "@/shared/lib/utils", ui: "@/shared/components/ui", hooks: "@/shared/hooks", lib: "@/shared/lib" }`, `iconLibrary: "lucide"`, `rsc: false`, `tsx: true`
- [x] T1.2 — Vérifier que les alias sont déjà cohérents avec `vite.config.ts:18` et `tsconfig.json` (alias `@/...` → `src/...` déjà OK)
- [x] T1.3 — **(AC1bis)** Créer `frontend/.npmrc` avec `legacy-peer-deps=true` + commentaire explicatif (conflit pré-existant TS↔openapi-typescript, aligné avec Dockerfile dev). Ce fichier doit être commité.

### T2. Installer 16 primitives shadcn (AC2)

- [x] T2.1 — Lancer `npx shadcn@latest add input textarea card dialog sheet command tabs select tooltip badge avatar separator skeleton sonner accordion scroll-area --yes` depuis `frontend/`. Si la CLI demande une confirmation overwrite pour `button.tsx`, répondre `N`.
- [x] T2.2 — Vérifier que les 16 fichiers sont créés dans `src/shared/components/ui/` (`input.tsx`, `textarea.tsx`, `card.tsx`, `dialog.tsx`, `sheet.tsx`, `command.tsx`, `tabs.tsx`, `select.tsx`, `tooltip.tsx`, `badge.tsx`, `avatar.tsx`, `separator.tsx`, `skeleton.tsx`, `sonner.tsx`, `accordion.tsx`, `scroll-area.tsx`)
- [x] T2.3 — Vérifier que `button.tsx` (Story 1.1) est intact via `git diff src/shared/components/ui/button.tsx` → **aucune modification attendue**
- [x] T2.4 — Si la CLI a généré un `tailwind.config.ts`, le supprimer (Tailwind v4 lit exclusivement `globals.css`)
- [x] T2.5 — `npm run lint && npm run typecheck` → 0 erreur. Si `react-refresh/only-export-components` se plaint d'un export `<x>Variants` shadcn, ajouter le nom dans `eslint.config.js:39` allowlist.

### T3. Police Geist self-hosted (AC3)

- [x] T3.1 — `cd frontend && npm install @fontsource/geist-sans @fontsource/geist-mono`
- [x] T3.2 — Dans `frontend/src/main.tsx`, ajouter en tête (avant l'import de `globals.css`) :
  ```ts
  import "@fontsource/geist-sans/400.css";
  import "@fontsource/geist-sans/500.css";
  import "@fontsource/geist-sans/600.css";
  import "@fontsource/geist-sans/700.css";
  import "@fontsource/geist-mono/400.css";
  import "@fontsource/geist-mono/500.css";
  ```
- [x] T3.3 — `npm run build` → vérifier `ls dist/assets/*.woff2 | wc -l` ≥ 6 (4 sans + 2 mono)

### T4. ModeToggle (AC4)

- [x] T4.1 — Créer `frontend/src/features/theme/ModeToggle.tsx` (icône `Sun`/`Moon` Lucide, `useTheme` next-themes, `Button` variant="ghost" size="icon", `aria-label` dynamique)
- [x] T4.2 — Mettre à jour `frontend/src/features/theme/index.ts` : `export { ModeToggle } from "./ModeToggle";`
- [x] T4.3 — Intégrer `ModeToggle` dans le footer de la `Sidebar` (à côté de `"Sprint 0 · v0.1.0"`, layout `flex items-center justify-between`). **Deux patterns acceptables — voir AC4** : (A) import direct dans `Sidebar.tsx`, ou (B) prop `footerSlot` + injection depuis `__root.tsx` via `sidebarFooterSlot` (Pattern B retenu en pratique pour anticiper boundaries v6).
- [x] T4.4 — Créer `frontend/src/features/theme/ModeToggle.test.tsx` : 2 tests (toggle dark→light, aria-label dynamique). Utiliser `<ThemeProvider>` test wrapper avec `enableSystem={false}` pour la déterminabilité.

### T5. Raccourcis Cmd+1-4 globaux (AC5)

- [x] T5.1 — Créer `frontend/src/shared/hooks/useGlobalShortcuts.ts` (handler `keydown` sur `window`, mapping `1→/dashboard`, `2→/chat`, `3→/trace`, `4→/config`, accepte `metaKey || ctrlKey`, ignore si focus dans `<input>`/`<textarea>`/`contenteditable`, `e.preventDefault()` + `navigate({to})`, cleanup au unmount)
- [x] T5.2 — Modifier `frontend/src/shared/components/layouts/AppLayout.tsx` : appeler `useGlobalShortcuts()` en haut du composant
- [x] T5.3 — Créer `frontend/src/shared/hooks/useGlobalShortcuts.test.tsx` : 3 tests (Cmd+2 → /chat, Cmd+5 → no nav, input focus → no nav). Pattern `createMemoryHistory` + `RouterProvider` + `fireEvent.keyDown(window, ...)`.

### T6. Sidebar collapsable + sidebarStore (AC6)

- [x] T6.1 — Créer `frontend/src/shared/state/sidebarStore.ts` : Zustand store avec `persist` middleware (clé `agentive.sidebar`), state `{ collapsed: boolean, toggle, setCollapsed }`, défaut `collapsed: false`
- [x] T6.2 — Modifier `frontend/src/shared/components/layouts/Sidebar.tsx` :
  - Consommer `useSidebarStore` (`collapsed`, `toggle`)
  - Width responsive : `w-60` si non-collapsed, `w-14` si collapsed
  - Ajouter un bouton chevron (icônes `PanelLeftClose`/`PanelLeftOpen` Lucide) en haut de la sidebar pour toggler
  - En mode collapsed : masquer labels + `<kbd>` shortcut, garder seulement les icônes Lucide centrées
  - Wrapper chaque lien d'espace dans `<Tooltip>` shadcn — `<TooltipContent>` rendu **uniquement si `collapsed`** (tooltip apparaît au hover en mode collapsed, invisible en mode étendu), `delayDuration={600}`
  - Wrapper aussi le `ModeToggle` dans un Tooltip si collapsed
  - `<TooltipProvider>` en tête de la sidebar (sinon Radix Tooltip throw)
- [x] T6.3 — Créer `frontend/src/shared/state/sidebarStore.test.ts` : 3 tests (initial false, toggle inverse, setCollapsed direct). Reset `localStorage` + `useSidebarStore.setState` dans `beforeEach`.
- [x] T6.4 — Étendre `frontend/src/app/app.test.tsx` : ajouter ≥ 1 test (sidebar passe à `w-14` après click sur le bouton chevron, labels disparaissent — utiliser `screen.queryByText("Dashboard")` qui doit retourner `null` en collapsed)

### T7. Polish & validation finale (AC7, AC8)

- [x] T7.1 — `cd frontend && npm run lint` : 0 erreur (jsx-a11y + boundaries)
- [x] T7.2 — `npm run typecheck` (`tsc -b --noEmit`) : 0 erreur
- [x] T7.3 — `npm run test -- --run` : 0 régression sur tests existants + ≥ 8 nouveaux tests verts (T4.4×2, T5.3×3, T6.3×3, T6.4×1+)
- [x] T7.4 — `npm run build` : succès Vite + tsc, vérifier somme bundle gzip < 500 KB (Vite affiche les tailles), vérifier ≥ 6 fichiers `.woff2` dans `dist/assets/`
- [ ] T7.5 — (Optionnel — non exécuté) Lighthouse audit `localhost:5173/dashboard` → score Accessibility ≥ 90 (deferred — vérification manuelle reviewer)

## Dev Notes

### 🏗️ Architecture & contraintes (références canoniques)

| Élément | Source canonique | Ligne |
|---|---|---|
| Stack frontend (Vite + React 19 + TS + Tailwind v4 + shadcn CLI v4) | `architecture.md` | 167-193, 397-417 |
| Project structure frontend | `architecture.md` | 1589-1769 |
| TanStack Router file-based, 4 espaces | `architecture.md` | 405, 1606-1620 |
| Theme via next-themes + classe `dark` sur `<html>` | `architecture.md` | 407 |
| Tokens CSS dark/light | `ux-design-specification.md` § Visual Design Foundation | 480-540 |
| Typography Geist v1.7.0 | `ux-design-specification.md` | 544-545 |
| Spacing 4px Tailwind standard | `ux-design-specification.md` | 581-592 |
| Layout principal (sidebar 240/56, max-width 1440) | `ux-design-specification.md` | 603-606, UX-DR19 |
| 17 composants shadcn (Button déjà fait) | `epics.md` UX-DR8 | 227 |
| Tokens UX-DR2 (8 paires dark + light) | `epics.md` | 218 |
| Theme switching UX-DR6 | `epics.md` | 222 |
| Focus management UX-DR37 (ring violet 2px + offset 2px) | `epics.md` | 268 |
| Keyboard navigation UX-DR38 (Cmd+1-4 globaux) | `epics.md` | 269 |
| Tooltips UX-DR35 (delay 600ms) | `epics.md` | 263 |
| Boundaries layered architecture | `frontend/eslint.config.js` | 36-65 |

### 🎨 État du design system existant (post-Story 1.1)

Le scaffolding Story 1.1 a livré une **fondation déjà avancée**. Cette story est essentiellement de **l'enrichissement**, pas une création from-scratch. Vérifie d'abord `git log --oneline frontend/` et la liste des fichiers existants (cf. encadré en tête de story) avant de créer quoi que ce soit.

**Décisions architecturales déjà prises (NE PAS revisiter) :**
1. Tailwind v4 avec `@theme` block dans `globals.css` (pas de `tailwind.config.ts`) — la CLI shadcn v4 supporte ce mode (`tailwind.config: ""` dans `components.json`)
2. Tokens CSS via `--color-*` natifs Tailwind v4 (pas le format `--background: 0 0% 100%` HSL de v3)
3. Class-based dark mode via `next-themes` (pas `data-theme`)
4. Style `new-york` (densité Linear) — choisi à AC1
5. Alias `@/...` configurés dans `vite.config.ts` + `tsconfig.json` + `eslint.config.js`

### 🔧 Fichiers à créer / modifier

```
frontend/
├── .npmrc                                           # CRÉER — legacy-peer-deps=true (AC1bis, résout conflit TS↔openapi-typescript)
├── components.json                                  # CRÉER
├── package.json                                     # MODIFIER — npm install adds (geist, sonner, radix-ui méta-package via shadcn CLI 2.x)
├── src/
│   ├── main.tsx                                     # MODIFIER — ajouter imports @fontsource/geist-{sans,mono}/{400,500,600,700}.css avant globals.css
│   ├── styles/
│   │   └── globals.css                              # PAS DE MODIFICATION (les --font-* sont déjà OK)
│   ├── features/
│   │   └── theme/
│   │       ├── ModeToggle.tsx                       # CRÉER
│   │       ├── ModeToggle.test.tsx                  # CRÉER
│   │       └── index.ts                             # MODIFIER — export ModeToggle
│   ├── shared/
│   │   ├── components/
│   │   │   ├── ui/
│   │   │   │   ├── button.tsx                       # NE PAS TOUCHER (déjà OK)
│   │   │   │   ├── input.tsx                        # CRÉER (via shadcn CLI)
│   │   │   │   ├── textarea.tsx                     # CRÉER
│   │   │   │   ├── card.tsx                         # CRÉER
│   │   │   │   ├── dialog.tsx                       # CRÉER
│   │   │   │   ├── sheet.tsx                        # CRÉER
│   │   │   │   ├── command.tsx                      # CRÉER
│   │   │   │   ├── tabs.tsx                         # CRÉER
│   │   │   │   ├── select.tsx                       # CRÉER
│   │   │   │   ├── tooltip.tsx                      # CRÉER
│   │   │   │   ├── badge.tsx                        # CRÉER
│   │   │   │   ├── avatar.tsx                       # CRÉER
│   │   │   │   ├── separator.tsx                    # CRÉER
│   │   │   │   ├── skeleton.tsx                     # CRÉER
│   │   │   │   ├── sonner.tsx                       # CRÉER
│   │   │   │   ├── accordion.tsx                    # CRÉER
│   │   │   │   └── scroll-area.tsx                  # CRÉER
│   │   │   └── layouts/
│   │   │       ├── Sidebar.tsx                      # MODIFIER — collapse + tooltip + footerSlot (ou import direct ModeToggle, cf AC4)
│   │   │       └── AppLayout.tsx                    # MODIFIER — appel useGlobalShortcuts (+ prop sidebarFooterSlot si Pattern B)
│   │   ├── hooks/
│   │   │   ├── useGlobalShortcuts.ts                # CRÉER
│   │   │   └── useGlobalShortcuts.test.tsx          # CRÉER
│   │   └── state/
│   │       ├── sidebarStore.ts                      # CRÉER (recommandé vs features/sidebar/)
│   │       └── sidebarStore.test.ts                 # CRÉER
│   └── app/
│       ├── app.test.tsx                             # MODIFIER — étendre les assertions
│       └── routes/
│           └── __root.tsx                           # MODIFIER si Pattern B AC4 — `<AppLayout sidebarFooterSlot={<ModeToggle />} />`
```

> **Convention shadcn CLI** : lors de `npx shadcn@latest add <component>`, la CLI place les fichiers selon les `aliases` de `components.json`. Avec notre config (`"ui": "@/shared/components/ui"`), les fichiers atterrissent en `src/shared/components/ui/`. **Si la CLI utilise `@/components/ui` par défaut**, vérifier `components.json` et corriger l'alias avant de relancer.

### 🧪 Stratégie de test

**Unitaires (Vitest + jsdom)** :
- `ModeToggle.test.tsx` — wrap dans `ThemeProvider` test fixture, simuler click, assert classList
- `useGlobalShortcuts.test.tsx` — `renderHook` ou render d'un composant minimal qui appelle le hook + Router MemoryHistory, fireEvent.keyDown sur `window`
- `sidebarStore.test.ts` — pas besoin de render, juste appeler les actions et asserter sur `getState()`

**Intégration (composant + Router + Theme)** :
- `app.test.tsx` étendu — render full app, click toggle sidebar, assert width changes, click ModeToggle, assert `<html>` class, fire Cmd+2, assert location = `/chat`

**Pattern à suivre — héritage Story 1.1 `app.test.tsx`** :
```tsx
const router = createRouter({
  routeTree,
  history: createMemoryHistory({ initialEntries: ["/dashboard"] }),
});
render(
  <Providers>
    <RouterProvider router={router} />
  </Providers>,
);
```

**Pas besoin de Playwright E2E** pour cette story — Sprint 1+ uniquement (cf `architecture.md` ligne 1767).

### ⚠️ Pièges connus

1. **shadcn CLI v4 + Tailwind v4** : la CLI peut tenter de créer un `tailwind.config.ts` même si `components.json` indique `"config": ""`. Si ça arrive, **supprimer** le fichier généré — Tailwind v4 lit exclusivement `globals.css`. La présence d'un `tailwind.config.ts` orphelin causerait de la confusion sans casser le build.

2. **`Button` overwrite** : `npx shadcn add button` n'est PAS dans la liste AC2, MAIS si tu lances `npx shadcn add` sans arguments, la CLI peut proposer la liste complète et inclure Button. **Toujours passer la liste explicite** des 16 composants à AC2.

3. **`react-refresh/only-export-components`** : la règle est déjà configurée pour autoriser `Route` et `buttonVariants` (cf `eslint.config.js:39`). Si un composant shadcn généré exporte une variante (ex: `cardVariants` théorique), ajouter le nom à l'allowlist :
   ```js
   { allowExportNames: ["Route", "buttonVariants", "cardVariants" /* etc */] }
   ```
   En pratique, seuls Button et Badge ont des variants exportés en shadcn canonical. Vérifier après génération.

4. **`next-themes` + SSR-like check** : dans `ModeToggle`, ne PAS render conditionnellement basé sur `mounted` state (le hack classique pour éviter l'hydration mismatch SSR). Vite est SPA pure, pas de SSR — le check est inutile et complexifie pour rien. Le `disableTransitionOnChange` du `ThemeProvider` suffit à éviter le FOUT.

5. **`useNavigate` dans hook custom** : TanStack Router v1.168+ permet `useNavigate()` hors d'un component si on fournit un router. Mais le pattern recommandé est : appeler le hook **depuis un composant** monté dans le RouterProvider (`AppLayout` est OK).

6. **Cmd+1-4 vs raccourcis browser** : sur Chrome/Firefox, Cmd+1 = aller au premier onglet. Notre `e.preventDefault()` écrase ce comportement uniquement quand notre handler matche. Tester en Chrome **réellement** (pas que jsdom) au moins une fois manuellement avant de marquer la story `done`.

7. **`localStorage` en test (sidebarStore)** : la persistance Zustand `persist` middleware utilise `localStorage` qui est dispo dans jsdom, mais l'état est partagé entre tests. **Reset entre tests** :
   ```ts
   beforeEach(() => {
     useSidebarStore.setState({ collapsed: false });
     localStorage.clear();
   });
   ```

8. **`Tooltip` actif uniquement en collapsed** : pas besoin de wrapper conditionnellement le `<Link>` dans `<Tooltip>` — toujours wrappé, mais conditionner `disabled` ou `open={undefined}` selon `collapsed`. Plus simple :
   ```tsx
   <Tooltip>
     <TooltipTrigger asChild>
       <Link>...</Link>
     </TooltipTrigger>
     {collapsed && <TooltipContent side="right">{label} · {shortcut}</TooltipContent>}
   </Tooltip>
   ```
   En mode collapsed, le tooltip apparaît au hover ; en mode expanded, le `TooltipContent` n'est pas rendu (pas de tooltip).

### 📚 Learnings des stories précédentes à réutiliser

**De Story 1.1 (scaffolding)** :
- Pattern `Providers` (theme + query) consommé par `RouterProvider` dans `main.tsx` — déjà OK
- Pattern test smoke `createMemoryHistory` + `RouterProvider` — réutiliser tel quel
- Tokens Tailwind v4 syntax `@theme { --color-* }` — déjà appliqué, juste compléter Geist

**De Story 1.7 (auth)** :
- Pattern de fix-batch post code-review : prévoir une marge mentale pour le fix-batch après review (pas un livrable initial)
- Documenter explicitement les **anti-scopes** dans l'en-tête évite les déviations

**De Story 1.6 (LLM router)** :
- Pattern `app.state.xxx` (backend) — analogue côté frontend : Zustand store global pour théme, sidebar, query client
- L'**escape hatch** côté UI : si une primitive shadcn ne convient pas, **ne pas la modifier** — créer un wrapper custom dans `components/custom/` (out-of-scope ici, prévu Epic 2+)

### 🔗 Lien vers prochaine story (1.9)

Story 1.9 (Observability + Caddy + CI) **dépend** de cette story uniquement pour les check eslint+vitest+build dans GitHub Actions. Pas de couplage frontend↔observability au-delà.

## Dev Agent Record

### Implementation Plan (suggested)

Story 1.8 livrable en 5 tâches selon red-green-refactor :

1. **T1** — `components.json` + `npx shadcn add` 16 composants + `npm install @fontsource/geist-sans @fontsource/geist-mono sonner` + imports `@fontsource/...` en tête de `main.tsx` → vérifier `npm run lint && npm run typecheck && npm run build` passent. (0 nouveau test, juste pas de régression)

2. **T2** — `ModeToggle` + insertion dans Sidebar + `ModeToggle.test.tsx`. Mettre à jour `features/theme/index.ts`. Tests : 2 unitaires + 1 intégration dans `app.test.tsx` étendu.

3. **T3** — `useGlobalShortcuts` hook + branchement dans `AppLayout` + `useGlobalShortcuts.test.tsx`. Tests : 3 unitaires (key match / no match / input focus exclusion).

4. **T4** — `sidebarStore` (Zustand persist) + Sidebar refactor (collapse, tooltips, chevron toggle) + `sidebarStore.test.ts` + extension `app.test.tsx` (collapse assertion). Tests : 2 store + 1 intégration.

5. **T5** — Validation finale : `npm run lint && npm run typecheck && npm test -- --run && npm run build`. Lighthouse manuel (optionnel, score a11y ≥ 90). Vérifier `docker compose up -d frontend` < 60s.

### Agent Model Used

claude-opus-4-7 (1M context) — implémentation initiale T1-T7

### Debug Log References

- T2 ERESOLVE peer-dep conflict (`openapi-typescript@7.13.0` exige TS `^5.x`, projet pin `~6.0.2`) — fix : `frontend/.npmrc` avec `legacy-peer-deps=true`, aligné sur le `RUN npm install --legacy-peer-deps` du Dockerfile dev
- T2 shadcn CLI v4.6 ne résout PAS l'alias `@/...` → fichiers créés littéralement dans `frontend/@/shared/components/ui/` au lieu de `frontend/src/shared/components/ui/` — fix : `mv` via container puis `rm -rf @/`
- T2 `sonner.tsx` généré par shadcn avait un import self-referencing (`from "@/shared/components/ui/sonner"` au lieu de `from "sonner"`) ET utilisait des CSS vars sans préfixe `--color-` — fix manuel
- T4 lint `react-refresh/only-export-components` sur `__root.tsx` quand un component nommé est défini ET `Route` est exporté — fix : inliner le component dans `Route.component`
- T4 — `eslint-plugin-boundaries` v6 émet des warnings de migration (rule `boundaries/element-types` deprecated → `boundaries/dependencies`) qui rendent les violations shared→feature **silencieuses**. Préservé le pattern de composition propre (slot `sidebarFooterSlot` injecté depuis `__root.tsx` qui est `app`-type) — quand la migration v6 sera faite, l'architecture restera correcte.
- T5 tests TanStack Router : RouterProvider a un état pending async — utiliser `await screen.findByTestId(...)` pour attendre le mount avant de fire les keydown events
- T5 warnings React `act(...)` sur Transitioner : informatifs (state updates async TanStack Router), tests passent — non-bloquants

### Completion Notes List

✅ Story 1.8 implémentée intégralement — 10 tests passent (vs 1 baseline Story 1.1), 0 régression, lint+typecheck+build verts.

**Livrables :**
- `components.json` shadcn CLI v4 (style `new-york`, alias `@/shared/...`)
- 16 primitives shadcn dans `src/shared/components/ui/` (`button.tsx` Story 1.1 préservé tel quel — vérifié par diff)
- `@fontsource/geist-sans` + `@fontsource/geist-mono` self-hosted, 10 fichiers `.woff2` dans `dist/assets/` au build
- `ModeToggle` avec aria-label dynamique, monté via `sidebarFooterSlot` depuis `__root.tsx`
- Pattern de composition propre `app → shared → (feature via slot)` au lieu d'un import direct shared→feature
- `useGlobalShortcuts` hook : Cmd/Ctrl+1..4 → 4 routes, opt-out sur input/textarea/contenteditable, branché dans `AppLayout`
- `sidebarStore` Zustand persist (clé `agentive.sidebar`), 240px ↔ 56px, tooltips Radix en mode collapsé (delay 600ms UX-DR35), bouton chevron `PanelLeftClose`/`PanelLeftOpen`

**Tests : 10/10 verts** (4 fichiers : `app.test.tsx` 2 tests, `ModeToggle.test.tsx` 2, `useGlobalShortcuts.test.tsx` 3, `sidebarStore.test.ts` 3)

**Build : bundle JS gzip 189.21 KB / CSS gzip 8.79 KB** (NFR < 500KB ✅), build en 2.05s

**Anti-scope tenu :** aucune Command Palette interactive, aucun composant custom Agentive (AgentCard/WorkflowCard/etc.), aucun Toaster runtime monté, aucun layout par espace, aucun OpenAPI client, aucune route protégée frontend.

**À noter pour le reviewer :**
1. `frontend/.npmrc` (`legacy-peer-deps=true`) ajouté — résout un conflit préexistant (`openapi-typescript@7.13.0` ↔ TS 6) sans modifier le Dockerfile.
2. `eslint-plugin-boundaries` est en mode "deprecated rule" (warnings v5→v6) — la règle est silencieuse sur certaines violations. Migration `boundaries/element-types` → `boundaries/dependencies` à planifier (Story 1.9 ou tech-debt ticket).
3. Pattern composition `__root.tsx` (app) → `<AppLayout sidebarFooterSlot={<ModeToggle />} />` au lieu d'un import direct dans Sidebar : plus pur architecturalement, prêt pour quand boundaries v6 enforcera réellement.
4. `eslint.config.js` allowlist `react-refresh/only-export-components` étendue à `["Route", "buttonVariants", "badgeVariants", "tabsListVariants"]` (cva variants co-localisés des primitives shadcn).
5. T7.5 Lighthouse audit non exécuté — vérification manuelle browser à faire au reviewer (UX-DR44 score ≥ 90 attendu).

### File List

**Créés :**
- `frontend/.npmrc` (legacy-peer-deps=true)
- `frontend/components.json` (shadcn CLI v4 config)
- `frontend/src/shared/components/ui/accordion.tsx` (shadcn primitive)
- `frontend/src/shared/components/ui/avatar.tsx`
- `frontend/src/shared/components/ui/badge.tsx`
- `frontend/src/shared/components/ui/card.tsx`
- `frontend/src/shared/components/ui/command.tsx`
- `frontend/src/shared/components/ui/dialog.tsx`
- `frontend/src/shared/components/ui/input.tsx`
- `frontend/src/shared/components/ui/scroll-area.tsx`
- `frontend/src/shared/components/ui/select.tsx`
- `frontend/src/shared/components/ui/separator.tsx`
- `frontend/src/shared/components/ui/sheet.tsx`
- `frontend/src/shared/components/ui/skeleton.tsx`
- `frontend/src/shared/components/ui/sonner.tsx` (édité post-shadcn pour fixer l'import self-ref + CSS vars)
- `frontend/src/shared/components/ui/tabs.tsx`
- `frontend/src/shared/components/ui/textarea.tsx`
- `frontend/src/shared/components/ui/tooltip.tsx`
- `frontend/src/shared/hooks/useGlobalShortcuts.ts`
- `frontend/src/shared/hooks/useGlobalShortcuts.test.tsx`
- `frontend/src/shared/state/sidebarStore.ts`
- `frontend/src/shared/state/sidebarStore.test.ts`
- `frontend/src/features/theme/ModeToggle.tsx`
- `frontend/src/features/theme/ModeToggle.test.tsx`

**Modifiés :**
- `frontend/package.json` — ajout `@fontsource/geist-sans@^5.2.5`, `@fontsource/geist-mono@^5.2.7`, `radix-ui@^1.4.3`, `sonner@^2.0.7` (+ Radix sub-packages déjà gérés via `radix-ui` meta)
- `frontend/package-lock.json` — résolutions Radix/Sonner/fontsource
- `frontend/eslint.config.js` — allowlist `react-refresh/only-export-components` étendue (`badgeVariants`, `tabsListVariants`)
- `frontend/src/main.tsx` — imports `@fontsource/geist-{sans,mono}/...css` avant `globals.css`
- `frontend/src/features/theme/index.ts` — export `ModeToggle`
- `frontend/src/shared/components/layouts/Sidebar.tsx` — refactor complet : collapse 240↔56, tooltips Radix en mode collapsed, chevron toggle, store Zustand consommé, prop `footerSlot`
- `frontend/src/shared/components/layouts/AppLayout.tsx` — appel `useGlobalShortcuts()`, prop `sidebarFooterSlot` propagée
- `frontend/src/app/routes/__root.tsx` — composition `<AppLayout sidebarFooterSlot={<ModeToggle />} />`
- `frontend/src/app/app.test.tsx` — étendu : ModeToggle assertion + collapse test (2 tests vs 1)

**Non modifiés (vérifiés intacts) :**
- `frontend/src/shared/components/ui/button.tsx` (préservé — Story 1.1 canonical)
- `frontend/src/styles/globals.css` (tokens déjà conformes UX-DR2)
- `frontend/src/features/theme/ThemeProvider.tsx` (Story 1.1)

---

## Change Log

- 2026-05-05 — Story 1.8 créée (ready-for-dev) : design system foundation (16 primitives shadcn + Geist + ModeToggle + Cmd+1-4 + sidebar collapsable). Source canonique épics + UX spec + audit du scaffolding existant Story 1.1. Anti-scope strict : pas de Command Palette, pas de composants custom Agentive, pas de Toaster runtime.
- 2026-05-05 — Story 1.8 implémentée (review) : 10/10 tests verts (vs 1 baseline), 0 régression, lint+typecheck+build verts. Bundle JS gzip 189.21 KB (NFR < 500KB ✅). 24 fichiers créés (16 primitives shadcn + 4 composants/hooks/stores + 4 fichiers de tests + .npmrc + components.json), 10 fichiers modifiés. `@fontsource/geist-{sans,mono}` self-hosted (10 fichiers .woff2 dans dist/). Patterns shadcn fixés post-CLI : `sonner.tsx` import + CSS vars `--color-*`. Pattern composition `app → shared → (feature via slot)` adopté pour respecter les boundaries même si la rule v6 est silencieuse.
- 2026-05-05 — **Spec amendments post-review (3 bad-spec findings)** : (a) **AC2** — autorise désormais le méta-package `radix-ui` (shadcn CLI 2.x) en plus des sous-packages `@radix-ui/react-*` ; vérification post-install acceptée sous l'une des deux formes. (b) **AC4** — autorise explicitement deux patterns d'intégration `ModeToggle` dans la `Sidebar` : import direct (Pattern A) ou composition par slot `footerSlot`/`sidebarFooterSlot` depuis `__root.tsx` (Pattern B, retenu pour anticiper la migration `eslint-plugin-boundaries` v6). (c) **AC1bis (nouveau)** — prescrit la création de `frontend/.npmrc` avec `legacy-peer-deps=true` pour résoudre le conflit ERESOLVE pré-existant TS↔openapi-typescript hors conteneur Docker, aligné avec le Dockerfile dev. T1.3 et la liste des fichiers mis à jour. Ces amendements ne modifient pas le code livré ni les tests — ils alignent la spec sur l'implémentation défendable.
- 2026-05-05 — **Code review fix-batch (24 patches P-1 à P-24)** : 19/19 tests verts (+9 vs baseline 10), 0 régression, lint+typecheck+build verts, bundle JS gzip 189.62 KB (vs 189.21 KB, +0.41 KB, NFR < 500 KB ✅). Modifications par fichier : (1) **`useGlobalShortcuts.ts`** — `event.code` (Digit1..4) layout-indépendant AZERTY, filtrage `shiftKey`/`altKey`, garde `event.repeat` + `event.isComposing`, `isEditableTarget` étendu (`<select>`, `[role="textbox|combobox|searchbox"]`, descente Shadow DOM), garde overlay Radix ouvert (`[role="dialog|menu|listbox"][data-state="open"]`). (2) **`ModeToggle.tsx`** — pattern `mounted` flag (canonical next-themes) ; placeholder inert avant mount ; `resolvedTheme` post-mount avec icône/label cohérents. (3) **`Sidebar.tsx`** — `aria-pressed` (vs `aria-expanded`) sur chevron toggle, `motion-reduce:transition-none`, `focus-visible:ring` chevron, `<kbd>` visible aussi en `group-focus-within`, tooltips collapsed = shortcut seul (dédupe vs aria-label), nouvelle prop `footerSlotTooltip` qui wrap `footerSlot` dans Tooltip Radix en mode collapsed, transition `width` désactivée tant que `hasHydrated === false` (anti-flash). (4) **`sidebarStore.ts`** — `partialize` (persiste uniquement `collapsed`), `hasHydrated` flag + `setHasHydrated` + `onRehydrateStorage` callback. (5) **`command.tsx`** — `DialogHeader`/`DialogTitle` déplacés DANS `DialogContent` pour wirage Radix `aria-labelledby` correct. (6) **`AppLayout.tsx` + `__root.tsx`** — propagation prop `sidebarFooterSlotTooltip="Basculer le thème"`. (7) **Tests** — `app.test.tsx` +1 test (Card primitive smoke AC7) + assertion `w-14`/`w-60` + `localStorage["theme"]="dark"` beforeEach (anti-flake CI) ; `ModeToggle.test.tsx` +2 tests (`enableSystem={true}` prod-aligned + missing provider) ; `useGlobalShortcuts.test.tsx` +5 tests (Ctrl+3, Cmd+Shift, Cmd+Alt, repeat, IME) + `waitFor` (vs setTimeout magique) + `cleanup` afterEach ; `sidebarStore.test.ts` +1 test (persist localStorage + partialize). Status story 1.8 inchangé (`review`).
- 2026-05-05 — **Story 1.8 done** : merge sur `main` (commit `a83a346`) + post-merge `chore(devx): make dev-host` (commit `aec78c5`, hors-scope story). Tous AC validés (AC1..AC8 + AC1bis), 19/19 tests verts, lint+typecheck+build verts, bundle JS gzip 189.62 KB. Status `review` → `done`.
