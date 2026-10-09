"use strict";
/* 结构图：自动分层布局 + SVG 绘制。位置用 CSS transform，连线用 CSS d，
   配合 util.patch 的增量更新，节点增删、移动、状态变化都会以过渡动画呈现。 */
import { h, s, clip, fmtBytes, fmtNum, fmtPct, NODE_ST, DATA_KIND, PIPE_KIND, REPLICA } from "./util.js";

export function layoutGraph(nodes, edges, W, H, GX, GY) {
  const idx = new Map(nodes.map((n, i) => [n.id, i]));
  const es = edges.filter((e) => idx.has(e.from) && idx.has(e.to) && e.from !== e.to);
  const layer = nodes.map(() => 0);
  for (let it = 0; it < nodes.length; it++) {
    let changed = false;
    for (const e of es) {
      const a = idx.get(e.from), b = idx.get(e.to);
      if (layer[b] < layer[a] + 1 && layer[a] + 1 < nodes.length) { layer[b] = layer[a] + 1; changed = true; }
    }
    if (!changed) break;
  }
  const cols = [];
  nodes.forEach((_, i) => { (cols[layer[i]] = cols[layer[i]] || []).push(i); });
  const cs = cols.filter(Boolean);
  const row = new Array(nodes.length).fill(0);
  cs.forEach((c) => c.forEach((i, r) => { row[i] = r; }));
  for (let sweep = 0; sweep < 3; sweep++) {
    for (let c = 1; c < cs.length; c++) {
      const bary = (i) => {
        const ps = es.filter((e) => idx.get(e.to) === i).map((e) => row[idx.get(e.from)]);
        return ps.length ? ps.reduce((a, b) => a + b, 0) / ps.length : row[i];
      };
      cs[c].sort((a, b) => bary(a) - bary(b));
      cs[c].forEach((i, r) => { row[i] = r; });
    }
  }
  const maxRows = Math.max(1, ...cs.map((c) => c.length));
  const totalH = maxRows * H + (maxRows - 1) * GY;
  const pos = new Array(nodes.length), order = [];
  cs.forEach((c, ci) => c.forEach((i, r) => {
    const colH = c.length * H + (c.length - 1) * GY;
    pos[i] = { x: ci * (W + GX), y: (totalH - colH) / 2 + r * (H + GY) };
    order.push(i);
  }));
  return { pos, order, es, idx, width: Math.max(1, cs.length) * W + (cs.length - 1) * GX, height: totalH };
}

/* 节点按执行/流向顺序返回（给步骤列表、要素×步骤矩阵用） */
export function orderedNodes(g) {
  const L = layoutGraph(g.nodes, g.edges, 1, 1, 1, 1);
  return L.order.map((i) => g.nodes[i]);
}

/* 每个节点的祖先集合 */
export function ancestors(g) {
  const parents = new Map(g.nodes.map((n) => [n.id, []]));
  for (const e of g.edges) parents.get(e.to)?.push(e.from);
  const memo = new Map();
  const walk = (id) => {
    if (memo.has(id)) return memo.get(id);
    const set = new Set(); memo.set(id, set);
    for (const p of parents.get(id) || []) { set.add(p); for (const x of walk(p)) set.add(x); }
    return set;
  };
  g.nodes.forEach((n) => walk(n.id));
  return memo;
}

const safe = (x) => String(x).replace(/[^A-Za-z0-9_-]/g, "_");
const px = (x, y) => `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
export function nodeSize(n) { return typeof n.size_bytes === "number" ? n.size_bytes : null; }

function pipelineNode(n, num, sel, W, H, key) {
  const pr = n.progress;
  const it = n.items;
  const itemsTxt = it ? [it.running ? `▶${it.running}` : "", it.done ? `✓${fmtNum(it.done)}` : "", it.failed ? `✗${it.failed}` : "", it.pending ? `…${it.pending}` : ""].filter(Boolean).join(" ") : "";
  const sub = n.status === "running" && pr != null ? `${NODE_ST.running} ${fmtPct(pr)}`
    : `${NODE_ST[n.status] || n.status}${n.kind ? " · " + (PIPE_KIND[n.kind] || n.kind) : ""}${n.stale ? " · 疑似停滞" : ""}`;
  return [
    s("circle", { class: "idx", cx: 22, cy: 24, r: 10 }),
    s("text", { class: "idxt", x: 22, y: 28 }, String(num)),
    s("text", { class: "t1", x: 40, y: 28 }, clip(n.name || n.id, 22)),
    s("text", { class: "t2", x: 14, y: 50 }, clip(n.role || n.desc || "（未填写作用说明）", 34)),
    s("text", { class: "t3", x: 14, y: 68 }, clip(sub, 24)),
    s("text", { class: "t4", x: W - 12, y: 68 }, itemsTxt),
    s("rect", { class: "ptrack", x: 14, y: H - 14, width: W - 28, height: 6, rx: 3 }),
    s("rect", { class: "pfill", x: 14, y: H - 14, height: 6, rx: 3,
                "data-wpx": Math.round((W - 28) * (pr || 0)), style: { width: Math.round((W - 28) * (pr || 0)) + "px" } }),
  ];
}

function datasetNode(n, sel, W, H) {
  const bits = [];
  if (n.size_bytes != null) bits.push(fmtBytes(n.size_bytes));
  const m = n.meta || {};
  if (m.count != null) bits.push(`${fmtNum(m.count)} ${m.count_unit || "条"}`);
  const pending = (n.locations || []).some((l) => l.stat && l.stat.pending);
  if (pending) bits.push("统计中…");
  const nl = (n.locations || []).length;
  const line3 = `${NODE_ST[n.status] || n.status} · ${nl ? nl + " 个地址" : "未记录地址"}`;
  const pr = n.progress;
  return [
    s("ellipse", { class: "ico", cx: 28, cy: 22, rx: 11, ry: 4.5 }),
    s("path", { class: "ico", d: "M17 22v26c0 2.6 5 4.7 11 4.7s11-2.1 11-4.7V22M17 35c0 2.6 5 4.7 11 4.7s11-2.1 11-4.7" }),
    s("text", { class: "t1", x: 50, y: 28 }, clip(n.name || n.id, 24)),
    s("text", { class: "t2", x: 50, y: 48 }, clip(bits.join(" · ") || (DATA_KIND[n.kind] || n.kind || ""), 30)),
    s("text", { class: "t3", x: 50, y: 66 }, clip(line3, 26)),
    s("text", { class: `t4 rep-${n.replica || ""}`, x: W - 12, y: 66 }, n.replica ? REPLICA[n.replica] : ""),
    s("rect", { class: "ptrack", x: 14, y: H - 14, width: W - 28, height: 6, rx: 3 }),
    s("rect", { class: "pfill", x: 14, y: H - 14, height: 6, rx: 3,
                "data-wpx": Math.round((W - 28) * (pr || 0)), style: { width: Math.round((W - 28) * (pr || 0)) + "px" } }),
  ];
}

export function drawGraph(kind, g, selId, gkey) {
  const isData = kind === "datasets";
  const W = isData ? 250 : 236, H = 98, GX = 86, GY = 30, PAD = 16;
  const nodes = g.nodes;
  const hasGroups = (g.groups || []).some((x) => x.nodes.length);
  const TOP = hasGroups ? 40 : PAD;
  const L = layoutGraph(nodes, g.edges, W, H, GX, GY);
  const vw = L.width + PAD * 2, vh = L.height + TOP + PAD;
  const at = (i) => ({ x: L.pos[i].x + PAD, y: L.pos[i].y + TOP });
  const id = safe(gkey);

  const defs = s("defs", {}, ["", "on", "run"].map((m) => s("marker", { id: `ah${m}-${id}`, viewBox: "0 0 10 10", refX: 9, refY: 5,
    markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" }, s("path", { class: `mk mk-${m || "off"}`, d: "M0 0L10 5L0 10z" }))));

  const groupEls = [];
  (g.groups || []).forEach((gr, gi) => {
    const mem = gr.nodes.map((nid) => L.idx.get(nid)).filter((i) => i != null);
    if (!mem.length) return;
    const xs = mem.map((i) => at(i).x), ys = mem.map((i) => at(i).y);
    const x1 = Math.min(...xs) - 12, y1 = Math.min(...ys) - 32, x2 = Math.max(...xs) + W + 12, y2 = Math.max(...ys) + H + 12;
    groupEls.push(s("g", { class: `group gc-${gi % 5}`, "data-key": "grp:" + gr.id, "data-fade": 1 },
      s("title", {}, `${gr.name}${gr.desc ? "：" + gr.desc : ""}`),
      s("rect", { class: "gbox", x: 0, y: 0, rx: 14, style: { transform: px(x1, y1), width: Math.round(x2 - x1) + "px", height: Math.round(y2 - y1) + "px" } }),
      s("text", { class: "glabel", x: 0, y: 0, style: { transform: px(x1 + 12, y1 + 20) } }, clip(`${gr.name}${gr.desc ? "  " + gr.desc : ""}`, Math.max(12, Math.round((x2 - x1) / 6.5))))));
  });

  const edgeEls = [];
  for (const e of L.es) {
    const a = L.idx.get(e.from), b = L.idx.get(e.to);
    const pa = at(a), pb = at(b);
    const x1 = pa.x + W, y1 = pa.y + H / 2, x2 = pb.x, y2 = pb.y + H / 2;
    const dx = Math.max(30, Math.abs(x2 - x1) / 2);
    const d = `M${Math.round(x1)},${Math.round(y1)} C${Math.round(x1 + dx)},${Math.round(y1)} ${Math.round(x2 - dx)},${Math.round(y2)} ${Math.round(x2)},${Math.round(y2)}`;
    const sa = nodes[a].status, sb = nodes[b].status;
    const mode = sb === "running" && ["done", "ready", "running"].includes(sa) ? "run" : ["done", "ready"].includes(sa) ? "on" : "";
    edgeEls.push(s("g", { "data-key": `e:${e.from}>${e.to}`, "data-fade": 1 },
      s("path", { class: `edge ${mode}`, d, style: { d: `path("${d}")` }, "marker-end": `url(#ah${mode}-${id})` }),
      s("text", { class: "elabel", x: 0, y: 0, style: { transform: px((x1 + x2) / 2, (y1 + y2) / 2 - 6) } }, e.label ? clip(e.label, 20) : "")));
  }

  const num = new Map(L.order.map((i, k) => [i, k + 1]));
  const nodeEls = nodes.map((n, i) => {
    const p = at(i);
    const sel = n.id === selId;
    return s("g", { class: `node st-${n.status} ${sel ? "sel" : ""} ${n.stale ? "stale" : ""}`, "data-key": "n:" + n.id, "data-fade": 1,
                    style: { transform: px(p.x, p.y) }, tabindex: 0, role: "button", "aria-label": n.name || n.id,
                    "data-act": "selnode", "data-a1": gkey, "data-a2": n.id },
      s("title", {}, `${n.name || n.id}${n.role ? "\n作用：" + n.role : ""}${n.desc ? "\n" + n.desc : ""}`),
      s("rect", { class: "box", x: 0, y: 0, width: W, height: H, rx: 10 }),
      s("rect", { class: "stripe", x: 0, y: 0, width: 6, height: H, rx: 3 }),
      isData ? datasetNode(n, sel, W, H) : pipelineNode(n, num.get(i), sel, W, H, gkey));
  });

  const svg = s("svg", { class: "graph", "data-key": "svg", viewBox: `0 0 ${vw} ${vh}`, width: vw, height: vh, role: "img", "aria-label": g.title || g.id },
    defs, s("g", { "data-key": "groups" }, groupEls), s("g", { "data-key": "edges" }, edgeEls), s("g", { "data-key": "nodes" }, nodeEls));
  return { el: h("div", { class: "graph-scroll", "data-key": "gs:" + gkey }, svg), order: L.order };
}
