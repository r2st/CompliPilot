import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { API_BASE, apiRequest, ApiError, tokenStore } from "./api";

const TOKENS = {
  access_token: "access-1",
  refresh_token: "refresh-1",
  expires_in: 3600,
  user: { id: 1, organization_id: 1, email: "a@b.com", full_name: "A B", phone: null, role: "admin" as const, is_active: true, has_totp: false, last_login_at: null, created_at: "2026-01-01T00:00:00Z" },
};

function jsonResponse(status: number, body: unknown) {
  return {
    status,
    ok: status >= 200 && status < 300,
    text: async () => JSON.stringify(body),
  } as Response;
}

describe("tokenStore", () => {
  afterEach(() => {
    tokenStore.clear();
  });

  it("round-trips a session through localStorage", () => {
    tokenStore.setSession(TOKENS);
    expect(tokenStore.getAccess()).toBe("access-1");
    expect(tokenStore.getRefresh()).toBe("refresh-1");
    expect(JSON.parse(tokenStore.getUserRaw()!)).toMatchObject({ email: "a@b.com" });
  });

  it("clears every key", () => {
    tokenStore.setSession(TOKENS);
    tokenStore.clear();
    expect(tokenStore.getAccess()).toBeNull();
    expect(tokenStore.getRefresh()).toBeNull();
    expect(tokenStore.getUserRaw()).toBeNull();
  });
});

describe("apiRequest", () => {
  beforeEach(() => {
    tokenStore.clear();
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("builds the URL with the API prefix and query params, dropping empty ones", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { ok: true }));

    await apiRequest("/dpdp/consents", {
      query: { limit: 50, purpose: "", granted_only: false, principal_ref: undefined },
      auth: false,
    });

    const calledUrl = fetchMock.mock.calls[0][0] as string;
    expect(calledUrl).toBe(`${API_BASE}/api/v1/dpdp/consents?limit=50&granted_only=false`);
  });

  it("attaches a bearer token when authenticated and auth is not disabled", async () => {
    tokenStore.setSession(TOKENS);
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { ok: true }));

    await apiRequest("/dashboard");

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>)["Authorization"]).toBe("Bearer access-1");
  });

  it("omits the Authorization header when auth is disabled", async () => {
    tokenStore.setSession(TOKENS);
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { ok: true }));

    await apiRequest("/auth/login", { auth: false });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>)["Authorization"]).toBeUndefined();
  });

  it("throws an ApiError with the server's code and message on failure", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(
      jsonResponse(409, { error: { code: "conflict", message: "Already withdrawn" } })
    );

    await expect(apiRequest("/dpdp/consents/1/withdraw", { method: "POST", auth: false })).rejects.toMatchObject({
      status: 409,
      code: "conflict",
      message: "Already withdrawn",
    });
  });

  it("treats a 204 response as no content", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce({ status: 204, ok: true, text: async () => "" } as Response);

    const result = await apiRequest("/documents/1", { method: "DELETE" });
    expect(result).toBeUndefined();
  });

  it("retries once after a silent refresh on 401, then gives up and clears the session", async () => {
    tokenStore.setSession(TOKENS);
    const fetchMock = vi.mocked(fetch);
    // First call: the protected request, unauthorized.
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { error: { code: "unauthorized", message: "Expired" } }));
    // Second call: the refresh attempt, also failing.
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { error: { code: "unauthorized", message: "Expired" } }));

    await expect(apiRequest("/dashboard")).rejects.toBeInstanceOf(ApiError);
    expect(tokenStore.getAccess()).toBeNull();
  });
});
