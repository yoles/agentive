/**
 * Tests — `templateForm.ts` (Story 2.3 T1.4).
 *
 * Couvre les helpers extraits de Story 2.2 (`buildPayload` discriminated
 * union, `parseProviderChain` shape validation, `parseContract` shape).
 */

import { describe, expect, it } from "vitest";
import {
  buildInitialForm,
  buildPayload,
  parseContract,
  parseProviderChain,
} from "./templateForm";

describe("buildInitialForm", () => {
  it("does not pre-fill system_prompt from prompt_base archetype baseline (P-01/P-02)", () => {
    const config = {
      prompt_base: "Tu es un Producteur",
      role: "producer",
    };
    const form = buildInitialForm(config);
    expect(form.system_prompt).toBe("");
  });

  it("hydrates system_prompt when explicitly set in config", () => {
    const form = buildInitialForm({ system_prompt: "Custom prompt" });
    expect(form.system_prompt).toBe("Custom prompt");
  });

  it("falls back to defaults when config is empty", () => {
    const form = buildInitialForm({});
    expect(form.llm_model).toBe("claude-3-5-sonnet-20241022");
    expect(form.temperature).toBe(0.7);
    expect(form.max_tokens).toBe(4096);
    expect(form.on_timeout).toBe("retry_with_backoff");
    expect(JSON.parse(form.provider_chain_raw)).toEqual(["anthropic"]);
  });
});

describe("parseProviderChain", () => {
  it("returns parsed array on valid input", () => {
    expect(parseProviderChain('["anthropic", "openai"]')).toEqual(["anthropic", "openai"]);
  });

  it("returns error string on malformed JSON", () => {
    const result = parseProviderChain("not json");
    expect(typeof result).toBe("string");
    expect(result).toContain("JSON invalide");
  });

  it("returns error string on non-array (e.g. number)", () => {
    expect(typeof parseProviderChain("42")).toBe("string");
  });

  it("returns error string on empty array", () => {
    expect(typeof parseProviderChain("[]")).toBe("string");
  });

  it("returns error string when array contains non-strings", () => {
    expect(typeof parseProviderChain('[1, 2, 3]')).toBe("string");
  });
});

describe("parseContract", () => {
  it("returns parsed object on valid input with core+extras", () => {
    const r = parseContract('{"core": {"q": "string"}, "extras": {}}');
    expect(r).toEqual({ core: { q: "string" }, extras: {} });
  });

  it("returns error string on malformed JSON", () => {
    expect(typeof parseContract("not json")).toBe("string");
  });

  it("returns error string when not an object (array)", () => {
    expect(typeof parseContract("[]")).toBe("string");
  });

  it("returns error string when core is not an object", () => {
    expect(typeof parseContract('{"core": "string", "extras": {}}')).toBe("string");
  });
});

describe("buildPayload", () => {
  const validForm = {
    system_prompt: "v2 prompt",
    llm_model: "claude-3-5-sonnet-20241022" as const,
    temperature: 0.5,
    max_tokens: 2048,
    provider_chain_raw: '["anthropic"]',
    input_contract_raw: '{"core": {"q": "string"}, "extras": {}}',
    output_contract_raw: '{"core": {"a": "string"}, "extras": {}}',
    on_timeout: "retry_with_backoff" as const,
    max_retries: 3,
    backoff_strategy: "exponential" as const,
  };

  it("returns ok=true with merged payload on valid input", () => {
    const r = buildPayload(validForm);
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.payload.system_prompt).toBe("v2 prompt");
      expect(r.payload.llm_model).toBe("claude-3-5-sonnet-20241022");
      expect(r.payload.provider_chain).toEqual(["anthropic"]);
      expect(r.payload.error_policy?.max_retries).toBe(3);
    }
  });

  it("returns ok=false with field=provider_chain on invalid JSON", () => {
    const r = buildPayload({ ...validForm, provider_chain_raw: "not json" });
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.field).toBe("provider_chain");
    }
  });

  it("returns ok=false with field=input_contract on invalid contract shape", () => {
    const r = buildPayload({
      ...validForm,
      input_contract_raw: '{"core": "not-a-dict", "extras": {}}',
    });
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.field).toBe("input_contract");
    }
  });

  it("omits system_prompt from payload when blank (P-01 backend skip bump)", () => {
    const r = buildPayload({ ...validForm, system_prompt: "   " });
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.payload.system_prompt).toBeUndefined();
    }
  });
});
