# Story 2.3 : Mode wizard guidé vs mode expert direct

Status: Done

> 📝 **Code Review Amendments — 2026-05-10** (post `bmad-code-review` adversarial 3 layers, John tranche 4 bad-spec) :
>
> - **B-01 — Expert sans Zod inline** : ferme P-07. AC3 + Décision #6 + T5.3 amendés ci-dessous → validation client Zod inline défer Story future (probable Story 2.7 quand form builder contracts arrive). `@hookform/resolvers ^5.2.2` reste installé pour usage futur. Sprint 1 = Expert sans validation client (backend RFC 7807 + `focusFirstInvalidField` reste l'autorité). D-F closure officielle = côté Wizard step gates uniquement (suffit pour mirroir Pydantic ↔ Zod).
> - **B-02 — Divergence WizardStepXSchema vs UpdateTemplateRequestSchema** : intentionnelle. AC6 amendé ci-dessous → Wizard step gates imposent completion (UX guidée single-pass) ; `UpdateTemplateRequestSchema` reste permissif (PATCH-like sémantique Story 2.2). Le Zod miroir Pydantic concerne `UpdateTemplateRequestSchema`, pas les `WizardStep*Schema` (couche UX au-dessus).
> - **B-03 — `max-w-3xl` au lieu de `max-w-2xl`** : intentionnel. AC3 + T6.1 amendés ci-dessous → 720-768px requis pour `WizardProgress` 5 étapes horizontal lisibles. Aucun rollback code.
> - **B-04 — `templateForm.ts` module pur (pas hook `useTemplateFormLogic`)** : intentionnel. Décision #2 amendée ci-dessous → helpers stateless = pas de hook custom.

> 🎯 **Troisième story Epic 2 — Agent Platform.** Cette story transforme la **page d'édition Story 2.2** (formulaire flat 12 champs en mode "Expert minimal") en **deux UX co-existantes** :
>
> 1. **Mode Wizard guidé** — 5 étapes séquentielles (Identité → Prompt → Contrats → LLM → Politique d'erreur), 1 étape visible à la fois, gates de validation à chaque transition, progression visible en haut. Cible : John "charge cognitive faible" / découverte d'un nouveau template.
> 2. **Mode Expert direct** — tous les champs visibles sous forme d'**accordéons empilés** (UX-DR24), 5 sections repliables (Identité / Prompt / Contrats / LLM / Politique d'erreur). Cible : John "charge cognitive haute" / itération rapide sur un template connu.
>
> Le **`ModeToggle` (UX-DR16)** persiste la préférence utilisateur en `localStorage` (Sprint 1 single-user) via Zustand `persist` (pattern Story 1.8 `sidebarStore`). La validation **Zod miroite la validation Pydantic backend** (D-F closure depuis Story 2.2 code-review).
>
> Backend : **AUCUN changement** — Story 2.2 a livré l'endpoint `PUT /api/v1/agents/templates/{id}` complet avec Pydantic v2 strict + `extra="forbid"` + tous les validators. Story 2.3 est **100% frontend**.
>
> **Pré-requis hérités (déjà disponibles — NE PAS recréer) :**
>
> - **Backend Story 2.2 stable** : `PUT /api/v1/agents/templates/{id}` + `GET` détail + atomicité P-02 + Pydantic schemas (`UpdateTemplateRequest`, `ErrorPolicy`, `LLMParams`, `ContractDefinition`, `LLMModel` Literal, `ProviderId` Literal). Toutes les contraintes (extra="forbid", min/max, Literal, dedup provider_chain via field_validator, model_validator non-empty payload, system_prompt min_length=1) restent valides — Zod doit les mirroir 1:1.
> - **Frontend feature `agent_registry`** (Story 2.1 + 2.2) : `api.ts` (`getTemplate`, `updateTemplate`), `hooks.ts` (`useTemplate(id)` avec UUID guard, `useUpdateTemplate(id)` avec invalidation queryKeys). À RÉUTILISER tel quel — la mutation reste identique, seule la couche présentation change.
> - **Frontend types** (`features/agent_registry/types.ts`) : `TemplateDetail`, `UpdateTemplateRequest`, `UpdateTemplateResponse`, `ErrorPolicy`, `LLMParams`, `ContractDefinition`, `LLMModel`, `ProviderId`. À RÉUTILISER. Les schemas Zod (T2) seront définis SÉPARÉMENT dans `features/agent_registry/schemas.ts` (nouveau fichier) et exporteront des types **compatibles** avec ceux existants (Zod `z.infer<>` pour validation).
> - **Frontend page `$templateId.tsx`** (Story 2.2) : actuellement 466 lignes, formulaire flat avec helpers internes (`buildInitialForm`, `buildPayload`, `parseProviderChain`, `parseContract`, `focusFirstInvalidField`). **Ces helpers seront EXTRAITS** (T1) dans un module partagé `features/agent_registry/templateForm.ts` consommé par les deux modes (Wizard + Expert).
> - **Composants UI shadcn** : `Accordion` (`shared/components/ui/accordion.tsx`) — disponible mais **PAS encore consommé** dans le repo. ⚠️ À vérifier en T0 : les classes Tailwind `animate-accordion-up`/`animate-accordion-down` (`accordion.tsx:56`) doivent être définies dans `src/styles/globals.css` (Tailwind v4 `@keyframes` + `@theme --animate-accordion-*`). Sinon, ajout requis (cf §"Décisions intégrées" #5). Autres primitives requises : `Button`, `Input`, `Textarea`, `Select`, `Label`, `Card` (déjà utilisées Story 2.2).
> - **Pattern Zustand persist** : `frontend/src/shared/state/sidebarStore.ts` (Story 1.8) montre la convention exacte (key `agentive.sidebar`, `partialize`, `onRehydrateStorage` → `setHasHydrated(true)`, flag `hasHydrated` exposé pour suppress flash de hydratation). À reproduire 1:1 pour `useModeStore` (T3).
> - **Pattern `ModeToggle` théorique** : `frontend/src/features/theme/ModeToggle.tsx` (Story 1.8, dark/light) montre le **pattern `mounted` flag** (suppress hydration flicker) + structure Button. **Nouveau composant** `features/agent_registry/ConfigModeToggle.tsx` (T3) — pas de réutilisation directe car le `ModeToggle` theme couple `next-themes` ; le nôtre couple `useModeStore`. Mais conceptuellement miroir.
> - **Form deps** : `react-hook-form ^7.72.1` + `zod ^4.3.6` déjà installés (`frontend/package.json`). **À ajouter Sprint 1** : `@hookform/resolvers` (peer dep officielle Zod ↔ react-hook-form) — installation obligatoire en T0. PAS de `framer-motion` — les animations accordéon utilisent Tailwind/CSS keyframes (cf décision #5).
> - **Convention queryKeys posée Story 2.2** : `["agent-template", id]` détail, `["agent-templates"]` liste future. Story 2.3 ne change rien (le ModeToggle ne génère pas de fetch, juste lit `useTemplate`).
> - **TanStack Router** : page actuelle `frontend/src/app/routes/config/agents/$templateId.tsx` reste la route file-based. Story 2.3 va la **réécrire** en host minimal qui charge `useTemplate` puis route vers `<TemplateWizardForm />` ou `<TemplateExpertForm />` selon `useModeStore.mode`.
> - **Convention test pattern** : `$templateId.test.tsx` Story 2.2 (vitest + RTL + memory router + TanStack Query Provider) — réutilisable pour les nouveaux tests Wizard/Expert.
>
> **Anti-scope strict (à NE PAS faire dans cette story) :**
>
> - **Backend changes** → AUCUN. Si l'implémentation rencontre une limitation backend, c'est un signal d'amend Story 2.2 (rare ; Story 2.2 fix-batch a stabilisé tout ce qui était nécessaire).
> - **Distinction template ↔ instance** → **Story 2.4** (rename de template, snapshot instances, etc. restent défer).
> - **Édition VISUELLE structurée des contrats** (form fields typés au lieu de Textarea JSON brut, ex : "ajouter un champ core" via UI) → **Sprint 4+** ou Story dédiée. Story 2.3 garde le `<Textarea>` JSON pour `input_contract`/`output_contract`/`provider_chain` (Sprint 1 acceptable — D14 reste partiellement ouvert : 2.3 améliore l'UX en encapsulant les Textarea dans un accordéon dédié + validation Zod inline, mais pas de form builder visuel).
> - **Tool Hub MCP, Sandbox bwrap, Playground, Review conversationnelle** → Stories 2.5 → 2.8.
> - **Liste paginée des templates** → Story 2.4 ou 2.7.
> - **Rollback de version (`prompts.is_active`)** → Story 2.4 ou 2.7 (D11 reste défer).
> - **Provider chain runtime fallback actif** → Story 4.6 (D12 reste défer).
> - **Error policy dispatcher actif** (qui APPLIQUE le retry au runtime) → Story 4.6 (D13 reste défer).
> - **Persistence backend de la préférence Wizard/Expert** → Sprint 4+ (multi-user). Sprint 1 = `localStorage` via Zustand persist (single-user OK, key `agentive.agent-config-mode`).
> - **Wizard "Company Architect" 7 étapes** (création de départements entiers) → **Epic 11 / Sprint 5** (Growth). Story 2.3 livre UNIQUEMENT un Wizard de configuration d'UN agent-template existant, PAS de génération d'archetypes ou de workflow.
> - **Save partiel mid-Wizard** → **NON Sprint 1**. Le Wizard valide chaque étape avant de passer à la suivante mais ne sauvegarde PAS un brouillon partiel ; le `mutateAsync` du PUT ne s'exécute QU'à l'étape finale "Validation". Si l'utilisateur quitte mid-Wizard, ses changements en cours sont perdus (le Wizard fait un Reset complet en abandonnant). Sprint 4+ pourra ajouter un draft autosave.
> - **Browser navigation guard "Unsaved changes"** (`window.onbeforeunload`) → reportée. Sprint 1 acceptable de ne pas avoir le warning ; à ajouter Story 2.7 ou plus tard si John est gêné en pratique.
> - **Swipe gestures mobile** (mentionné UX-DR §1266) → reportée Sprint 4+ (mobile-first n'est pas Sprint 1, le viewport cible est desktop).
> - **Animations framer-motion** → NON. Tailwind/CSS keyframes uniquement (décision #5).
> - **Refactor Story 2.2 page existante en place** → on REMPLACE complètement `$templateId.tsx`. Le contenu Story 2.2 (formulaire flat) DISPARAÎT — Story 2.3 livre Wizard + Expert via le `ModeToggle`, l'expert mode étant la version "tout visible" plus riche que le flat 2.2.
>
> **Décisions intégrées (Epic 1 retro + Story 2.1 + 2.2 + code-review 2026-05-09) :**
>
> 1. **Pas de changement backend** — la cible Sprint 1 est : 0 ligne de Python touchée. Toute la validation Zod côté front mirroir Pydantic 1:1 ; un mismatch Zod/Pydantic = bug à fixer côté Zod (le backend reste l'autorité).
> 2. **`templateForm.ts` module pur partagé** *(amendement CR 2026-05-10 — B-04 : ex "useTemplateFormLogic" hook → corrigé en module pur car helpers stateless)* — extraction Story 2.2 helpers (`buildInitialForm`, `buildPayload`, `parseProviderChain`, `parseContract`) dans un **module pur** `features/agent_registry/templateForm.ts` (pas de hook custom car stateless). `focusFirstInvalidField` extrait à part (`focusFirstInvalidField.ts`, couple DOM). Wizard et Expert importent directement les helpers via le barrel `@/features/agent_registry`. Garantit que les deux modes produisent un payload identique pour le PUT.
> 3. **Zustand persist pattern Story 1.8** — `useModeStore` reproduit `sidebarStore` 1:1 (`name: "agentive.agent-config-mode"`, `partialize: { mode }`, `hasHydrated` flag, `onRehydrateStorage`). Pas de variations.
> 4. **Switch mid-edit préserve l'état** — quand l'utilisateur toggle Wizard ↔ Expert pendant l'édition, le `FormState` (10 champs string/number) est CONSERVÉ. Ni reset ni perte. Solution : le `FormState` vit dans le composant parent (`$templateId.tsx`) ou dans un hook custom partagé (`useTemplateForm()`). Wizard et Expert reçoivent le state + setter en props.
> 5. **Animations Accordion shadcn — keyframes Tailwind v4 OBLIGATOIRES** — le composant `accordion.tsx:56` utilise `data-[state=closed]:animate-accordion-up data-[state=open]:animate-accordion-down` ; ces utilités requièrent que les keyframes `accordion-up` / `accordion-down` soient définies dans `src/styles/globals.css` (Tailwind v4 syntax `@keyframes` + `@theme inline { --animate-accordion-up: ... }`). **À vérifier en T0**, ajouter si manquantes (référence : [shadcn accordion docs](https://ui.shadcn.com/docs/components/accordion)). Sans, l'accordéon s'ouvrira/fermera instantanément (UX dégradée mais pas cassante).
> 6. **`@hookform/resolvers` ajouté en deps (usage défer)** — `npm install @hookform/resolvers` reste requis Sprint 1 (peer dep officielle). **Amendement CR 2026-05-10 (B-01)** : Sprint 1 ne branche PAS RHF côté Expert (validation backend-only post-PUT, pattern Story 2.2 — RFC 7807 toast + `focusFirstInvalidField`). Wizard step gates utilisent Zod en validation manuelle (pas RHF). La dépendance est conservée pour usage Story future (probable Story 2.7 quand le form builder visuel contracts arrive — D14 closure complète).
> 7. **Pattern `mounted` flag pour ModeToggle** — miroir `ModeToggle` theme Story 1.8 : utiliser `useModeStore.hasHydrated` pour suppress visual flicker au premier render (avant que `localStorage.getItem("agentive.agent-config-mode")` ne soit lu). Sans, premier render = `wizard` (default) puis flash vers `expert` si l'utilisateur avait persisté.
> 8. **Default mode = `wizard`** — premier accès UX guidé pour un nouvel utilisateur. Décision UX cohérente avec UX-DR16 ("guidance par défaut, expert sur opt-in").
> 9. **Wizard step gates strict — pas de skip** — chaque "Suivant" valide les champs de l'étape courante via Zod. Si invalid, l'étape reste visible avec messages d'erreur inline (`FormMessage` shadcn). Pas de "skip optional", pas de "save & continue later". L'utilisateur doit corriger pour avancer.
> 10. **Wizard étape "Validation" finale** — récap de tous les champs en lecture seule + bouton "Sauvegarder" (équivalent du Submit Expert). Permet la review avant le PUT. Cinquième étape pour l'Identité, le Prompt, les Contrats, le LLM/Error policy, et la Validation = 5 étapes au total (cohérent avec la spec "5-7" — on choisit 5 pour rester compact Sprint 1).
> 11. **Wizard "Annuler" mid-flight** — bouton secondaire visible à toutes les étapes ; clic = retour à l'étape 1 + reset du `FormState` à `buildInitialForm(template.config)`. Pas de prompt de confirmation Sprint 1 (pragmatique ; une story future pourra ajouter le warning si gênant).
> 12. **Expert accordéons tous OUVERTS au montage** — le mode Expert vise "tout visible". Le Radix `<Accordion type="multiple">` permet plusieurs sections ouvertes simultanément ; le `defaultValue={[...]}` ouvre toutes les sections au premier render. L'utilisateur peut replier ce qu'il veut ensuite. Pas de persistance des sections dépliées Sprint 1.
> 13. **D-F (Zod runtime validation) FERMÉ ici** — Story 2.3 livre les schemas Zod miroitant Pydantic et les utilise pour la validation client (gates Wizard + soumission Expert). Le mismatch Zod/Pydantic = bug à fixer Zod (backend = autorité). Tests dédiés (T7) couvrent ce miroir.
> 14. **D14 (contracts visuels) RESTE PARTIELLEMENT OUVERT** — Story 2.3 améliore l'UX des contrats en : (a) encapsulant les Textarea JSON dans un accordéon dédié "Contrats" (Expert) et une étape Wizard dédiée ; (b) validation Zod inline sur la shape `{core: dict, extras: dict}`. Mais PAS de form builder visuel (champs `core` ajoutables UI). Le defer complet attend Sprint 4+ ou Story dédiée. Documenter clairement que D14 reste partiel.
> 15. **Smoke test runtime non requis** — Story 2.3 est 100% frontend, aucun changement backend ; le smoke test backend Story 2.2 reste valide. Vérification visuelle manuelle suggérée (cf T6.5) mais pas un AC bloquant.

## Story

**As John (développeur solo / Owner)**,
**I want** basculer entre un mode Wizard guidé (étapes structurées avec validation séquentielle) et un mode Expert direct (tous les champs visibles sous forme d'accordéons empilés) pour la configuration d'un agent-template,
**So that** je choisis le niveau de détail et la guidance UX selon ma charge cognitive du moment, sans perdre l'état de mes modifications en cours en switchant entre les deux modes.

## Acceptance Criteria

### AC1 — `ConfigModeToggle` (UX-DR16) — switch persistant Wizard ↔ Expert

**Given** je navigue vers `/config/agents/{templateId}` (page Story 2.2 réécrite par cette story)
**When** la page se monte
**Then** un composant `ConfigModeToggle` est visible **en haut** de la zone de configuration (à droite du titre "Configuration de l'agent", ou dans un header sticky), affichant deux options mutuellement exclusives : `Wizard` | `Expert`. Le composant respecte UX-DR16 :
  - `role="radiogroup"` avec navigation clavier (← / → ou Tab)
  - Variant icon+text (icônes lucide-react `Wand2` pour Wizard, `LayoutGrid` pour Expert)
  - Active state : background + shadow distincts
  - Accessibilité : `aria-checked` sur l'option active, focus visible

**And** la valeur initiale est **`wizard`** par défaut (premier accès) ou la valeur persistée dans `localStorage` (key `agentive.agent-config-mode`) si l'utilisateur a déjà choisi un mode auparavant.
**And** la persistance est gérée par un store Zustand `useModeStore` (nouveau fichier `frontend/src/features/agent_registry/modeStore.ts`) avec middleware `persist` (pattern miroir Story 1.8 `sidebarStore.ts`).
**And** un flag `hasHydrated` est exposé par le store ; le `ConfigModeToggle` masque tout indicateur visuel tant que `hasHydrated === false` (suppress flicker au premier render — pattern Story 1.8).

**Given** je clique sur l'option opposée (Wizard ↔ Expert)
**When** le switch s'effectue
**Then** la valeur est immédiatement persistée dans `localStorage` via `useModeStore.setMode(value)`.
**And** la zone de configuration en dessous re-render le mode correspondant (Wizard ou Expert) **sans recharger la page** ni refetch le template.
**And** le `FormState` en cours d'édition est PRÉSERVÉ (cf AC4).

### AC2 — Mode Wizard — 5 étapes séquentielles + validation Zod gate

**Given** le mode `wizard` est actif
**When** la zone de configuration se monte
**Then** un composant `<TemplateWizardForm template={template} formState={state} onChange={...} onSubmit={...} />` rend l'UX en 5 étapes :
  1. **Identité** — `name` (read-only Sprint 1, rename = Story 2.4) + summary archétype (lecture seule)
  2. **Prompt** — `system_prompt` (Textarea rows=12, monospace, placeholder = `prompt_base` archétype, validation Zod : `min(1).max(50000)` ou vide pour conserver le prompt archétype — comportement Story 2.2)
  3. **Contrats** — `input_contract` + `output_contract` (Textarea JSON brut, validation Zod : `{core: object, extras: object}`)
  4. **LLM** — `llm_model` (Select whitelist) + `llm_params.temperature` + `llm_params.max_tokens` + `provider_chain` (Textarea JSON validation Zod : `array(enum(["anthropic", "openai"])).min(1).max(4)`)
  5. **Politique d'erreur & Validation** — `error_policy.on_timeout` + `error_policy.max_retries` + `error_policy.backoff_strategy` + récap en lecture seule des étapes 1-4 + bouton **"Sauvegarder"** déclenchant `useUpdateTemplate.mutateAsync(payload)`.

**And** une **barre de progression** est affichée en haut (UX-DR24 ligne 622-625), montrant les 5 étapes (numérotées + labellées), avec l'étape courante mise en évidence (ring violet UX-DR37). Cliquer sur une étape précédente est autorisé (rollback UX). Cliquer sur une étape future est BLOQUÉ tant que les étapes intermédiaires ne sont pas validées.

**Given** je suis à l'étape N (1 ≤ N ≤ 4) et je clique sur "Suivant"
**When** Zod valide les champs de l'étape N
**Then** si VALIDE → l'étape N+1 s'affiche, l'étape N est marquée `completed` (checkmark vert dans la progression).
**And** si INVALIDE → l'étape N reste affichée avec messages d'erreur inline (`<FormMessage>` shadcn) sous chaque champ invalide ; le focus va au premier champ en erreur (réutilisation de `focusFirstInvalidField` extrait en T1).

**Given** je suis à l'étape 5 (Validation finale)
**When** je clique sur "Sauvegarder"
**Then** le `FormState` est passé à `buildPayload` (extrait de Story 2.2 en T1) ; si `BuildPayloadResult` est `ok: false` → toast erreur + focus sur l'étape correspondante (mapping `result.field` → step) ; si `ok: true` → `useUpdateTemplate.mutateAsync(payload)` ; sur succès → toast `"Template mis à jour (v{response.version})"` + reset Wizard à l'étape 1 + reset `FormState` via `buildInitialForm(updatedTemplate.config)` ; sur erreur RFC 7807 → toast erreur + focus champ via `focusFirstInvalidField` + restoration sur l'étape correspondante.

**And** un bouton secondaire **"Annuler"** est visible à toutes les étapes (1-5) ; clic = retour à l'étape 1 + reset `FormState = buildInitialForm(template.config)` (rejet des modifications).

### AC3 — Mode Expert — accordéons empilés + validation Zod inline

**Given** le mode `expert` est actif
**When** la zone de configuration se monte
**Then** un composant `<TemplateExpertForm template={template} formState={state} onChange={...} onSubmit={...} />` rend l'UX sous forme d'accordéons empilés (UX-DR24 ligne 622-625), composé de 5 sections :
  1. **Identité** (`<AccordionItem value="identity">`) — `name` read-only
  2. **Prompt** (`value="prompt"`) — `system_prompt`
  3. **Contrats** (`value="contracts"`) — `input_contract` + `output_contract`
  4. **LLM** (`value="llm"`) — `llm_model` + `llm_params.*` + `provider_chain`
  5. **Politique d'erreur** (`value="error_policy"`) — `error_policy.on_timeout` + `max_retries` + `backoff_strategy`

**And** l'`<Accordion type="multiple" defaultValue={["identity", "prompt", "contracts", "llm", "error_policy"]}>` ouvre TOUTES les sections au premier render (mode Expert = "tout visible") ; l'utilisateur peut replier ce qu'il veut ensuite (état NON persisté Sprint 1).

**And** chaque section affiche les mêmes champs que le formulaire Story 2.2, avec :
  - **Amendement CR 2026-05-10 (B-01) — Validation Zod inline DÉFER** : Sprint 1 = validation backend-only post-PUT (pattern Story 2.2 conservé : RFC 7807 toast + `focusFirstInvalidField`). Le branchage `react-hook-form` + `zodResolver` + `<FormMessage>` per field est **défer Story future** (probable Story 2.7 avec form builder visuel). `@hookform/resolvers` reste installé pour ce futur. **D-F closure officielle = côté Wizard step gates uniquement** (suffit pour mirroir Pydantic ↔ Zod et tests T7).
  - Focus visible UX-DR37 sur tous les inputs
  - **Amendement CR 2026-05-10 (B-03)** : Layout `max-w-3xl` (768px) — augmenté depuis `max-w-2xl` (640px Story 2.2) pour permettre au `WizardProgress` 5 étapes horizontal d'être lisible (titres step + pastilles) sans wrap. Accordéons full width à l'intérieur du container.

**And** un bouton **"Sauvegarder"** (sticky en bas, ou en pied de page de la section "Politique d'erreur") déclenche `useUpdateTemplate.mutateAsync(payload)`. Le pattern Story 2.2 (toast succès / erreur RFC 7807 + focus extraction) reste identique.

**And** un bouton secondaire **"Annuler"** restore `FormState = buildInitialForm(template.config)` (équivalent du Cancel Story 2.2).

### AC4 — Switch Wizard ↔ Expert préserve le `FormState` en cours d'édition

**Given** je suis en mode Wizard à l'étape 3 (Contrats), j'ai modifié `system_prompt` (étape 2) et `input_contract` (étape 3, en cours)
**When** je switch via le `ConfigModeToggle` vers Expert
**Then** le mode Expert s'affiche immédiatement avec :
  - Tous mes changements en cours préservés dans tous les champs (`system_prompt`, `input_contract`, etc.)
  - Aucun refetch backend (le `useTemplate` hook est en cache TanStack Query 30s)
  - Aucun reset (Wizard step counter peut être réinitialisé à 1, c'est OK ; ce qui compte = `FormState` intact)

**Given** je suis en mode Expert avec plusieurs sections modifiées
**When** je switch vers Wizard
**Then** symétriquement, le mode Wizard ouvre l'étape 1 (Identité) avec tous les changements précédents conservés. Si je clique "Suivant", l'étape 2 affiche le `system_prompt` modifié.

**Note implémentation** : le `FormState` doit vivre dans le composant **parent** (`$templateId.tsx`) ou dans un **hook partagé** (`useTemplateForm()` retournant `{ formState, setFormState, reset }`). Wizard et Expert reçoivent state+setter en props. Le Wizard maintient SEUL son `currentStep`, mais le `FormState` est externe.

### AC5 — `templateForm.ts` — module partagé extrait depuis Story 2.2

**Given** Story 2.2 livrait dans `$templateId.tsx` les helpers `buildInitialForm`, `buildPayload`, `parseProviderChain`, `parseContract`, `focusFirstInvalidField` (lignes 88-227 du fichier 2.2)
**When** Story 2.3 entre en implémentation
**Then** ces helpers sont **EXTRAITS dans un nouveau module pur** `frontend/src/features/agent_registry/templateForm.ts` :
  - `buildInitialForm(config: Record<string, unknown>): FormState`
  - `buildPayload(form: FormState): BuildPayloadResult` (discriminated union `{ok: true, payload}` ou `{ok: false, field, message}`)
  - `parseProviderChain(raw: string): ProviderId[] | string`
  - `parseContract(raw: string): ContractDefinition | string`
  - Type `FormState` exporté
  - Type `BuildPayloadResult` exporté
**And** `focusFirstInvalidField(apiError)` est extrait dans `frontend/src/features/agent_registry/focusFirstInvalidField.ts` (séparé car couple DOM, pas pure logic).
**And** `frontend/src/features/agent_registry/index.ts` réexporte `templateForm.*` + `focusFirstInvalidField` (barrel pattern Story 2.1).
**And** la page `$templateId.tsx` (réécrite Story 2.3) **n'importe plus ces helpers en interne** — tous les imports passent par le barrel `@/features/agent_registry`.
**And** Wizard form + Expert form consomment ces helpers identiques → garantie que les deux modes produisent un payload PUT byte-identique pour les mêmes inputs utilisateur.

### AC6 — Schemas Zod miroitant Pydantic backend (D-F closure)

**Given** le backend Pydantic v2 (`backend/src/agentive_backend/features/m2_agent_registry/schemas.py`) définit `UpdateTemplateRequest`, `ErrorPolicy`, `LLMParams`, `ContractDefinition`, `LLMModel` Literal, `ProviderId` Literal, avec contraintes (extra="forbid", min/max, enum, dedup provider_chain, model_validator non-empty)
**When** Story 2.3 entre en implémentation
**Then** un nouveau fichier `frontend/src/features/agent_registry/schemas.ts` définit les Zod schemas miroirs :
  - `LLMModelSchema = z.enum(["claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022", "gpt-4o", "gpt-4o-mini"])`
  - `ProviderIdSchema = z.enum(["anthropic", "openai"])`
  - `LLMParamsSchema = z.object({ temperature: z.number().min(0).max(2), max_tokens: z.number().int().min(1).max(200_000) })`
  - `ContractDefinitionSchema = z.object({ core: z.record(z.string(), z.unknown()).optional().default({}), extras: z.record(z.string(), z.unknown()).optional().default({}) })` *(B1 amend Story 2.2 : `core` permissif default `{}`)*
  - `ErrorPolicySchema = z.object({ on_timeout: z.enum(["retry_with_backoff", "fail_fast", "fallback_provider"]).default("retry_with_backoff"), max_retries: z.number().int().min(0).max(10).default(3), backoff_strategy: z.enum(["exponential", "linear", "constant"]).default("exponential") })`
  - `UpdateTemplateRequestSchema = z.object({ system_prompt: z.string().min(1).max(50_000).optional(), input_contract: ContractDefinitionSchema.optional(), output_contract: ContractDefinitionSchema.optional(), llm_model: LLMModelSchema.optional(), llm_params: LLMParamsSchema.optional(), provider_chain: z.array(ProviderIdSchema).min(1).max(4).refine(arr => new Set(arr).size === arr.length, { message: "duplicate provider in chain" }).optional(), error_policy: ErrorPolicySchema.optional() }).strict().refine(payload => Object.values(payload).some(v => v !== undefined), { message: "at least one field must be provided" })`

**And** chaque sub-step Wizard utilise un **subset** du schema pour son gate de validation : étape 2 valide `{ system_prompt }`, étape 3 valide `{ input_contract, output_contract }`, etc. (via `z.object({}).pick({ ... })` ou sub-schemas dédiés).

**And** un **test de cohérence** (T7) vérifie que pour chaque cas valide/invalide testé côté backend (`test_schemas_update.py`), le résultat Zod est identique (accept/reject). Voir T7 pour la liste exhaustive.

**Amendement CR 2026-05-10 (B-02) — Divergence intentionnelle WizardStepXSchema vs UpdateTemplateRequestSchema** :
- Le **mirroir Zod ↔ Pydantic concerne `UpdateTemplateRequestSchema`** : tous les champs y sont `.optional()` (PATCH-like sémantique livrée Story 2.2 — un PUT peut modifier 1 seul champ sans toucher aux autres).
- Les **`WizardStep1Schema..WizardStep5Schema`** sont une **couche UX au-dessus** : elles imposent la **completion** de chaque étape (ex : `WizardStep4Schema` exige `provider_chain.min(1)`, `llm_model`, `llm_params` complets). C'est cohérent avec l'esprit "Wizard guidé single-pass" (Décision #9 — pas de skip optional). Si un user veut un PUT partiel, il bascule en mode Expert (Décision #1 future quand Expert Zod inline arrive — voir B-01) ou utilise directement l'API.
- **Aucun bug à fixer** : c'est un design choice. Tests cohérence (T7) couvrent uniquement `UpdateTemplateRequestSchema` ↔ Pydantic, pas `WizardStep*Schema` (qui sont par nature plus stricts).

### AC7 — Tests : ≥ 12 nouveaux tests, 0 régression baseline

**Tests frontend (≥ 12) :**

- **`modeStore.test.ts`** ≥ 3 tests : default = `wizard`, persistence localStorage, `setMode` met à jour la valeur + déclenche le `partialize`.
- **`ConfigModeToggle.test.tsx`** ≥ 2 tests : render des deux options, clic switch + appelle `setMode`, accessibilité `role="radiogroup"` + navigation clavier.
- **`schemas.test.ts`** ≥ 3 tests : un cas valide minimal (juste `{ system_prompt }`), un cas invalide (`extra=forbid` rejette champ inconnu), un cas invalide (`provider_chain` avec duplicate), un cas invalide (payload entièrement undefined → refine catches).
- **`templateForm.test.ts`** ≥ 2 tests : `buildPayload` discriminated union sur JSON malformé, `parseProviderChain` shape validation (Array + every typeof === "string").
- **`TemplateWizardForm.test.tsx`** ≥ 2 tests : Next button bloqué si Zod invalide à l'étape courante, complete les 5 étapes → mutate appelé avec payload merged.
- **`TemplateExpertForm.test.tsx`** ≥ 2 tests : tous les accordéons rendus défaut open, save submit déclenche mutate avec payload merged.
- **`$templateId.test.tsx`** (étendu) : 1+ test sur switch Wizard ↔ Expert préserve `FormState` (AC4).

**Régression** : `make test` reste vert sur **435 backend (inchangé) + ≥ 45 frontend** (33 baseline + ≥ 12 nouveaux). Aucune modification des tests Story 2.2 sauf si refactor de `$templateId.test.tsx` (mise à jour des assertions pour la nouvelle structure 2.3 — documenter en Completion Notes).

### AC8 — Lint + typecheck + accessibilité

**Given** le code Story 2.3 est implémenté
**When** `make lint-frontend` est exécuté
**Then** ESLint + tsc retournent 0 erreur. Pas d'`any` introduit. Pas de `react-hooks/set-state-in-effect` (pattern "derived state during render" si nécessaire — cf Story 2.2 P-14).

**And** chaque accordéon Expert + chaque étape Wizard expose des labels accessibles (`<Label htmlFor="...">`) et des `aria-describedby` pour les helper texts.

**And** le `ConfigModeToggle` répond aux raccourcis clavier ← / → entre les options (option Sprint 1 simple : Tab navigation suffisante, mais documentation `aria-keyshortcuts="ArrowLeft ArrowRight"` souhaitée pour signaler l'intent).

**And** `prefers-reduced-motion` est respecté côté CSS keyframes accordéon (déjà géré par shadcn Accordion + Tailwind).

## Tasks / Subtasks

- [x] **T0 — Pré-requis dépendances + animations Accordion (AC1, AC3)**
  - [ ] T0.1 `cd frontend && npm install @hookform/resolvers` (peer dep officielle Zod ↔ react-hook-form). Commit `chore(frontend): add @hookform/resolvers Sprint 1`.
  - [ ] T0.2 Vérifier dans `frontend/src/styles/globals.css` la présence des keyframes `@keyframes accordion-up` / `@keyframes accordion-down` + leurs utilités Tailwind v4 (`@theme inline { --animate-accordion-up: accordion-up 0.2s ease-out; --animate-accordion-down: accordion-down 0.2s ease-out; }`). Si absentes → ajouter (référence shadcn docs). Smoke test : monter rapidement un Accordion vide dans un `App.test.tsx`-like et vérifier que les classes `data-[state=closed]:animate-accordion-up` produisent une transition.
  - [ ] T0.3 Vérifier `package.json` après install : `react-hook-form ^7.72.1` + `zod ^4.3.6` + `@hookform/resolvers ^3.x` présents.

- [x] **T1 — Extract templateForm helpers depuis Story 2.2 (AC5)**
  - [ ] T1.1 Créer `frontend/src/features/agent_registry/templateForm.ts` ; déplacer `FormState` type + `buildInitialForm` + `buildPayload` + `parseProviderChain` + `parseContract` + `BuildPayloadResult` type depuis `$templateId.tsx`. Pure logic, aucun import React.
  - [ ] T1.2 Créer `frontend/src/features/agent_registry/focusFirstInvalidField.ts` ; déplacer la fonction depuis `$templateId.tsx`. Couple DOM (`document.getElementById`), donc séparé du module pur.
  - [ ] T1.3 Mettre à jour `frontend/src/features/agent_registry/index.ts` (barrel) avec exports : `buildInitialForm`, `buildPayload`, `parseProviderChain`, `parseContract`, `focusFirstInvalidField`, types `FormState`, `BuildPayloadResult`.
  - [ ] T1.4 Tests `templateForm.test.ts` ≥ 2 tests (AC7).

- [x] **T2 — Schemas Zod miroitant Pydantic (AC6, D-F closure)**
  - [ ] T2.1 Créer `frontend/src/features/agent_registry/schemas.ts` avec les 6 Zod schemas (cf AC6 verbatim). Réutiliser le typage existant `types.ts` via `z.infer<typeof ...Schema>` quand pertinent.
  - [ ] T2.2 Exporter via `index.ts` : `LLMModelSchema`, `ProviderIdSchema`, `LLMParamsSchema`, `ContractDefinitionSchema`, `ErrorPolicySchema`, `UpdateTemplateRequestSchema`, `WizardStep1Schema..WizardStep5Schema` (sub-schemas pour gates Wizard).
  - [ ] T2.3 Tests `schemas.test.ts` ≥ 3 tests (AC7) — assertion notable : `UpdateTemplateRequestSchema.safeParse({ rogue: "x" }).success === false` + `UpdateTemplateRequestSchema.safeParse({ provider_chain: ["anthropic", "anthropic"] }).success === false` + `UpdateTemplateRequestSchema.safeParse({}).success === false` (refine non-empty).

- [x] **T3 — `useModeStore` Zustand persist + `ConfigModeToggle` (AC1)**
  - [ ] T3.1 Créer `frontend/src/features/agent_registry/modeStore.ts` ; reproduire 1:1 le pattern `sidebarStore.ts` (Story 1.8). Type : `{ mode: "wizard" | "expert", hasHydrated: boolean, setMode: (m) => void, setHasHydrated: (v) => void }`. Default `mode: "wizard"`. Key persist : `agentive.agent-config-mode`. Partialize : `{ mode }` uniquement.
  - [ ] T3.2 Créer `frontend/src/features/agent_registry/ConfigModeToggle.tsx` ; pattern miroir `features/theme/ModeToggle.tsx` (mounted flag, deux Boutons côte à côte). Variant icon+text (`Wand2` Wizard + `LayoutGrid` Expert). Accessibility : `role="radiogroup"`, `aria-checked`, focus visible. Suppress visual indicator si `!hasHydrated` (suppress flicker).
  - [ ] T3.3 Exporter via `index.ts` : `useModeStore`, `ConfigModeToggle`.
  - [ ] T3.4 Tests `modeStore.test.ts` ≥ 3 tests + `ConfigModeToggle.test.tsx` ≥ 2 tests (AC7).

- [x] **T4 — `TemplateWizardForm` (AC2)**
  - [ ] T4.1 Créer `frontend/src/features/agent_registry/TemplateWizardForm.tsx`. Props : `{ template: TemplateDetail, formState: FormState, onFormStateChange: (state: FormState) => void, onSubmit: () => Promise<void>, isPending: boolean }`. État interne : `currentStep` (1-5), `completedSteps` (Set).
  - [ ] T4.2 Composer 5 étapes (Identité / Prompt / Contrats / LLM / Validation+ErrorPolicy). Utiliser `react-hook-form` + `zodResolver(WizardStepNSchema)` par étape pour gates de validation locale. Bouton "Suivant" déclenche `await trigger()` (RHF) ; si `true`, advance ; sinon, focus first invalid (réutilise `focusFirstInvalidField` adapté ou usage natif RHF `setFocus`).
  - [ ] T4.3 Barre de progression en haut (composant inline ou nouveau `<WizardProgress steps={[...]} currentStep={N} completedSteps={...} onStepClick={...} />`). Cliquer une étape précédente = navigation OK ; cliquer une étape future = bloqué (déjà géré via `currentStep` + `completedSteps`).
  - [ ] T4.4 Étape 5 "Validation" : récap lecture seule + bouton "Sauvegarder" déclenchant `props.onSubmit()`. Pendant `isPending`, bouton disabled + spinner.
  - [ ] T4.5 Bouton secondaire "Annuler" visible à toutes les étapes ; clic = appel `onFormStateChange(buildInitialForm(template.config))` + reset `currentStep` à 1.
  - [ ] T4.6 Tests `TemplateWizardForm.test.tsx` ≥ 2 tests (AC7).

- [x] **T5 — `TemplateExpertForm` (AC3)**
  - [ ] T5.1 Créer `frontend/src/features/agent_registry/TemplateExpertForm.tsx`. Props : `{ template: TemplateDetail, formState: FormState, onFormStateChange: (state: FormState) => void, onSubmit: () => Promise<void>, isPending: boolean }`.
  - [ ] T5.2 Composer 5 `<AccordionItem>` (Identité / Prompt / Contrats / LLM / Politique d'erreur). Utiliser `<Accordion type="multiple" defaultValue={["identity", "prompt", "contracts", "llm", "error_policy"]}>` pour ouvrir toutes les sections au montage. Réutiliser les champs Story 2.2 (Input/Textarea/Select) tels quels.
  - [ ] ~~T5.3 Validation Zod inline via `react-hook-form` + `zodResolver(UpdateTemplateRequestSchema)`. Erreurs sous chaque champ via `<FormMessage>` shadcn (à vérifier : si `Form` shadcn n'est pas dans le repo, utiliser `<p className="text-destructive text-xs">` direct, pattern Story 2.1).~~ **Amendement CR 2026-05-10 (B-01) — DÉFER Story future** (probable Story 2.7). Sprint 1 = validation backend-only post-PUT (pattern Story 2.2). Voir AC3 amendé.
  - [ ] T5.4 Bouton "Sauvegarder" en bas (sticky ou pied de section "Politique d'erreur") + bouton secondaire "Annuler" qui restore `formState = buildInitialForm(template.config)`.
  - [ ] T5.5 Tests `TemplateExpertForm.test.tsx` ≥ 2 tests (AC7).

- [x] **T6 — Réécriture page `$templateId.tsx` (AC4 — switch préserve state)**
  - [ ] T6.1 **Réécrire** `frontend/src/app/routes/config/agents/$templateId.tsx`. Le contenu Story 2.2 (formulaire flat 12 fields inline) DISPARAÎT. Nouveau host minimal :
    ```tsx
    export function AgentTemplateDetail() {
      const { templateId } = Route.useParams();
      const isValidUuid = UUID_RE.test(templateId);
      const templateQuery = useTemplate(isValidUuid ? templateId : null);
      const updateMutation = useUpdateTemplate(templateId);
      const { mode, hasHydrated } = useModeStore();

      // FormState partagé entre Wizard et Expert (AC4 — switch préserve state)
      const [formState, setFormState] = useState<FormState | null>(null);
      const [hydratedFromId, setHydratedFromId] = useState<string | null>(null);
      if (templateQuery.data && (hydratedFromId !== templateQuery.data.template_id || formState === null)) {
        setHydratedFromId(templateQuery.data.template_id);
        setFormState(buildInitialForm(templateQuery.data.config));
      }

      // ... guards UUID/loading/error (Story 2.2 P-14 destructure narrow) ...

      const handleSubmit = async () => {
        // P-12 guard isPending + P-09 buildPayload + P-11 focusFirstInvalidField
        // (logique Story 2.2 réutilisée 1:1 ici, partagée entre les deux modes)
      };

      return (
        <section className="container mx-auto flex max-w-3xl flex-col gap-6 py-6"> {/* Amend CR 2026-05-10 (B-03) : 2xl→3xl pour WizardProgress 5 étapes */}
          <header className="flex items-center justify-between">
            <h1>Configuration de l'agent</h1>
            <ConfigModeToggle />
          </header>
          {hasHydrated && mode === "wizard" && (
            <TemplateWizardForm template={template} formState={formState} onFormStateChange={setFormState} onSubmit={handleSubmit} isPending={updateMutation.isPending} />
          )}
          {hasHydrated && mode === "expert" && (
            <TemplateExpertForm template={template} formState={formState} onFormStateChange={setFormState} onSubmit={handleSubmit} isPending={updateMutation.isPending} />
          )}
        </section>
      );
    }
    ```
  - [ ] T6.2 Migrer/adapter `$templateId.test.tsx` (Story 2.2) — au minimum, garder les tests de la guard UUID + chargement + erreur. Ajouter 1 test dédié AC4 (switch Wizard ↔ Expert préserve FormState).
  - [ ] T6.3 Vérifier que la query `useTemplate` ne refetch PAS au switch (devrait être OK car staleTime 30s — le `useModeStore` n'invalide rien).
  - [ ] T6.4 Vérifier import-linter ESLint boundaries v6 : `$templateId.tsx` importe uniquement de `@/features/agent_registry`, `@/shared/...`. Pas de cross-feature.
  - [ ] T6.5 Smoke test manuel (non bloquant) : `make up` puis ouvrir `https://localhost:8443/config/agents/{templateId-existant}` ; tester switch Wizard ↔ Expert ; remplir Wizard étape par étape ; soumettre ; vérifier toast + log structlog backend (`m2.agent_template.updated`).

- [x] **T7 — Tests miroir Zod ↔ Pydantic + smoke + commit (AC7, AC8)**
  - [ ] T7.1 Compléter `schemas.test.ts` avec un **test de cohérence Zod ↔ Pydantic** : pour chaque cas testé dans `backend/tests/unit/m2_agent_registry/test_schemas_update.py` (extra forbid, llm_model invalid, provider_chain duplicates/empty/max_length, error_policy bounds, empty payload), reproduire le même cas côté Zod et asserter le même verdict (accept/reject). Maintient l'invariant Sprint 1 que Zod miroite Pydantic 1:1.
  - [ ] T7.2 `make test` local : assert 0 failed, 0 errors, ≥ 435 backend (inchangé) + ≥ 45 frontend (33 baseline + ≥ 12 nouveaux).
  - [ ] T7.3 `make lint-backend` + `make lint-frontend` verts (AC8).
  - [ ] T7.4 Vérifier que `git grep "audit-event bypass cleanup"` retourne TOUJOURS 2 hits (Story 2.3 ne touche pas le backend, mais la convention reste invariante).
  - [ ] T7.5 Commit conventional `feat(m2): Story 2.3 — mode wizard guidé vs expert direct (Zod mirror Pydantic + Zustand persist)`. Story marquée `Review` dans le fichier `2-3-mode-wizard-vs-expert.md` et sprint-status.yaml `2-3-... → review`.

## Dev Notes

### Architecture & patterns

- **Aucun changement backend** — Story 2.3 est 100% frontend. Le backend Story 2.2 fournit déjà toutes les contraintes nécessaires (Pydantic `extra="forbid"`, `min_length`, `Field(ge=, le=)`, `Literal` enums, `field_validator` dedup, `model_validator` non-empty). Le rôle de Zod côté frontend = **fail fast UX** + **gates Wizard sans round-trip serveur**, mais le backend reste l'**autorité finale** (toute mismatch Zod/Pydantic est un bug Zod à corriger).
- **`FormState` partagé** — vit dans le composant parent `$templateId.tsx`. Wizard et Expert reçoivent `(formState, setFormState)` en props. Garantit AC4 (switch préserve state).
- **Pattern "derived state during render"** (Story 2.2 P-14) — réutilisé pour hydrater `formState` depuis `templateQuery.data` sans `useEffect` (évite `react-hooks/set-state-in-effect` ESLint).
- **Pattern Zustand persist Story 1.8** — reproduit 1:1 (`name`, `partialize`, `onRehydrateStorage`, `hasHydrated` flag). Aucune variation.
- **Pattern `mounted` flag pour ModeToggle** — réutilisé du `ModeToggle` theme Story 1.8 (suppress flicker au premier render avant rehydratation Zustand).
- **`react-hook-form` + `zodResolver`** — chaque étape Wizard et le formulaire Expert ont leur propre `useForm({ resolver: zodResolver(...) })`. Le `formState` global du parent est synchronisé via `onChange` callback (alternative : utiliser `RHF.formState.values` + `useEffect` pour push au parent — pattern à clarifier en T4 selon préférence dev).
- **Accordion shadcn** — `<Accordion type="multiple">` permet plusieurs sections ouvertes simultanément. `defaultValue={[...]}` ouvre toutes les sections au montage. État NON persisté Sprint 1.
- **Conventions queryKeys Story 2.2** — `["agent-template", id]` détail, `["agent-templates"]` liste future. Story 2.3 ne change rien.
- **import-linter / ESLint boundaries v6** — la feature `agent_registry` reste isolée. `$templateId.tsx` importe uniquement de `@/features/agent_registry` (barrel) + `@/shared/components/ui/*` + `@/shared/api/*`. Pas de cross-feature.

### Patterns de réutilisation

| Pattern Story 2.2 | À réutiliser tel quel | Localisation |
|---|---|---|
| `buildInitialForm`, `buildPayload`, `parse*` | EXTRAIRE dans `templateForm.ts` (T1) | `$templateId.tsx:88-194` (Story 2.2) |
| `focusFirstInvalidField` | EXTRAIRE dans `focusFirstInvalidField.ts` (T1) | `$templateId.tsx:200-227` (Story 2.2) |
| `useTemplate` + `useUpdateTemplate` | Réutiliser tel quel (hooks identiques) | `features/agent_registry/hooks.ts` |
| Toast feedback sonner | Identique (toast.success/error) | Pattern Story 2.1 |
| Pattern "derived state during render" | Réutiliser pour hydrater `formState` depuis `templateQuery.data` | `$templateId.tsx:240-256` (Story 2.2) |
| Atomicité backend P-02 | Réutilisé tel quel (aucun changement) | `service.py:update_template` Story 2.2 |
| RFC 7807 error handling + extract `errors[].loc[1]` | Réutilisé via `focusFirstInvalidField` extrait | Pattern Story 2.1 + 2.2 |

| Pattern Story 1.8 | À réutiliser tel quel | Localisation |
|---|---|---|
| Zustand persist middleware | Reproduire 1:1 pour `useModeStore` | `shared/state/sidebarStore.ts` |
| `mounted` flag pour ModeToggle | Reproduire pour `ConfigModeToggle` | `features/theme/ModeToggle.tsx` |

### Project Structure Notes

**Fichiers frontend NOUVEAUX :**

- `frontend/src/features/agent_registry/templateForm.ts` (T1.1) — helpers extraits Story 2.2
- `frontend/src/features/agent_registry/focusFirstInvalidField.ts` (T1.2) — DOM helper extrait Story 2.2
- `frontend/src/features/agent_registry/templateForm.test.ts` (T1.4)
- `frontend/src/features/agent_registry/schemas.ts` (T2.1) — Zod miroir Pydantic
- `frontend/src/features/agent_registry/schemas.test.ts` (T2.3)
- `frontend/src/features/agent_registry/modeStore.ts` (T3.1) — Zustand persist
- `frontend/src/features/agent_registry/modeStore.test.ts` (T3.4)
- `frontend/src/features/agent_registry/ConfigModeToggle.tsx` (T3.2)
- `frontend/src/features/agent_registry/ConfigModeToggle.test.tsx` (T3.4)
- `frontend/src/features/agent_registry/TemplateWizardForm.tsx` (T4.1)
- `frontend/src/features/agent_registry/TemplateWizardForm.test.tsx` (T4.6)
- `frontend/src/features/agent_registry/TemplateExpertForm.tsx` (T5.1)
- `frontend/src/features/agent_registry/TemplateExpertForm.test.tsx` (T5.5)

**Fichiers frontend MODIFIÉS :**

- `frontend/src/app/routes/config/agents/$templateId.tsx` (T6.1) — RÉÉCRITURE complète : remplace le formulaire flat 2.2 par host minimal Wizard/Expert toggle. **Le diff sera massif** (~500 lignes supprimées + ~80 lignes ajoutées) — c'est attendu, c'est le but de la story.
- `frontend/src/app/routes/config/agents/$templateId.test.tsx` (T6.2) — adapté à la nouvelle structure ; test AC4 ajouté.
- `frontend/src/features/agent_registry/index.ts` (T1.3 + T2.2 + T3.3) — exports étendus (templateForm, schemas, modeStore, ConfigModeToggle, TemplateWizardForm, TemplateExpertForm).
- `frontend/package.json` + `package-lock.json` (T0.1) — `+@hookform/resolvers ^3.x`.
- `frontend/src/styles/globals.css` (T0.2, conditionnel) — ajout keyframes `accordion-up` / `accordion-down` + utilités Tailwind v4 si manquantes.

**Fichiers NON modifiés (vérifier que ça reste vrai post-implémentation) :**

- Backend complet — aucun changement
- `frontend/src/features/agent_registry/hooks.ts` — `useTemplate` + `useUpdateTemplate` inchangés
- `frontend/src/features/agent_registry/api.ts` — inchangé
- `frontend/src/features/agent_registry/types.ts` — inchangé (les types Zod-inferred ajoutés en T2 vivent dans `schemas.ts`)
- `frontend/src/features/agent_registry/ArchetypeSelector.tsx` + `ArchetypePreview.tsx` + `icons.ts` (Story 2.1) — inchangés
- `Makefile`, `docker-compose.yml`, `.import-linter`, Migrations Alembic, ESLint config — inchangés

### Tech-debt status (post-Story 2.3)

- **D-F (Zod runtime validation)** : **fermé** — Zod schemas miroitent Pydantic, consommés par Wizard gates + Expert form validation.
- **D14 (contracts visuels)** : **partiellement fermé** — l'UX des contrats est améliorée (encapsulés dans accordéon Expert + étape Wizard dédiée + validation Zod inline) mais PAS de form builder visuel. Defer complet attend Sprint 4+ ou Story dédiée.
- **D-G (focus error best-effort)** : **inchangé** — `focusFirstInvalidField` extrait mais comportement identique. Sprint 4+ pourra raffiner pour les step-aware errors.
- **D11, D12, D13, D15** : inchangés (pas dans le scope de cette story).
- **NOUVEAU defer** :
  - **D16** : Pas de save partiel mid-Wizard (draft autosave) — défer Sprint 4+
  - **D17** : Pas de browser navigation guard "Unsaved changes" (`onbeforeunload`) — défer Sprint 4+
  - **D18** : Pas de swipe gestures mobile pour le Wizard — défer Sprint 4+
  - **D19** : Pas de raccourci clavier ← / → entre étapes Wizard explicite (Tab natif suffit Sprint 1) — défer Sprint 4+
  - **D20** : Pas de persistance Backend de la préférence Wizard/Expert (single-user `localStorage` Sprint 1) — défer Sprint 4+ multi-user

### References

- `_bmad-output/planning-artifacts/epics.md:833-854` — Story 2.3 spec verbatim (AC1-AC3 brut)
- `_bmad-output/planning-artifacts/prd.md:494-502` — FR9 / FR10 / FR11 (FR11 = scope direct 2.3)
- `_bmad-output/planning-artifacts/ux-design-specification.md:622-625` — Layout Config wizard 640px / Expert accordéons
- `_bmad-output/planning-artifacts/ux-design-specification.md:953-959` — UX-DR16 ModeToggle full spec
- `_bmad-output/planning-artifacts/ux-design-specification.md:897` — UX-DR24 Accordion expert mode
- `_bmad-output/planning-artifacts/ux-design-specification.md:1080-1095` — Form input specs (textarea prompt monospace, select LLM, ModeToggle wizard/expert)
- `_bmad-output/planning-artifacts/ux-design-specification.md:858-859` — Pattern Progressive Disclosure
- `_bmad-output/planning-artifacts/ux-design-specification.md:871` — Escape hatches (annuler workflow, fermer wizard, passer en expert)
- `_bmad-output/planning-artifacts/architecture.md:401-417` — Stack frontend (React 19, RHF v8 + Zod v4, Zustand v5)
- `_bmad-output/planning-artifacts/architecture.md:1168-1170` — Préservation contexte UX (search params + Zustand transient)
- `_bmad-output/implementation-artifacts/2-2-configurer-agent-complet.md` — Story 2.2 patterns + tech-debt (D14 contrats visuels, D-F Zod, D-G focus error)
- `_bmad-output/implementation-artifacts/epic-1-retro-2026-05-08.md` — décisions audit-event bypass + boundaries v6 + persistence patterns
- `frontend/src/app/routes/config/agents/$templateId.tsx` (Story 2.2) — formulaire flat à RÉÉCRIRE en host minimal
- `frontend/src/features/agent_registry/hooks.ts` (Story 2.2) — `useTemplate` + `useUpdateTemplate` à RÉUTILISER
- `frontend/src/features/agent_registry/types.ts` (Story 2.2) — types base à miroir via Zod (`z.infer`)
- `frontend/src/shared/state/sidebarStore.ts` (Story 1.8) — pattern Zustand persist exact à reproduire
- `frontend/src/features/theme/ModeToggle.tsx` (Story 1.8) — pattern `mounted` flag à reproduire
- `frontend/src/shared/components/ui/accordion.tsx` (Story 1.8) — primitive shadcn à consommer
- `backend/src/agentive_backend/features/m2_agent_registry/schemas.py` (Story 2.2) — Pydantic v2 schemas à miroir 1:1 en Zod
- Commit `7a0079b` — Story 2.2 done baseline
- Commit `c2f9154` — Story 2.2 fix-batch B1 + P-01..P-17 (D-F + D14 partial defer notes)

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (1M context) — bmad-dev-story single-pass execution.

### Debug Log References

- Test `getByLabelText(/system prompt/i)` ambigu en Expert mode (collide avec `<h2>System prompt</h2>` du heading et le AccordionTrigger label "System prompt"). Fix : utiliser `document.getElementById("tpl-system-prompt")` directement dans les tests.
- Test `getByLabelText(/provider chain/i)` collide aussi avec heading + label. Idem : `document.getElementById("tpl-provider-chain")`.
- Test AC4 : `findByLabelText(/prompt/i)` retournait le bouton du WizardProgress (label "Prompt") au lieu du textarea. Fix : `document.getElementById("tpl-system-prompt")` après `findByTestId("template-wizard-form")`.
- Test `submits PUT` : `getByRole("button", { name: /sauvegarder/i })` matche aussi le bouton Annuler dans certains rendus (sonner Toaster portal). Fix : `getByTestId("expert-save-button")` plus précis.
- Test `TemplateExpertForm.test.tsx` : `getByLabelText(/system prompt/i, { selector: "label" })` mal interprété — le `selector` est pour le contrôle, pas le label. Fix : `getByLabelText(/^prompt$/i)` plus spécifique au label "Prompt" du textarea.
- 1 itération sur le sed regex pour remplacer `<Label>` par `<label>` natif (pas de primitive `Label` dans le repo).

### Completion Notes List

- ✅ AC1 ConfigModeToggle UX-DR16 + useModeStore Zustand persist (key `agentive.agent-config-mode`, default `wizard`, `hasHydrated` flag pour suppress flicker)
- ✅ AC2 Wizard 5 étapes (Identité / Prompt / Contrats / LLM / ErrorPolicy+Validation), gates Zod par étape, progression cliquable backwards, bouton Annuler restore via `buildInitialForm`
- ✅ AC3 Expert 5 accordéons empilés défaut open (`type="multiple" defaultValue=ALL_SECTIONS`), tous les 12 champs visibles
- ✅ AC4 Switch Wizard ↔ Expert préserve `FormState` (vit dans le parent `$templateId.tsx`, props vers les deux modes) — test e2e `AC4 — switching Wizard ↔ Expert preserves the in-flight FormState`
- ✅ AC5 Helpers extraits Story 2.2 dans `templateForm.ts` (pure logic) + `focusFirstInvalidField.ts` (DOM helper) — réutilisés par les deux modes ET par le host `$templateId.tsx`
- ✅ AC6 Schemas Zod miroitant Pydantic backend (`schemas.ts`) — 6 schemas atomiques + 5 sub-schemas Wizard step gates + 1 schema racine `UpdateTemplateRequestSchema` strict + non-empty refine. **D-F fermé**.
- ✅ AC7 Tests : **+48 frontend (33 baseline → 81)** et 435 backend inchangé. Largement au-delà des ≥ 12 demandés. Cohérence Zod ↔ Pydantic vérifiée par 9 tests miroir dans `schemas.test.ts`.
- ✅ AC8 Lint backend (ruff + mypy) + frontend (eslint + tsc) verts. Pas d'`any` introduit. ConfigModeToggle accessible (`role="radiogroup"` + `aria-checked`).
- ✅ T0.1 `@hookform/resolvers ^5.2.2` ajouté à `package.json`.
- ✅ T0.2 Keyframes `accordion-up` / `accordion-down` ajoutées dans `src/styles/globals.css` (Tailwind v4 `@theme inline { --animate-* }` + `@keyframes`).
- ✅ Smoke test runtime non re-vérifié (Story 2.3 = 100% frontend, le smoke Story 2.2 reste valide). Vérification visuelle manuelle suggérée pour confirmer que les Tailwind animations rendent correctement à l'ouverture/fermeture des accordéons (test JSDOM ne couvre pas l'aspect visuel).

#### Décisions techniques d'implémentation

- **Validation Wizard step gates** : implémentée en **validation manuelle Zod** (pas `react-hook-form`) — l'état est lifted au parent et chaque étape valide son sub-schema sur clic "Suivant". Plus simple, évite le coût de sync RHF ↔ formState parent. `react-hook-form` + `@hookform/resolvers` reste installé pour usage futur (Story 2.4+ avec contracts visual editor).
- **`<label>` natif au lieu d'une primitive `Label`** : aucune primitive `Label` dans `shared/components/ui/`. Pattern Story 2.1/2.2 réutilisé (pas d'import de Radix Label, conservation de la cohérence).
- **`buildPayload`/`parseProviderChain`/`parseContract` extraits tels quels** : pas de modification de la logique Story 2.2. Le contrat de retour (discriminated union `{ok: true, payload}` ou `{ok: false, field, message}`) est conservé.
- **`focusFirstInvalidField` extrait dans son propre fichier** : couple DOM (`document.getElementById`), donc séparé de la pure logic `templateForm.ts`. Le mapping `field → input id` reste partagé.
- **Pattern derived state during render** réutilisé pour hydrater `formState` depuis `templateQuery.data` (cohérent Story 2.2 P-14, évite ESLint `react-hooks/set-state-in-effect`).

### File List

**Frontend NEW (12 + 1 globals.css edit)**
- `frontend/src/features/agent_registry/templateForm.ts` (T1.1)
- `frontend/src/features/agent_registry/templateForm.test.ts` (T1.4) — 16 tests
- `frontend/src/features/agent_registry/focusFirstInvalidField.ts` (T1.2)
- `frontend/src/features/agent_registry/schemas.ts` (T2.1) — 6 atomic + 5 wizard step + 1 root
- `frontend/src/features/agent_registry/schemas.test.ts` (T2.3) — 19 tests (incl. 9 cohérence Zod ↔ Pydantic)
- `frontend/src/features/agent_registry/modeStore.ts` (T3.1)
- `frontend/src/features/agent_registry/modeStore.test.ts` (T3.4) — 4 tests
- `frontend/src/features/agent_registry/ConfigModeToggle.tsx` (T3.2)
- `frontend/src/features/agent_registry/ConfigModeToggle.test.tsx` (T3.4) — 3 tests
- `frontend/src/features/agent_registry/TemplateWizardForm.tsx` (T4.1) — 5 steps + WizardProgress sub-component
- `frontend/src/features/agent_registry/TemplateWizardForm.test.tsx` (T4.6) — 2 tests (gate blocked + complete flow)
- `frontend/src/features/agent_registry/TemplateExpertForm.tsx` (T5.1) — 5 accordéons all-open
- `frontend/src/features/agent_registry/TemplateExpertForm.test.tsx` (T5.5) — 3 tests (render + submit + cancel restore)

**Frontend MODIFIED (3)**
- `frontend/src/app/routes/config/agents/$templateId.tsx` (T6.1) — RÉÉCRITURE COMPLÈTE : 466 lignes → 175 lignes (host minimal). Le formulaire flat Story 2.2 disparaît au profit du toggle Wizard/Expert.
- `frontend/src/app/routes/config/agents/$templateId.test.tsx` (T6.2) — adapté nouvelle structure (force mode `expert` au beforeEach pour tests existants Story 2.2 + nouveau test AC4 switch préserve state)
- `frontend/src/features/agent_registry/index.ts` — exports étendus (templateForm, schemas, modeStore, ConfigModeToggle, TemplateWizardForm, TemplateExpertForm, focusFirstInvalidField)
- `frontend/src/styles/globals.css` (T0.2) — ajout `--animate-accordion-up` / `--animate-accordion-down` + `@keyframes` Radix-compatible
- `frontend/package.json` + `package-lock.json` (T0.1) — `+@hookform/resolvers ^5.2.2`

**Backend & autres : aucun changement** — Story 2.3 = 100% frontend.
