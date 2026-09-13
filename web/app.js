/* ==========================================================================
   Prediction Engine — client

   Talks to the FastAPI routes in src/api.py. No dependencies, no build step.

   Escaping: team, competition and venue names come from third-party feeds,
   so every interpolated value goes through esc(). Numbers go through the fmt
   helpers, which render a placeholder rather than "NaN" when a field is
   absent.

   Tables: every <td> carries a data-label. Below 760px the CSS turns each row
   into a card and uses that label as the field name, so a seven-column table
   stays readable on a phone instead of living in a horizontal scroller.
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
const signed = (v, d = 0) => (isNum(v) ? (v > 0 ? "+" : "") + v.toFixed(d) : "–");

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

/** Table cell carrying the column name, for the mobile card layout. */
const td = (label, content, cls = "") =>
  `<td${cls ? ` class="${cls}"` : ""} data-label="${esc(label)}">${content}</td>`;
/** Lead cell: renders as the card's title on mobile rather than a label pair. */
const tdLead = (content) => `<td data-lead>${content}</td>`;

const skeletonRows = (n = 6) =>
  `<div class="fixtures" aria-busy="true" aria-label="Loading">${
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
  `<div class="callout callout--${tone}">${icon(tone === "info" ? "info" : "alert")}<div>${body}</div></div>`;

/* ---------- theme -------------------------------------------------------- */

const prefersDark = () => matchMedia("(prefers-color-scheme: dark)").matches;
const isDark = () => document.documentElement.dataset.theme
  ? document.documentElement.dataset.theme === "dark"
  : prefersDark();

function initTheme() {
  const btn = $("#theme-toggle");
  const paint = () => {
    const dark = isDark();
    $("#theme-icon").setAttribute("href", dark ? "#i-moon" : "#i-sun");
    btn.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
  };
  btn.addEventListener("click", () => {
    const next = isDark() ? "light" : "dark";
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

/* ---------- responsive rail --------------------------------------------- */

/** The filter rails are <details>. On a wide screen they must always be open
 *  (the CSS hides the summary so they read as a static sidebar); on a narrow
 *  one the user controls them and the default is collapsed. */
function syncRails() {
  const wide = matchMedia("(min-width: 1081px)").matches;
  for (const rail of $$(".rail")) {
    if (wide) rail.open = true;
    else if (rail.dataset.touched !== "true") rail.open = false;
  }
}

/* ---------- mobile navigation -------------------------------------------- */

/** The hamburger menu. Deliberately more than a class toggle: a menu that
 *  traps focus behind it, or that a keyboard user cannot dismiss, is worse
 *  than the scrolling strip it replaced. */
const nav = {
  isMobile: () => matchMedia("(max-width: 760px)").matches,
  isOpen: () => $("#nav-toggle").getAttribute("aria-expanded") === "true",
};

function setNavOpen(open, { restoreFocus = true } = {}) {
  const toggle = $("#nav-toggle");
  const panel = $("#mainnav");
  const scrim = $("#nav-scrim");

  toggle.setAttribute("aria-expanded", String(open));
  toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
  panel.dataset.open = String(open);
  scrim.hidden = !open;
  // Stop the page scrolling underneath an open menu.
  document.body.style.overflow = open ? "hidden" : "";

  if (open) {
    panel.querySelector("a")?.focus();
  } else if (restoreFocus && panel.contains(document.activeElement)) {
    toggle.focus();
  }
}

function initNav() {
  const toggle = $("#nav-toggle");
  const panel = $("#mainnav");

  toggle.addEventListener("click", () => setNavOpen(!nav.isOpen()));
  $("#nav-scrim").addEventListener("click", () => setNavOpen(false));

  // Navigating closes the menu; the hash change re-renders behind it.
  panel.addEventListener("click", (ev) => {
    if (ev.target.closest("a") && nav.isMobile()) setNavOpen(false, { restoreFocus: false });
  });

  addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && nav.isOpen()) {
      ev.preventDefault();
      setNavOpen(false);
    }
  });

  // Widening past the breakpoint must not leave the body scroll-locked or the
  // panel stranded open behind a hamburger that is no longer rendered.
  matchMedia("(max-width: 760px)").addEventListener("change", (e) => {
    if (!e.matches) setNavOpen(false, { restoreFocus: false });
  });
}

/* ---------- performance panes -------------------------------------------- */

function selectPerfPane(which) {
  for (const tab of $$("#perf-tabs [role=tab]")) {
    const on = tab.id === `tab-${which}`;
    tab.setAttribute("aria-selected", String(on));
    $(`#${tab.getAttribute("aria-controls")}`).hidden = !on;
  }
}

/* ---------- routing ------------------------------------------------------ */

const VIEWS = ["board", "matchup", "performance"];
const loaded = new Set();

// Destinations that used to be their own view. Kept so existing links and
// bookmarks land somewhere sensible instead of silently falling back.
const MOVED = { record: "performance", backtest: "performance", accumulator: "board" };

function route() {
  const want = location.hash.replace("#", "");
  const view = VIEWS.includes(want) ? want : (MOVED[want] || "board");
  for (const v of VIEWS) $(`#view-${v}`).hidden = v !== view;
  for (const a of $$(".mainnav a")) {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  if (!loaded.has(view)) {
    loaded.add(view);
    if (view === "matchup") initMatchup();
    if (view === "performance") { loadRecord(); loadBacktest(); }
  }
  if (want === "backtest") selectPerfPane("backtest");
  if (want === "accumulator") $("#acca-tool")?.setAttribute("open", "");
}

/* ==========================================================================
   BOARD
   ========================================================================== */

const board = { data: null, sport: "all", query: "", sort: "time", dir: "asc", leagues: new Set() };

async function loadBoard(force = false) {
  const btn = $("#board-refresh");
  btn.dataset.busy = "true";
  btn.disabled = true;
  $("#board-content").innerHTML = skeletonRows();
  $("#board-coverage").innerHTML = "";
  $("#board-meta").textContent = force ? "Re-fetching the feeds…" : "Loading the board…";
  try {
    board.data = await getJSON("/api/fixtures/live" + (force ? "?refresh=true" : ""));
    renderRail();
    renderBoard(true);
    renderSummary();
    renderCoverage();
    const s = board.data.summary || {};
    setStatus(s.leagues_live > 0 ? "ok" : "stale",
      `${s.leagues_live || 0}/${s.leagues_total || 0} feeds live`);
  } catch (err) {
    board.data = null;
    $("#board-meta").textContent = "";
    $("#board-content").innerHTML = emptyState(
      "The fixture feeds did not answer",
      `${esc(err.message)}<br>The models themselves are local and unaffected, so the
       Matchup tab will still price any pairing you type in.`,
      `<a class="btn btn--secondary" href="#matchup">Go to Matchup</a>`
    );
    setStatus("down", "Feeds unavailable");
  } finally {
    btn.dataset.busy = "false";
    btn.disabled = false;
  }
}

/** Flattens the API shape into one list of groups so filtering and sorting
 *  only has to be written once for both sports. */
function boardGroups() {
  if (!board.data) return [];
  const groups = [];
  for (const [div, g] of Object.entries(board.data.soccer || {})) {
    if (g?.fixtures?.length) {
      groups.push({ key: div, sport: "soccer", league: g.league || div,
                    fixtures: g.fixtures, stale: g.stale,
                    modelStale: g.model_stale, modelAge: g.model_age_days,
                    modelDate: g.model_trained_as_of });
    }
  }
  const afl = board.data.afl;
  if (afl?.fixtures?.length) {
    groups.push({ key: "AFL", sport: "afl", league: "AFL",
                  fixtures: afl.fixtures, stale: afl.stale,
                  modelStale: afl.model_stale, modelAge: afl.model_age_days,
                  modelDate: afl.model_trained_as_of });
  }
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
    : `<p class="hint" style="padding:0 .5rem">Nothing on the board.</p>`;
  updateFilterCount();
}

function updateFilterCount() {
  const n = (board.query ? 1 : 0) + (board.sport !== "all" ? 1 : 0) + board.leagues.size;
  $("#filter-count").textContent = n ? String(n) : "";
}

const kickoffKey = (f) => `${f.date || "9999-99-99"}T${f.time || "00:00"}`;
const topProb = (p) => Math.max(p?.prob_home_win ?? 0, p?.prob_draw ?? 0, p?.prob_away_win ?? 0);

/* ---------- derived prediction fields ------------------------------------
   The board is a scan surface: every column here has to be readable at a
   glance and comparable straight down the column, across competitions. */

/** Bookmaker shorthand for the model's call. 1 / X / 2 is the notation the
 *  whole category uses, so it is what the column shows; the full wording
 *  lives in the title attribute and in the expanded row. */
function tipFor(p, sport) {
  const h = p.prob_home_win ?? 0, d = p.prob_draw ?? 0, a = p.prob_away_win ?? 0;
  // AFL draws are ~1% and the market refunds them, so a two-way call is the
  // honest reading there; only soccer can be tipped X.
  if (sport === "soccer" && d > h && d > a) return { code: "X", prob: d, word: "Draw" };
  return h >= a
    ? { code: "1", prob: h, word: "Home win" }
    : { code: "2", prob: a, word: "Away win" };
}

/** Totals call. Soccer runs on the 2.5 line the model is trained against;
 *  the AFL line is per-fixture and comes back with the prediction. */
function totalsFor(p, sport) {
  if (sport === "soccer") {
    const over = p.prob_over_2_5 ?? 0;
    return over >= 0.5
      ? { code: "O2.5", prob: over } : { code: "U2.5", prob: p.prob_under_2_5 ?? 0 };
  }
  const line = p.total_line ?? 169.5;
  const over = p.prob_over_total ?? 0;
  return over >= 0.5
    ? { code: `O${line}`, prob: over } : { code: `U${line}`, prob: p.prob_under_total ?? 0 };
}

const predScore = (p) =>
  isNum(p.predicted_score_home) && isNum(p.predicted_score_away)
    ? `${p.predicted_score_home}–${p.predicted_score_away}` : "–";

/* ---------- crests, flags, odds ------------------------------------------
   No crest imagery is licensed or fetched: the badges are generated from the
   club name, so they work offline, cost no requests, and cannot 404. */

/** Country code per competition. Deliberately NOT flag emoji: Windows ships
 *  no flag glyphs at all, so 🇳🇱 renders as the letters "NL" and England's
 *  tag sequence renders as a blank white flag — verified in Chromium here.
 *  A code chip renders identically on every platform and reads as a
 *  deliberate mark rather than a broken one. */
const DIV_CC = {
  E0: "ENG", E1: "ENG", SC0: "SCO", D1: "GER", F1: "FRA", I1: "ITA",
  N1: "NED", SP1: "ESP", P1: "POR", BRA: "BRA", ARG: "ARG", MEX: "MEX",
  USA: "USA", T1: "TUR", G1: "GRE", B1: "BEL", AFL: "AUS",
};
const ccFor = (div, sport) => DIV_CC[div] || (sport === "afl" ? "AUS" : "—");

/** Deterministic monogram badge. The hue comes from the club name, so a
 *  team keeps the same colour everywhere on the board and between sessions
 *  without anything being stored. */
function monogram(name) {
  const clean = String(name || "").trim();
  if (!clean) return "";
  let h = 0;
  for (let i = 0; i < clean.length; i++) h = (h * 31 + clean.charCodeAt(i)) >>> 0;
  const words = clean.split(/[\s.-]+/).filter(Boolean);
  const initials = (words.length > 1 ? words[0][0] + words[1][0] : clean.slice(0, 2))
    .toUpperCase();
  return `<i class="crest" style="--h:${h % 360}" aria-hidden="true">${esc(initials)}</i>`;
}

/** Model-implied fair odds. Explicitly NOT a bookmaker price — 1 ÷ p, with
 *  no margin — which is why the column is labelled "model odds". */
const fairOdds = (p) => (isNum(p) && p > 0 ? (1 / p).toFixed(2) : "–");

/** The 1 / X / 2 box set. Palmerbet-style bordered price cells, with the
 *  model's own call filled rather than outlined, so the tip is legible at a
 *  glance without a separate column repeating it. */
function oddsBoxes(p, sport, tip) {
  const legs = sport === "soccer"
    ? [["1", p.prob_home_win], ["X", p.prob_draw], ["2", p.prob_away_win]]
    : [["1", p.prob_home_win], ["2", p.prob_away_win]];
  return `<span class="odds" style="--n:${legs.length}">${legs.map(([code, prob]) => `
    <span class="odd${code === tip.code ? " is-pick" : ""}">
      <b class="odd__k">${code}</b>
      <b class="odd__v">${fairOdds(prob)}</b>
      <span class="odd__p">${pct(prob, 0)}</span>
    </span>`).join("")}</span>`;
}

/** "L-L-D-L-L" -> five pills, oldest first. Already scoped to that team. */
function formPills(results) {
  if (!results) return "";
  return `<span class="form">${results.split("-").slice(-5).map((r) => {
    const k = r.trim().toUpperCase();
    return `<i class="form__p form__p--${k === "W" ? "w" : k === "D" ? "d" : "l"}">${esc(k)}</i>`;
  }).join("")}</span>`;
}

/** "England - Premier League" -> "Premier League". The country is dropped
 *  because the column is already narrow and the full name stays in title. */
const shortLeague = (name) => {
  const parts = String(name || "").split(" - ");
  return parts.length > 1 ? parts.slice(1).join(" - ") : name;
};

/* ---------- sorting ------------------------------------------------------
   Column headers are the primary control (the category convention), and the
   "Order by" select drives the same state for the two model-specific
   orderings that are not plain columns. */

const SORTS = {
  time:   { label: "Kick-off",   get: (r) => kickoffKey(r.f) },
  league: { label: "Competition", get: (r) => `${r.league}${kickoffKey(r.f)}` },
  match:  { label: "Match",      get: (r) => (r.f.home_team_live_name || "").toLowerCase() },
  tip:    { label: "Tip",        get: (r) => (r.tip ? r.tip.code : "~") },
  prob:   { label: "Confidence", get: (r) => (r.tip ? r.tip.prob : -1) },
};

function sortRows(rows) {
  const spec = SORTS[board.sort] || SORTS.time;
  const dir = board.dir === "desc" ? -1 : 1;
  return [...rows].sort((x, y) => {
    const a = spec.get(x), b = spec.get(y);
    const cmp = typeof a === "number" ? a - b : String(a).localeCompare(String(b));
    // Kick-off is the stable tiebreak: two fixtures with the same tip or the
    // same confidence should still read chronologically.
    return cmp !== 0 ? cmp * dir : kickoffKey(x.f).localeCompare(kickoffKey(y.f));
  });
}

const COLUMNS = [
  { key: "time",   label: "Kick-off", sort: "time",   cls: "c-when" },
  { key: "league", label: "Comp",     sort: "league", cls: "c-league" },
  { key: "match",  label: "Match",    sort: "match",  cls: "c-match" },
  { key: "form",   label: "Form",     sort: null,     cls: "c-form" },
  // Sorting the odds block sorts by the model's confidence in its own call,
  // which is what "best price" means on a board with no bookmaker attached.
  { key: "odds",   label: "Model odds", sort: "prob", cls: "c-odds" },
  { key: "pred",   label: "Score",    sort: null,     cls: "c-pred" },
  { key: "ou",     label: "Totals",   sort: null,     cls: "c-ou" },
  { key: "open",   label: "",         sort: null,     cls: "c-open" },
];

function renderBoard(animate = false) {
  if (!board.data) return;
  const q = board.query.trim().toLowerCase();
  const hit = (...f) => !q || f.some((x) => String(x || "").toLowerCase().includes(q));

  // Flatten every competition into one list. The league becomes a column
  // rather than a section heading, which is what lets a single sort run
  // across the whole board instead of only within one competition.
  let rows = [];
  for (const g of boardGroups()) {
    if (board.sport !== "all" && g.sport !== board.sport) continue;
    if (board.leagues.size && !board.leagues.has(g.key)) continue;
    for (const f of g.fixtures) {
      if (!hit(f.home_team_live_name, f.away_team_live_name, g.league, f.venue)) continue;
      const p = f.prediction || {};
      rows.push({
        f, group: g, sport: g.sport, league: g.league, p,
        tip: p.error ? null : tipFor(p, g.sport),
      });
    }
  }

  const content = $("#board-content");
  if (!rows.length) {
    const filtered = q || board.leagues.size || board.sport !== "all";
    content.innerHTML = filtered
      ? emptyState("Nothing matches those filters",
          "Widen the sport, clear the competition selection, or search for a different team.",
          `<button class="btn btn--secondary" type="button" id="board-reset">Reset filters</button>`)
      : emptyState("No upcoming fixtures right now",
          `Every feed answered with an empty schedule. Check the coverage panel above for which
           competitions are between seasons and which have no free feed at all. You can still price
           any pairing by hand.`,
          `<button class="btn btn--secondary" type="button" id="board-refetch">Re-fetch feeds</button>
           <a class="btn btn--ghost" href="#matchup">Custom matchup</a>`);
  } else {
    rows = sortRows(rows);
    const head = COLUMNS.map((c) => {
      if (!c.sort) {
        return `<th scope="col" class="${c.cls}">${
          c.label ? esc(c.label) : `<span class="sr-only">Details</span>`}</th>`;
      }
      const active = board.sort === c.sort;
      const dir = active ? board.dir : "none";
      return `<th scope="col" class="${c.cls}" aria-sort="${active ? `${dir}ending` : "none"}">
        <button type="button" class="th-sort" data-sort="${c.sort}" data-active="${active}">
          ${esc(c.label)}<svg aria-hidden="true" class="th-sort__arrow"><use href="#i-sort"/></svg>
        </button></th>`;
    }).join("");

    content.innerHTML = `
      <div class="table-wrap board-table-wrap">
        <table class="board-table" data-animate="${animate}">
          <caption class="sr-only">
            Upcoming fixtures priced by the model. Tip is the model's call in 1 / X / 2 notation,
            Score is the most likely exact scoreline for that call, and Probability is the model's
            confidence in it. Activate a row to open the full reasoning.
          </caption>
          <thead><tr>${head}</tr></thead>
          <tbody>${rows.map(fixtureRow).join("")}</tbody>
        </table>
      </div>`;
  }

  const total = boardGroups().reduce((n, g) => n + g.fixtures.length, 0);
  const meta = $("#board-meta");
  meta.textContent = rows.length === total ? "" : `Showing ${rows.length} of ${total} fixtures`;
  meta.hidden = rows.length === total;
  updateFilterCount();
}

/** Coverage panel. This replaces the old banner that dumped raw
 *  "HTTPError: HTTP Error 429" strings at the user — it says what each
 *  competition's actual state is and what, if anything, can be done. */
const COVERAGE_COPY = {
  live:          { tone: "pos",  label: "Live" },
  stale:         { tone: "warn", label: "Cached" },
  "no-fixtures": { tone: "mute", label: "None listed" },
  snapshot:      { tone: "warn", label: "Snapshot" },
  unsupported:   { tone: "mute", label: "No feed" },
  unconfigured:  { tone: "warn", label: "No API key" },
  error:         { tone: "neg",  label: "Error" },
};

function renderSummary() {
  const d = board.data;
  if (!d) return;
  const s = d.summary || {};
  const cov = d.coverage || [];

  $("#sum-fixtures").textContent = s.total_fixtures ?? "–";
  $("#sum-feeds").textContent = `${s.leagues_live ?? 0}/${s.leagues_total ?? 0}`;

  // Earliest kick-off still ahead of us, across both sports.
  const now = Date.now();
  const upcoming = boardGroups()
    .flatMap((g) => g.fixtures)
    .map((f) => new Date(`${f.date}T${f.time || "00:00:00"}Z`).getTime())
    .filter((t) => Number.isFinite(t) && t > now)
    .sort((a, b) => a - b);
  $("#sum-next").textContent = upcoming.length
    ? new Date(upcoming[0]).toLocaleString(undefined,
        { weekday: "short", hour: "2-digit", minute: "2-digit" })
    : "–";

  // Model health is the honest one. Reported as a count of stale models
  // rather than a ratio, because a ratio here reads identically to the feed
  // ratio beside it and the two mean completely different things.
  const stale = cov.filter((c) => c.model_stale).length;
  const el = $("#sum-health");
  el.textContent = stale ? String(stale) : "0";
  el.dataset.tone = stale ? "warn" : "pos";
  $("#sum-health-label").textContent = stale === 1 ? "stale model" : "stale models";
  el.title = stale
    ? `${stale} competition${stale === 1 ? "" : "s"} priced by a model that has not seen a match in over 60 days`
    : "Every model trained within the last 60 days";
}

function renderCoverage() {
  const box = $("#board-coverage");
  const cov = board.data?.coverage || [];
  if (!cov.length) { box.innerHTML = ""; return; }

  const states = board.data.summary?.states || {};
  // A competition can have a perfectly live feed and a two-year-old model.
  // That is exactly the case worth reporting, so freshness gets in here too.
  const notLive = cov.filter((c) => c.state !== "live" || c.model_stale);
  if (!notLive.length) { box.innerHTML = ""; return; }

  const order = ["error", "unconfigured", "snapshot", "stale", "no-fixtures", "unsupported"];
  const rows = [...notLive].sort(
    (a, b) => order.indexOf(a.state) - order.indexOf(b.state));

  const staleModels = cov.filter((c) => c.model_stale).length;
  const summary = [
    ...order.filter((s) => states[s]).map((s) => `${states[s]} ${COVERAGE_COPY[s].label.toLowerCase()}`),
    ...(staleModels ? [`${staleModels} stale model${staleModels === 1 ? "" : "s"}`] : []),
  ].join(", ");

  box.innerHTML = `
    <details class="coverage-panel">
      <summary>
        ${icon("info")}
        <span>Feed coverage: ${states.live || 0} live${summary ? `, ${esc(summary)}` : ""}</span>
      </summary>
      <div class="coverage">
        ${rows.map((c) => {
          const meta = COVERAGE_COPY[c.state] || COVERAGE_COPY.error;
          return `<div class="coverage__row">
            <span class="badge badge--${meta.tone}">${esc(meta.label)}</span>
            <span class="coverage__name">${esc(c.league)}</span>
            <span class="coverage__detail">${esc(c.detail || "")}${
              c.model_stale
                ? `<em class="coverage__stale">Model last trained ${esc(c.model_trained_as_of)}, ${c.model_age_days} days ago.</em>`
                : ""}</span>
          </div>`;
        }).join("")}
        <p class="why-scope">
          Competitions marked "no feed" have no free source listing their fixtures. They are still
          fully modelled: use the Matchup tab to price any pairing in them.
        </p>
      </div>
    </details>`;
}

let rowId = 0;

/** Feeds return legal club names — "Wolverhampton Wanderers FC", "SBV
 *  Excelsior" — which wrapped to two and three lines and made every row a
 *  different height, destroying the vertical rhythm a scan surface depends
 *  on. Dropping the corporate affix is what every results service does and
 *  is the difference between a board and a spreadsheet. The untouched name
 *  stays in the title attribute and in the expanded row. */
const CLUB_AFFIX = /^(fc|afc|sv|sc|bsc|vfl|vfb|ss|ssc|as|ac|us|ud|cd|cf|rc|rcd|sd|ca|club|fk|nk|hnk|bk|if|ik|gif|aik)\b[.\s]*|[\s.]*\b(fc|afc|cf|sc|ac|as|sv|bv|bk|if|sk|fk|kv|sd|cd|ud|cp|oa?fc)\.?$/gi;

function shortTeam(name) {
  const n = String(name || "").replace(CLUB_AFFIX, "").trim();
  // Never return an empty string: a club literally called "FC" keeps its name.
  return n.length >= 3 ? n : String(name || "");
}

/** Home v away, with the side the model tips carrying the emphasis. The
 *  favourite is marked by weight rather than colour, so it survives the
 *  forced-colors and monochrome-print paths the rest of the board honours. */
function matchCell(f, tip) {
  const warn = f.unmatched?.length
    ? `<span class="match__warn" title="${esc(f.unmatched.join(" and "))} not in the model; priced at competition-average strength">${icon("alert")}</span>`
    : "";
  // Stacked home-over-away with a badge each, the way every scores product
  // sets a fixture — an inline "A v B" reads as prose, not as a match.
  const side = (name, fav) =>
    `<span class="match__side${fav ? " is-fav" : ""}">
       ${monogram(name)}<span class="match__name" title="${esc(name)}">${esc(shortTeam(name))}</span>
     </span>`;
  return `<span class="match">
    ${side(f.home_team_live_name, tip?.code === "1")}
    ${side(f.away_team_live_name, tip?.code === "2")}
  </span>${warn}`;
}

function fixtureRow(r) {
  const { f, sport, league, p } = r;
  const { time, day } = fmtKick(f.date, f.time);
  const span = COLUMNS.length;
  const leagueCell =
    `<span class="league-tag" title="${esc(league)}"><i class="cc">${
      esc(ccFor(f.div ?? r.group?.key, sport))}</i><span class="league-tag__n">${
      esc(shortLeague(league))}</span></span>`;
  const when = `<span class="when__t">${esc(time)}</span><span class="when__d">${esc(day)}</span>`;

  // A fixture the model could not price still belongs on the board — hiding
  // it would quietly misrepresent the schedule — but it gets no tip columns.
  if (p.error) {
    return `<tr class="fx fx--dead">
      ${td("Kick-off", when, "c-when")}
      ${td("Competition", leagueCell, "c-league")}
      <td class="c-match" data-lead>${matchCell(f, null)}</td>
      <td class="c-dead" colspan="${span - 3}">
        <span class="badge badge--mute">${icon("alert")}No price: ${esc(p.error)}</span>
      </td>
    </tr>`;
  }

  const id = `fx-${rowId++}`;
  const tip = r.tip;
  const totals = totalsFor(p, sport);

  const form = p.rationale
    ? `${formPills(p.rationale.home_form?.results)}${formPills(p.rationale.away_form?.results)}`
    : "";

  return `<tr class="fx" data-row="${id}">
    ${td("Kick-off", when, "c-when")}
    ${td("Competition", leagueCell, "c-league")}
    <td class="c-match" data-lead>${matchCell(f, tip)}</td>
    ${td("Form", form || `<span class="dash">–</span>`, "c-form")}
    ${td("Model odds", oddsBoxes(p, sport, tip), "c-odds")}
    ${td("Score", `<span class="pred">${predScore(p)}</span>`, "c-pred")}
    ${td("Totals", `<span class="ou">${esc(totals.code)}<b>${pct(totals.prob)}</b></span>`, "c-ou")}
    <td class="c-open">
      <button class="disclose" type="button" aria-expanded="false" aria-controls="${id}">
        <span class="sr-only">Reasoning for ${esc(f.home_team_live_name)} v ${esc(f.away_team_live_name)}</span>
        ${icon("chevron")}
      </button>
    </td>
  </tr>
  <tr class="fx-detail" id="${id}" hidden>
    <td colspan="${span}"><div class="fx-detail__inner">${
      f.unmatched?.length
        ? `<div class="callout callout--warn" style="margin-bottom:1rem">${icon("alert")}<div>
             <strong>${esc(f.unmatched.join(" and "))}</strong>
             ${f.unmatched.length === 2 ? "are" : "is"} not in this competition's training data,
             so ${f.unmatched.length === 2 ? "they are" : "it is"} priced at competition-average
             strength. Treat this line as much weaker evidence than the rest of the board.
           </div></div>`
        : ""
    }${detailBody(p, sport)}</div></td>
  </tr>`;
}

function detailBody(p, sport) {
  const eloPair = isNum(p.elo_home) ? `${Math.round(p.elo_home)} – ${Math.round(p.elo_away)}` : "–";
  const readouts = sport === "soccer"
    ? [
        ["Expected goals", `${num(p.expected_goals_home)} – ${num(p.expected_goals_away)}`],
        ["Over 2.5", pct(p.prob_over_2_5)],
        ["Under 2.5", pct(p.prob_under_2_5)],
        ["Both teams score", pct(p.prob_btts_yes)],
        ["Elo", eloPair],
      ]
    : [
        ["Projected margin", `${signed(p.predicted_margin_home)} pts`],
        ["Projected total", num(p.predicted_total_points, 0)],
        [`Over ${p.total_line ?? 169.5}`, pct(p.prob_over_total)],
        ["Draw", pct(p.prob_draw, 2)],
        ["Elo", eloPair],
      ];

  return `<div class="readouts">${
      readouts.map(([k, v]) => `<div class="readout">${esc(k)}<b>${v}</b></div>`).join("")
    }</div>${rationale(p.rationale)}`;
}

function rationale(r) {
  if (!r?.narrative?.length) {
    return `<p class="why-scope">No rationale was produced for this fixture.</p>`;
  }
  // Narrative bullets are model-generated from local data and may contain <b>.
  return `<ul class="why">${r.narrative.map((b) => `<li>${b}</li>`).join("")}</ul>
          ${r.data_scope ? `<p class="why-scope">${r.data_scope}</p>` : ""}`;
}

/* ==========================================================================
   MATCHUP
   ========================================================================== */

let matchupReady = false;

async function initMatchup() {
  if (matchupReady) return;
  matchupReady = true;
  try {
    const leagues = await getJSON("/api/soccer/leagues");
    $("#s-league").innerHTML = leagues
      .map((l) => `<option value="${esc(l.code)}">${esc(l.name)}</option>`).join("");
    await loadSoccerTeams();
  } catch (err) {
    $("#s-err").textContent = `Could not load competitions: ${err.message}`;
  }
  try {
    const [teams, venues] = await Promise.all([
      getJSON("/api/afl/teams"), getJSON("/api/afl/venues"),
    ]);
    $("#a-teams").innerHTML = teams.map((t) => `<option value="${esc(t)}">`).join("");
    $("#a-venues").innerHTML = venues.map((v) => `<option value="${esc(v)}">`).join("");
  } catch (err) {
    $("#a-err").textContent = `AFL model unavailable: ${err.message}`;
  }
  $("#a-date").value = new Date().toISOString().slice(0, 10);
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

function marketTable(rows) {
  const body = rows.filter(([, odds]) => odds).map(([label, odds, ea]) => {
    if (!ea) return "";
    const value = ea.edge > 0.02;
    return `<tr>
      ${tdLead(esc(label))}
      ${td("Your odds", num(Number(odds)), "num")}
      ${td("Book implied", pct(ea.market_implied_prob), "num")}
      ${td("Model edge", `<span class="${ea.edge > 0 ? "delta-pos" : "delta-neg"}">${ea.edge > 0 ? "+" : ""}${pct(ea.edge)}</span>`, "num")}
      ${td("Kelly stake", pct(ea.kelly_stake_fraction, 2), "num")}
      ${td("Call", `<span class="badge badge--${value ? "pos" : "mute"}">${icon(value ? "check" : "cross")}${value ? "Value" : "Pass"}</span>`)}
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

async function runAFL() {
  const err = $("#a-err");
  err.textContent = "";
  const home = $("#a-home").value.trim();
  const away = $("#a-away").value.trim();
  if (!home || !away) { err.textContent = "Enter both team names."; return; }

  const btn = $("#a-run");
  btn.disabled = true;
  $("#a-results").innerHTML = `<div class="panel"><div class="panel__body">${skeletonRows(2)}</div></div>`;
  const params = new URLSearchParams({ home, away });
  if ($("#a-venue").value.trim()) params.set("venue", $("#a-venue").value.trim());
  if ($("#a-date").value) params.set("game_date", $("#a-date").value);
  if ($("#a-final").checked) params.set("is_final", "true");
  if ($("#a-neutral").checked) params.set("neutral", "true");
  if ($("#a-oh").value) params.set("odds_home", $("#a-oh").value);
  if ($("#a-oa").value) params.set("odds_away", $("#a-oa").value);

  try {
    const r = await getJSON(`/api/afl/predict?${params}`);
    $("#a-results").innerHTML = `<div class="panel"><div class="panel__body">
      ${outcomeBlock([
        ["home", `${r.home_team} win`, r.prob_home_win],
        ["away", `${r.away_team} win`, r.prob_away_win],
      ])}
      ${readoutPanel("Model readouts", [
        ["Projected margin", `${signed(r.predicted_margin_home)} <small>home</small>`],
        ["Projected total", num(r.predicted_total_points, 0)],
        [`Over ${r.total_line}`, `${pct(r.prob_over_total)} / ${pct(r.prob_under_total)}`],
        ["Draw", pct(r.prob_draw, 2)],
        ["Elo rating", isNum(r.elo_home) ? `${Math.round(r.elo_home)} – ${Math.round(r.elo_away)}` : "–"],
        ["Venue games (3yr)", `${num(r.home_venue_experience, 0)} – ${num(r.away_venue_experience, 0)}`],
      ])}
      ${r.market_analysis ? marketTable([
        [`${r.home_team} win`, $("#a-oh").value, r.market_analysis.H],
        [`${r.away_team} win`, $("#a-oa").value, r.market_analysis.A],
      ]) : ""}
      <h3 class="section-title">Why this number</h3>
      ${rationale(r.rationale)}
      <p class="note" style="margin-top:1.5rem">
        ${r.venue ? `${esc(r.venue)} · ` : ""}model trained as of ${esc(r.model_trained_as_of)}
      </p>
    </div></div>`;
  } catch (e) {
    $("#a-results").innerHTML = "";
    err.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

async function fetchRealOdds(kind) {
  const p = kind === "soccer"
    ? { note: "#s-odds-note", btn: "#s-fetch-odds", home: "#s-home", away: "#s-away" }
    : { note: "#a-odds-note", btn: "#a-fetch-odds", home: "#a-home", away: "#a-away" };
  const noteEl = $(p.note), btn = $(p.btn);
  const home = $(p.home).value.trim(), away = $(p.away).value.trim();
  if (!home || !away) { noteEl.textContent = "Enter both team names first."; return; }

  btn.dataset.busy = "true";
  btn.disabled = true;
  noteEl.textContent = "Checking the odds board…";
  const params = new URLSearchParams({ home, away });
  if (kind === "soccer") params.set("div", $("#s-league").value);
  try {
    const r = await getJSON(`/api/odds/${kind}?${params}`);
    if (!r.available) {
      noteEl.textContent = `No live price found. ${r.reason || ""}`;
      return;
    }
    const set = (id, v) => { if (v != null) $(id).value = v; };
    if (kind === "soccer") {
      set("#s-oh", r.odds_home); set("#s-od", r.odds_draw); set("#s-oa", r.odds_away);
    } else {
      set("#a-oh", r.odds_home); set("#a-oa", r.odds_away);
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
          <div class="metric__sub">Best available across tracked books</div>
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
      <div class="panel" style="margin-top:1.5rem">${d.legs.map(accaLeg).join("")}</div>
      <p class="note" style="margin-top:1rem">
        <strong>Model-implied odds</strong> are 1 ÷ the model's probability for that pick. They read
        the model's confidence and are not a price any sportsbook has offered. Where a real price is
        shown it is the best decimal across books tracked by the-odds-api.com as of the last refresh.
        Both-teams-to-score legs and AFL totals never carry a real price, because no provider wired
        into this app quotes those markets at our line. This is a screening tool, not a
        recommendation.
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
          <div class="leg__meta">${esc(leg.home)} v ${esc(leg.away)} · ${esc(leg.league)}${
            leg.venue ? ` · ${esc(leg.venue)}` : ""} · ${esc(day)} ${esc(time)}</div>
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

// A stacked card is roughly four times the height of a table row, so a
// 100-row page that is a comfortable scroll on a desktop becomes 30,000+
// pixels on a phone. Page size follows the layout.
const pageSize = () => (matchMedia("(max-width: 760px)").matches ? 25 : 100);
const record = { data: null, sport: "all", limit: pageSize(), sort: null, dir: "asc" };

async function loadRecord() {
  const box = $("#log-content");
  box.innerHTML = skeletonRows(5);
  record.limit = pageSize();
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
  sel.innerHTML = `<option value="">All</option>` +
    leagues.map((l) => `<option value="${esc(l)}">${esc(l)}</option>`).join("");
  if (leagues.includes(keep)) sel.value = keep;
}

/** A prediction is settled once any of its markets has a real result. */
const isSettled = (e) =>
  Object.values(e.markets || {}).some((m) => "actual" in m);

function recordRows() {
  if (!record.data) return [];
  const from = $("#log-from").value, to = $("#log-to").value, league = $("#log-league").value;
  const rows = record.data.entries.filter((e) => {
    if (record.sport !== "all" && e.sport !== record.sport) return false;
    if (league && e.league !== league) return false;
    if (!e.date) return true;
    if (from && e.date < from) return false;
    if (to && e.date > to) return false;
    return true;
  });

  // Default: settled first. The server sorts by date descending, which put
  // every unplayed fixture above every result — 142 of 211 rows at one
  // point, so the whole first page read "Pending" and the track record this
  // page exists to show was two clicks below the fold. Within the settled
  // block the newest result leads; pending fixtures run in kick-off order,
  // so the next match to be graded is the first one under the divider.
  if (!record.sort) {
    return rows.sort((a, b) => {
      const sa = isSettled(a), sb = isSettled(b);
      if (sa !== sb) return sa ? -1 : 1;
      const da = a.date || "", db = b.date || "";
      return sa ? db.localeCompare(da) : da.localeCompare(db);
    });
  }

  const spec = REC_SORTS[record.sort];
  const dir = record.dir === "desc" ? -1 : 1;
  return rows.sort((a, b) => {
    const x = spec.get(a), y = spec.get(b);
    const cmp = typeof x === "number" ? x - y : String(x).localeCompare(String(y));
    return cmp !== 0 ? cmp * dir : (b.date || "").localeCompare(a.date || "");
  });
}

/* Column sorts for the track record. "Result" sorts by whether the call came
   in, which is the question the page is actually asked — not alphabetically
   by team name, which would be sorting the label rather than the outcome. */
const marketOf = (e, key) =>
  (e.markets || {})[key === "result" ? (e.sport === "afl" ? "h2h" : "1x2")
    : key === "secondary" ? "btts"
    : e.sport === "afl" ? "total_points" : "over_under_2_5"] || {};
const outcomeRank = (m) =>
  !("actual" in m) ? -1 : m.correct ? 2 : 0;

const REC_SORTS = {
  fixture: { get: (e) => `${e.home} ${e.away}`.toLowerCase() },
  date:    { get: (e) => e.date || "" },
  league:  { get: (e) => e.league || "" },
  result:    { get: (e) => outcomeRank(marketOf(e, "result")) },
  secondary: { get: (e) => outcomeRank(marketOf(e, "secondary")) },
  totals:    { get: (e) => outcomeRank(marketOf(e, "totals")) },
  status:  { get: (e) => (isSettled(e) ? 2 : e.graded ? 1 : 0) },
};

const REC_COLUMNS = [
  { key: "fixture",   label: "Fixture" },
  { key: "date",      label: "Date" },
  { key: "league",    label: "Competition" },
  { key: "result",    label: "Result" },
  { key: "secondary", label: "Secondary" },
  { key: "totals",    label: "Totals" },
  { key: null,        label: "Score" },
  { key: "status",    label: "Status" },
];

const pickLabel = (e, market, pick) => {
  if (market === "1x2") return pick === "H" ? e.home : pick === "A" ? e.away : "Draw";
  if (market === "h2h") return pick === "HOME" ? e.home : e.away;
  if (market === "over_under_2_5") return pick === "OVER" ? "Over 2.5" : "Under 2.5";
  if (market === "total_points") {
    const line = e.markets?.total_points?.line ?? 169.5;
    return pick === "OVER" ? `Over ${line}` : `Under ${line}`;
  }
  return pick;
};

function marketCell(e, market) {
  const m = e.markets?.[market];
  if (!m) return `<span style="color:var(--ink-3)">–</span>`;
  const prob = m.probs?.[m.pick];
  const label = `<span class="mkt__pick">${esc(pickLabel(e, market, m.pick))}</span>
    <span class="mkt__p num">${pct(prob)}</span>`;
  if (!("actual" in m)) return `<span class="mkt">${label}</span>`;
  const hit = m.correct;
  // The marker sits on the same line as the call it grades; letting it wrap
  // onto its own line detaches it from the pick it belongs to.
  return `<span class="mkt"><span class="mkt__call">${label}</span>
    <span class="res res--${hit ? "hit" : "miss"}" title="${
      hit ? "Correct" : "Wrong"}" role="img" aria-label="${hit ? "Correct" : "Wrong"}">${
      icon(hit ? "check" : "cross")}</span></span>`;
}

function accuracyMetric(label, stat, sub) {
  const has = stat && stat.accuracy !== null && stat.graded > 0;
  return `<div class="metric">
    <div class="metric__label">${esc(label)}</div>
    <div class="metric__value">${has ? pct(stat.accuracy, 1) : `<small>not graded yet</small>`}</div>
    <div class="metric__sub">${sub || `${stat?.correct ?? 0} correct of ${stat?.graded ?? 0} settled`}</div>
    <div class="meter"><div class="meter__fill ${has && stat.accuracy >= 0.5 ? "meter__fill--pos" : ""}"
         style="--fill:${has ? stat.accuracy.toFixed(4) : 0}"></div></div>
  </div>`;
}

function renderRecord() {
  const box = $("#log-content");
  if (!record.data) { box.innerHTML = ""; return; }
  const s = record.data.summary;
  const rows = recordRows();
  const showAfl = record.sport !== "soccer";
  const showSoccer = record.sport !== "afl";

  let metrics = "";
  if (showSoccer) {
    metrics += accuracyMetric("Soccer 1X2", s.soccer_1x2)
             + accuracyMetric("Both teams score", s.soccer_btts)
             + accuracyMetric("Over / under 2.5", s.soccer_over_under);
  }
  if (showAfl) {
    metrics += accuracyMetric("AFL head-to-head", s.afl_h2h)
             + accuracyMetric("AFL total points", s.afl_total);
  }
  metrics += `<div class="metric">
      <div class="metric__label">Logged</div>
      <div class="metric__value">${s.total_logged}</div>
      <div class="metric__sub">${s.awaiting_result ?? 0} awaiting a result · ${rows.length} match filters</div>
    </div>`;
  if (showAfl && isNum(s.afl_margin_mae)) {
    metrics += `<div class="metric">
      <div class="metric__label">AFL margin error</div>
      <div class="metric__value">${num(s.afl_margin_mae, 1)}<small> pts</small></div>
      <div class="metric__sub">Mean absolute error on settled games</div>
    </div>`;
  }

  let html = `<div class="metrics" style="margin-bottom:1.5rem">${metrics}</div>`;

  if (!rows.length) {
    html += record.data.entries.length
      ? emptyState("No entries in that range", "Widen the dates, or clear the competition and sport filters.")
      : emptyState("Nothing logged yet",
          `The log fills in as fixtures appear on the board. Each entry stays pending until the day
           after the match, then grades itself against the real final score from the same feed that
           supplied the fixture.`,
          `<a class="btn btn--secondary" href="#board">Open the board</a>`);
  } else {
    const page = rows.slice(0, record.limit);
    html += `<div class="table-wrap"><table>
      <thead><tr>${REC_COLUMNS.map((c) => {
        if (!c.key) return `<th scope="col">${esc(c.label)}</th>`;
        const on = record.sort === c.key;
        return `<th scope="col" aria-sort="${on ? `${record.dir}ending` : "none"}">
          <button type="button" class="th-sort" data-rsort="${c.key}" data-active="${on}">
            ${esc(c.label)}<svg aria-hidden="true" class="th-sort__arrow"><use href="#i-sort"/></svg>
          </button></th>`;
      }).join("")}</tr></thead>
      <tbody>${page.map((e, i) => {
        const afl = e.sport === "afl";
        // One divider where results stop and unplayed fixtures begin, so the
        // switch from record to schedule is visible rather than something
        // you infer from the Status column changing.
        // Only meaningful in the default order, where the table really is
        // partitioned; under a column sort the two states interleave.
        const divider = !record.sort && i > 0 && isSettled(page[i - 1]) && !isSettled(e)
          ? `<tr class="rec-split"><td colspan="8">
               Not yet played — ${rows.filter((x) => !isSettled(x)).length} awaiting a result
             </td></tr>`
          : "";
        return `${divider}<tr>
          ${tdLead(`${esc(e.home)} v ${esc(e.away)}`)}
          ${td("Date", esc(e.date || ""), "num nowrap")}
          ${td("Competition", esc(e.league))}
          ${td("Result", marketCell(e, afl ? "h2h" : "1x2"))}
          ${td("Secondary", afl ? `<span style="color:var(--ink-3)">–</span>` : marketCell(e, "btts"))}
          ${td("Totals", marketCell(e, afl ? "total_points" : "over_under_2_5"))}
          ${td("Score", e.actual_home_score != null
              ? `${esc(e.actual_home_score)}–${esc(e.actual_away_score)}` : "–", "num")}
          ${td("Status", statusBadge(e))}
        </tr>`;
      }).join("")}</tbody>
      <caption>A fixture is settled once its real final score can be fetched. "Unresolved" means no
        result was ever available, which normally means the match was postponed or abandoned. This
        log is stored on the server's disk and resets if the app is redeployed without a volume.</caption>
    </table></div>`;

    if (rows.length > page.length) {
      html += `<div class="more-row">
        <button class="btn btn--secondary" type="button" id="log-more">
          Show ${Math.min(pageSize(), rows.length - page.length)} more
        </button>
        <span class="hint">${page.length} of ${rows.length} shown. CSV covers all ${rows.length}.</span>
      </div>`;
    }
  }
  box.innerHTML = html;
}

function statusBadge(e) {
  if (!e.graded) return `<span class="badge badge--mute">Pending</span>`;
  return isSettled(e)
    ? `<span class="badge badge--pos">${icon("check")}Settled</span>`
    : `<span class="badge badge--warn">${icon("alert")}Unresolved</span>`;
}

function exportRecordCsv() {
  const rows = recordRows();
  if (!rows.length) return;
  const markets = ["1x2", "btts", "over_under_2_5", "h2h", "total_points"];
  const headers = ["date", "sport", "league", "home", "away",
    ...markets.flatMap((m) => [`${m}_pick`, `${m}_prob`, `${m}_actual`, `${m}_correct`]),
    "actual_home_score", "actual_away_score", "predicted_margin_home", "actual_margin_home", "status"];

  const cell = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const fields = (e, key) => {
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
      ...markets.flatMap((m) => fields(e, m)),
      e.actual_home_score ?? "", e.actual_away_score ?? "",
      e.predicted_margin_home ?? "", e.actual_margin_home ?? "", status].map(cell).join(","));
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
  const afl = data.afl;
  const box = $("#backtest-content");

  if (!soccer.length && !afl) {
    box.innerHTML = emptyState("No backtest reports on this deployment",
      `Reports are written to <code>reports/</code> by the training runs:
       <code>python3 src/train.py</code> and <code>python3 src/train_afl.py</code>.`);
    return;
  }

  let html = "";

  if (soccer.length) {
    const beat = soccer.filter((x) => x.brier.blended_calibrated < x.brier.market);
    const mean = (f) => soccer.reduce((s, x) => s + f(x), 0) / soccer.length;
    const edges = soccer.map((x) => x.brier.market - x.brier.blended_calibrated);
    const scale = Math.max(...edges.map(Math.abs)) || 1;

    html += `<h2 class="section-title" style="margin-top:0">Soccer</h2>
    <div class="metrics" style="margin-bottom:1.5rem">
      <div class="metric">
        <div class="metric__label">Competitions backtested</div>
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

    const rows = soccer.map((x, i) => ({ x, edge: edges[i] })).sort((a, b) => b.edge - a.edge);
    html += `<div class="table-wrap"><table>
        <thead><tr>
          <th scope="col">Competition</th><th scope="col" class="num">Held out</th>
          <th scope="col" class="num">Model Brier</th><th scope="col" class="num">Market Brier</th>
          <th scope="col" class="num">Difference</th><th scope="col">Market ← → model</th>
          <th scope="col" class="num">Model log-loss</th>
        </tr></thead>
        <tbody>${rows.map(({ x, edge }) => {
          const w = (Math.abs(edge) / scale * 48).toFixed(1);
          return `<tr>
            ${tdLead(esc(x.league))}
            ${td("Held out", x.n_test_holdout.toLocaleString(), "num")}
            ${td("Model Brier", x.brier.blended_calibrated.toFixed(3), "num")}
            ${td("Market Brier", x.brier.market.toFixed(3), "num")}
            ${td("Difference", `<span class="${edge > 0 ? "delta-pos" : "delta-neg"}">${edge > 0 ? "+" : ""}${edge.toFixed(3)}</span>`, "num")}
            ${td("Market ← → model", `<div class="divbar"><i class="${edge > 0 ? "pos" : "neg"}" style="width:${w}%"></i></div>`)}
            ${td("Model log-loss", x.log_loss.blended_calibrated.toFixed(3), "num")}
          </tr>`;
        }).join("")}</tbody>
        <caption>Brier score and log-loss are both "lower is better", with 0 a perfect forecast.
          The market column is the closing bookmaker price with the overround removed, taken from
          real historical odds. A positive difference means the model scored better than that
          closing line on held-out matches.</caption>
      </table></div>`;

    html += valueBetSection(soccer);
  }

  if (afl) html += aflBacktestSection(afl);

  html += `<p class="note" style="margin-top:1.5rem">
    These figures are read straight from <code>reports/soccer_backtest.json</code> and
    <code>reports/afl_backtest.json</code>, written by the training scripts against matches the
    models never saw during fitting. They are not recomputed in the browser and have not been
    filtered to flatter the models.
  </p>`;

  box.innerHTML = html;
}

function aflBacktestSection(a) {
  const w = a.win_probability || {};
  const b = a.baselines || {};
  const lift = (a.model_accuracy ?? 0) - (b.always_home_accuracy ?? 0);
  const marginLift = (b.mean_margin_mae ?? 0) - (a.margin_mae ?? 0);

  return `<h2 class="section-title">AFL</h2>
    <div class="metrics" style="margin-bottom:1.5rem">
      <div class="metric">
        <div class="metric__label">Tipping accuracy</div>
        <div class="metric__value">${pct(a.model_accuracy)}</div>
        <div class="metric__sub">vs ${pct(b.always_home_accuracy)} always backing the home side
          (<span class="${lift > 0 ? "delta-pos" : "delta-neg"}">${lift > 0 ? "+" : ""}${pct(lift)}</span>)</div>
        <div class="meter"><div class="meter__fill ${lift > 0 ? "meter__fill--pos" : "meter__fill--neg"}"
             style="--fill:${(a.model_accuracy ?? 0).toFixed(4)}"></div></div>
      </div>
      <div class="metric">
        <div class="metric__label">Win-probability Brier</div>
        <div class="metric__value">${(w.blended?.brier ?? 0).toFixed(4)}</div>
        <div class="metric__sub">Elo alone ${(w.elo_only?.brier ?? 0).toFixed(4)}</div>
      </div>
      <div class="metric">
        <div class="metric__label">Margin error</div>
        <div class="metric__value">${num(a.margin_mae, 1)}<small> pts</small></div>
        <div class="metric__sub">vs ${num(b.mean_margin_mae, 1)} predicting the average
          (<span class="${marginLift > 0 ? "delta-pos" : "delta-neg"}">${marginLift > 0 ? "−" : "+"}${num(Math.abs(marginLift), 1)}</span>)</div>
      </div>
      <div class="metric">
        <div class="metric__label">Total points error</div>
        <div class="metric__value">${num(a.total_mae, 1)}<small> pts</small></div>
        <div class="metric__sub">vs ${num(b.mean_total_mae, 1)} predicting the average</div>
      </div>
    </div>
    <div class="table-wrap"><table>
      <thead><tr>
        <th scope="col">Variant</th><th scope="col" class="num">Brier</th>
        <th scope="col" class="num">Log-loss</th><th scope="col">What it is</th>
      </tr></thead>
      <tbody>
        <tr>${tdLead("Elo only")}${td("Brier", (w.elo_only?.brier ?? 0).toFixed(4), "num")}
            ${td("Log-loss", (w.elo_only?.log_loss ?? 0).toFixed(4), "num")}
            ${td("What it is", "Rating difference alone, no form or venue")}</tr>
        <tr>${tdLead("Margin model")}${td("Brier", (w.margin_model?.brier ?? 0).toFixed(4), "num")}
            ${td("Log-loss", (w.margin_model?.log_loss ?? 0).toFixed(4), "num")}
            ${td("What it is", "Gradient-boosted margin, converted to a win probability")}</tr>
        <tr>${tdLead("Blended (shipped)")}${td("Brier", (w.blended?.brier ?? 0).toFixed(4), "num")}
            ${td("Log-loss", (w.blended?.log_loss ?? 0).toFixed(4), "num")}
            ${td("What it is", "65% margin model, 35% Elo — what the board serves")}</tr>
      </tbody>
      <caption>Held out on seasons ${(a.test_seasons || []).join(", ")}
        (${(a.n_test_holdout || 0).toLocaleString()} matches, ${a.n_draws_in_holdout || 0} drawn and
        excluded from win-probability scoring, since a two-way market refunds them). Trained on
        ${(a.n_train || 0).toLocaleString()} matches from ${(a.train_seasons || [])[0]} onward.
        Over/under ${a.total_line} accuracy was ${pct(a.total_over_under?.accuracy)} against an
        actual over-rate of ${pct(a.total_over_under?.actual_over_rate)}.</caption>
    </table></div>`;
}

/**
 * Flat-stake value-betting simulation. This is the number that actually
 * decides whether the model is worth acting on, so it is reported whole:
 * every bet the rule would have placed, at the real historical price,
 * including the competitions where it lost money.
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
        <div class="metric__label">Competitions in profit</div>
        <div class="metric__value">${profitable}<small> of ${rows.length}</small></div>
        <div class="meter"><div class="meter__fill ${profitable * 2 >= rows.length ? "meter__fill--pos" : "meter__fill--neg"}"
             style="--fill:${(profitable / rows.length).toFixed(4)}"></div></div>
      </div>
    </div>
    <div class="table-wrap"><table>
      <thead><tr>
        <th scope="col">Competition</th><th scope="col" class="num">Bets</th>
        <th scope="col" class="num">Staked</th><th scope="col" class="num">Returned</th>
        <th scope="col" class="num">ROI</th><th scope="col">Loss ← → profit</th>
        <th scope="col" class="num">Strike rate</th>
      </tr></thead>
      <tbody>${rows.map((r) => `<tr>
        ${tdLead(esc(r.league))}
        ${td("Bets", r.bets, "num")}
        ${td("Staked", r.staked.toFixed(0), "num")}
        ${td("Returned", r.returned.toFixed(2), "num")}
        ${td("ROI", `<span class="${r.roi > 0 ? "delta-pos" : "delta-neg"}">${r.roi > 0 ? "+" : ""}${(r.roi * 100).toFixed(1)}%</span>`, "num")}
        ${td("Loss ← → profit", `<div class="divbar"><i class="${r.roi > 0 ? "pos" : "neg"}" style="width:${(Math.abs(r.roi) / scale * 48).toFixed(1)}%"></i></div>`)}
        ${td("Strike rate", pct(r.winRate), "num")}
      </tr>`).join("")}</tbody>
      <caption>Backs every outcome where the model's probability exceeded the bookmaker's implied
        probability by the training script's threshold, one unit per bet, settled at the real
        historical price. No staking plan, no filtering by competition, no removal of losing runs.</caption>
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
  initNav();
  syncRails();
  matchMedia("(min-width: 1081px)").addEventListener("change", syncRails);
  for (const rail of $$(".rail")) {
    rail.addEventListener("toggle", () => { rail.dataset.touched = "true"; });
  }

  addEventListener("hashchange", route);
  route();

  /* --- board --- */
  $("#board-refresh").addEventListener("click", () => loadBoard(true));
  // The select covers the two orderings that are not plain columns; both it
  // and the column headers write the same (sort, dir) state.
  $("#board-sort").addEventListener("change", (e) => {
    const v = e.target.value;
    if (v === "confidence") { board.sort = "prob"; board.dir = "desc"; }
    else if (v === "closest") { board.sort = "prob"; board.dir = "asc"; }
    else { board.sort = "time"; board.dir = "asc"; }
    renderBoard();
  });
  // 60ms, not 160: filtering is a pure client-side re-render over data that
  // is already in memory, so the debounce only needs to coalesce keystrokes,
  // not protect a request. At 160ms it read as lag.
  $("#board-search").addEventListener("input", debounce((e) => {
    board.query = e.target.value;
    renderBoard();
  }, 60));
  pressGroup($("#board-sport"), (s) => { board.sport = s; renderBoard(); });

  $("#board-leagues").addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-league]");
    if (!btn) return;
    const key = btn.dataset.league;
    if (board.leagues.has(key)) board.leagues.delete(key); else board.leagues.add(key);
    btn.setAttribute("aria-pressed", String(board.leagues.has(key)));
    renderBoard();
  });

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
    // Sorting a column the board is already sorted by flips the direction;
    // a new column starts in its natural direction — chronological for
    // kick-off and alphabetical for text, but strongest-first for a
    // probability, because nobody opens a board looking for its worst call.
    const sortBtn = ev.target.closest(".th-sort[data-sort]");
    if (sortBtn) {
      const key = sortBtn.dataset.sort;
      if (board.sort === key) board.dir = board.dir === "asc" ? "desc" : "asc";
      else { board.sort = key; board.dir = key === "prob" ? "desc" : "asc"; }
      $("#board-sort").value =
        board.sort === "time" && board.dir === "asc" ? "time"
          : board.sort === "prob" && board.dir === "desc" ? "confidence"
          : board.sort === "prob" ? "closest"
          : "column";
      renderBoard();
      // Sorting replaced the whole table, so focus has nowhere to return to
      // unless it is put back on the header that was just activated.
      $(`.th-sort[data-sort="${key}"]`)?.focus();
      return;
    }

    const toggle = ev.target.closest(".disclose[aria-controls]");
    if (!toggle) return;
    const panel = document.getElementById(toggle.getAttribute("aria-controls"));
    const open = toggle.getAttribute("aria-expanded") !== "true";
    toggle.setAttribute("aria-expanded", String(open));
    panel.hidden = !open;
    toggle.closest("tr")?.classList.toggle("is-open", open);
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
  $("#a-run").addEventListener("click", runAFL);
  $("#s-fetch-odds").addEventListener("click", () => fetchRealOdds("soccer"));
  $("#a-fetch-odds").addEventListener("click", () => fetchRealOdds("afl"));
  for (const id of ["#s-home", "#s-away", "#s-oh", "#s-od", "#s-oa"]) {
    $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") runSoccer(); });
  }
  for (const id of ["#a-home", "#a-away", "#a-venue", "#a-oh", "#a-oa"]) {
    $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") runAFL(); });
  }

  /* --- performance panes --- */
  $("#perf-tabs").addEventListener("click", (ev) => {
    const tab = ev.target.closest("[role=tab]");
    if (tab) selectPerfPane(tab.id.replace("tab-", ""));
  });

  /* --- accumulator (now a Board tool) --- */
  $("#acca-build").addEventListener("click", buildAccumulator);

  /* --- track record --- */
  const refilter = () => { record.limit = pageSize(); renderRecord(); };
  // "input" as well as "change": a date field fires change only once the
  // value is committed, which on a mobile date picker means after the sheet
  // is dismissed — the filter looked like it had been ignored until then.
  for (const id of ["#log-from", "#log-to", "#log-league"]) {
    $(id).addEventListener("change", refilter);
    $(id).addEventListener("input", refilter);
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
    const sortBtn = ev.target.closest(".th-sort[data-rsort]");
    if (sortBtn) {
      const key = sortBtn.dataset.rsort;
      if (record.sort === key) {
        // Third click returns to the default settled-first order rather than
        // trapping the reader in a column they only wanted to glance at.
        if (record.dir === "desc") { record.sort = null; record.dir = "asc"; }
        else record.dir = "desc";
      } else {
        record.sort = key;
        // Outcome columns lead with the wins; text columns read A-Z.
        record.dir = ["result", "secondary", "totals", "status", "date"].includes(key)
          ? "desc" : "asc";
      }
      renderRecord();
      $(`.th-sort[data-rsort="${key}"]`)?.focus();
      return;
    }
    if (!ev.target.closest("#log-more")) return;
    record.limit += pageSize();
    renderRecord();
    $("#log-more")?.focus();
  });

  /* --- keyboard --- */
  addEventListener("keydown", (e) => {
    const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName || "");
    if (e.key === "/" && !typing && !$("#view-board").hidden) {
      e.preventDefault();
      $("#board-filters").open = true;
      $("#board-search").focus();
    }
    if (e.key === "Escape" && document.activeElement === $("#board-search")) {
      $("#board-search").value = "";
      board.query = "";
      renderBoard();
    }
  });

  loadBoard(false);
}

init();
