import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.resetModules();
});

async function captureIdentity(authMode: string, identityMode: "customer" | "approver") {
  vi.stubEnv("VITE_TICKETPILOT_AUTH_MODE", authMode);
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
  vi.stubGlobal("fetch", fetchMock);
  const { api } = await import("./api");
  await api.identity(identityMode);
  return new Headers(fetchMock.mock.calls[0][1].headers);
}

describe("API authentication", () => {
  it("keeps local demo bearer authentication", async () => {
    const headers = await captureIdentity("bearer", "customer");
    expect(headers.get("Authorization")).toBe("Bearer demo-customer-token");
    expect(headers.has("X-TicketPilot-Identity")).toBe(false);
  });

  it.each(["customer", "approver"] as const)(
    "lets the gateway authenticate the browser and select the %s demo identity",
    async (mode) => {
      const headers = await captureIdentity("gateway", mode);
      expect(headers.has("Authorization")).toBe(false);
      expect(headers.get("X-TicketPilot-Identity")).toBe(mode);
    },
  );

  it("preserves idempotency headers in gateway mode", async () => {
    vi.stubEnv("VITE_TICKETPILOT_AUTH_MODE", "gateway");
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);
    const { api } = await import("./api");
    await api.createTicket({ subject: "test", message: "物流到哪了" }, "same-request");
    const headers = new Headers(fetchMock.mock.calls[0][1].headers);
    expect(headers.get("Idempotency-Key")).toBe("same-request");
    expect(headers.get("X-TicketPilot-Identity")).toBe("customer");
  });
});
