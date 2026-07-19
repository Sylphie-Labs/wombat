import { useEffect, useRef, useState } from "react";

import { POLL_GIVE_UP_MS, POLL_INTERVAL_MS, pollChatReply, sendChat } from "../chat";
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
 * dead pane. TK-266 (ISS-19): `timed_out` (the send's own abort firing
 * before the fetch settles) is likewise an honest transcript line, never a
 * stuck "Sending...". No history persistence, no streaming/typing indicator,
 * and no usage tracking of any kind (DEC-29).
 *
 * TK-270 (DEC-56(b)): a `held` result now carries an `id` - the pane starts
 * polling `pollChatReply(id)` every `POLL_INTERVAL_MS`, up to the pinned
 * `POLL_GIVE_UP_MS` bound. A reply that lands appends with a VISIBLE
 * "replied late" marker and polling for that id stops; the bound elapsing
 * first appends an honest gave-up line and stops - never an infinite loop.
 */

type TranscriptEntry =
  | { readonly id: string; readonly kind: "user"; readonly text: string }
  | { readonly id: string; readonly kind: "replied"; readonly text: string }
  | { readonly id: string; readonly kind: "held" }
  | { readonly id: string; readonly kind: "replied_late"; readonly text: string }
  | { readonly id: string; readonly kind: "poll_gave_up" }
  | { readonly id: string; readonly kind: "timed_out" };

function nextId(): string {
  return crypto.randomUUID();
}

export function ChatPane() {
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const mountedRef = useRef(true);
  const pollTimersRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set());

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

  useEffect(() => {
    return () => {
      mountedRef.current = false;
      for (const timer of pollTimersRef.current) {
        clearTimeout(timer);
      }
      pollTimersRef.current.clear();
    };
  }, []);

  // TK-270 (DEC-56(b)): schedules the next poll for a held reply's `id`,
  // `elapsedMs` after the original held response. Stops (without scheduling
  // again) once the reply arrives or `POLL_GIVE_UP_MS` is reached.
  function schedulePoll(id: string, elapsedMs: number): void {
    const timer = setTimeout(() => {
      pollTimersRef.current.delete(timer);
      void pollChatReply(id).then((result) => {
        if (!mountedRef.current) return;
        if (result.kind === "replied") {
          setTranscript((prev) => [
            ...prev,
            { id: nextId(), kind: "replied_late", text: result.text },
          ]);
          return;
        }
        const nextElapsedMs = elapsedMs + POLL_INTERVAL_MS;
        if (nextElapsedMs >= POLL_GIVE_UP_MS) {
          setTranscript((prev) => [...prev, { id: nextId(), kind: "poll_gave_up" }]);
          return;
        }
        schedulePoll(id, nextElapsedMs);
      });
    }, POLL_INTERVAL_MS);
    pollTimersRef.current.add(timer);
  }

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
        schedulePoll(result.id, 0);
      } else if (result.kind === "timed_out") {
        // TK-266 (ISS-19): the fetch never settled - stay honest about the
        // possible loss instead of leaving the pane stuck on "Sending...".
        setTranscript((prev) => [...prev, { id: nextId(), kind: "timed_out" }]);
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
          if (entry.kind === "timed_out") {
            return (
              <p key={entry.id} className={ink.muted}>
                Wombat stopped responding - the message may be lost.
              </p>
            );
          }
          if (entry.kind === "replied_late") {
            return (
              <p key={entry.id} className={ink.primary}>
                Wombat (replied late): {entry.text}
              </p>
            );
          }
          if (entry.kind === "poll_gave_up") {
            return (
              <p key={entry.id} className={ink.muted}>
                Still no reply after several minutes - giving up waiting.
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
