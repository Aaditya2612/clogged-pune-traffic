const DAY_NAMES = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"];
const COLORS = {0:"#2ecc71",1:"#f1c40f",2:"#e67e22",3:"#c0392b"};
const LABELS = {0:"Fast moving",1:"Moderate",2:"Slow",3:"Gridlocked"};
const SHORT = {0:"Fast",1:"Mod",2:"Slow",3:"Gridlock"};
const PUNE_BOUNDS = [[18.42, 73.77], [18.62, 73.94]];

const HW_LABELS = {
  motorway:"Motorway", trunk:"Trunk road", primary:"Main road", secondary:"Major road",
  tertiary:"Road", residential:"Residential street", living_street:"Living street",
  service:"Service road", unclassified:"Road", road:"Road", track:"Track"
};

let mode = "forecast";
let hour = 8;
let dateStr = todayStr();
let meta = null;
let playTimer = null;
let selectedArea = "All Pune";
let bnFilter = "all";
let bnArea = "";
let bnAreaCounts = [];

// per-segment state
let lines = [];
let geoms = [];
let names = [];
let highways = [];
let arterials = [];
let freeflows = [];
let values = { buckets: [], speeds: [], congestions: [] };
let areasData = [];
let searchQuery = "";
let persistentArea = "";
let persistentData = null;
let detailId = -1;

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// line weight scales with zoom: thin when zoomed out (so colours don't merge
// into fat ugly bands), close to normal when zoomed in for precise reading.
const WEIGHT_BASE = { arterial: 3.0, normal: 1.8 };
function zoomScale(z) {
  if (z <= 11) return 0.42;
  if (z >= 15) return 1.0;
  return 0.42 + (z - 11) / 4 * 0.58;
}
function weightFor(arterial, z) {
  return (arterial ? WEIGHT_BASE.arterial : WEIGHT_BASE.normal) * zoomScale(z);
}

// ---- road hover / click picking ------------------------------------------
// Roads are drawn thin (see WEIGHT_BASE), so letting Leaflet hit-test the
// strokes makes them fiddly to hover. Instead we hit-test the cursor against
// the geometry with a generous radius and drive one shared tooltip from that.
const HIT_PX = 11;   // grab radius around the cursor, in pixels
const CELL = 0.01;   // spatial-index cell size, ~1.1 km in degrees
let segBB = [];      // [minLat, maxLat, minLng, maxLng] per segment
let segGrid = new Map();

function buildHitIndex() {
  segBB = new Array(geoms.length);
  segGrid = new Map();
  for (let i = 0; i < geoms.length; i++) {
    const g = geoms[i];
    let minLat = Infinity, maxLat = -Infinity, minLng = Infinity, maxLng = -Infinity;
    for (let k = 0; k < g.length; k++) {
      const p = g[k];
      if (p[0] < minLat) minLat = p[0];
      if (p[0] > maxLat) maxLat = p[0];
      if (p[1] < minLng) minLng = p[1];
      if (p[1] > maxLng) maxLng = p[1];
    }
    segBB[i] = [minLat, maxLat, minLng, maxLng];
    for (let r = Math.floor(minLat / CELL); r <= Math.floor(maxLat / CELL); r++) {
      for (let c = Math.floor(minLng / CELL); c <= Math.floor(maxLng / CELL); c++) {
        const key = r + ":" + c;
        let cell = segGrid.get(key);
        if (!cell) segGrid.set(key, cell = []);
        cell.push(i);
      }
    }
  }
}

// distance from the cursor to segment i, in units where 1.0 == HIT_PX
function segDistance(i, lat, lng, thrLat, thrLng) {
  const g = geoms[i];
  let best = Infinity;
  for (let k = 0; k < g.length - 1; k++) {
    const ax = (g[k][1] - lng) / thrLng, ay = (g[k][0] - lat) / thrLat;
    const bx = (g[k + 1][1] - lng) / thrLng, by = (g[k + 1][0] - lat) / thrLat;
    const dx = bx - ax, dy = by - ay;
    const len2 = dx * dx + dy * dy;
    let t = len2 > 0 ? -(ax * dx + ay * dy) / len2 : 0;
    if (t < 0) t = 0; else if (t > 1) t = 1;
    const qx = ax + t * dx, qy = ay + t * dy;
    const d2 = qx * qx + qy * qy;
    if (d2 < best) best = d2;
  }
  return Math.sqrt(best);
}

// nearest segment within HIT_PX pixels of (lat, lng), or -1
function pickSegment(lat, lng) {
  const z = map.getZoom();
  const p0 = map.project([lat, lng], z);
  const dLatPx = Math.abs(map.project([lat + 0.01, lng], z).y - p0.y);
  const dLngPx = Math.abs(map.project([lat, lng + 0.01], z).x - p0.x);
  if (dLatPx <= 0 || dLngPx <= 0) return -1;
  const thrLat = HIT_PX * 0.01 / dLatPx;   // degrees spanning HIT_PX
  const thrLng = HIT_PX * 0.01 / dLngPx;
  const r0 = Math.floor((lat - thrLat) / CELL), r1 = Math.floor((lat + thrLat) / CELL);
  const c0 = Math.floor((lng - thrLng) / CELL), c1 = Math.floor((lng + thrLng) / CELL);
  let best = -1, bestD = 1;
  for (let r = r0; r <= r1; r++) {
    for (let c = c0; c <= c1; c++) {
      const cell = segGrid.get(r + ":" + c);
      if (!cell) continue;
      for (let k = 0; k < cell.length; k++) {
        const i = cell[k];
        const bb = segBB[i];
        if (bb[0] > lat + thrLat || bb[1] < lat - thrLat ||
            bb[2] > lng + thrLng || bb[3] < lng - thrLng) continue;
        const d = segDistance(i, lat, lng, thrLat, thrLng);
        if (d < bestD) { bestD = d; best = i; }
      }
    }
  }
  return bestD < 1 ? best : -1;
}

let hoverId = -1;
let hoverTip = null;

function initHover() {
  buildHitIndex();
  hoverTip = L.tooltip({ sticky: true, direction: "top", opacity: 1 }).setContent("");
  let raf = 0, pending = null;
  const flush = () => {
    raf = 0;
    const ll = pending;
    pending = null;
    if (ll) onHoverMove(ll);
  };
  map.on("mousemove", (e) => {
    pending = e.latlng;
    if (!raf) raf = requestAnimationFrame(flush);
  });
  map.on("click", (e) => {
    const id = pickSegment(e.latlng.lat, e.latlng.lng);
    if (id >= 0) showDetail(id);
  });
  map.getContainer().addEventListener("mouseleave", hideHoverTip);
}

function onHoverMove(ll) {
  const id = pickSegment(ll.lat, ll.lng);
  if (id < 0) { hideHoverTip(); return; }
  if (id !== hoverId) {
    hoverId = id;
    hoverTip.setContent(tooltipHTML(id));
    map.getContainer().style.cursor = "pointer";
  }
  hoverTip.setLatLng(ll).openOn(map);
}

function hideHoverTip() {
  if (hoverId === -1) return;
  hoverId = -1;
  hoverTip.close();
  map.getContainer().style.cursor = "";
}

const map = L.map("map").setView([18.52, 73.855], 12.2);
map.setMaxBounds(PUNE_BOUNDS);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, attribution: "&copy; OpenStreetMap contributors"
}).addTo(map);

const segLayer = L.layerGroup().addTo(map);
// blink overlay: its own canvas renderer so pulsing a picked road doesn't make
// the 28k-segment base canvas redraw every animation frame.
const blinkRenderer = L.canvas({ padding: 0.5 });
const blinkLayer = L.layerGroup().addTo(map);
let blinkRaf = 0;

const modeSwitch = document.getElementById("modeSwitch");
const msKnob = document.getElementById("msKnob");
const dateInput = document.getElementById("dateInput");
const summaryDate = document.getElementById("summaryDate");
const summaryDist = document.getElementById("summaryDist");
const clockSvg = document.getElementById("clockSvg");
const clockHand = document.getElementById("clockHand");
const clockLabel = document.getElementById("clockLabel");

function localDateStr(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${dd}`;
}
function todayStr() { return localDateStr(new Date()); }
function hhmm(h) { return String(h).padStart(2, "0") + ":00"; }
function fmtDate(s) {
  const [y,m,d] = s.split("-").map(Number);
  return new Date(y, m-1, d).toDateString();
}
function roadName(i) {
  return names[i] || (HW_LABELS[highways[i]] || "Road");
}

// ---- init ----
async function init() {
  meta = await (await fetch("/api/meta")).json();
  setDateLimits();
  applyNow();
  dateInput.value = dateStr;

  const geo = await (await fetch("/api/segments")).json();
  const renderer = L.canvas({ padding: 0.5 });

  geo.features.forEach((f, i) => {
    const p = f.properties;
    geoms[i] = f.geometry.coordinates.map(c => [c[1], c[0]]);
    names[i] = p.name;
    highways[i] = p.highway;
    arterials[i] = p.arterial;
    freeflows[i] = p.freeflow;
    const line = L.polyline(geoms[i], {
      color: "#555", weight: weightFor(p.arterial, map.getZoom()),
      opacity: 0.8, lineCap: "round", lineJoin: "round", renderer,
      interactive: false   // hover/click handled by the fat-radius picker below
    });
    lines[i] = line;
    segLayer.addLayer(line);
  });

  // keep line weight in step with zoom (updates only when the scale step changes)
  let lastScale = zoomScale(map.getZoom());
  map.on("zoomend", () => {
    const z = map.getZoom();
    const s = zoomScale(z);
    if (Math.abs(s - lastScale) < 0.001) return;
    lastScale = s;
    for (let i = 0; i < lines.length; i++) {
      lines[i].setStyle({ weight: weightFor(arterials[i], z) });
    }
  });

  buildClock();
  updateClock();
  renderModeUI();
  areasData = (await (await fetch("/api/areas")).json()).areas;
  await refreshValues();
  renderPanel();
  initHover();
}

function tooltipHTML(i) {
  const b = values.buckets[i];
  return `<b>${roadName(i)}</b><br>` +
    `<span style="color:${COLORS[b]}">${LABELS[b]}</span> · ${values.speeds[i]} km/h<br>` +
    `Congestion ${values.congestions[i]}% · free-flow ${freeflows[i]} km/h`;
}

async function refreshValues() {
  const res = await fetch(`/api/values?date=${dateStr}&hour=${hour}&mode=${mode}`);
  const v = await res.json();
  values = v;
  for (let i = 0; i < lines.length; i++) lines[i].setStyle({ color: COLORS[v.buckets[i]] });
  updateBadge(v);
}

function updateBadge(v) {
  const n = v.buckets.length || 1;
  const counts = [0,0,0,0];
  v.buckets.forEach(b => counts[b]++);
  summaryDate.textContent = `${fmtDate(v.date)} · ${v.day_name} ${hhmm(v.hour)}`;
  summaryDist.innerHTML = [0,1,2,3].map(i =>
    `<span class="dist-chip"><span class="dot" style="background:${COLORS[i]}"></span>${SHORT[i]} ${(counts[i]/n*100).toFixed(0)}%</span>`
  ).join("");
}

// ---- clock ----
function buildClock() {
  const ticks = document.getElementById("clockTicks");
  let html = "";
  for (let h = 0; h < 24; h++) {
    const rad = h * 15 * Math.PI / 180;
    const major = h % 6 === 0;
    const r1 = major ? 46 : 50, r2 = 56;
    const x1 = 60 + r1 * Math.sin(rad), y1 = 60 - r1 * Math.cos(rad);
    const x2 = 60 + r2 * Math.sin(rad), y2 = 60 - r2 * Math.cos(rad);
    html += `<line x1="${x1.toFixed(2)}" y1="${y1.toFixed(2)}" x2="${x2.toFixed(2)}" y2="${y2.toFixed(2)}" class="tick${major ? " major" : ""}"/>`;
  }
  [[0,"00"],[6,"06"],[12,"12"],[18,"18"]].forEach(([h, txt]) => {
    const rad = h * 15 * Math.PI / 180, r = 37;
    const x = 60 + r * Math.sin(rad), y = 60 - r * Math.cos(rad) + 3;
    html += `<text x="${x.toFixed(1)}" y="${y.toFixed(1)}" class="clock-num" text-anchor="middle">${txt}</text>`;
  });
  ticks.innerHTML = html;
}

function updateClock() {
  const rad = hour * 15 * Math.PI / 180;
  clockHand.setAttribute("x2", (60 + 34 * Math.sin(rad)).toFixed(2));
  clockHand.setAttribute("y2", (60 - 34 * Math.cos(rad)).toFixed(2));
  clockLabel.textContent = hhmm(hour);
}

function clampHour(h) {
  h = ((h % 24) + 24) % 24;
  if (mode === "history" && dateStr === meta.today && h > meta.now_hour) h = meta.now_hour;
  return h;
}

let dragging = false;
clockSvg.addEventListener("pointerdown", e => {
  dragging = true; clockSvg.setPointerCapture(e.pointerId); setHourFromPointer(e);
});
clockSvg.addEventListener("pointermove", e => { if (dragging) setHourFromPointer(e); });
clockSvg.addEventListener("pointerup", () => { dragging = false; commitHour(); });
clockSvg.addEventListener("pointercancel", () => { dragging = false; commitHour(); });

function setHourFromPointer(e) {
  const rect = clockSvg.getBoundingClientRect();
  const cx = rect.left + rect.width / 2, cy = rect.top + rect.height / 2;
  const dx = e.clientX - cx, dy = e.clientY - cy;
  let ang = Math.atan2(dx, -dy) * 180 / Math.PI;
  if (ang < 0) ang += 360;
  hour = clampHour(Math.round(ang / 15) % 24);
  updateClock();
}

function commitHour() {
  refresh();
}

// ---- tabs / panel ----
const tabs = document.querySelectorAll(".tab");
const panel = document.getElementById("panel");
// one delegated listener for every clickable result row (bottlenecks, persistent)
// so lists of thousands of segments don't each need their own handler.
panel.addEventListener("click", (e) => {
  const el = e.target.closest(".item");
  if (el && el.dataset.id) focusSegment(Number(el.dataset.id));
});
let activeTab = "peaks";
tabs.forEach(t => t.addEventListener("click", () => {
  tabs.forEach(x => x.classList.remove("active"));
  t.classList.add("active");
  activeTab = t.dataset.tab;
  renderPanel();
}));

function renderPanel() {
  if (activeTab === "peaks") return renderPeaks();
  if (activeTab === "bottlenecks") return renderBottlenecks();
  if (activeTab === "persistent") return renderPersistent();
  if (activeTab === "forecast") return renderForecast();
  if (activeTab === "search") return renderSearch();
}

function svgChart(hours, W = 292, H = 96) {
  const n = hours.length;
  const padL = 26, padR = 4, padT = 8, padB = 18;
  const cw = W - padL - padR, ch = H - padT - padB;
  const vals = hours.map(h => (h.congestion == null ? 0 : h.congestion));
  let mx = Math.max(...vals, 10);
  mx = Math.ceil(mx / 20) * 20;
  const bw = cw / n;
  let rects = "";
  hours.forEach((h, i) => {
    const v = h.congestion == null ? 0 : h.congestion;
    const bh = Math.max(v / mx * ch, 0.6);
    const x = padL + i * bw;
    const y = padT + (ch - bh);
    rects += `<rect class="bar" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${Math.max(bw - 0.6, 0.5).toFixed(2)}" height="${bh.toFixed(2)}" fill="${COLORS[h.bucket]}"><title>${hhmm(h.hour)} · ${h.congestion == null ? "no data" : h.congestion + "%"}</title></rect>`;
  });
  const xTicks = [[0, "start"], [6, "middle"], [12, "middle"], [18, "middle"], [23, "end"]];
  const labels = xTicks.map(([t, anchor]) => {
    const x = padL + (t + 0.5) * bw;
    return `<text x="${x.toFixed(1)}" y="${H - 5}" class="axis" text-anchor="${anchor}">${hhmm(t)}</text>`;
  }).join("");
  const grid = `<line x1="${padL}" y1="${padT + ch}" x2="${W - padR}" y2="${padT + ch}" class="axis-line"/>`;
  const ymax = `<text x="${padL - 4}" y="${padT + 7}" class="axis" text-anchor="end">${mx}%</text>`;
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">${ymax}${grid}${rects}${labels}</svg>`;
}

async function renderPeaks() {
  const r = await (await fetch("/api/peaks")).json();
  const b = await (await fetch("/api/benchmark")).json();
  let html = `<div class="card"><h3>Forecast accuracy (backtest)</h3>
    <div class="kv"><span class="k">Correlation (r)</span><span class="v">${b.correlation ?? "—"}</span></div>
    <div class="kv"><span class="k">Mean abs. error</span><span class="v">${b.mae_pct ?? "—"}%</span></div>
    <div class="sub">History vs forecast over the past ${b.days} days</div>
  </div>`;
  html += `<div class="card"><h3>Typical peak &amp; off-peak</h3>
    <div class="kv"><span class="k">Peak</span><span class="v">${r.peak.day_name} ${hhmm(r.peak.hour)} · ${r.peak.congestion}%</span></div>
    <div class="kv"><span class="k">Quietest</span><span class="v">${r.offpeak.day_name} ${hhmm(r.offpeak.hour)} · ${r.offpeak.congestion}%</span></div>
  </div>`;
  html += `<div class="card"><h3>Weekday congestion curve</h3>${svgChart(r.weekday)}</div>`;
  html += `<div class="card"><h3>Weekend congestion curve</h3>${svgChart(r.weekend)}</div>`;
  panel.innerHTML = html;
}

async function renderBottlenecks() {
  const r = await (await fetch(`/api/bottlenecks?date=${dateStr}&hour=${hour}&mode=${mode}&filter=${bnFilter}&area=${encodeURIComponent(bnArea)}&top=0`)).json();
  bnAreaCounts = r.area_counts || [];
  const modeLabel = mode === "forecast" ? "Forecast" : "Historical";
  let html = `<div class="card"><h3>Bottlenecks</h3>
    <div class="kv"><span class="k">${fmtDate(r.date)} · ${r.day_name} ${hhmm(r.hour)}</span><span class="v">${modeLabel}</span></div>
    <select id="bnFilter">
      <option value="all"${bnFilter === "all" ? " selected" : ""}>Slow &amp; gridlocked</option>
      <option value="slow"${bnFilter === "slow" ? " selected" : ""}>Slow only</option>
      <option value="gridlocked"${bnFilter === "gridlocked" ? " selected" : ""}>Gridlocked only</option>
    </select>
    <input id="bnAreaInput" class="search-input" type="text" autocomplete="off"
           placeholder="Filter by area…" />
    <div id="bnAreaChips" class="chip-row"></div>
  </div>`;
  if (!r.segments.length) {
    html += `<div class="empty">No bottlenecks here at this time — traffic is flowing freely.</div>`;
  } else {
    const total = r.count;
    const shown = r.segments.length;
    html += `<div class="card sub"><div class="sub">${total} congested segment${total === 1 ? "" : "s"}${bnArea ? " in " + esc(bnArea) : ""}${total > shown ? " (top " + shown + " listed)" : ""}</div>`;
    r.segments.forEach(s => {
      html += `<div class="item" style="border-color:${COLORS[s.bucket]}" data-id="${s.id}">
        <div class="txt"><div class="nm">${esc(s.name || "Unnamed")}</div>
        <div class="sub">${esc(s.highway)} · ${s.label} · ${esc(s.area)}</div></div>
        <div class="val">${s.congestion}%</div>
      </div>`;
    });
    html += `</div>`;
  }
  panel.innerHTML = html;
  const sel = document.getElementById("bnFilter");
  if (sel) sel.addEventListener("change", () => { bnFilter = sel.value; renderBottlenecks(); });
  const areaInput = document.getElementById("bnAreaInput");
  if (areaInput) areaInput.addEventListener("input", () => renderBnAreaChips(areaInput.value));
  renderBnAreaChips("");
}

function renderBnAreaChips(query) {
  const box = document.getElementById("bnAreaChips");
  if (!box) return;
  const q = (query || "").trim().toLowerCase();
  const counts = {};
  bnAreaCounts.forEach(a => { counts[a.name] = a.count; });
  const total = bnAreaCounts.reduce((s, a) => s + a.count, 0);
  const names = areasData.map(a => a.name).filter(n => !q || n.toLowerCase().includes(q));

  let html = `<span class="chip${bnArea === "" ? " active" : ""}" data-area="">All Pune ` +
    `<span class="cnt">${total}</span></span>`;
  names.forEach(n => {
    html += `<span class="chip${bnArea === n ? " active" : ""}" data-area="${esc(n)}">` +
      `${esc(n)} <span class="cnt">${counts[n] || 0}</span></span>`;
  });
  if (!names.length) html += `<span class="sub">No matching areas</span>`;
  box.innerHTML = html;

  box.querySelectorAll(".chip").forEach(el => el.addEventListener("click", () => {
    bnArea = el.dataset.area;
    renderBottlenecks();
  }));
}

async function renderPersistent() {
  if (!persistentData) {
    persistentData = await (await fetch("/api/persistent?min_avg=55&top=300")).json();
  }
  panel.innerHTML =
    `<div class="card"><h3>Persistently congested</h3>
      <div class="sub">Long-run average over a typical week · &ge; ${persistentData.min_avg}% congestion</div>
      <input id="pAreaInput" class="search-input" type="text" autocomplete="off"
             placeholder="Filter by area…" />
      <div id="pAreaChips" class="chip-row"></div>
    </div><div id="pList"></div>`;
  const input = document.getElementById("pAreaInput");
  input.addEventListener("input", () => renderPersistentChips(input.value));
  renderPersistentChips("");
  renderPersistentList();
}

function renderPersistentChips(query) {
  const box = document.getElementById("pAreaChips");
  if (!box) return;
  const q = query.trim().toLowerCase();
  const rows = persistentData.segments;
  const counts = {};
  rows.forEach(s => { counts[s.area] = (counts[s.area] || 0) + 1; });
  const names = areasData.map(a => a.name).filter(n => !q || n.toLowerCase().includes(q));

  let html = `<span class="chip${persistentArea === "" ? " active" : ""}" data-area="">All Pune ` +
    `<span class="cnt">${rows.length}</span></span>`;
  names.forEach(n => {
    html += `<span class="chip${persistentArea === n ? " active" : ""}" data-area="${esc(n)}">` +
      `${esc(n)} <span class="cnt">${counts[n] || 0}</span></span>`;
  });
  if (!names.length) html += `<span class="sub">No matching areas</span>`;
  box.innerHTML = html;

  box.querySelectorAll(".chip").forEach(el => el.addEventListener("click", () => {
    persistentArea = el.dataset.area;
    const inp = document.getElementById("pAreaInput");
    if (inp) inp.value = "";
    renderPersistentChips("");
    renderPersistentList();
  }));
}

function renderPersistentList() {
  const box = document.getElementById("pList");
  if (!box) return;
  const rows = persistentData.segments.filter(s => !persistentArea || s.area === persistentArea);
  const where = persistentArea || "All Pune";
  let html = `<div class="card"><h3>${esc(where)} <span class="cnt-pill">${rows.length}</span></h3>`;
  if (!rows.length) {
    html += `<div class="sub">No persistently congested roads here.</div>`;
  } else {
    rows.forEach(s => {
      html += `<div class="item" data-id="${s.id}">
        <div class="txt"><div class="nm">${esc(s.name || "Unnamed")} ${s.flagged_persistent ? "★" : ""}</div>
        <div class="sub">${esc(s.highway)}${s.bottleneck ? " · bottleneck" : ""} · ${esc(s.area)}</div></div>
        <div class="val">${s.avg_congestion}%</div>
      </div>`;
    });
  }
  html += `</div>`;
  box.innerHTML = html;
}

async function renderForecast() {
  const areas = await (await fetch("/api/areas")).json();
  const r = await (await fetch(`/api/area/${encodeURIComponent(selectedArea)}?date=${dateStr}&hour=${hour}&mode=${mode}&hours=24`)).json();
  let html = `<div class="card"><h3>Locality forecast</h3>
    <select id="areaSelect"></select>
    <div class="sub">${r.name} · ${fmtDate(r.date)} (${r.day_name})</div>
    <div class="chart">${svgChart(r.day)}</div>`;

  if (r.current && r.current.distribution) {
    const d = r.current.distribution;
    html += `<div class="dist-row">${[0,1,2,3].map(i =>
      `<span class="dist-chip"><span class="dot" style="background:${COLORS[i]}"></span>${SHORT[i]} ${d[LABELS[i]]}%</span>`
    ).join("")}</div>`;
  }

  html += `</div><div class="card"><h3>Next 24 hours</h3>`;
  r.forecast.forEach(p => {
    html += `<div class="kv"><span class="k">+${p.offset_h}h · ${p.day_name} ${hhmm(p.hour)}</span><span class="v" style="color:${COLORS[p.bucket]}">${p.label}${p.congestion != null ? " (" + p.congestion + "%)" : ""}</span></div>`;
  });
  html += `</div>`;
  panel.innerHTML = html;

  const sel = document.getElementById("areaSelect");
  const opts = ["All Pune"].concat(areas.areas.map(a => a.name));
  sel.innerHTML = opts.map(o => `<option${o === selectedArea ? " selected" : ""}>${o}</option>`).join("");
  sel.addEventListener("change", () => {
    selectedArea = sel.value;
    flyToArea(selectedArea, areas.areas);
    renderForecast();
  });
}

function renderSearch() {
  if (!document.getElementById("searchInput")) {
    panel.innerHTML =
      `<div class="card"><h3>Search</h3>
        <input id="searchInput" class="search-input" type="text" autocomplete="off"
               placeholder="Search an area or road…" />
        <div class="sub">Find a Pune locality or a road by name</div>
      </div>
      <div id="searchResults"></div>`;
    const inp = document.getElementById("searchInput");
    inp.value = searchQuery;
    inp.addEventListener("input", () => { searchQuery = inp.value; runSearch(); });
    inp.focus();
  }
  runSearch();
}

async function runSearch() {
  const res = document.getElementById("searchResults");
  if (!res) return;
  const q = searchQuery.trim();
  if (!q) {
    res.innerHTML = `<div class="card"><div class="sub">Type to search Pune areas and roads.</div></div>`;
    return;
  }
  const r = await (await fetch(`/api/search?q=${encodeURIComponent(q)}`)).json();
  let html = "";
  if (r.areas.length) {
    html += `<div class="card"><h3>Areas</h3>`;
    r.areas.forEach(a => {
      html += `<div class="item area-item" data-name="${esc(a.name)}">
        <div class="txt"><div class="nm">${esc(a.name)}</div>
        <div class="sub">locality · ${a.count} segments</div></div>
        <div class="val">&#8599;</div></div>`;
    });
    html += `</div>`;
  }
  if (r.roads.length) {
    html += `<div class="card"><h3>Roads</h3>`;
    r.roads.forEach(rd => {
      html += `<div class="item road-item" data-name="${esc(rd.name)}">
        <div class="txt"><div class="nm">${esc(rd.name)}</div>
        <div class="sub">${esc(rd.highway)} · ${rd.count} segment${rd.count > 1 ? "s" : ""}</div></div>
        <div class="val">&#8599;</div></div>`;
    });
    html += `</div>`;
  }
  if (!r.areas.length && !r.roads.length) {
    html = `<div class="card"><div class="sub">No matches for &ldquo;${esc(q)}&rdquo;.</div></div>`;
  }
  res.innerHTML = html;
  res.querySelectorAll(".area-item").forEach(el =>
    el.addEventListener("click", () => selectAreaResult(el.dataset.name)));
  res.querySelectorAll(".road-item").forEach(el =>
    el.addEventListener("click", () => selectRoadResult(el.dataset.name)));
}

function selectAreaResult(name) {
  flyToArea(name, areasData);
}

function selectRoadResult(name) {
  const idxs = [];
  for (let i = 0; i < names.length; i++) if (names[i] === name) idxs.push(i);
  if (!idxs.length) return;
  const bounds = L.latLngBounds([]);
  idxs.forEach(i => geoms[i].forEach(c => bounds.extend(c)));
  if (bounds.isValid()) map.fitBounds(bounds, { padding: [40, 40], maxZoom: 16 });
  blinkAfterMove(idxs, 4);
  showDetail(idxs[0]);
}

function flyToArea(name, areaList) {
  if (name === "All Pune") { map.fitBounds(PUNE_BOUNDS); return; }
  const a = areaList.find(x => x.name === name);
  if (a && a.bounds) {
    map.fitBounds([[a.bounds.min_lat, a.bounds.min_lon], [a.bounds.max_lat, a.bounds.max_lon]],
      { padding: [30, 30], maxZoom: 14 });
  }
}

// ---- transient "blink" highlight -----------------------------------------
// When we fly to a road we pulse it a few times in its own congestion colour
// instead of leaving a persistent overlay, so afterwards the user can still
// read the road's real bucket colour.
function cancelBlink() {
  if (blinkRaf) { cancelAnimationFrame(blinkRaf); blinkRaf = 0; }
  blinkLayer.clearLayers();
}

function blinkSegments(ids, opts) {
  opts = opts || {};
  cancelBlink();
  const list = ids.filter(i => geoms[i]).slice(0, 500);
  if (!list.length) return;

  const baseW = opts.weight || 5;
  const paths = list.map(i => L.polyline(geoms[i], {
    color: COLORS[values.buckets[i]] || "#9aa0aa",
    weight: baseW, opacity: 0,
    lineCap: "round", lineJoin: "round",
    interactive: false, renderer: blinkRenderer,
  }).addTo(blinkLayer));

  const per = 430;                   // ms per pulse
  const pulses = 3;
  const start = performance.now();
  const total = pulses * per;

  const tick = (now) => {
    const elapsed = now - start;
    if (elapsed >= total) { cancelBlink(); return; }
    const pulse = Math.sin(Math.PI * ((elapsed % per) / per));   // eased 0->1->0
    const w = baseW + pulse * baseW * 0.9;
    const op = 0.28 + pulse * 0.72;
    for (let k = 0; k < paths.length; k++) paths[k].setStyle({ weight: w, opacity: op });
    blinkRaf = requestAnimationFrame(tick);
  };
  blinkRaf = requestAnimationFrame(tick);
}

// start the blink once the map has finished its fly/fit animation
function blinkAfterMove(ids, weight) {
  let done = false;
  const run = () => {
    if (done) return;
    done = true;
    map.off("moveend", run);
    blinkSegments(ids, { weight });
  };
  map.once("moveend", run);
  setTimeout(run, 450);
}

// ---- map focus ----
function focusSegment(id) {
  if (!geoms[id]) return;
  const b = L.latLngBounds([]);
  geoms[id].forEach(c => b.extend(c));
  if (b.isValid()) map.fitBounds(b, { padding: [60, 60], maxZoom: 15 });
  blinkAfterMove([id], 6);
  showDetail(id);
}

async function showDetail(id) {
  detailId = id;
  const r = await (await fetch(`/api/segment/${id}?date=${dateStr}&mode=${mode}&hour=${hour}`)).json();
  const p = r.properties;
  document.getElementById("detail").classList.remove("hidden");
  document.getElementById("detailName").textContent = p.name || roadName(id) || `Segment #${id}`;
  document.getElementById("detailMeta").innerHTML =
    `<div class="kv"><span class="k">Date</span><span class="v">${fmtDate(p.date)} (${p.day_name})</span></div>` +
    `<div class="kv"><span class="k">Road class</span><span class="v">${p.highway}</span></div>` +
    `<div class="kv"><span class="k">Free-flow speed</span><span class="v">${p.freeflow} km/h</span></div>` +
    `<div class="kv"><span class="k">At ${hhmm(p.selected_hour)}</span><span class="v" style="color:${COLORS[p.current.bucket]}">${p.current.label} · ${p.current.congestion}% · ${p.current.speed} km/h</span></div>` +
    `<div class="kv"><span class="k">Avg congestion (this date)</span><span class="v">${p.avg_congestion_today}%</span></div>` +
    `<div class="kv"><span class="k">Peak</span><span class="v">${hhmm(p.peak.hour)} · ${p.peak.congestion}%</span></div>` +
    `<div class="kv"><span class="k">Mellows at</span><span class="v">${hhmm(p.offpeak.hour)} · ${p.offpeak.congestion}%</span></div>` +
    (p.persistent ? `<div class="kv"><span class="k">Flag</span><span class="v">Persistent congestion</span></div>` : "") +
    (p.bottleneck ? `<div class="kv"><span class="k">Flag</span><span class="v">Bottleneck (junction)</span></div>` : "");
  document.getElementById("detailChart").innerHTML = svgChart(r.hours, 330, 110);
}

document.getElementById("detailClose").addEventListener("click", () => {
  detailId = -1;
  document.getElementById("detail").classList.add("hidden");
});

// ---- controls ----
function setDateLimits() {
  if (!meta) return;
  dateInput.min = mode === "forecast" ? meta.forecast_min : meta.history_min;
  dateInput.max = mode === "forecast" ? meta.forecast_max : meta.history_max;
}

function renderModeUI() {
  document.querySelectorAll(".ms-label").forEach(l => {
    l.classList.toggle("active", l.dataset.mode === mode);
  });
  modeSwitch.classList.toggle("forecast", mode === "forecast");
}

function refresh() {
  cancelBlink();
  refreshValues();
  renderPanel();
  // keep the open detail popup in step with the newly chosen date/hour/mode
  const open = detailId >= 0 && !document.getElementById("detail").classList.contains("hidden");
  if (open) showDetail(detailId);
}

function setMode(m) {
  if (mode === m) return;
  mode = m;
  renderModeUI();
  setDateLimits();
  if (mode === "forecast") dateStr = meta.forecast_min;
  else {
    const y = new Date();
    y.setDate(y.getDate() - 1);
    dateStr = localDateStr(y);
    if (dateStr < meta.history_min) dateStr = meta.history_min;
  }
  dateInput.value = dateStr;
  hour = clampHour(hour);
  updateClock();
  refresh();
}

function onDateChange() {
  dateStr = dateInput.value;
  if (!dateStr) return;
  hour = clampHour(hour);
  updateClock();
  refresh();
}

function shiftDate(delta) {
  const d = new Date(dateStr + "T00:00:00");
  d.setDate(d.getDate() + delta);
  let s = localDateStr(d);
  if (s < dateInput.min) s = dateInput.min;
  if (s > dateInput.max) s = dateInput.max;
  dateStr = s;
  dateInput.value = dateStr;
  hour = clampHour(hour);
  updateClock();
  refresh();
}

function applyNow() {
  // point the controls at the current local moment (live forecast view)
  const now = new Date();
  mode = "forecast";
  renderModeUI();
  setDateLimits();  // forecast window: today .. +30d, so "now" always fits
  dateStr = localDateStr(now);
  if (dateStr < dateInput.min) dateStr = dateInput.min;
  if (dateStr > dateInput.max) dateStr = dateInput.max;
  hour = clampHour(now.getHours());
}

function goNow() {
  applyNow();
  dateInput.value = dateStr;
  updateClock();
  refresh();
}

modeSwitch.addEventListener("click", () => setMode(mode === "forecast" ? "history" : "forecast"));
document.querySelectorAll(".ms-label").forEach(l =>
  l.addEventListener("click", () => setMode(l.dataset.mode)));

dateInput.addEventListener("change", onDateChange);
document.getElementById("prevDay").addEventListener("click", () => shiftDate(-1));
document.getElementById("nextDay").addEventListener("click", () => shiftDate(1));
document.getElementById("nowBtn").addEventListener("click", goNow);

document.getElementById("playBtn").addEventListener("click", () => {
  if (playTimer) {
    clearInterval(playTimer); playTimer = null;
    document.getElementById("playBtn").innerHTML = "&#9654; Animate";
    return;
  }
  document.getElementById("playBtn").innerHTML = "&#9632; Stop";
  playTimer = setInterval(() => {
    hour = clampHour(hour + 1);
    updateClock();
    refresh();
  }, 500);
});

init();
