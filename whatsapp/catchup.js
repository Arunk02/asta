/**
 * Messages he sent while the bridge was offline — the laptop asleep, or the
 * bridge restarting — and how long to wait between reconnects.
 *
 * WhatsApp delivers those on reconnect as `messages.upsert` with type "append",
 * not "notify", and the bridge used to take "notify" only. So everything he sent
 * to the Asta group while the Mac slept was dropped without a log line, and he
 * never got an answer, even after it woke (12 Oct: nothing from him had reached
 * Asta since 9 Oct 16:42).
 *
 * Pure functions, so they can be tested without a socket.
 */

/** Seconds since the epoch; Baileys gives a number or a protobuf Long. */
export function tsOf(msg) {
  const t = msg?.messageTimestamp;
  if (t == null) return 0;
  if (typeof t === "object" && typeof t.toNumber === "function") return t.toNumber();
  return Number(t) || 0;
}

export function textOf(msg) {
  return (
    msg?.message?.conversation ||
    msg?.message?.extendedTextMessage?.text ||
    msg?.message?.imageMessage?.caption ||
    ""
  );
}

/**
 * Split a caught-up batch into what to answer, oldest first, and what to skip
 * (with why). Taken: in his allowed chat, not sent by this bridge, with text or
 * a photo, newer than the last message handled, and not older than maxAgeSec —
 * a day-old "yes send it" must not act on whatever is waiting now.
 */
export function takeCatchup(messages, { lastTs, now, maxAgeSec, allowed, mine, botMark }) {
  const take = [];
  const skipped = [];
  for (const m of messages || []) {
    const ts = tsOf(m);
    const text = textOf(m);
    const why = !allowed(m?.key?.remoteJid) ? "other chat"
      : mine(m?.key?.id) || (botMark && text.startsWith(botMark)) ? "ours"
      : !text && !m?.message?.imageMessage ? "no text"
      : ts <= lastTs ? "already handled"
      : now - ts > maxAgeSec ? "too old"
      : "";
    if (why) skipped.push({ id: m?.key?.id, why });
    else take.push(m);
  }
  take.sort((a, b) => tsOf(a) - tsOf(b));
  return { take, skipped };
}

/**
 * Wait before reconnect attempt `failures` (1, 2, …): 3 s doubling to a minute.
 * Asleep, the bridge reconnected every 3 s on each dark wake — 10,705 times
 * between 9 and 12 Oct.
 */
export function backoffMs(failures, base = 3000, cap = 60000) {
  return Math.min(cap, base * 2 ** Math.max(0, failures - 1));
}

/** "10:40 and 11:15" / "10:40, 11:02 and 11:15" in local time. */
export function timesOf(messages) {
  const t = messages.map((m) =>
    new Date(tsOf(m) * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }));
  return t.length < 2 ? t.join("") : `${t.slice(0, -1).join(", ")} and ${t[t.length - 1]}`;
}
