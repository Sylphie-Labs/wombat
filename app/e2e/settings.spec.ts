import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

import { _electron as electron, expect, test, type ElectronApplication } from "@playwright/test";

/**
 * TK-201: the whole-app dev-launch smoke (EP-32's proof). Drives the REAL
 * Electron UI end to end - set the assistant name, select the fish TTS
 * provider, enter a fake key, save - then proves the round trip landed where
 * the runtime actually reads it (the backend-cwd pin, Q-111(c/d)):
 *
 *   1. AC1 happy path: the edit lands in a throwaway-cwd wombat.settings.json
 *      AND a throwaway-service keyring account, and a spawned Python check
 *      proves load_config() + voice.select.build_tts_adapter resolves the
 *      fish provider from exactly that state (no live cloud call - DEF-7).
 *   2. AC2 security: a tokenless request to the settings API is 401; the
 *      renderer exposes no Node globals; no token ever appears in a URL.
 *
 * Deliberately ONE spec (complexity_budget) - the Python-side API tests
 * (tests/settings_app/test_api.py) carry the correctness load; this only
 * proves the whole chain is wired together.
 */

const FAKE_VOICE_ID = "e2e-fake-voice-id";
const FAKE_FISH_KEY = "e2e-fake-fish-key";
const ASSISTANT_NAME = "E2E Steward";
const KEYRING_SERVICE = `wombat-e2e-${process.pid}`;

/** WOMBAT_PYTHON: the caller's env, else the repo's own venv, else "python". */
function resolvePythonCommand(): string {
  if (process.env.WOMBAT_PYTHON) {
    return process.env.WOMBAT_PYTHON;
  }
  const venvPython = path.resolve(process.cwd(), "..", ".venv", "Scripts", "python.exe");
  if (existsSync(venvPython)) {
    return venvPython;
  }
  return "python";
}

/** Runs a `python -c <code>` snippet, returning trimmed stdout. Throws on nonzero exit. */
function runPython(
  python: string,
  code: string,
  options: { cwd?: string; env?: NodeJS.ProcessEnv } = {},
): string {
  const result = spawnSync(python, ["-c", code], {
    cwd: options.cwd,
    env: options.env ?? process.env,
    encoding: "utf-8",
  });
  if (result.status !== 0) {
    throw new Error(
      `python -c failed (exit ${String(result.status)}): ${result.stderr || result.stdout}`,
    );
  }
  return result.stdout.trim();
}

test.describe("settings smoke (TK-201)", () => {
  test.setTimeout(60000);

  let tempDir: string;
  let electronApp: ElectronApplication;
  let python: string;

  test.beforeEach(() => {
    tempDir = mkdtempSync(path.join(os.tmpdir(), "wombat-e2e-"));
    python = resolvePythonCommand();
  });

  test.afterEach(async () => {
    await electronApp?.close();
    // Never leave a real vault entry polluted - delete the throwaway account under the
    // throwaway service, ignoring any failure (e.g. it was never written).
    spawnSync(python, [
      "-c",
      `import keyring\ntry:\n    keyring.delete_password(${JSON.stringify(KEYRING_SERVICE)}, "voice-fish-api-key")\nexcept Exception:\n    pass\n`,
    ]);
    rmSync(tempDir, { recursive: true, force: true });
  });

  test("UI edit lands in settings.json + keyring, and Python selects fish", async () => {
    electronApp = await electron.launch({
      args: ["."],
      env: {
        ...process.env,
        WOMBAT_BACKEND_CWD: tempDir,
        WOMBAT_KEYRING_SERVICE: KEYRING_SERVICE,
        WOMBAT_PYTHON: python,
      },
    });

    const page = await electronApp.firstWindow();
    await page.waitForSelector("#assistant-name");

    // --- AC2(b): no Node globals leak into the renderer, no token in a URL --------------------
    const nodeGlobals = await page.evaluate(() => ({
      require: typeof (globalThis as Record<string, unknown>).require,
      process: typeof (globalThis as Record<string, unknown>).process,
    }));
    expect(nodeGlobals.require).toBe("undefined");
    expect(nodeGlobals.process).toBe("undefined");
    expect(page.url()).not.toContain("token");

    // --- AC2(a): a tokenless request to the settings API is 401 -------------------------------
    // The port is read ONLY via the existing preload bridge (never a URL param, Q-109(e)).
    const { port } = await page.evaluate(
      () =>
        (window as unknown as { wombatSettings: { getInfo(): Promise<{ port: number }> } })
          .wombatSettings.getInfo(),
    );
    const tokenlessResponse = await fetch(`http://127.0.0.1:${port}/settings`);
    expect(tokenlessResponse.status).toBe(401);

    // --- AC1: drive the real UI ----------------------------------------------------------------
    await page.locator("#assistant-name").fill(ASSISTANT_NAME);
    await page.locator("#tts-provider").selectOption("fish");
    await page.locator("#tts-voice-id").fill(FAKE_VOICE_ID);
    await page.locator("#key-fish").fill(FAKE_FISH_KEY);

    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByText("Restart Wombat to apply these changes.")).toBeVisible();

    // --- AC1(a): the temp-cwd wombat.settings.json carries the saved values -------------------
    const settingsPath = path.join(tempDir, "wombat.settings.json");
    const onDisk = JSON.parse(readFileSync(settingsPath, "utf-8")) as Record<string, unknown>;
    expect(onDisk.wombat_assistant_name).toBe(ASSISTANT_NAME);
    expect(onDisk.wombat_tts_provider).toBe("fish");
    expect(onDisk.wombat_tts_voice_id).toBe(FAKE_VOICE_ID);

    // --- AC1(b): the key landed under the throwaway keyring service ---------------------------
    const storedKey = runPython(
      python,
      `import keyring\nprint(keyring.get_password(${JSON.stringify(KEYRING_SERVICE)}, "voice-fish-api-key") or "")\n`,
    );
    expect(storedKey).toBe(FAKE_FISH_KEY);

    // --- AC1(c): load_config() + build_tts_adapter, run from the temp cwd, resolves fish ------
    // Never a live cloud call at construction (DEF-7) - the DeepSeek/base-url env values below
    // exist only to satisfy load_config()'s REQUIRED_ENV; nothing here reaches the network.
    const selectedClassName = runPython(
      python,
      "import os\n" +
        "from wombat.config import load_config\n" +
        "from wombat.voice.key_store import KeyringVoiceKeyStore\n" +
        "from wombat.voice.select import build_tts_adapter\n" +
        "config = load_config()\n" +
        "store = KeyringVoiceKeyStore(service=os.environ['WOMBAT_KEYRING_SERVICE'])\n" +
        "adapter = build_tts_adapter(config, store)\n" +
        "primary = getattr(adapter, '_primary', adapter)\n" +
        "print(type(primary).__name__)\n",
      {
        cwd: tempDir,
        env: {
          ...process.env,
          DEEPSEEK_API_KEY: "x",
          DEEPSEEK_BASE_URL: "http://localhost:1",
          WOMBAT_KEYRING_SERVICE: KEYRING_SERVICE,
        },
      },
    );
    expect(selectedClassName).toBe("FishAudioTTSAdapter");
  });
});
