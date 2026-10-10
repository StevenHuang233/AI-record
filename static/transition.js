"use strict";
/* 模块块：热力图（heatmap）和状态流转图（flow）。
   热力图 = 论文里常见的「转移矩阵」：行 / 列标签、格内数值、右侧色条，可切换多张矩阵（全部 / 理科 / 文科 / 各数据集…）。
   流转图 = 状态之间怎么走：节点按阶段排成列，箭头粗细 = 概率，允许但没出现的路径画成虚线。
   两者都画成自带样式的 SVG（颜色、字号全写成属性，不依赖页面 CSS），所以「下载 SVG / PNG」得到的图和页面上一致，可以直接放进论文。
   几何计算（heatmapModel / flowModel）是纯函数，不碰 DOM，Node 里可以直接测。 */
import { h, s, fmtNum } from "./util.js";
import { S } from "./store.js";

const asArr = (x) => (Array.isArray(x) ? x : x == null || x === "" ? [] : [x]);
const FONT = "-apple-system, 'PingFang SC', 'Microsoft YaHei', 'Noto Sans SC', sans-serif";

/* ── 配色：ColorBrewer Blues（和常见的 seaborn / matplotlib「Blues」一致） ───────── */
const BLUES = ["#f7fbff", "#deebf7", "#c6dbef", "#9ecae1", "#6baed6", "#4292c6", "#2171b5", "#08519c", "#08306b"]
  .map((x) => [1, 3, 5].map((i) => parseInt(x.slice(i, i + 2), 16)));
export function heatColor(t) {
  const x = Math.max(0, Math.min(1, Number.isFinite(t) ? t : 0)) * (BLUES.length - 1);
  const i = Math.min(BLUES.length - 2, Math.floor(x)), f = x - i;
  return `rgb(${[0, 1, 2].map((k) => Math.round(BLUES[i][k] + (BLUES[i + 1][k] - BLUES[i][k]) * f)).join(",")})`;
}

const tabsOf = (b) => asArr(b.matrices).map((m) => ({ id: String(m.id), name: String(m.name || m.id) }));
const pickTab = (b, key) => {
  const tabs = tabsOf(b);
  const want = S.sel[`${key}#tab`];
  return tabs.find((t) => t.id === want) ? want : (tabs[0] && tabs[0].id);
};

/* ── 热力图几何 ───────────────────────────────────────────────────────────── */
export function heatmapModel(b, tabId, show = "p") {
  const m = asArr(b.matrices).find((x) => String(x.id) === tabId);
  if (!m) return null;
  const rowsAll = asArr(b.rows).map((r) => (typeof r === "object" ? r : { label: String(r) }));
  const colsAll = asArr(b.cols).map((c) => (typeof c === "object" ? c : { label: String(c) }));
  const vals = asArr(m.values).map((r) => asArr(r).map(Number));
  const cnts = m.counts ? asArr(m.counts).map((r) => asArr(r).map(Number)) : null;
  const rowTotal = (i) => (m.row_totals ? Number(m.row_totals[i]) : cnts ? cnts[i].reduce((a, c) => a + c, 0) : (vals[i] || []).reduce((a, c) => a + c, 0));
  const keepRow = rowsAll.map((_, i) => !(b.hide_empty !== false) || rowTotal(i) > 0);
  const keepCol = colsAll.map((_, j) => !(b.hide_empty !== false) || vals.some((r, i) => keepRow[i] && (r[j] || 0) > 0));
  const rows = rowsAll.map((r, i) => ({ ...r, i, total: rowTotal(i) })).filter((r) => keepRow[r.i]);
  const cols = colsAll.map((c, j) => ({ ...c, j })).filter((c) => keepCol[c.j]);
  const maxLabel = (xs) => Math.max(1, ...xs.map((x) => [...String(x.label)].reduce((w, ch) => w + (ch.charCodeAt(0) > 255 ? 1 : 0.55), 0)));
  const CELL_W = 56, CELL_H = 38, FS = 12.5;
  const left = Math.max(70, Math.round(maxLabel(rows) * FS) + 26);
  const top = Math.max(60, Math.round(maxLabel(cols) * FS) + 22);
  const gridW = cols.length * CELL_W, gridH = rows.length * CELL_H;
  let max = 0;
  for (const r of rows) for (const c of cols) max = Math.max(max, vals[r.i] ? vals[r.i][c.j] || 0 : 0);
  const vmax = typeof b.vmax === "number" ? b.vmax : max || 1;
  const cells = [];
  rows.forEach((r, ri) => cols.forEach((c, ci) => {
    const v = vals[r.i] ? vals[r.i][c.j] || 0 : 0;
    const n = cnts && cnts[r.i] ? cnts[r.i][c.j] || 0 : null;
    const t = v / vmax;
    cells.push({ r: ri, c: ci, v, n, x: left + ci * CELL_W, y: top + ri * CELL_H, fill: heatColor(t), ink: t > 0.5 ? "#ffffff" : "#222222",
      text: show === "n" && n != null ? fmtNum(n) : v.toFixed(Number(b.digits ?? 2)),
      tip: `${r.label} → ${c.label}\n概率 ${(v * 100).toFixed(1)}%${n != null ? `\n次数 ${fmtNum(n)}（该行共 ${fmtNum(r.total)}）` : ""}` });
  }));
  const barX = left + gridW + 22;
  return { rows, cols, cells, left, top, gridW, gridH, CELL_W, CELL_H, FS, vmax, barX, barW: 16, width: barX + 16 + 46, height: top + gridH + 16 };
}

/* ── 流转图几何 ───────────────────────────────────────────────────────────── */
export function flowModel(b, tabId) {
  const NW = 138, NH = 32, COLW = 214, ROWH = 52, PADX = 34, PADY = 108, GP = 14;
  const nodes = asArr(b.nodes).map((n) => ({ ...n, w: NW, h: NH, x: PADX + (n.col || 0) * COLW, y: PADY + (n.row || 0) * ROWH }));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const groups = asArr(b.groups).map((g) => {
    const ms = asArr(g.nodes).map((id) => byId.get(id)).filter(Boolean);
    const x0 = Math.min(...ms.map((n) => n.x)) - GP, y0 = Math.min(...ms.map((n) => n.y)) - GP - 12;
    const x1 = Math.max(...ms.map((n) => n.x + n.w)) + GP, y1 = Math.max(...ms.map((n) => n.y + n.h)) + GP;
    return { ...g, x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
  });
  const box = new Map([...nodes, ...groups].map((o) => [o.id, o]));
  const width = Math.max(...[...nodes, ...groups].map((o) => o.x + o.w)) + PADX;
  const height = Math.max(...[...nodes, ...groups].map((o) => o.y + o.h)) + 40;
  const thr = typeof b.threshold === "number" ? b.threshold : 0.02;
  const list = asArr(b.edges).map((e, k) => ({ e, k, A: box.get(e.from), B: box.get(e.to) })).filter((x) => x.A && x.B);

  /* 同一个端点上有多条边时，把落点沿边框错开（按对端的高度排序），否则箭头全挤在中点糊成一团 */
  const slot = new Map();
  const spread = (end, other, key) => {
    const group = list.filter((x) => x[end] === other && x.A !== x.B)
      .sort((p, q) => (p[end === "A" ? "B" : "A"].y + p[end === "A" ? "B" : "A"].h / 2) - (q[end === "A" ? "B" : "A"].y + q[end === "A" ? "B" : "A"].h / 2));
    group.forEach((x, i) => {
      const n = group.length, step = n > 1 ? Math.min(16, Math.max(other.h - 14, 36) / (n - 1)) : 0;
      slot.set(`${x.k}:${key}`, (i - (n - 1) / 2) * step);
    });
  };
  for (const o of box.values()) { spread("A", o, "out"); spread("B", o, "in"); }

  const bez = (p0, p1, p2, p3, t) => {
    const u = 1 - t;
    return [u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
            u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1]];
  };
  const edges = list.map(({ e, k, A, B }) => {
    const p = e.w && typeof e.w === "object" ? Number(e.w[tabId] || 0) : Number(e.w || 0);
    const self = A === B;
    let d, lx, ly;
    if (self) {
      const x1 = A.x + A.w - 18, x0 = A.x + 18, y = A.y;
      d = `M${x1},${y} C${x1 + 26},${y - 46} ${x0 - 26},${y - 46} ${x0},${y}`;
      lx = (x0 + x1) / 2; ly = y - 36;
    } else {
      const x1 = A.x + A.w, y1 = A.y + A.h / 2 + (slot.get(`${k}:out`) || 0), x2 = B.x, y2 = B.y + B.h / 2 + (slot.get(`${k}:in`) || 0);
      let c1, c2;
      if (typeof e.bend === "number") { c1 = [x1 + 70, y1 + e.bend]; c2 = [x2 - 70, y2 + e.bend]; }
      else { const dx = Math.max(36, (x2 - x1) / 2); c1 = [x1 + dx, y1]; c2 = [x2 - dx, y2]; }
      d = `M${x1},${y1} C${c1[0]},${c1[1]} ${c2[0]},${c2[1]} ${x2},${y2}`;
      [lx, ly] = bez([x1, y1], c1, c2, [x2, y2], e.lt ?? 0.3);          // 标签放在靠近起点的位置：不同起点的标签天然错开
      ly -= 5;
    }
    return { k, from: e.from, to: e.to, p, d, lx, ly, self, dashed: p <= 0, shown: p >= thr,
      width: p <= 0 ? 1 : +(1.2 + 6 * Math.sqrt(p)).toFixed(2),
      label: p > 0 ? p.toFixed(2) : "", tip: `${A.label} → ${B.label}：${p > 0 ? (p * 100).toFixed(1) + "%" : "规则允许，当前数据里没有"}${e.note ? "\n" + e.note : ""}` };
  });
  const phases = asArr(b.phases).map((ph) => ({ ...ph, x: PADX + (ph.col || 0) * COLW + NW / 2 }));
  return { nodes, groups, edges, phases, width, height, threshold: thr, NW, NH };
}

/* ── SVG 绘制 ─────────────────────────────────────────────────────────────── */
const T = (attrs, ...kids) => s("text", { "font-family": FONT, ...attrs }, ...kids);

function heatmapSvg(b, tabId, show, key) {
  const M = heatmapModel(b, tabId, show);
  if (!M) return null;
  const grad = `hm-grad-${key.replace(/[^\w]/g, "")}`;
  return s("svg", { xmlns: "http://www.w3.org/2000/svg", width: M.width, height: M.height, viewBox: `0 0 ${M.width} ${M.height}`, "data-svg": "heatmap" },
    s("rect", { width: M.width, height: M.height, fill: "#ffffff" }),
    s("defs", {}, s("linearGradient", { id: grad, x1: 0, y1: 1, x2: 0, y2: 0 }, [0, 0.25, 0.5, 0.75, 1].map((t) => s("stop", { offset: `${t * 100}%`, "stop-color": heatColor(t) })))),
    M.cols.map((c, ci) => T({ x: M.left + ci * M.CELL_W + M.CELL_W / 2 + 4, y: M.top - 10, "font-size": M.FS, fill: "#222",
      transform: `rotate(-90 ${M.left + ci * M.CELL_W + M.CELL_W / 2 + 4} ${M.top - 10})`, "text-anchor": "start" }, c.label)),
    M.rows.map((r, ri) => T({ x: M.left - 10, y: M.top + ri * M.CELL_H + M.CELL_H / 2 + 4, "font-size": M.FS, fill: "#222", "text-anchor": "end" }, r.label)),
    M.cells.map((c) => s("g", {}, s("title", {}, c.tip),
      s("rect", { x: c.x, y: c.y, width: M.CELL_W, height: M.CELL_H, fill: c.fill, stroke: "#ffffff", "stroke-width": 1.5 }),
      T({ x: c.x + M.CELL_W / 2, y: c.y + M.CELL_H / 2 + 4.5, "font-size": 12.5, fill: c.ink, "text-anchor": "middle" }, c.text))),
    s("rect", { x: M.barX, y: M.top, width: M.barW, height: M.gridH, fill: `url(#${grad})`, stroke: "#cccccc", "stroke-width": 0.5 }),
    [0, 0.25, 0.5, 0.75, 1].map((t) => T({ x: M.barX + M.barW + 6, y: M.top + M.gridH - t * M.gridH + 4, "font-size": 11, fill: "#444" }, (t * M.vmax).toFixed(2))));
}

function flowSvg(b, tabId, thr, key) {
  const M = flowModel({ ...b, threshold: thr }, tabId);
  const id = key.replace(/[^\w]/g, "");
  const marker = (mid, color) => s("marker", { id: mid, markerUnits: "userSpaceOnUse", markerWidth: 11, markerHeight: 11, refX: 10, refY: 5, orient: "auto" },
    s("path", { d: "M0,0 L10,5 L0,10 z", fill: color }));
  return s("svg", { xmlns: "http://www.w3.org/2000/svg", width: M.width, height: M.height, viewBox: `0 0 ${M.width} ${M.height}`, "data-svg": "flow" },
    s("rect", { width: M.width, height: M.height, fill: "#ffffff" }),
    s("defs", {}, marker(`ah-${id}`, "#2f5f9f"), marker(`ad-${id}`, "#b5b5b5")),
    M.phases.map((p) => T({ x: p.x, y: 30, "font-size": 13, "font-weight": 600, fill: "#555", "text-anchor": "middle" }, p.label)),
    M.groups.map((g) => s("g", {}, s("rect", { x: g.x, y: g.y, width: g.w, height: g.h, rx: 12, fill: g.fill || "#f4f7fb", stroke: "#c9d4e5", "stroke-dasharray": "5 4" }),
      g.label ? T({ x: g.x + 12, y: g.y + 15, "font-size": 11.5, fill: "#6b7a90" }, g.label) : null)),
    M.edges.filter((e) => e.shown || e.dashed).map((e) => s("g", { opacity: e.dashed ? 0.8 : 0.62 }, s("title", {}, e.tip),
      s("path", { d: e.d, fill: "none", stroke: e.dashed ? "#c4c4c4" : "#2f5f9f", "stroke-width": e.width, ...(e.dashed ? { "stroke-dasharray": "4 4" } : {}),
        "marker-end": `url(#${e.dashed ? "ad" : "ah"}-${id})` }))),
    M.edges.filter((e) => e.shown).map((e) => T({ x: e.lx, y: e.ly, "font-size": 11, "font-weight": 600, fill: "#1d3f73", "text-anchor": "middle",
      stroke: "#ffffff", "stroke-width": 3, "paint-order": "stroke" }, e.label)),
    M.nodes.map((n) => s("g", {}, n.tip ? s("title", {}, n.tip) : null,
      s("rect", { x: n.x, y: n.y, width: n.w, height: n.h, rx: 8, fill: n.fill || "#ffffff", stroke: n.stroke || "#2f5f9f", "stroke-width": 1.4 }),
      T({ x: n.x + n.w / 2, y: n.y + n.h / 2 + 4.5, "font-size": 12.5, fill: n.ink || "#1d2b44", "text-anchor": "middle" }, n.label))));
}

/* ── 块：外壳（标题、标签页、选项、下载、说明） ──────────────────────────── */
const tabBar = (b, key, cur) => h("div", { class: "views", "data-key": "tabs" },
  tabsOf(b).map((t) => h("button", { class: `vtab ${t.id === cur ? "on" : ""}`, "data-key": "t:" + t.id, "data-act": "bltab", "data-a1": key, "data-a2": t.id }, t.name)));
const optBtn = (key, name, val, cur, label) => h("button", { class: `vtab ${cur === val ? "on" : ""}`, "data-key": `${name}:${val}`, "data-act": "blopt", "data-a1": `${key}#${name}`, "data-a2": String(val) }, label);

function shell(b, key, controls, svg, extra) {
  return h("div", { class: "tblock", "data-key": "tb:" + key, "data-tb": key },
    b.title ? h("div", { class: "chart-title" }, b.title) : null,
    h("div", { class: "tb-controls", "data-key": "ctl" }, controls),
    h("div", { class: "tb-fig", "data-key": "fig" }, svg || h("div", { class: "empty sm" }, "没有数据")),
    h("div", { class: "tb-actions", "data-key": "act" },
      h("button", { class: "tl-btn", "data-act": "bldl", "data-a1": key, "data-a2": "svg", title: "下载矢量图，可直接放进论文" }, "下载 SVG"),
      h("button", { class: "tl-btn", "data-act": "bldl", "data-a1": key, "data-a2": "png", title: "下载 2 倍分辨率的 PNG" }, "下载 PNG"), extra || null),
    b.caption ? h("div", { class: "legend", "data-key": "cap" }, b.caption) : null);
}

export function heatmapBlock(b, key) {
  const tab = pickTab(b, key);
  const hasN = asArr(b.matrices).some((m) => m.counts);
  const show = hasN && S.sel[`${key}#show`] === "n" ? "n" : "p";
  const controls = [asArr(b.matrices).length > 1 ? tabBar(b, key, tab) : null,
    hasN ? h("div", { class: "views", "data-key": "show" }, optBtn(key, "show", "p", show, "转移概率"), optBtn(key, "show", "n", show, "次数")) : null];
  const m = asArr(b.matrices).find((x) => String(x.id) === tab);
  const note = m && m.n_dialogues != null ? h("span", { class: "id", "data-key": "n" }, `${fmtNum(m.n_dialogues)} 段对话${m.n_rows != null ? ` · ${fmtNum(m.n_rows)} 条视频` : ""}`) : null;
  return shell(b, key, controls, tab ? heatmapSvg(b, tab, show, key) : null, note);
}

export function flowBlock(b, key) {
  const tab = pickTab(b, key);
  const thr = Number(S.sel[`${key}#thr`] ?? b.threshold ?? 0.02);
  const controls = [asArr(b.matrices).length > 1 ? tabBar(b, key, tab) : null,
    h("div", { class: "views", "data-key": "thr" }, h("span", { class: "id" }, "只标出概率 ≥ "),
      [0, 0.02, 0.05, 0.1, 0.2].map((v) => optBtn(key, "thr", v, thr, v === 0 ? "全部" : v.toFixed(2))))];
  return shell(b, key, controls, flowSvg(b, tab, thr, key));
}

/* ── 下载（只在浏览器里用） ─────────────────────────────────────────────── */
export function svgText(svgEl) {
  const clone = svgEl.cloneNode(true);
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  return `<?xml version="1.0" encoding="UTF-8"?>\n` + new XMLSerializer().serializeToString(clone);
}

export function downloadBlock(key, fmt) {
  const host = [...document.querySelectorAll("[data-tb]")].find((x) => x.dataset.tb === key);
  const svg = host && host.querySelector("svg");
  if (!svg) return;
  const name = (host.querySelector(".chart-title")?.textContent || key).replace(/[^\w\u4e00-\u9fa5-]+/g, "_").slice(0, 60);
  const save = (blob, ext) => { const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `${name}.${ext}`; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 4000); };
  const text = svgText(svg);
  if (fmt === "svg") return save(new Blob([text], { type: "image/svg+xml" }), "svg");
  const img = new Image(), w = Number(svg.getAttribute("width")), hgt = Number(svg.getAttribute("height"));
  img.onload = () => {
    const c = document.createElement("canvas"); c.width = w * 2; c.height = hgt * 2;
    const g = c.getContext("2d"); g.scale(2, 2); g.drawImage(img, 0, 0, w, hgt);
    c.toBlob((bl) => bl && save(bl, "png"), "image/png");
  };
  img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(text);
}
