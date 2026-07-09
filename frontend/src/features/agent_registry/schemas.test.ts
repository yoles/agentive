/**
 * Tests Zod — `schemas.ts` (Story 2.3 T2.3 + T7.1 cohérence Zod ↔ Pydantic).
 *
 * Pour chaque cas testé côté backend dans
 * `backend/tests/unit/agent_registry/test_schemas_update.py`, on
 * reproduit le même cas en Zod et on asserte le même verdict (accept/reject).
 * Maintient l'invariant Sprint 1 : Zod miroite Pydantic 1:1.
 */

import { describe, expect, it } from "vitest";
import {
  ContractDefinitionSchema,
  ErrorPolicySchema,
  LLMParamsSchema,
  ProviderIdSchema,
  UpdateTemplateRequestSchema,
} from "./schemas";

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// UpdateTemplateRequest — strict + non-empty
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("UpdateTemplateRequestSchema (Zod ↔ Pydantic mirror)", () => {
  it("accepts a full payload (mirror of test_update_request_accepts_full_payload)", () => {
    const result = UpdateTemplateRequestSchema.safeParse({
      system_prompt: "Tu es un agent Producteur expert TypeScript.",
      input_contract: { core: { task: "string" }, extras: {} },
      output_contract: { core: { code: "string" }, extras: {} },
      llm_model: "claude-3-5-sonnet-20241022",
      llm_params: { temperature: 0.2, max_tokens: 4096 },
      provider_chain: ["anthropic"],
      error_policy: {
        on_timeout: "retry_with_backoff",
        max_retries: 3,
        backoff_strategy: "exponential",
      },
    });
    expect(result.success).toBe(true);
  });

  it("accepts a partial payload (mirror of test_update_request_accepts_partial_payload)", () => {
    const result = UpdateTemplateRequestSchema.safeParse({
      system_prompt: "Just the prompt.",
    });
    expect(result.success).toBe(true);
  });

  it("rejects unknown extra fields (mirror of test_update_request_extra_field_forbidden)", () => {
    const result = UpdateTemplateRequestSchema.safeParse({
      system_prompt: "x",
      rogue_field: "should fail",
    });
    expect(result.success).toBe(false);
  });

  it("rejects llm_model outside the whitelist (mirror of test_update_request_invalid_llm_model)", () => {
    const result = UpdateTemplateRequestSchema.safeParse({
      llm_model: "gpt-5-omni-1234",
    });
    expect(result.success).toBe(false);
  });

  it("rejects provider_chain with unknown member (mirror of test_update_request_invalid_provider_chain_member)", () => {
    const result = UpdateTemplateRequestSchema.safeParse({
      provider_chain: ["mistral"],
    });
    expect(result.success).toBe(false);
  });

  it("rejects provider_chain longer than max_length 4 (mirror of test_update_request_provider_chain_max_length)", () => {
    const result = UpdateTemplateRequestSchema.safeParse({
      provider_chain: ["anthropic", "openai", "anthropic", "openai", "anthropic"],
    });
    expect(result.success).toBe(false);
  });

  it("rejects provider_chain with duplicates (mirror of test_update_request_provider_chain_no_duplicates_p13)", () => {
    const result = UpdateTemplateRequestSchema.safeParse({
      provider_chain: ["anthropic", "openai", "anthropic"],
    });
    expect(result.success).toBe(false);
  });

  it("rejects empty payload `{}` (mirror of test_update_request_empty_payload_rejected_p03)", () => {
    const result = UpdateTemplateRequestSchema.safeParse({});
    expect(result.success).toBe(false);
  });

  it("rejects payload with all-null fields (mirror of test_update_request_all_none_rejected_p03)", () => {
    const result = UpdateTemplateRequestSchema.safeParse({
      system_prompt: null,
      input_contract: null,
      output_contract: null,
      llm_model: null,
      llm_params: null,
      provider_chain: null,
      error_policy: null,
    });
    expect(result.success).toBe(false);
  });
});

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// ContractDefinition — permissif (B1 amend Story 2.2)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("ContractDefinitionSchema (B1 permissif)", () => {
  it("accepts {} → core et extras default à {}", () => {
    const result = ContractDefinitionSchema.safeParse({});
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.core).toEqual({});
      expect(result.data.extras).toEqual({});
    }
  });

  it("accepts core+extras populated", () => {
    const result = ContractDefinitionSchema.safeParse({
      core: { q: "string" },
      extras: { meta: { nested: "ok" } },
    });
    expect(result.success).toBe(true);
  });

  it("rejects core that is not an object", () => {
    const result = ContractDefinitionSchema.safeParse({ core: "not-a-dict", extras: {} });
    expect(result.success).toBe(false);
  });
});

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// ErrorPolicy / LLMParams (bounds, defaults)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

describe("ErrorPolicySchema (NFR14 bounds)", () => {
  it("accepts defaults via {}", () => {
    const r = ErrorPolicySchema.safeParse({});
    expect(r.success).toBe(true);
    if (r.success) {
      expect(r.data.on_timeout).toBe("retry_with_backoff");
      expect(r.data.max_retries).toBe(3);
      expect(r.data.backoff_strategy).toBe("exponential");
    }
  });

  it("rejects max_retries out of [0, 10]", () => {
    expect(ErrorPolicySchema.safeParse({ max_retries: -1 }).success).toBe(false);
    expect(ErrorPolicySchema.safeParse({ max_retries: 11 }).success).toBe(false);
  });

  it("rejects on_timeout outside enum", () => {
    expect(
      ErrorPolicySchema.safeParse({ on_timeout: "ignore_silently" }).success,
    ).toBe(false);
  });
});

describe("LLMParamsSchema (bounds)", () => {
  it("rejects temperature outside [0, 2]", () => {
    expect(LLMParamsSchema.safeParse({ temperature: -0.1, max_tokens: 100 }).success).toBe(
      false,
    );
    expect(LLMParamsSchema.safeParse({ temperature: 2.1, max_tokens: 100 }).success).toBe(
      false,
    );
  });

  it("rejects max_tokens outside [1, 200_000]", () => {
    expect(LLMParamsSchema.safeParse({ temperature: 0.7, max_tokens: 0 }).success).toBe(
      false,
    );
    expect(
      LLMParamsSchema.safeParse({ temperature: 0.7, max_tokens: 200_001 }).success,
    ).toBe(false);
  });
});

describe("ProviderIdSchema", () => {
  it("accepts whitelisted providers", () => {
    expect(ProviderIdSchema.safeParse("anthropic").success).toBe(true);
    expect(ProviderIdSchema.safeParse("openai").success).toBe(true);
  });
  it("rejects unknown providers", () => {
    expect(ProviderIdSchema.safeParse("mistral").success).toBe(false);
  });
});
