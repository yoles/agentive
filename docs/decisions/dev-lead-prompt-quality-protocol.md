# Juger la qualité du prompt de décomposition (Story 5.1)

**Status:** Accepted (Sprint 2)
**Date:** 2026-09-14
**Story:** 5.1 — Dev Lead, orchestrateur
**Origine:** T8.3, relevée en `intent_gap` par la revue de code (tâche cochée, moitié faite)

## Décision

**La qualité du prompt de décomposition n'est pas prouvée, et ne le sera pas
par la suite de tests.** Elle se juge manuellement, avec de vraies clés, selon
le protocole de `docs/runbooks/pole-dev.md` § 7 — et le résultat de ce
jugement se consigne dans la table de ce même paragraphe.

**Tant que cette table est vide, la Story 5.1 ne doit pas prétendre que
l'AC3 est vérifiée sur le fond.** Elle est vérifiée sur la forme : le contrat
traverse le moteur sans déformation.

## Problème

L'AC3 demande que « sur 3 cas, la décomposition soit cohérente et les agents
assignés logiques ». Les tests E2E tournent sur `MockProvider` : les sorties
sont pré-écrites. Ils prouvent que du JSON conforme traverse le moteur, que
les rôles appartiennent au jeu fermé et que le plan est cohérent avec ses
délégations — ils ne prouvent **rien** sur ce que le modèle produirait.

T8.3 demandait le protocole « **et ce qu'il a donné** ». Seule la première
moitié a été faite ; la tâche était cochée.

## Options

### A. Test d'intégration avec de vraies clés en CI — rejetée

Non déterministe, coûteux, et il transformerait une régression de provider en
échec de CI. Le dépôt a déjà tranché ce point ailleurs (`make spike-m3-real`
vs `make spike-m3-mock`, Story 1.2) : la reproductibilité est mockée, le
jugement est réel et manuel.

### B. LLM-as-judge sur les 3 cas — rejetée pour le Sprint 2

Déplace la question sans la résoudre : il faudrait alors prouver le juge. À
reconsidérer quand il y aura assez de cas pour qu'un jugement manuel ne passe
plus à l'échelle — ce n'est pas le cas avec trois.

### C. Protocole manuel consigné, table de résultat versionnée — **retenue**

Le protocole vit dans le runbook, à côté du `curl` qui le déclenche. La table
de résultat est versionnée avec lui : une table vide est une **affirmation
visible** qu'il n'a jamais tourné, là où l'absence de table laissait croire
que la question ne se posait pas.

## Conséquences

- La table `docs/runbooks/pole-dev.md` § 7 est aujourd'hui **vide et marquée
  comme telle**. C'est l'état honnête.
- Elle doit être remplie au premier passage avec de vraies clés. C'est une
  action **humaine**, qui demande des clés que la CI n'a pas.
- Le jugement est à refaire à chaque modification substantielle du
  `system_prompt` du Dev Lead : le `bump_version` d'`update_template` en est
  le déclencheur naturel.
