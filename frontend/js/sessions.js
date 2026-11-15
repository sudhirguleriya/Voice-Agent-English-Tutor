// Sessions: searchable, filterable history + the end-of-session review.
import { sayBtn } from "./speech.js";
import { $, $$, api, cap, esc, fixHtml, fmtDuration, fmtMinSec, fmtWhen, highlight, ICON, showError } from "./util.js";

let all = [], selected = null, filter = "all";

const titleOf = (s) => s.title || s.topic || (s.mode === "practice" ? "Practice conversation" : "Live session");

export async function showSessions(id) {
  const list = $("#sessionList");
  list.innerHTML = `<p class="empty">Loading…</p>`;
  try { all = await api("/api/sessions"); } catch (e) { list.innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
  selected = id || selected || all[0]?.id || null;
  if (selected && !all.some((s) => s.id === selected)) selected = all[0]?.id || null;
  renderList();
  if (selected) showDetail(selected);
  else $("#sessionDetail").innerHTML = `<div class="card detail-head"><h2 class="cap">No sessions yet</h2>
    <p class="sub">Start a Live or Practice session. When you press End &amp; review, its summary appears here.</p></div>`;
}

function renderList() {
  const q = $("#sSearch").value.trim().toLowerCase();
  const rows = all.filter((s) => (filter === "all" || s.mode === filter) && (!q || `${titleOf(s)} ${s.topic || ""}`.toLowerCase().includes(q)));
  const today = new Date().toDateString();
  const groups = [["Today", rows.filter((s) => new Date(s.started_at).toDateString() === today)],
                  ["Earlier", rows.filter((s) => new Date(s.started_at).toDateString() !== today)]];
  const row = (s) => `<button type="button" class="srow" data-id="${s.id}" ${s.id === selected ? 'aria-current="true"' : ""}>
      <span class="top"><span class="ttl">${esc(titleOf(s))}</span>${s.mistakes ? `<span class="cnt">${s.mistakes}</span>` : ""}</span>
      <span class="sm"><b>${s.mode === "practice" ? "Practice" : "Live"}</b>·<span>${fmtWhen(s.started_at)}</span>·<span>${fmtDuration(s.duration_s)}</span></span></button>`;
  $("#sessionList").innerHTML = rows.length
    ? groups.filter(([, g]) => g.length).map(([label, g]) => `<span class="label group-label">${label}</span>${g.map(row).join("")}`).join("")
    : `<p class="empty">${all.length ? "No sessions match." : "No sessions yet."}</p>`;
}

$("#sessionList").onclick = (e) => {
  const b = e.target.closest(".srow");
  if (b) location.hash = `#sessions/${b.dataset.id}`;
};
$("#sSearch").oninput = renderList;
$("#sFilter").onclick = (e) => {
  const b = e.target.closest("[data-mode]");
  if (!b) return;
  filter = b.dataset.mode;
  $$("#sFilter button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
  renderList();
};

async function showDetail(id) {
  const box = $("#sessionDetail");
  box.innerHTML = `<div class="card detail-head"><p class="sub">Loading the review…</p></div>`;
  let s;
  try { s = await api(`/api/sessions/${id}`); } catch (e) { box.innerHTML = `<div class="card detail-head"><p>${esc(e.message)}</p></div>`; return; }
  const mine = s.utterances.filter((u) => u.speaker === "me");
  const mistakes = s.all_mistakes || [];
  const said = mine.map((u) => highlight(u.text, u.mistakes.map((m) => m.original), "err")).join(" ");
  const better = mine.map((u) => highlight(u.corrected || u.text, u.mistakes.map((m) => m.corrected), "ok")).join(" ");
  const practiceTopic = mistakes.length ? `Practise: ${[...new Set(mistakes.map((m) => m.rule))].slice(0, 2).join(", ")}` : (s.topic || "");

  box.innerHTML = `
    <div class="card detail-head">
      <div class="detail-top">
        <div><span class="kicker"><span class="tag indigo">${s.mode === "practice" ? "Practice" : "Live"}</span>${fmtWhen(s.started_at, true)}</span>
          <h2 id="d-h">${esc(titleOf(s))}</h2></div>
        ${mistakes.length ? `<a class="btn primary" href="#practice?topic=${encodeURIComponent(practiceTopic)}">Practise these mistakes${ICON.arrow("currentColor", 16)}</a>` : ""}
      </div>
      <div class="stats">
        <div class="stat"><span>Duration</span><b>${fmtMinSec(s.duration_s)}</b></div>
        <div class="stat"><span>Sentences</span><b>${s.sentences}</b></div>
        <div class="stat"><span>Mistakes</span><b class="warn">${mistakes.length}</b></div>
        <div class="stat"><span>Most common</span><b>${s.most_common ? cap(s.most_common) : "–"}</b></div>
      </div>
    </div>

    <div class="pair">
      <div class="card"><h3>What you said</h3><p class="prose">${said || '<span class="sub">Nothing recorded.</span>'}</p></div>
      <div class="card"><h3 class="good">${ICON.check(16)}Better version</h3><p class="prose">${better || "–"}</p></div>
    </div>

    <div class="card polish">
      <h3 class="cap">How a fluent speaker would say it</h3>
      ${s.corrected_paragraph
        ? `<p class="prose">${esc(s.corrected_paragraph)}${sayBtn(s.corrected_paragraph)}</p>
           ${s.key_points?.length ? `<ul class="points">${s.key_points.map((k) => `<li>${esc(k)}</li>`).join("")}</ul>` : ""}`
        : `<p class="sub">Not written yet.</p>`}
      <div class="row" style="margin-top:0"><button type="button" class="btn small" id="redoSummary">${s.corrected_paragraph ? "Rewrite" : "Write it now"}</button>
        <button type="button" class="btn small ghost danger" id="delSession">Delete session</button></div>
    </div>

    ${s.mode === "practice" ? `<details class="card polish"><summary class="cap" style="cursor:pointer">Whole conversation</summary>
      ${s.utterances.filter((u) => u.speaker !== "other").map((u) => `<p class="prose"><b>${u.speaker === "agent" ? "Partner" : "You"}:</b> ${esc(u.text)}</p>`).join("")}</details>` : ""}

    <div class="card">
      <h3 class="title">Mistake breakdown</h3>
      ${mistakes.length ? `<div class="table-wrap"><div class="grid-table">
        <div class="grid-row head"><span>You said</span><span>Better</span><span>Type</span><span>Why</span></div>
        ${mistakes.map((m) => `<div class="grid-row"><s>${esc(m.original)}</s>${m.corrected ? `<b>${esc(m.corrected)}</b>` : '<span class="sub">leave it out</span>'}
          <span><span class="tag">${cap(m.category)}</span></span><span class="why">${esc(m.explanation)}</span></div>`).join("")}
      </div></div>` : `<p class="empty">No mistakes found in this session. Nicely done.</p>`}
    </div>`;

  $("#redoSummary").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = "Writing…";
    try { await api(`/api/sessions/${id}/summary`, { method: "POST" }); await showSessions(id); } catch (err) { showError(err.message); e.target.disabled = false; }
  };
  $("#delSession").onclick = async () => {
    if (!confirm("Delete this session and its mistakes?")) return;
    await api(`/api/sessions/${id}`, { method: "DELETE" });
    selected = null;
    location.hash = "#sessions";
    showSessions();
  };
}

export { fixHtml };
