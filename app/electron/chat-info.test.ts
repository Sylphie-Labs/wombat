import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { parseChatHandshake, readChatInfo } from "./chat-info";

/**
 * TK-223 (Q-111(a)): readChatInfo is exercised end-to-end against a
 * throwaway temp directory standing in for the backend root - real
 * node:fs reads, no mocking - mirroring env-config.test.ts.
 */

describe("parseChatHandshake", () => {
  it("accepts a well-formed handshake body", () => {
    expect(parseChatHandshake('{"port": 54321, "token": "abc123"}')).toEqual({
      port: 54321,
      token: "abc123",
    });
  });

  it("rejects malformed JSON", () => {
    expect(parseChatHandshake("not json")).toBeNull();
  });

  it("rejects a JSON array", () => {
    expect(parseChatHandshake('[54321, "abc123"]')).toBeNull();
  });

  it("rejects a missing port", () => {
    expect(parseChatHandshake('{"token": "abc123"}')).toBeNull();
  });

  it("rejects a non-integer port", () => {
    expect(parseChatHandshake('{"port": "54321", "token": "abc123"}')).toBeNull();
  });

  it("rejects a missing token", () => {
    expect(parseChatHandshake('{"port": 54321}')).toBeNull();
  });

  it("rejects an empty token", () => {
    expect(parseChatHandshake('{"port": 54321, "token": ""}')).toBeNull();
  });
});

describe("readChatInfo", () => {
  let tempDir: string;

  afterEach(() => {
    if (tempDir) {
      rmSync(tempDir, { recursive: true, force: true });
    }
  });

  it("returns null when WOMBAT_CHAT_HANDSHAKE_FILE is unset (chat disabled)", () => {
    tempDir = mkdtempSync(path.join(os.tmpdir(), "wombat-chat-info-"));
    expect(readChatInfo({ WOMBAT_BACKEND_CWD: tempDir }, tempDir)).toBeNull();
  });

  it("reads a relative handshake path resolved against the backend root", () => {
    tempDir = mkdtempSync(path.join(os.tmpdir(), "wombat-chat-info-"));
    writeFileSync(
      path.join(tempDir, "chat-handshake.json"),
      JSON.stringify({ port: 5555, token: "tok-xyz" }),
    );

    const env = {
      WOMBAT_BACKEND_CWD: tempDir,
      WOMBAT_CHAT_HANDSHAKE_FILE: "chat-handshake.json",
    };
    expect(readChatInfo(env, tempDir)).toEqual({ port: 5555, token: "tok-xyz" });
  });

  it("reads the handshake path from a .env file at the backend root (env-var precedence)", () => {
    tempDir = mkdtempSync(path.join(os.tmpdir(), "wombat-chat-info-"));
    writeFileSync(
      path.join(tempDir, "chat-handshake.json"),
      JSON.stringify({ port: 6001, token: "tok-dotenv" }),
    );
    writeFileSync(
      path.join(tempDir, ".env"),
      "WOMBAT_CHAT_HANDSHAKE_FILE=chat-handshake.json\n",
    );

    expect(readChatInfo({ WOMBAT_BACKEND_CWD: tempDir }, tempDir)).toEqual({
      port: 6001,
      token: "tok-dotenv",
    });
  });

  it("an absolute handshake path is used as-is", () => {
    tempDir = mkdtempSync(path.join(os.tmpdir(), "wombat-chat-info-"));
    const absolutePath = path.join(tempDir, "elsewhere.json");
    writeFileSync(absolutePath, JSON.stringify({ port: 7002, token: "tok-abs" }));

    const env = { WOMBAT_BACKEND_CWD: tempDir, WOMBAT_CHAT_HANDSHAKE_FILE: absolutePath };
    expect(readChatInfo(env, tempDir)).toEqual({ port: 7002, token: "tok-abs" });
  });

  it("returns null (never throws) when the configured file does not exist", () => {
    tempDir = mkdtempSync(path.join(os.tmpdir(), "wombat-chat-info-"));
    const env = { WOMBAT_BACKEND_CWD: tempDir, WOMBAT_CHAT_HANDSHAKE_FILE: "missing.json" };
    expect(readChatInfo(env, tempDir)).toBeNull();
  });

  it("returns null when the handshake file contains malformed JSON", () => {
    tempDir = mkdtempSync(path.join(os.tmpdir(), "wombat-chat-info-"));
    writeFileSync(path.join(tempDir, "chat-handshake.json"), "not json");
    const env = {
      WOMBAT_BACKEND_CWD: tempDir,
      WOMBAT_CHAT_HANDSHAKE_FILE: "chat-handshake.json",
    };
    expect(readChatInfo(env, tempDir)).toBeNull();
  });
});
