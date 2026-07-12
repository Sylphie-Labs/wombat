import { describe, expect, it, vi } from "vitest";

import { gmailMessageUrl, isValidGmailMessageId, openGmailMessage } from "./gmail-open";

/**
 * TK-251 AC2: the IPC handler's validation + external-open behavior, against
 * a fake `openExternal` - no real Electron `shell` in this suite.
 */

describe("isValidGmailMessageId", () => {
  it("accepts a plain URL-safe token", () => {
    expect(isValidGmailMessageId("18c9a1b2f3d4e5f6")).toBe(true);
    expect(isValidGmailMessageId("abc-DEF_123")).toBe(true);
  });

  it("rejects a non-string, empty, or unsafe value", () => {
    expect(isValidGmailMessageId("")).toBe(false);
    expect(isValidGmailMessageId(123)).toBe(false);
    expect(isValidGmailMessageId(null)).toBe(false);
    expect(isValidGmailMessageId(undefined)).toBe(false);
    expect(isValidGmailMessageId("https://evil.example/")).toBe(false);
    expect(isValidGmailMessageId("has spaces")).toBe(false);
    expect(isValidGmailMessageId("javascript:alert(1)")).toBe(false);
  });
});

describe("gmailMessageUrl", () => {
  it("builds the Gmail web URL from a validated message id", () => {
    expect(gmailMessageUrl("abc123")).toBe("https://mail.google.com/mail/#all/abc123");
  });
});

describe("openGmailMessage", () => {
  it("opens the constructed Gmail URL externally for a valid message id", async () => {
    const openExternal = vi.fn().mockResolvedValue(undefined);

    const result = await openGmailMessage("abc123", openExternal);

    expect(result).toEqual({ ok: true });
    expect(openExternal).toHaveBeenCalledWith("https://mail.google.com/mail/#all/abc123");
  });

  it("never invokes openExternal for an invalid message id", async () => {
    const openExternal = vi.fn().mockResolvedValue(undefined);

    const result = await openGmailMessage("not a url-safe token!", openExternal);

    expect(result).toEqual({ ok: false, reason: "invalid-message-id" });
    expect(openExternal).not.toHaveBeenCalled();
  });
});
