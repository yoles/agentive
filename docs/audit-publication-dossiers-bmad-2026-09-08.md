# Audit de publication des dossiers BMAD

**Date :** 2026-09-08  
**Périmètre :** `_bmad/`, `_bmad-output/`, `.claude/skills/bmad-*` et leur présence dans l'historique Git local  
**Objet :** déterminer si ces contenus peuvent être publiés et identifier les informations sensibles

## Conclusion exécutive

**Recommandation : ne pas rendre les dossiers BMAD publics en l'état.**

Aucun secret actif évident (clé privée, jeton GitHub/OpenAI/Anthropic/AWS, etc.) n'a été détecté par les recherches de signatures réalisées. Les identifiants de base de données et tokens rencontrés semblent être des exemples ou des valeurs de test.

L'absence de secret brut ne rend cependant pas ces dossiers adaptés à une publication : `_bmad-output/` concentre des informations stratégiques et de sécurité importantes, notamment le PRD complet, la roadmap, l'architecture, les choix d'infrastructure, les dettes et vulnérabilités connues, des comptes rendus internes, des décisions nominatives et des diffs de code. Certains documents confirment également l'existence locale d'une vraie clé fournisseur et l'exécution d'un appel facturé, sans révéler la valeur de la clé.

Le risque global est donc **élevé pour `_bmad-output/`**, **faible à moyen pour `_bmad/` sur le plan de la confidentialité mais moyen sur les plans licence, marque et maintenance**, et **moyen pour `.claude/skills/bmad-*`**.

Enfin, ignorer ou supprimer ces fichiers dans le dernier commit ne suffit pas : ils existent déjà dans l'historique Git. `origin/main` contient encore 355 fichiers `_bmad`, 25 fichiers `_bmad-output` et 301 fichiers `.claude/skills/bmad-*`. `origin/staging` ne contient plus les deux premiers ensembles, mais conserve les 301 fichiers de skills.

## Inventaire

| Ensemble | Fichiers locaux | Taille approximative | État Git actuel | Risque de publication |
|---|---:|---:|---|---|
| `_bmad/` | 355 | 3,0 Mo | Ignoré, non suivi sur `staging` | Moyen |
| `_bmad-output/` | 46 | 2,5 Mo | Ignoré, non suivi sur `staging` | Élevé |
| `.claude/skills/bmad-*` | 301 dans 45 dossiers | inclus dans 2,5 Mo pour `.claude/skills` | Suivi par Git | Moyen |

Les règles d'exclusion sont explicitement documentées dans `.gitignore` : le commentaire indique que le framework et le planning BMAD sont « hors code » et ne doivent pas vivre dans le dépôt produit.

## Résultats détaillés

### 1. Secrets techniques

**Résultat : aucun secret actif évident détecté, avec réserve.**

Les contrôles ont recherché dans les fichiers actuels et les versions Git accessibles :

- clés privées PEM ;
- signatures courantes de jetons AWS, GitHub, OpenAI, Google et Slack ;
- URL de bases de données contenant un couple utilisateur/mot de passe ;
- affectations explicites de variables comme `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `AGENTIVE_API_TOKEN`, `SECRET_KEY` et `FERNET_KEY`.

Les correspondances restantes ont été qualifiées comme exemples, placeholders ou credentials de test : `change_me`, clés factices, domaines `example.com`/`.local`, ou mot de passe de base de données CI `test`.

Cette conclusion n'est pas une garantie cryptographique : aucun exécutable spécialisé tel que Gitleaks, TruffleHog ou detect-secrets n'était disponible dans l'environnement. Un scan dédié de l'intégralité du dépôt et de son historique reste obligatoire avant publication.

### 2. Information sensible sans être un secret

`_bmad-output/` révèle notamment :

- la vision du produit, ses innovations, ses personas, ses critères de succès et sa roadmap ;
- l'utilisation visée par une agence et certains domaines ou cas d'usage futurs ;
- l'architecture détaillée, les frontières de modules, le modèle de données, les endpoints, les flux et les choix de fournisseurs ;
- les mécanismes d'authentification, de chiffrement, de sandbox, de réseau, de proxy et de déploiement ;
- des dettes de sécurité et lacunes connues, dont des travaux de chiffrement ou de rotation de clés encore incomplets ;
- les constats de revues adversariales, avec sévérités Critical/High et détails de correction ;
- les incidents de développement, commandes de diagnostic, résultats de benchmarks et hypothèses de capacité ;
- des diffs `.review-*.diff` qui recopient de larges portions du code et peuvent conserver des versions anciennes ou supprimées ;
- des décisions internes datées et attribuées à une personne ;
- l'existence d'une clé Anthropic valide dans un `.env` local et d'un smoke test réel facturé, sans exposer la clé elle-même ;
- un sous-domaine d'agence utilisé dans une ancienne spécification.

Ces éléments faciliteraient la reconnaissance de l'architecture et des points faibles par un tiers. Ils ont aussi une valeur concurrentielle : le PRD et les brainstormings décrivent le positionnement et plusieurs fonctionnalités différenciantes avant leur livraison.

### 3. Données personnelles et identité

Les fichiers de configuration BMAD contiennent le prénom/pseudonyme `John`. Plusieurs livrables associent ensuite ce nom au rôle de propriétaire ou décideur. Un artefact plus récent associe explicitement `John` à `Yohann`.

Indépendamment des dossiers BMAD, les métadonnées Git contiennent le nom complet et une adresse professionnelle de l'auteur. Une publication du dépôt exposera ces métadonnées même si les documents BMAD sont exclus.

Le niveau est **modéré** : il ne s'agit pas de credentials, mais de données identifiantes et d'informations sur l'organisation du travail.

### 4. Licence, attribution et marque BMAD

Le framework installé est identifié comme BMAD 6.2.0. Les 355 fichiers `_bmad/` et les 301 fichiers de skills ressemblent à du contenu tiers installé/généré. Aucun fichier `LICENSE`, `NOTICE` ou `COPYING` propre à ces arborescences n'a été trouvé.

La licence racine du dépôt est MIT avec le copyright « Agentive contributors ». Elle ne conserve pas le copyright BMAD. Or le projet officiel BMAD est sous licence MIT et demande de conserver sa notice dans les copies ou portions substantielles. Sa documentation de marque précise aussi que les noms BMAD restent des marques et ne doivent pas suggérer une approbation officielle.

Conséquence : republier ces centaines de fichiers tels quels sous la seule licence racine crée un **risque d'attribution/licence évitable**. Si ces fichiers doivent réellement être redistribués, il faut conserver la notice BMAD appropriée, documenter leur provenance/version et respecter les règles de marque. Le choix le plus propre reste de ne pas vendorer le framework et de fournir une procédure d'installation.

Références officielles :

- https://github.com/bmad-code-org/BMAD-METHOD/blob/main/LICENSE
- https://github.com/bmad-code-org/BMAD-METHOD/blob/main/TRADEMARK.md

### 5. Exposition dans l'historique Git

Le commit `5bc2b50` a dé-tracké `_bmad/` et `_bmad-output/`, supprimant environ 64 317 lignes suivies dans 386 fichiers. Ce commit indique expressément qu'aucune réécriture d'historique n'a été effectuée.

État observé :

- `origin/main` : 355 fichiers `_bmad`, 25 fichiers `_bmad-output`, 301 fichiers `.claude/skills/bmad-*` ;
- `origin/staging` : 0 fichier `_bmad`, 0 fichier `_bmad-output`, 301 fichiers `.claude/skills/bmad-*` ;
- les anciens contenus restent accessibles dans les commits parents même après fusion du commit de suppression.

Si le dépôt est actuellement privé et doit devenir public, le publier avec son historique actuel publiera donc les artefacts historiques. Si le dépôt a déjà été public, il faut considérer les anciennes données comme potentiellement divulguées ; une réécriture ultérieure ne peut pas annuler les clones ou caches déjà réalisés.

## Décision recommandée par dossier

### `_bmad-output/` — ne pas publier

Conserver dans un dépôt privé ou un espace documentaire privé. Pour un dépôt open source, ne publier que des documents volontairement éditorialisés et expurgés, jamais le dossier généré complet ni les diffs de revue.

### `_bmad/` — ne pas vendorer sans nécessité

Ce dossier n'est pas particulièrement confidentiel hors configuration utilisateur, mais il ajoute beaucoup de contenu généré/tiers, du bruit et une obligation d'attribution. Préférer un script ou une instruction d'installation avec version épinglée. Si une copie est indispensable, retirer les configurations personnelles et ajouter les notices de licence/provenance requises.

### `.claude/skills/bmad-*` — retirer du dépôt public ou régulariser

Ces 301 fichiers sont encore suivis sur les deux branches distantes examinées. Préférer leur génération lors du setup. Sinon, documenter clairement qu'ils proviennent de BMAD, conserver la licence tierce et vérifier les règles de marque. Aucun secret évident n'y a été détecté.

## Plan d'action avant passage en public

1. **Ne pas basculer la visibilité du dépôt maintenant.**
2. Garder `_bmad/` et `_bmad-output/` dans `.gitignore`.
3. Retirer ou régulariser `.claude/skills/bmad-*` selon le choix « installation » ou « vendoring avec attribution ».
4. Créer de préférence un nouveau dépôt public à historique propre, ou une branche orpheline/snapshot nettoyé, plutôt que publier l'historique actuel.
5. Si l'historique doit absolument être conservé, le réécrire avec `git filter-repo`, faire valider le résultat, puis coordonner le remplacement des branches distantes. Cette opération est destructive pour les clones existants et doit être planifiée séparément.
6. Lancer Gitleaks et, idéalement, un second scanner sur **tous les commits et toutes les refs** du candidat public.
7. Relire et expurger tout document public : noms/personnes, domaine d'agence, clients, stratégie, roadmap non annoncée, détails de sécurité, dettes/vulnérabilités, logs et confirmations relatives aux clés locales.
8. Exclure systématiquement les `.review-*.diff` et autres captures brutes de revue.
9. Vérifier les métadonnées Git (nom et e-mail des auteurs) et décider si leur publication est acceptée.
10. Ajouter un inventaire des composants tiers et les notices nécessaires, notamment BMAD si du contenu BMAD reste distribué.
11. Si une clé réelle a pu être copiée ailleurs dans le dépôt ou une ref non inspectée, la révoquer et la remplacer avant publication ; ne pas se contenter de supprimer son fichier.

## Option de publication raisonnable

Le code Agentive peut être publié sans les dossiers BMAD. Une structure sûre serait :

- code source et tests ;
- README public rédigé volontairement ;
- architecture publique simplifiée, sans topologie opérationnelle ni dettes exploitables ;
- roadmap limitée aux éléments déjà annoncés ;
- procédure locale pour installer BMAD ;
- artefacts de planification et revues détaillées conservés dans un espace privé.

## Méthode et limites

L'audit a utilisé l'inventaire des fichiers, `git status`, `git ls-files`, `git ls-tree`, `git log`, `git grep`, des recherches d'expressions régulières et une lecture ciblée des documents les plus exposants. Les valeurs potentiellement sensibles ont été masquées pendant l'analyse.

Limites : pas de validation de tous les formats possibles de secrets à haute entropie, pas d'analyse des objets Git inaccessibles par les refs locales, pas d'inspection des caches GitHub ni des forks, et pas d'avis juridique. La conclusion de licence repose sur les fichiers locaux et la licence officielle BMAD consultée à la date de l'audit.
