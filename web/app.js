/* ==========================================================================
   Sports Prediction Engine — client

   Talks to the FastAPI routes in src/api.py. No dependencies, no build step.

   Note on escaping: team and league names come from third-party schedule
   feeds, so every interpolated value goes through esc()/attr(). Numbers are
   formatted through the fmt helpers, which return an em-dash placeholder
   rather than "NaN" or "undefined" when a field is absent.
   ========================================================================== */

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/* ---------- formatting --------------------------------------------------- */

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const pct  = (v, d = 1) => (isNum(v) ? (v * 100).toFixed(d) + "%" : "–");
const num  = (v, d = 2) => (isNum(v) ? v.toFixed(d) : "–");
const signed = (v, d = 1) => (isNum(v) ? (v > 0 ? "+" : "") + v.toFixed(d) : "–");

function fmtKick(dateStr, timeStr) {
  if (!dateStr) return { time: "–", day: "" };
  const d = new Date(`${dateStr}T${timeStr || "00:00:00"}Z`);
  if (Number.isNaN(d.getTime())) return { time: "–", day: esc(dateStr) };
  return {
    time: timeStr ? d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : "–",
    day: d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" }),
  };
}

async function getJSON(url) {
  const res = await fetch(url);
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok) throw new Error(body?.detail || `Request failed (${res.status})`);
  return body;
}

/* Shared fragments ------------------------------------------------------- */

const icon = (id, cls = "") => `<svg class="${cls}" aria-hidden="true"><use href="#i-${id}"/></svg>`;

const skeletonRows = (n = 6) =>
  `<div class="fixtures" aria-busy="true" aria-label="Loading fixtures">${
    Array.from({ length: n }, () =>
      `<div class="skel-row">
         <div class="skeleton" style="width:70%"></div>
         <div class="skeleton"></div>
         <div class="skeleton"></div>
         <div class="skeleton" style="width:60%"></div>
         <div class="skeleton" style="width:16px"></div>
       </div>`).join("")}</div>`;

const emptyState = (title, body, actions = "") =>
  `<div class="panel"><div class="empty">
     ${icon("empty")}
     <h3>${esc(title)}</h3>
     <p>${body}</p>
     ${actions ? `<div class="empty__actions">${actions}</div>` : ""}
   </div></div>`;

const callout = (tone, body) =>
  `<div class="callout callout--${tone}">${icon(tone === "neg" ? "alert" : tone === "warn" ? "alert" : "info")}<div>${body}</div></div>`;

/* ---------- theme -------------------------------------------------------- */

function initTheme() {
  const btn = $("#theme-toggle");
  const paint = () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    $("#theme-icon").setAttribute("href", dark ? "#i-moon" : "#i-sun");
    btn.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
  };
  btn.addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("spe-theme", next); } catch { /* private mode */ }
    paint();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", paint);
  paint();
}

function setStatus(state, text) {
  $("#model-status").dataset.state = state;
  $("#model-status-text").textContent = text;
}

/* ---------- routing ------------------------------------------------------ */

const VIEWS = ["board", "matchup", "accumulator", "record", "backtest"];
const loaded = new Set();

function route() {
  const want = location.hash.replace("#", "");
  const view = VIEWS.includes(want) ? want : "board";
  for (const v of VIEWS) $(`#view-${v}`).hidden = v !== view;
  for (const a of $$(".mainnav a")) {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  if (!loaded.has(view)) {
    loaded.add(view);
    if (view === "record") loadRecord();
    if (view === "backtest") loadBacktest();
  }
}

/* ==========================================================================
   BOARD
   ========================================================================== */

const board = {
  data: null,
  sport: "all",
  query: "",
  sort: "time",
  leagues: new Set(),   // empty = every league
};

async function loadBoard(force = false) {
  const btn = $("#board-refresh");
  btn.dataset.busy = "true";
  btn.disabled = true;
  $("#board-content").innerHTML = skeletonRows();
  $("#board-errors").innerHTML = "";
  $("#board-meta").textContent = force ? "Re-fetching the schedule feed…" : "Loading the board…";
  try {
    board.data = await getJSON("/api/fixtures/live" + (force ? "?refresh=true" : ""));
    renderRail();
    renderBoard(true);
    setStatus("ok", "Feed live · " + new Date().toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }));
  } catch (err) {
    board.data = null;
    $("#board-meta").textContent = "";
    $("#board-content").innerHTML = emptyState(
      "The schedule feed did not answer",
      `${esc(err.message)}<br>The models themselves are local and unaffected, so the
       Matchup tab will still price any pairing you type in.`,
      `<a class="btn btn--secondary" href="#matchup">Go to Matchup</a>`
    );
    setStatus("down", "Feed unavailable");
  } finally {
    btn.dataset.busy = "false";
    btn.disabled = false;
  }
}

/** Flattens the API's {soccer:{div:{league,fixtures}}, basketball:{...}} into
 *  one list of groups so filtering and sorting only has to be written once. */
function boardGroups() {
  if (!board.data) return [];
  const groups = [];
  for (const [div, g] of Object.entries(board.data.soccer || {})) {
    if (g?.fixtures?.length) groups.push({ key: div, sport: "soccer", league: g.league || div, fixtures: g.fixtures });
  }
  const bb = board.data.basketball;
  if (bb?.fixtures?.length) groups.push({ key: "NBA", sport: "basketball", league: bb.league || "NBA", fixtures: bb.fixtures });
  return groups;
}

function renderRail() {
  const groups = boardGroups();
  $("#board-leagues").innerHTML = groups.length
    ? groups.map((g) =>
        `<button type="button" class="rail__item" data-league="${esc(g.key)}"
                 aria-pressed="${board.leagues.has(g.key)}">
           <span>${esc(g.league)}</span>
           <span class="rail__count">${g.fixtures.length}</span>
         </button>`).join("")
    : `<p class="hint" style="padding:0 .5rem">No leagues on the board.</p>`;
}

const kickoffKey = (f) => `${f.date || "9999-99-99"}T${f.time || "00:00"}`;
const topProb = (p) => Math.max(p?.prob_home_win ?? 0, p?.prob_draw ?? 0, p?.prob_away_win ?? 0);

function renderBoard(animate = false) {
  if (!board.data) return;
  const q = board.query.trim().toLowerCase();
  const hit = (...fields) => !q || fields.some((f) => String(f || "").toLowerCase().includes(q));

  let shown = 0;
  const blocks = [];

  for (const g of boardGroups()) {
    if (board.sport !== "all" && g.sport !== board.sport) continue;
    if (board.leagues.size && !board.leagues.has(g.key)) continue;

    let fixtures = g.fixtures.filter((f) =>
      hit(f.home_team_live_name, f.away_team_live_name, g.league));
    if (!fixtures.length) continue;

    fixtures = [...fixtures].sort((a, b) => {
      if (board.sort === "time") return kickoffKey(a).localeCompare(kickoffKey(b));
      const pa = topProb(a.prediction), pb = topProb(b.prediction);
      return board.sort === "confidence" ? pb - pa : pa - pb;
    });

    shown += fixtures.length;
    blocks.push(
      `<section class="league-block">
         <div class="league-block__head">
           <h2>${esc(g.league)}</h2>
           <span class="rail__count">${fixtures.length} fixture${fixtures.length === 1 ? "" : "s"}</span>
         </div>
         <div class="fixtures" data-animate="${animate}">
           ${fixtures.map((f) => fixtureRow(f, g.sport)).join("")}
         </div>
       </section>`);
  }

  const content = $("#board-content");
  if (!shown) {
    content.innerHTML = q || board.leagues.size || board.sport !== "all"
      ? emptyState("Nothing matches those filters",
          "Widen the sport, clear the league selection, or search for a different team.",
          `<button class="btn btn--secondary" type="button" id="board-reset">Reset filters</button>`)
      : emptyState("No upcoming fixtures right now",
          `The feed returned an empty schedule. That usually means these leagues are between
           seasons rather than that anything is broken. You can still price any pairing by hand.`,
          `<button class="btn btn--secondary" type="button" id="board-refetch">Re-fetch feed</button>
           <a class="btn btn--ghost" href="#matchup">Custom matchup</a>`);
  } else {
    content.innerHTML = blocks.join("");
  }

  const total = boardGroups().reduce((n, g) => n + g.fixtures.length, 0);
  $("#board-meta").textContent = shown === total
    ? `${total} fixture${total === 1 ? "" : "s"} on the board`
    : `${shown} of ${total} fixtures shown`;

  const errs = board.data.errors || [];
  $("#board-errors").innerHTML = errs.length
    ? callout("warn",
        `<details><summary>${errs.length} league${errs.length === 1 ? "" : "s"} returned no fixtures</summary>
         <ul>${errs.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></details>`)
    : "";
}

let rowId = 0;

function fixtureRow(f, sport) {
  const p = f.prediction || {};
  const home = esc(f.home_team_live_name);
  const away = esc(f.away_team_live_name);
  const { time, day } = fmtKick(f.date, f.time);
  const when = `<div class="fixture__when"><b>${esc(time)}</b>${esc(day)}</div>`;

  if (p.error) {
    return `<div class="fixture"><div class="fixture__summary" style="cursor:default">
      ${when}
      <div class="fixture__teams">
        <div class="fixture__team"><span class="side">H</span><span>${home}</span></div>
        <div class="fixture__team"><span class="side">A</span><span>${away}</span></div>
      </div>
      <div class="odds" style="grid-column:span 2">
        <span class="badge badge--mute">${icon("alert")} No price: ${esc(p.error)}</span>
      </div>
      <span></span>
    </div></div>`;
  }

  const id = `fx-${rowId++}`;
  const isSoccer = sport === "soccer";
  const h = p.prob_home_win ?? 0, d = p.prob_draw ?? 0, a = p.prob_away_win ?? 0;
  const best = Math.max(h, isSoccer ? d : 0, a);

  const bar = isSoccer
    ? `<i class="is-home" style="width:${(h * 100).toFixed(2)}%"></i>
       <i class="is-draw" style="width:${(d * 100).toFixed(2)}%"></i>
       <i class="is-away" style="width:${(a * 100).toFixed(2)}%"></i>`
    : `<i class="is-home" style="width:${(h * 100).toFixed(2)}%"></i>
       <i class="is-away" style="width:${(a * 100).toFixed(2)}%"></i>`;

  const cell = (kind, label, v) =>
    `<span class="probcell"><i class="swatch swatch--${kind}"></i>${label}<b>${pct(v)}</b></span>`;

  const cells = isSoccer
    ? cell("home", "H", h) + cell("draw", "D", d) + cell("away", "A", a)
    : cell("home", "H", h) + cell("away", "A", a);

  // The pick is the model's most likely outcome, with its implied fair price.
  let pickLabel, pickProb;
  if (best === h) { pickLabel = f.home_team_live_name; pickProb = h; }
  else if (isSoccer && best === d) { pickLabel = "Draw"; pickProb = d; }
  else { pickLabel = f.away_team_live_name; pickProb = a; }

  return `<div class="fixture">
    <button class="fixture__summary" type="button" aria-expanded="false" aria-controls="${id}">
      ${when}
      <div class="fixture__teams">
        <div class="fixture__team${best === h ? " fixture__team--fav" : ""}"><span class="side">H</span><span>${home}</span></div>
        <div class="fixture__team${best === a ? " fixture__team--fav" : ""}"><span class="side">A</span><span>${away}</span></div>
      </div>
      <div class="odds">
        <div class="probbar">${bar}</div>
        <div class="probcells" style="--cols:${isSoccer ? 3 : 2}">${cells}</div>
      </div>
      <div class="fixture__pick">
        <b>${esc(pickLabel)}</b>
        <span>${pct(pickProb)} · fair ${num(1 / pickProb)}</span>
      </div>
      <span class="disclose">${icon("chevron")}</span>
    </button>
    <div class="fixture__detail" id="${id}" data-open="false"><div>
      <div class="fixture__detail-inner">${detailBody(p, isSoccer)}</div>
    </div></div>
  </div>`;
}

function detailBody(p, isSoccer) {
  const readouts = isSoccer
    ? [
        ["Expected goals", `${num(p.expected_goals_home)} – ${num(p.expected_goals_away)}`],
        ["Over 2.5", pct(p.prob_over_2_5)],
        ["Under 2.5", pct(p.prob_under_2_5)],
        ["Both teams score", pct(p.prob_btts_yes)],
        ["Elo", isNum(p.elo_home) ? `${Math.round(p.elo_home)} – ${Math.round(p.elo_away)}` : "–"],
      ]
    : [
        ["Predicted margin", signed(p.predicted_margin_home)],
        ["Predicted total", num(p.predicted_total_points, 1)],
        ["Elo", isNum(p.elo_home) ? `${Math.round(p.elo_home)} – ${Math.round(p.elo_away)}` : "–"],
      ];

  return `<div class="readouts">${
      readouts.map(([k, v]) => `<div class="readout">${esc(k)}<b>${v}</b></div>`).join("")
    }</div>${rationale(p.rationale)}`;
}

function rationale(r) {
  if (!r?.narrative?.length) {
    return `<p class="why-scope">No rationale was produced for this fixture.</p>`;
  }
  // Narrative bullets are model-generated and already contain <b> markup.
  return `<ul class="why">${r.narrative.map((b) => `<li>${b}</li>`).join("")}</ul>
          ${r.data_scope ? `<p class="why-scope">${r.data_scope}</p>` : ""}`;
}

/* ==========================================================================
   MATCHUP
   ========================================================================== */

async function initMatchup() {
  try {
    const leagues = await getJSON("/api/soccer/leagues");
    $("#s-league").innerHTML = leagues
      .map((l) => `<option value="${esc(l.code)}">${esc(l.name)}</option>`).join("");
    await loadSoccerTeams();
  } catch (err) {
    $("#s-err").textContent = `Could not load leagues: ${err.message}`;
  }
  try {
    const teams = await getJSON("/api/basketball/teams");
    $("#n-teams").innerHTML = teams.map((t) => `<option value="${esc(t)}">`).join("");
  } catch { /* NBA model may not be trained on this deployment */ }
  $("#n-date").value = new Date().toISOString().slice(0, 10);
}

async function loadSoccerTeams() {
  const div = $("#s-league").value;
  if (!div) return;
  const teams = await getJSON(`/api/soccer/teams?div=${encodeURIComponent(div)}`);
  $("#s-teams").innerHTML = teams.map((t) => `<option value="${esc(t)}">`).join("");
}

function outcomeBlock(items) {
  return `<div class="outcomes" style="--cols:${items.length}">${
    items.map(([kind, label, v]) => `
      <div class="outcome">
        <div class="outcome__label"><i class="swatch swatch--${kind}"></i><span>${esc(label)}</span></div>
        <div class="outcome__value">${pct(v)}</div>
        <div class="outcome__bar"><i class="swatch--${kind}" style="--fill:${isNum(v) ? v.toFixed(4) : 0}"></i></div>
      </div>`).join("")
  }</div>`;
}

function readoutPanel(title, rows) {
  return `<h3 class="section-title">${esc(title)}</h3>
    <div class="metrics">${
      rows.map(([k, v]) => `<div class="metric">
        <div class="metric__label">${esc(k)}</div>
        <div class="metric__value">${v}</div>
      </div>`).join("")
    }</div>`;
}

/** Edge table. `ea` is the API's market_analysis entry for one outcome. */
function marketTable(rows) {
  const body = rows.filter(([, odds]) => odds).map(([label, odds, ea]) => {
    if (!ea) return "";
    const value = ea.edge > 0.02;
    return `<tr>
      <td>${esc(label)}</td>
      <td class="num">${num(Number(odds))}</td>
      <td class="num">${pct(ea.market_implied_prob)}</td>
      <td class="num ${ea.edge > 0 ? "delta-pos" : "delta-neg"}">${ea.edge > 0 ? "+" : ""}${pct(ea.edge)}</td>
      <td class="num">${pct(ea.kelly_stake_fraction, 2)}</td>
      <td><span class="badge badge--${value ? "pos" : "mute"}">${icon(value ? "check" : "cross")}${value ? "Value" : "Pass"}</span></td>
    </tr>`;
  }).join("");
  if (!body) return "";
  return `<h3 class="section-title">Against the book</h3>
    <div class="table-wrap"><table>
      <thead><tr>
        <th scope="col">Outcome</th><th scope="col" class="num">Your odds</th>
        <th scope="col" class="num">Book implied</th><th scope="col" class="num">Model edge</th>
        <th scope="col" class="num">Kelly stake</th><th scope="col">Call</th>
      </tr></thead>
      <tbody>${body}</tbody>
      <caption>Kelly is quarter-Kelly, as a fraction of bankroll. "Value" is flagged above a
        2 percentage-point edge, which is a threshold, not a recommendation.</caption>
    </table></div>`;
}

async function runSoccer() {
  const err = $("#s-err");
  err.textContent = "";
  const div = $("#s-league").value;
  const home = $("#s-home").value.trim();
  const away = $("#s-away").value.trim();
  if (!home || !away) { err.textContent = "Enter both team names."; return; }

  const btn = $("#s-run");
  btn.disabled = true;
  $("#s-results").innerHTML = `<div class="panel"><div class="panel__body">${skeletonRows(2)}</div></div>`;
  const params = new URLSearchParams({ div, home, away });
  for (const [k, id] of [["odds_home", "#s-oh"], ["odds_draw", "#s-od"], ["odds_away", "#s-oa"]]) {
    if ($(id).value) params.set(k, $(id).value);
  }
  try {
    const r = await getJSON(`/api/soccer/predict?${params}`);
    $("#s-results").innerHTML = `<div class="panel"><div class="panel__body">
      ${outcomeBlock([
        ["home", `${r.home_team} win`, r.prob_home_win],
        ["draw", "Draw", r.prob_draw],
        ["away", `${r.away_team} win`, r.prob_away_win],
      ])}
      ${readoutPanel("Model readouts", [
        ["Expected goals", `${num(r.expected_goals_home)} – ${num(r.expected_goals_away)}`],
        ["Over / under 2.5", `${pct(r.prob_over_2_5)} / ${pct(r.prob_under_2_5)}`],
        ["Both teams score", `${pct(r.prob_btts_yes)} <small>yes</small>`],
        ["Elo rating", isNum(r.elo_home) ? `${Math.round(r.elo_home)} – ${Math.round(r.elo_away)}` : "–"],
      ])}
      ${r.market_analysis ? marketTable([
        [`${r.home_team} win`, $("#s-oh").value, r.market_analysis.H],
        ["Draw", $("#s-od").value, r.market_analysis.D],
        [`${r.away_team} win`, $("#s-oa").value, r.market_analysis.A],
      ]) : ""}
      <h3 class="section-title">Why this number</h3>
      ${rationale(r.rationale)}
      <p class="note" style="margin-top:1.5rem">${esc(r.league)} · model trained as of ${esc(r.model_trained_as_of)}</p>
    </div></div>`;
  } catch (e) {
    $("#s-results").innerHTML = "";
    err.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

async function runNBA() {
  const err = $("#n-err");
  err.textContent = "";
  const home = $("#n-home").value.trim();
  const away = $("#n-away").value.trim();
  if (!home || !away) { err.textContent = "Enter both team names."; return; }

  const btn = $("#n-run");
  btn.disabled = true;
  $("#n-results").innerHTML = `<div class="panel"><div class="panel__body">${skeletonRows(2)}</div></div>`;
  const params = new URLSearchParams({
    home, away,
    game_date: $("#n-date").value,
    is_playoffs: $("#n-playoffs").checked,
    neutral_site: $("#n-neutral").checked,
  });
  if ($("#n-oh").value) params.set("odds_home", $("#n-oh").value);
  if ($("#n-oa").value) params.set("odds_away", $("#n-oa").value);
  try {
    const r = await getJSON(`/api/basketball/predict?${params}`);
    $("#n-results").innerHTML = `<div class="panel"><div class="panel__body">
      ${outcomeBlock([
        ["home", `${r.home_team} win`, r.prob_home_win],
        ["away", `${r.away_team} win`, r.prob_away_win],
      ])}
      ${readoutPanel("Model readouts", [
        ["Predicted margin", `${signed(r.predicted_margin_home)} <small>home</small>`],
        ["Predicted total", num(r.predicted_total_points, 1)],
        ["Elo rating", isNum(r.elo_home) ? `${Math.round(r.elo_home)} – ${Math.round(r.elo_away)}` : "–"],
      ])}
      ${r.market_analysis ? marketTable([
        [`${r.home_team} win`, $("#n-oh").value, r.market_analysis.H],
        [`${r.away_team} win`, $("#n-oa").value, r.market_analysis.A],
      ]) : ""}
      <h3 class="section-title">Why this number</h3>
      ${rationale(r.rationale)}
      <p class="note" style="margin-top:1.5rem">Model trained as of ${esc(r.model_trained_as_of)}</p>
    </div></div>`;
  } catch (e) {
    $("#n-results").innerHTML = "";
    err.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

async function fetchRealOdds(kind) {
  const noteEl = $(kind === "soccer" ? "#s-odds-note" : "#n-odds-note");
  const btn = $(kind === "soccer" ? "#s-fetch-odds" : "#n-fetch-odds");
  const home = $(kind === "soccer" ? "#s-home" : "#n-home").value.trim();
  const away = $(kind === "soccer" ? "#s-away" : "#n-away").value.trim();
  if (!home || !away) { noteEl.textContent = "Enter both team names first."; return; }

  btn.dataset.busy = "true";
  btn.disabled = true;
  noteEl.textContent = "Checking the odds board…";
  const params = new URLSearchParams({ home, away });
  if (kind === "soccer") params.set("div", $("#s-league").value);
  try {
    const r = await getJSON(`/api/odds/${kind === "soccer" ? "soccer" : "basketball"}?${params}`);
    if (!r.available) {
      noteEl.textContent = `No live price found. ${r.reason || ""}`;
      return;
    }
    const set = (id, v) => { if (v != null) $(id).value = v; };
    if (kind === "soccer") {
      set("#s-oh", r.odds_home); set("#s-od", r.odds_draw); set("#s-oa", r.odds_away);
    } else {
      set("#n-oh", r.odds_home); set("#n-oa", r.odds_away);
    }
    noteEl.textContent =
      `Best price per outcome across ${r.bookmaker_count} bookmaker(s) via the-odds-api.com, for ${r.event_home} v ${r.event_away}. Prices move; check your book before staking.`;
  } catch (e) {
    noteEl.textContent = `Odds lookup failed: ${e.message}`;
  } finally {
    btn.dataset.busy = "false";
    btn.disabled = false;
  }
}

/* ==========================================================================
   ACCUMULATOR
   ========================================================================== */

async function buildAccumulator() {
  const box = $("#acca-content");
  const btn = $("#acca-build");
  btn.disabled = true;
  box.innerHTML = `<div class="panel"><div class="panel__body">${skeletonRows(3)}</div></div>`;
  const params = new URLSearchParams({
    legs: $("#acca-legs").value || 3,
    min_odds: $("#acca-min-odds").value || 5,
  });
  if ($("#acca-from").value) params.set("date_from", $("#acca-from").value);
  if ($("#acca-to").value) params.set("date_to", $("#acca-to").value);

  try {
    const d = await getJSON(`/api/accumulator?${params}`);
    if (!d.ok) {
      box.innerHTML = emptyState("No combination clears that target", esc(d.reason));
      return;
    }
    box.innerHTML = `
      <div class="metrics">
        <div class="metric">
          <div class="metric__label">Combined model-implied odds</div>
          <div class="metric__value">${num(d.combined_odds)}</div>
          <div class="metric__sub">1 ÷ joint probability, not a price on offer</div>
        </div>
        <div class="metric">
          <div class="metric__label">Combined bookmaker odds</div>
          <div class="metric__value">${d.combined_real_odds != null
            ? num(d.combined_real_odds)
            : `<small>not priced for every leg</small>`}</div>
          <div class="metric__sub">Best available across tracked UK books</div>
        </div>
        <div class="metric">
          <div class="metric__label">Joint win probability</div>
          <div class="metric__value">${pct(d.combined_probability)}</div>
          <div class="meter"><div class="meter__fill" style="--fill:${(d.combined_probability ?? 0).toFixed(4)}"></div></div>
        </div>
        <div class="metric">
          <div class="metric__label">Fixtures searched</div>
          <div class="metric__value">${d.candidates_considered}</div>
        </div>
      </div>
      <div class="panel" style="margin-top:1.5rem">
        ${d.legs.map(accaLeg).join("")}
      </div>
      <p class="note" style="margin-top:1rem">
        <strong>Model-implied odds</strong> are 1 ÷ the model's probability for that pick. They read
        the model's confidence and are not a price any sportsbook has offered. Where a real price is
        shown it is the best decimal across UK books tracked by the-odds-api.com as of the last
        refresh. Both-teams-to-score legs never carry a real price, because no provider wired into
        this app quotes that market. This is a screening tool, not a recommendation.
      </p>`;
  } catch (e) {
    box.innerHTML = callout("neg", `Could not build an accumulator: ${esc(e.message)}`);
  } finally {
    btn.disabled = false;
  }
}

function accaLeg(leg, i) {
  const { time, day } = fmtKick(leg.date, leg.time);
  return `<div class="leg">
    <div class="leg__head">
      <div style="display:flex; gap:.75rem; align-items:baseline; min-width:0">
        <span class="leg__index">${i + 1}</span>
        <div style="min-width:0">
          <div class="leg__pick">${esc(leg.label)}</div>
          <div class="leg__meta">${esc(leg.home)} v ${esc(leg.away)} · ${esc(leg.league)} · ${esc(day)} ${esc(time)}</div>
        </div>
      </div>
      <div class="readouts" style="margin:0">
        <div class="readout">Model<b>${pct(leg.probability)}</b></div>
        <div class="readout">Implied<b>${num(1 / leg.probability)}</b></div>
        <div class="readout">Book<b>${leg.real_odds != null
          ? `<span class="delta-pos">${num(leg.real_odds)}</span>`
          : `<span style="color:var(--ink-3); font-weight:500">n/a</span>`}</b></div>
      </div>
    </div>
    ${leg.rationale?.length
      ? `<ul class="why" style="margin-top:.75rem">${leg.rationale.map((b) => `<li>${b}</li>`).join("")}</ul>`
      : ""}
  </div>`;
}

/* ==========================================================================
   TRACK RECORD
   ========================================================================== */

const PAGE = 100;
const record = { data: null, sport: "all", limit: PAGE };

async function loadRecord() {
  const box = $("#log-content");
  box.innerHTML = skeletonRows(5);
  record.limit = PAGE;
  try {
    record.data = await getJSON("/api/tracking/log");
    fillRecordLeagues();
    renderRecord();
  } catch (e) {
    record.data = null;
    box.innerHTML = callout("neg", `Could not load the prediction log: ${esc(e.message)}`);
  }
}

function fillRecordLeagues() {
  if (!record.data) return;
  const sel = $("#log-league");
  const keep = sel.value;
  const scope = record.data.entries.filter((e) => record.sport === "all" || e.sport === record.sport);
  const leagues = [...new Set(scope.map((e) => e.league))].sort();
  sel.innerHTML = `<option value="">All leagues</option>` +
    leagues.map((l) => `<option value="${esc(l)}">${esc(l)}</option>`).join("");
  if (leagues.includes(keep)) sel.value = keep;
}

function recordRows() {
  if (!record.data) return [];
  const from = $("#log-from").value, to = $("#log-to").value, league = $("#log-league").value;
  return record.data.entries.filter((e) => {
    if (record.sport !== "all" && e.sport !== record.sport) return false;
    if (league && e.league !== league) return false;
    if (!e.date) return true;
    if (from && e.date < from) return false;
    if (to && e.date > to) return false;
    return true;
  });
}

const pickLabel = (e, market, pick) => {
  if (market === "1x2") return pick === "H" ? e.home : pick === "A" ? e.away : "Draw";
  if (market === "moneyline") return pick === "HOME" ? e.home : e.away;
  if (market === "over_under_2_5") return pick === "OVER" ? "Over 2.5" : "Under 2.5";
  return pick;
};

function marketCell(e, market) {
  const m = e.markets?.[market];
  if (!m) return `<span style="color:var(--ink-3)">–</span>`;
  const prob = m.probs?.[m.pick];
  const label = `${esc(pickLabel(e, market, m.pick))} <span style="color:var(--ink-3)" class="num">${pct(prob)}</span>`;
  if (!("actual" in m)) return label;
  return `${label} <span style="color:var(--${m.correct ? "pos" : "neg"})" title="${m.correct ? "Correct" : "Wrong"}">
    ${icon(m.correct ? "check" : "cross")}</span>`;
}

function accuracyMetric(label, stat) {
  const has = stat.accuracy !== null && stat.graded > 0;
  return `<div class="metric">
    <div class="metric__label">${esc(label)}</div>
    <div class="metric__value">${has ? pct(stat.accuracy, 1) : `<small>not graded yet</small>`}</div>
    <div class="metric__sub">${stat.correct} correct of ${stat.graded} settled</div>
    <div class="meter"><div class="meter__fill ${has && stat.accuracy >= 0.5 ? "meter__fill--pos" : ""}"
         style="--fill:${has ? stat.accuracy.toFixed(4) : 0}"></div></div>
  </div>`;
}

function renderRecord() {
  const box = $("#log-content");
  if (!record.data) { box.innerHTML = ""; return; }
  const s = record.data.summary;
  const rows = recordRows();

  const ml = {
    graded: s.soccer_1x2.graded + s.basketball_moneyline.graded,
    correct: s.soccer_1x2.correct + s.basketball_moneyline.correct,
  };
  ml.accuracy = ml.graded ? ml.correct / ml.graded : null;

  let html = `<div class="metrics" style="margin-bottom:1.5rem">
    ${accuracyMetric("Match result / moneyline", ml)}
    ${accuracyMetric("Both teams to score", s.soccer_btts)}
    ${accuracyMetric("Over / under 2.5", s.soccer_over_under)}
    <div class="metric">
      <div class="metric__label">Predictions logged</div>
      <div class="metric__value">${s.total_logged}</div>
      <div class="metric__sub">${rows.length} match the current filters</div>
    </div>
  </div>`;

  if (!rows.length) {
    html += record.data.entries.length
      ? emptyState("No entries in that range", "Widen the dates, or clear the league and sport filters.")
      : emptyState("Nothing logged yet",
          `The log fills in as fixtures appear on the board. Each entry stays pending until the day
           after kick-off, then grades itself against the real final score from the same feed that
           supplied the fixture.`,
          `<a class="btn btn--secondary" href="#board">Open the board</a>`);
  } else {
    // The log only grows, so render a page at a time. Rows are already
    // newest-first; the CSV export always covers the full filtered set.
    const page = rows.slice(0, record.limit);
    html += `<div class="table-wrap"><table>
      <thead><tr>
        <th scope="col">Date</th><th scope="col">League</th><th scope="col">Fixture</th>
        <th scope="col">Result / moneyline</th><th scope="col">BTTS</th>
        <th scope="col">O/U 2.5</th><th scope="col">Score</th><th scope="col">Status</th>
      </tr></thead>
      <tbody>${page.map((e) => `<tr>
        <td class="num">${esc(e.date || "")}</td>
        <td>${esc(e.league)}</td>
        <td>${esc(e.home)} v ${esc(e.away)}</td>
        <td>${marketCell(e, e.sport === "basketball" ? "moneyline" : "1x2")}</td>
        <td>${marketCell(e, "btts")}</td>
        <td>${marketCell(e, "over_under_2_5")}</td>
        <td class="num">${e.actual_home_score != null ? `${esc(e.actual_home_score)}–${esc(e.actual_away_score)}` : "–"}</td>
        <td>${statusBadge(e)}</td>
      </tr>`).join("")}</tbody>
      <caption>A fixture is settled once its real final score can be fetched. "Unresolved" means no
        result was ever available, which normally means the match was postponed or abandoned. This
        log is stored on the server's disk and resets if the app is redeployed without a volume.</caption>
    </table></div>`;

    if (rows.length > page.length) {
      html += `<div style="display:flex; align-items:center; gap:1rem; margin-top:1rem">
        <button class="btn btn--secondary" type="button" id="log-more">
          Show ${Math.min(PAGE, rows.length - page.length)} more
        </button>
        <span class="hint">${page.length} of ${rows.length} shown. CSV export covers all ${rows.length}.</span>
      </div>`;
    }
  }
  box.innerHTML = html;
}

function statusBadge(e) {
  if (!e.graded) return `<span class="badge badge--mute">Pending</span>`;
  const settled = Object.values(e.markets || {}).some((m) => "actual" in m);
  return settled
    ? `<span class="badge badge--pos">${icon("check")}Settled</span>`
    : `<span class="badge badge--warn">${icon("alert")}Unresolved</span>`;
}

function exportRecordCsv() {
  const rows = recordRows();
  if (!rows.length) return;
  const markets = ["1x2", "btts", "over_under_2_5", "moneyline"];
  const headers = ["date", "sport", "league", "home", "away",
    ...markets.flatMap((m) => [`${m}_pick`, `${m}_prob`, `${m}_actual`, `${m}_correct`]),
    "actual_home_score", "actual_away_score", "status"];

  const cell = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const marketFields = (e, key) => {
    const m = e.markets?.[key];
    if (!m) return ["", "", "", ""];
    const p = m.probs?.[m.pick];
    return [m.pick, isNum(p) ? p.toFixed(4) : "", m.actual ?? "", "actual" in m ? m.correct : ""];
  };

  const lines = [headers.join(",")];
  for (const e of rows) {
    const status = !e.graded ? "PENDING"
      : Object.values(e.markets || {}).some((m) => "actual" in m) ? "SETTLED" : "UNRESOLVED";
    lines.push([e.date, e.sport, e.league, e.home, e.away,
      ...markets.flatMap((m) => marketFields(e, m)),
      e.actual_home_score ?? "", e.actual_away_score ?? "", status].map(cell).join(","));
  }

  const url = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8;" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = `prediction-log-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

/* ==========================================================================
   BACKTEST
   ========================================================================== */

async function loadBacktest() {
  const box = $("#backtest-content");
  box.innerHTML = skeletonRows(5);
  try {
    renderBacktest(await getJSON("/api/analytics"));
  } catch (e) {
    box.innerHTML = callout("neg", `Could not load backtest results: ${esc(e.message)}`);
  }
}

function renderBacktest(data) {
  const soccer = data.soccer || [];
  const bb = data.basketball;
  const box = $("#backtest-content");

  if (!soccer.length && !bb) {
    box.innerHTML = emptyState("No backtest reports on this deployment",
      `Reports are written to <code>reports/</code> by the training run. Run
       <code>python3 src/train.py</code> to generate them.`);
    return;
  }

  let html = "";

  if (soccer.length) {
    const beat = soccer.filter((x) => x.brier.blended_calibrated < x.brier.market);
    const mean = (f) => soccer.reduce((s, x) => s + f(x), 0) / soccer.length;
    // Diverging: market Brier minus model Brier. Positive means the model was
    // sharper than the closing line for that league.
    const edges = soccer.map((x) => x.brier.market - x.brier.blended_calibrated);
    const scale = Math.max(...edges.map(Math.abs)) || 1;

    html += `<div class="metrics" style="margin-bottom:1.5rem">
      <div class="metric">
        <div class="metric__label">Leagues backtested</div>
        <div class="metric__value">${soccer.length}</div>
        <div class="metric__sub">${soccer.reduce((n, x) => n + x.n_test_holdout, 0).toLocaleString()} held-out matches</div>
      </div>
      <div class="metric">
        <div class="metric__label">Mean model Brier</div>
        <div class="metric__value">${mean((x) => x.brier.blended_calibrated).toFixed(3)}</div>
        <div class="metric__sub">Lower is better</div>
      </div>
      <div class="metric">
        <div class="metric__label">Mean market Brier</div>
        <div class="metric__value">${mean((x) => x.brier.market).toFixed(3)}</div>
        <div class="metric__sub">De-vigged closing price</div>
      </div>
      <div class="metric">
        <div class="metric__label">Sharper than the close</div>
        <div class="metric__value">${beat.length}<small> of ${soccer.length}</small></div>
        <div class="meter"><div class="meter__fill ${beat.length * 2 >= soccer.length ? "meter__fill--pos" : "meter__fill--neg"}"
             style="--fill:${(beat.length / soccer.length).toFixed(4)}"></div></div>
      </div>
    </div>`;

    const rows = soccer
      .map((x, i) => ({ x, edge: edges[i] }))
      .sort((a, b) => b.edge - a.edge);

    html += `<h2 class="section-title">Soccer, by league</h2>
      <div class="table-wrap"><table>
        <thead><tr>
          <th scope="col">League</th><th scope="col" class="num">Held out</th>
          <th scope="col" class="num">Model Brier</th><th scope="col" class="num">Market Brier</th>
          <th scope="col" class="num">Difference</th><th scope="col">Market sharper ← → model sharper</th>
          <th scope="col" class="num">Model log-loss</th>
        </tr></thead>
        <tbody>${rows.map(({ x, edge }) => {
          const w = (Math.abs(edge) / scale * 48).toFixed(1);
          return `<tr>
            <td>${esc(x.league)}</td>
            <td class="num">${x.n_test_holdout.toLocaleString()}</td>
            <td class="num">${x.brier.blended_calibrated.toFixed(3)}</td>
            <td class="num">${x.brier.market.toFixed(3)}</td>
            <td class="num ${edge > 0 ? "delta-pos" : "delta-neg"}">${edge > 0 ? "+" : ""}${edge.toFixed(3)}</td>
            <td><div class="divbar"><i class="${edge > 0 ? "pos" : "neg"}" style="width:${w}%"></i></div></td>
            <td class="num">${x.log_loss.blended_calibrated.toFixed(3)}</td>
          </tr>`;
        }).join("")}</tbody>
        <caption>Brier score and log-loss are both "lower is better", with 0 a perfect forecast.
          The market column is the closing bookmaker price with the overround removed, taken from
          real historical odds. A positive difference means the model scored better than that
          closing line on held-out matches.</caption>
      </table></div>`;

    html += valueBetSection(soccer);
  }

  if (bb) {
    html += `<h2 class="section-title">NBA</h2>
      <div class="metrics">
        <div class="metric">
          <div class="metric__label">Brier, Elo + margin model</div>
          <div class="metric__value">${bb.brier.calibrated.toFixed(3)}</div>
          <div class="metric__sub">${bb.n_test_holdout.toLocaleString()} held-out games</div>
        </div>
        <div class="metric">
          <div class="metric__label">Brier, Elo alone</div>
          <div class="metric__value">${bb.brier.elo_only.toFixed(3)}</div>
          <div class="metric__sub">Baseline for comparison</div>
        </div>
        <div class="metric">
          <div class="metric__label">Margin error</div>
          <div class="metric__value">${bb.margin_mae.toFixed(1)}<small> pts</small></div>
          <div class="metric__sub">Mean absolute error</div>
        </div>
        <div class="metric">
          <div class="metric__label">Total points error</div>
          <div class="metric__value">${bb.total_mae.toFixed(1)}<small> pts</small></div>
          <div class="metric__sub">Mean absolute error</div>
        </div>
      </div>`;
  }

  html += `<p class="note" style="margin-top:1.5rem">
    These figures are read straight from <code>reports/soccer_backtest.json</code> and
    <code>reports/basketball_backtest.json</code>, written by <code>src/train.py</code> against
    matches the model never saw during fitting. They are not recomputed in the browser and have not
    been filtered to flatter the model.
  </p>`;

  box.innerHTML = html;
}

/**
 * Flat-stake value-betting simulation. This is the number that actually
 * decides whether the model is worth acting on, so it is reported whole:
 * every bet the rule would have placed, at the real historical price,
 * including the leagues where it lost money.
 */
function valueBetSection(soccer) {
  const rows = soccer.map((x) => {
    const arms = Object.values(x.value_betting_sim || {});
    const bets = arms.reduce((n, a) => n + a.n_bets, 0);
    const staked = arms.reduce((n, a) => n + a.staked, 0);
    const returned = arms.reduce((n, a) => n + a.returned, 0);
    const wins = arms.reduce((n, a) => n + a.win_rate * a.n_bets, 0);
    return { league: x.league, bets, staked, returned,
             roi: staked ? returned / staked - 1 : null,
             winRate: bets ? wins / bets : null };
  }).filter((r) => r.bets > 0);

  if (!rows.length) return "";

  const bets = rows.reduce((n, r) => n + r.bets, 0);
  const staked = rows.reduce((n, r) => n + r.staked, 0);
  const returned = rows.reduce((n, r) => n + r.returned, 0);
  const roi = returned / staked - 1;
  const profitable = rows.filter((r) => r.roi > 0).length;
  const scale = Math.max(...rows.map((r) => Math.abs(r.roi))) || 1;

  rows.sort((a, b) => b.roi - a.roi);

  return `<h2 class="section-title">Flat-stake value-bet simulation</h2>
    <div class="metrics" style="margin-bottom:1.5rem">
      <div class="metric">
        <div class="metric__label">Return on turnover</div>
        <div class="metric__value ${roi > 0 ? "delta-pos" : "delta-neg"}">${roi > 0 ? "+" : ""}${(roi * 100).toFixed(1)}%</div>
        <div class="metric__sub">${bets.toLocaleString()} bets, one unit each</div>
      </div>
      <div class="metric">
        <div class="metric__label">Staked</div>
        <div class="metric__value">${staked.toLocaleString(undefined, { maximumFractionDigits: 0 })}<small> u</small></div>
      </div>
      <div class="metric">
        <div class="metric__label">Returned</div>
        <div class="metric__value">${returned.toLocaleString(undefined, { maximumFractionDigits: 0 })}<small> u</small></div>
        <div class="metric__sub">${(returned - staked).toFixed(0)} units net</div>
      </div>
      <div class="metric">
        <div class="metric__label">Leagues in profit</div>
        <div class="metric__value">${profitable}<small> of ${rows.length}</small></div>
        <div class="meter"><div class="meter__fill ${profitable * 2 >= rows.length ? "meter__fill--pos" : "meter__fill--neg"}"
             style="--fill:${(profitable / rows.length).toFixed(4)}"></div></div>
      </div>
    </div>
    <div class="table-wrap"><table>
      <thead><tr>
        <th scope="col">League</th><th scope="col" class="num">Bets</th>
        <th scope="col" class="num">Staked</th><th scope="col" class="num">Returned</th>
        <th scope="col" class="num">ROI</th><th scope="col">Loss ← → profit</th>
        <th scope="col" class="num">Strike rate</th>
      </tr></thead>
      <tbody>${rows.map((r) => `<tr>
        <td>${esc(r.league)}</td>
        <td class="num">${r.bets}</td>
        <td class="num">${r.staked.toFixed(0)}</td>
        <td class="num">${r.returned.toFixed(2)}</td>
        <td class="num ${r.roi > 0 ? "delta-pos" : "delta-neg"}">${r.roi > 0 ? "+" : ""}${(r.roi * 100).toFixed(1)}%</td>
        <td><div class="divbar"><i class="${r.roi > 0 ? "pos" : "neg"}" style="width:${(Math.abs(r.roi) / scale * 48).toFixed(1)}%"></i></div></td>
        <td class="num">${pct(r.winRate)}</td>
      </tr>`).join("")}</tbody>
      <caption>Backs every outcome where the model's probability exceeded the bookmaker's implied
        probability by the training script's threshold, one unit per bet, settled at the real
        historical price. No staking plan, no filtering by league, no removal of losing runs.</caption>
    </table></div>`;
}

/* ==========================================================================
   WIRING
   ========================================================================== */

function pressGroup(container, onPick) {
  container.addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-sport]");
    if (!btn) return;
    for (const b of $$("button[data-sport]", container)) {
      b.setAttribute("aria-pressed", String(b === btn));
    }
    onPick(btn.dataset.sport);
  });
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function init() {
  initTheme();
  addEventListener("hashchange", route);
  route();

  /* --- board --- */
  $("#board-refresh").addEventListener("click", () => loadBoard(true));
  $("#board-sort").addEventListener("change", (e) => { board.sort = e.target.value; renderBoard(); });
  $("#board-search").addEventListener("input", debounce((e) => {
    board.query = e.target.value;
    renderBoard();
  }, 160));
  pressGroup($("#board-sport"), (s) => { board.sport = s; renderBoard(); });

  $("#board-leagues").addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-league]");
    if (!btn) return;
    const key = btn.dataset.league;
    if (board.leagues.has(key)) board.leagues.delete(key); else board.leagues.add(key);
    btn.setAttribute("aria-pressed", String(board.leagues.has(key)));
    renderBoard();
  });

  // Row expansion, plus the two recovery buttons the empty states can render.
  $("#board-content").addEventListener("click", (ev) => {
    if (ev.target.closest("#board-refetch")) { loadBoard(true); return; }
    if (ev.target.closest("#board-reset")) {
      board.query = ""; board.sport = "all"; board.leagues.clear();
      $("#board-search").value = "";
      for (const b of $$("#board-sport button")) b.setAttribute("aria-pressed", String(b.dataset.sport === "all"));
      renderRail();
      renderBoard();
      return;
    }
    const summary = ev.target.closest(".fixture__summary[aria-controls]");
    if (!summary) return;
    const panel = document.getElementById(summary.getAttribute("aria-controls"));
    const open = summary.getAttribute("aria-expanded") !== "true";
    summary.setAttribute("aria-expanded", String(open));
    panel.dataset.open = String(open);
  });

  /* --- matchup --- */
  $("#matchup-tabs").addEventListener("click", (ev) => {
    const tab = ev.target.closest("[role=tab]");
    if (!tab) return;
    for (const t of $$("[role=tab]", $("#matchup-tabs"))) {
      const on = t === tab;
      t.setAttribute("aria-selected", String(on));
      $(`#${t.getAttribute("aria-controls")}`).hidden = !on;
    }
  });
  $("#s-league").addEventListener("change", () => loadSoccerTeams().catch(() => {}));
  $("#s-run").addEventListener("click", runSoccer);
  $("#n-run").addEventListener("click", runNBA);
  $("#s-fetch-odds").addEventListener("click", () => fetchRealOdds("soccer"));
  $("#n-fetch-odds").addEventListener("click", () => fetchRealOdds("basketball"));
  for (const id of ["#s-home", "#s-away", "#s-oh", "#s-od", "#s-oa"]) {
    $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") runSoccer(); });
  }
  for (const id of ["#n-home", "#n-away", "#n-oh", "#n-oa"]) {
    $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") runNBA(); });
  }

  /* --- accumulator --- */
  $("#acca-build").addEventListener("click", buildAccumulator);

  /* --- track record --- */
  // Any filter change starts the paged table over at the first page.
  const refilter = () => { record.limit = PAGE; renderRecord(); };
  for (const id of ["#log-from", "#log-to", "#log-league"]) {
    $(id).addEventListener("change", refilter);
  }
  pressGroup($("#log-sport"), (s) => { record.sport = s; fillRecordLeagues(); refilter(); });
  $("#log-clear").addEventListener("click", () => {
    $("#log-from").value = ""; $("#log-to").value = ""; $("#log-league").value = "";
    record.sport = "all";
    for (const b of $$("#log-sport button")) b.setAttribute("aria-pressed", String(b.dataset.sport === "all"));
    fillRecordLeagues();
    refilter();
  });
  $("#log-export").addEventListener("click", exportRecordCsv);
  $("#log-content").addEventListener("click", (ev) => {
    if (!ev.target.closest("#log-more")) return;
    record.limit += PAGE;
    renderRecord();
    // Keep the reading position: focus the new "show more" button if there is one.
    $("#log-more")?.focus();
  });

  /* --- keyboard --- */
  addEventListener("keydown", (e) => {
    const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName || "");
    if (e.key === "/" && !typing && !$("#view-board").hidden) {
      e.preventDefault();
      $("#board-search").focus();
    }
    if (e.key === "Escape" && document.activeElement === $("#board-search")) {
      $("#board-search").value = "";
      board.query = "";
      renderBoard();
    }
  });

  loadBoard(false);
  initMatchup();
}

init();
