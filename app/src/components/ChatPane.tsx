import { useEffect, useState } from "react";

import { sendChat } from "../chat";
import { ink } from "../tokens";
import { Button } from "./Button";
import { Field } from "./Field";
import { Panel } from "./Panel";

/**
 * TK-223 (Q-111(a) ruled shape): the app's chat pane - a transcript + send
 * box over the TK-222 runtime chat handshake, built only from the TK-225
 * base components. `held` renders an HONEST transcript state (the gate's
 * hold authority stands - never a spinner that lies); no handshake or a
 * dead runtime renders a visible "wombat is not running" state instead of a
 * dead pane. No history persistence, no streaming/typing indicator, and no
 * usage tracking of any kind (DEC-29).
 */

type TranscriptEntry =
  | { readonly id: string; readonly kind: "user"; readonly text: string }
  | { readonly id: string; readonly kind: "replied"; readonly text: string }
  | { readonly id: string; readonly kind: "held" };

function nextId(): string {
  return crypto.randomUUID();
}

export function ChatPane() {
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    let cancelled = false;
    window.wombatChat
      .getInfo()
      .then((info) => {
        if (!cancelled && info === null) {
          setUnavailable(true);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setUnavailable(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleSend(): Promise<void> {
    const text = draft.trim();
    if (!text || sending) return;

    setTranscript((prev) => [...prev, { id: nextId(), kind: "user", text }]);
    setDraft("");
    setSending(true);
    try {
      const result = await sendChat(text);
      if (result.kind === "replied") {
        setUnavailable(false);
        setTranscript((prev) => [...prev, { id: nextId(), kind: "replied", text: result.text }]);
      } else if (result.kind === "held") {
        setUnavailable(false);
        setTranscript((prev) => [...prev, { id: nextId(), kind: "held" }]);
      } else {
        setUnavailable(true);
      }
    } finally {
      setSending(false);
    }
  }

  return (
    <Panel className="flex flex-col gap-4">
      <h2 className="text-sm font-semibold">Chat</h2>

      {unavailable && (
        <p className={ink.muted}>Wombat is not running - start it to chat.</p>
      )}

      <div className="flex flex-col gap-2" role="log" aria-label="Chat transcript">
        {transcript.map((entry) => {
          if (entry.kind === "user") {
            return (
              <p key={entry.id} className={ink.primary}>
                You: {entry.text}
              </p>
            );
          }
          if (entry.kind === "replied") {
            return (
              <p key={entry.id} className={ink.primary}>
                Wombat: {entry.text}
              </p>
            );
          }
          return (
            <p key={entry.id} className={ink.muted}>
              No reply within 30s - wombat is holding this or still working.
            </p>
          );
        })}
      </div>

      <div className="flex items-end gap-2">
        <div className="flex-1">
          <Field
            id="chat-message"
            label="Message"
            value={draft}
            disabled={sending}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void handleSend();
              }
            }}
          />
        </div>
        <Button
          type="button"
          onClick={() => void handleSend()}
          disabled={sending || draft.trim() === ""}
        >
          {sending ? "Sending..." : "Send"}
        </Button>
      </div>
    </Panel>
  );
}
