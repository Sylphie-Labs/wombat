/**
 * TK-251 (RULING r3, contract v2.75 - binding): the "open in Gmail" bridge.
 * A row click in the renderer passes ONLY the `message_id` (never a
 * renderer-supplied URL) - THIS module validates it as a plain URL-safe
 * token, constructs the `https://mail.google.com/mail/#all/{message_id}`
 * URL itself, and hands it to `shell.openExternal` (injected so this module
 * stays testable without a real Electron `shell`). There is no in-app
 * navigation path - a failed validation never reaches `openExternal`.
 */

// Gmail message ids are hex-like tokens in practice, but the guard is
// intentionally generic: any plain URL-safe token (letters, digits, `-`, `_`)
// is accepted, anything else is rejected before a URL is ever built.
const MESSAGE_ID_PATTERN = /^[A-Za-z0-9_-]+$/;

export type OpenGmailMessageResult =
  | { readonly ok: true }
  | { readonly ok: false; readonly reason: "invalid-message-id" };

export function isValidGmailMessageId(messageId: unknown): messageId is string {
  return typeof messageId === "string" && MESSAGE_ID_PATTERN.test(messageId);
}

/** Builds the Gmail web URL for a validated message id - never called with an unvalidated value. */
export function gmailMessageUrl(messageId: string): string {
  return `https://mail.google.com/mail/#all/${messageId}`;
}

/**
 * Validates `messageId`, then opens the corresponding Gmail URL externally
 * via `openExternal` (main.ts wires this to `shell.openExternal`). An
 * invalid id never reaches `openExternal` - no URL is ever constructed from
 * unvalidated renderer input.
 */
export async function openGmailMessage(
  messageId: unknown,
  openExternal: (url: string) => Promise<void>,
): Promise<OpenGmailMessageResult> {
  if (!isValidGmailMessageId(messageId)) {
    return { ok: false, reason: "invalid-message-id" };
  }
  await openExternal(gmailMessageUrl(messageId));
  return { ok: true };
}
