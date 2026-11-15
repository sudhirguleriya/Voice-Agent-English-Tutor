// My Focus Areas: headline numbers, recurring patterns with examples and drills, mistakes by type, progress.
import { sayBtn } from "./speech.js";
import { $, api, cap, esc, fmtWhen, ICON, showError } from "./util.js";

export async function showFocus() {
  let d;
  try { d = await api("/api/focus"); } catch (e) { $("#focusList").innerHTML = `<p class="empty">${esc(e.message)}</p>`; return; }
  const run = d.run, t = d.totals;
  $("#focusMeta").textContent = run
    ? `Found across your last ${t.sessions} session${t.sessions === 1 ? "" : "s"} · Updated ${fmtWhen(run.created_at, true).replace(/^Today/, "today")}` +
      (d.new_since_run > 0 ? ` · ${d.new_since_run} new mistakes since` : "")
    : "Talk for a few sessions, then analyse your history to find the patterns that keep coming back.";
  $("#analyseBtn").onclick = analyse;

  $("#focusTiles").innerHTML = [["Mistakes recorded", t.mistakes], ["Sessions analysed", t.sessions], ["Minutes spoken", t.minutes]]
    .map(([k, v]) => `<div class="card tile"><span>${k}</span><b>${v}</b></div>`).join("");

  const areas = run?.focus_areas || [];
  $("#focusList").innerHTML = `<h2 id="fa-h">${areas.length ? `Top ${areas.length} to work on` : "Your focus areas"}</h2>` + (areas.length
    ? areas.map((a) => `
      <article class="card area">
        <div class="area-top">
          <span class="rank">${a.rank}</span>
          <div class="txt"><h3>${esc(a.title)}</h3><p>${esc(a.description)}</p></div>
          <div class="side"><span class="tag">${cap(a.category)}</span>
            <span>${a.occurrences} time${a.occurrences === 1 ? "" : "s"}${a.sessions ? ` · ${a.sessions} session${a.sessions === 1 ? "" : "s"}` : ""}</span></div>
        </div>
        ${a.examples.length ? `<div class="examples"><span class="label">From your own speech</span>
          ${a.examples.map((e) => `<div class="ex" title="${esc(e.sentence || "")}"><s>${esc(e.original)}</s>${ICON.arrow("#5F6672")}<b>${esc(e.corrected) || "leave it out"}</b></div>`).join("")}</div>` : ""}
        <div class="drill-box">
          <div class="dl"><span class="label">Drill${a.drills.length > 1 ? "s" : ""}</span>
            ${a.drills.slice(0, 2).map(drillHtml).join("")}
            ${a.drills.length > 2 ? `<details class="more"><summary>${a.drills.length - 2} more drill${a.drills.length > 3 ? "s" : ""}</summary>
              ${a.drills.slice(2).map(drillHtml).join("")}</details>` : ""}</div>
          <a class="btn primary small" href="#practice?topic=${encodeURIComponent("Practise: " + a.title)}">Practise this${ICON.arrow("currentColor", 15)}</a>
        </div>
      </article>`).join("")
    : `<div class="card area"><p class="sub">No focus areas yet. After a few sessions press <b>Analyse my history</b>.
        It also runs by itself after a session once there's enough new material.</p></div>`);

  $("#focusSide").innerHTML = typesCard(t.categories) + progressCard(d.progress) + `
    <section class="dark-card" aria-labelledby="hw-h"><h2 id="hw-h">How this works</h2>
      <p>Every mistake is tagged by type. After each session, Voice Coach looks for patterns that keep coming back, and your practice partner steers conversations toward your top focus areas.</p></section>`;
}

const drillHtml = (x) => `<div class="drill">${esc(x.prompt)}
  <details><summary>Show answer</summary><span class="ans">${esc(x.answer)}</span>${sayBtn(x.answer)}</details></div>`;

function typesCard(cats) {
  if (!cats.length) return "";
  const max = Math.max(...cats.map((c) => c.n));
  return `<section class="card side-card" aria-labelledby="bt-h"><h2 id="bt-h">Mistakes by type</h2>
    ${cats.slice(0, 8).map((c, i) => `<div class="typebar"><div class="row1"><span>${cap(c.category)}</span><span>${c.n}</span></div>
      <div class="track"><i class="${i < 3 ? "" : "light"}" style="width:${Math.round((c.n / max) * 100)}%"></i></div></div>`).join("")}</section>`;
}

function progressCard(p) {
  if (!p?.today) return "";
  const t = p.today, b = p.before || {};
  const delta = (now, was) => {
    if (now == null || was == null) return "";
    const d = +(now - was).toFixed(1);
    return d ? `<span class="delta ${d < 0 ? "good" : "bad"}">${d > 0 ? "▲" : "▼"} ${Math.abs(d)} vs avg</span>` : `<span class="delta">same as avg</span>`;
  };
  const max = Math.max(1, ...p.trend.map((x) => x.mistakes_per_100 || 0));
  return `<section class="card side-card" aria-labelledby="pg-h"><h2 id="pg-h">Progress <span class="sub">(${esc(t.day)})</span></h2>
    <div class="prog">
      <div><b>${t.mistakes_per_100 ?? "–"}</b><span>mistakes / 100 words</span>${delta(t.mistakes_per_100, b.mistakes_per_100)}</div>
      <div><b>${t.fillers_per_100 ?? "–"}</b><span>fillers / 100 words</span>${delta(t.fillers_per_100, b.fillers_per_100)}</div>
      <div><b>${t.wpm ?? "–"}</b><span>words / min (natural 120–160)</span></div>
    </div>
    ${p.trend.length > 1 ? `<div class="trend" role="img" aria-label="Mistakes per 100 words by day">${p.trend.slice(-14).map((x) =>
      `<div title="${x.day}: ${x.mistakes_per_100 ?? "–"}"><i style="height:${((x.mistakes_per_100 || 0) / max) * 100}%"></i><span>${x.day.slice(8)}</span></div>`).join("")}</div>` : ""}
  </section>`;
}

async function analyse() {
  const b = $("#analyseBtn"), label = b.querySelector("span");
  b.disabled = true; label.textContent = "Analysing…";
  try { await api("/api/focus/analyse", { method: "POST" }); await showFocus(); }
  catch (e) { showError(e.message); }
  b.disabled = false; label.textContent = "Analyse my history";
}
