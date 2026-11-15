// Small shared helpers.
export const $ = (s, root = document) => root.querySelector(s);
export const $$ = (s, root = document) => [...root.querySelectorAll(s)];

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const nice = (s) => esc(String(s || "").replace(/_/g, " "));
export const cap = (s) => { const t = String(s || "").replace(/_/g, " "); return esc(t.charAt(0).toUpperCase() + t.slice(1)); };

export const store = {
  get(k, d) { try { const v = localStorage.getItem("vc2." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("vc2." + k, JSON.stringify(v)); } catch {} },
};

// fetch JSON; always gives a readable error, even if the server answered with plain text
export async function api(url, opts = {}) {
  let r;
  try { r = await fetch(url, opts); } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new Error("Can't reach the Voice Coach server. Is it still running?");
  }
  const txt = await r.text();
  let d;
  try { d = txt ? JSON.parse(txt) : {}; } catch { d = { detail: txt.slice(0, 200) || r.statusText }; }
  if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : r.statusText);
  return d;
}
export const postJSON = (url, body) => api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export function showError(msg) {
  const b = $("#banner");
  b.textContent = msg + "  (click to dismiss)";
  b.hidden = false;
  clearTimeout(showError.t);
  showError.t = setTimeout(() => (b.hidden = true), 15000);
}

export const fmtClock = (ms) => { const s = Math.max(0, Math.floor(ms / 1000)); return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; };
export const fmtDuration = (s) => (s >= 60 ? `${Math.round(s / 60)} min` : `${s || 0} s`);
export const fmtMinSec = (s) => `${Math.floor((s || 0) / 60)}:${String((s || 0) % 60).padStart(2, "0")}`;

export function fmtWhen(iso, withDay = false) {
  if (!iso) return "";
  const d = new Date(iso), now = new Date();
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const sameDay = d.toDateString() === now.toDateString();
  const yesterday = new Date(now - 864e5).toDateString() === d.toDateString();
  if (sameDay) return withDay ? `Today, ${time}` : time;
  if (yesterday) return withDay ? `Yesterday, ${time}` : "Yesterday";
  return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }) + (withDay ? `, ${time}` : "");
}

export const ICON = {
  arrow: (c = "currentColor", s = 14) => `<svg aria-hidden="true" width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="${c}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>`,
  check: (s = 16) => `<svg aria-hidden="true" width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-10"/></svg>`,
  speaker: `<svg aria-hidden="true" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5L6 9H3v6h3l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/></svg>`,
  target: `<svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#4F46E5" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/></svg>`,
  pause: `<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M9 5v14M15 5v14"/></svg>`,
  play: `<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>`,
};

// "<s>wrong</s> → <b>right</b>"  (an empty correction means "leave it out")
export const fixHtml = (a, b) => `<s>${esc(a)}</s>${ICON.arrow("#5F6672")}${b ? `<b>${esc(b)}</b>` : `<span class="rm">leave it out</span>`}`;

// Wrap the first occurrence of each fragment inside `text` with <mark class="cls">, escaping everything.
export function highlight(text, fragments, cls) {
  const raw = String(text || ""), low = raw.toLowerCase(), ranges = [];
  for (const f of fragments || []) {
    const frag = String(f || "").trim();
    if (!frag) continue;
    let i = -1, from = 0;
    while ((i = low.indexOf(frag.toLowerCase(), from)) >= 0) {
      const before = i === 0 || !/[a-z0-9']/i.test(raw[i - 1]);
      const after = i + frag.length >= raw.length || !/[a-z0-9']/i.test(raw[i + frag.length]);
      if (before && after && !ranges.some(([a, b]) => i < b && i + frag.length > a)) { ranges.push([i, i + frag.length]); break; }
      from = i + 1;
    }
  }
  ranges.sort((x, y) => x[0] - y[0]);
  let out = "", pos = 0;
  for (const [a, b] of ranges) { out += esc(raw.slice(pos, a)) + `<mark class="${cls}">${esc(raw.slice(a, b))}</mark>`; pos = b; }
  return out + esc(raw.slice(pos));
}
