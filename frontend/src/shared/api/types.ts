/**
 * OpenAPI types — placeholder auto-généré.
 *
 * En Sprint 0, ce fichier est vide. Il sera généré automatiquement via :
 *     make gen-api-types
 * qui exécute `openapi-typescript http://backend:8000/openapi.json -o src/shared/api/types.ts`
 *
 * Voir architecture.md § Frontend Architecture.
 */

export type HealthResponse = {
  status: "ok";
  version: string;
};

export type ReadyResponse = {
  status: "ready" | "not_ready";
  checks: Record<string, string>;
};
