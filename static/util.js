"use strict";
/* 基础工具：DOM 构建（不使用 innerHTML）、增量 patch（让 CSS 过渡动画得以发生）、格式化、中文标签。 */

export const SVGNS = "http://www.w3.org/2000/svg";

/* 样式统一序列化成 style 属性：这样 morph 的属性同步才能把新样式带到已存在的元素上 */
function setStyle(el, st) {
  if (typeof st === "string") { el.setAttribute("style", st); return; }
  const css = Object.entries(st).map(([k, v]) => `${k.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase())}:${String(v)}`).join(";");
  if (css) el.setAttribute("style", css);
}

function build(el, attrs, kids) {
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "style") setStyle(el, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
export const h = (tag, attrs, ...kids) => build(document.createElement(tag), attrs, kids);
export const s = (tag, attrs, ...kids) => build(document.createElementNS(SVGNS, tag), attrs, kids);

/* ── 增量 patch ───────────────────────────────────────────────────────────────
   视图每次都生成一棵全新的离线 DOM，再 patch 进页面里已有的 DOM：
   已存在的元素只改变属性/文本，所以 CSS transition 会在「旧值 → 新值」之间播放动画；
   带 data-key 的元素按 key 匹配；新元素淡入、进度条从 0 长出；data-fade 的元素被移除时淡出。 */
const queue = [];
let firstPaint = true;

export function flush() {
  void document.body.offsetHeight;
  for (const f of queue.splice(0)) f();
}

function onInsert(node) {
  if (node.nodeType !== 1) return;
  for (const el of [node, ...node.querySelectorAll("[data-fade],[data-w],[data-wpx]")]) {
    if (el.hasAttribute("data-w")) { const w = el.dataset.w; el.style.width = "0%"; queue.push(() => { el.style.width = w + "%"; }); }
    if (el.hasAttribute("data-wpx")) { const w = el.dataset.wpx; el.style.width = "0px"; queue.push(() => { el.style.width = w + "px"; }); }
    if (el.hasAttribute("data-fade") && !firstPaint) { el.classList.add("entering"); queue.push(() => el.classList.remove("entering")); }
  }
}

function leave(el) {
  el.__leaving = true;
  el.classList.add("leaving");
  setTimeout(() => el.remove(), 600);
}

function syncAttrs(dst, src) {
  for (const a of [...dst.attributes]) if (!src.hasAttribute(a.name)) dst.removeAttribute(a.name);
  for (const a of src.attributes) if (dst.getAttribute(a.name) !== a.value) dst.setAttribute(a.name, a.value);
}

const keyOf = (n) => (n.nodeType === 1 ? n.getAttribute("data-key") : null);
const compatible = (a, b) => a.nodeType === b.nodeType && (a.nodeType !== 1 || (a.tagName === b.tagName && a.namespaceURI === b.namespaceURI));

function morphChildren(dst, src) {
  const live = [...dst.childNodes].filter((n) => !n.__leaving);
  const keyed = new Map();
  const unkeyed = [];
  for (const n of live) { const k = keyOf(n); if (k) keyed.set(k, n); else unkeyed.push(n); }
  const used = new Set();
  let ui = 0, prev = null;
  for (const sk of [...src.childNodes]) {
    let match = null;
    const k = keyOf(sk);
    if (k) { match = keyed.get(k); if (match && !compatible(match, sk)) match = null; }
    else if (ui < unkeyed.length && compatible(unkeyed[ui], sk)) match = unkeyed[ui++];
    let want = prev ? prev.nextSibling : dst.firstChild;
    while (want && want.__leaving) want = want.nextSibling;
    if (match) {
      used.add(match);
      if (match.nodeType === 3) { if (match.nodeValue !== sk.nodeValue) match.nodeValue = sk.nodeValue; }
      else if (match.nodeType === 1) morph(match, sk);
      if (want !== match) dst.insertBefore(match, want);
      prev = match;
    } else {
      dst.insertBefore(sk, want);
      onInsert(sk);
      used.add(sk);
      prev = sk;
    }
  }
  for (const n of live) {
    if (used.has(n)) continue;
    if (n.nodeType === 1 && n.hasAttribute("data-fade")) leave(n); else n.remove();
  }
}

export function morph(dst, src) {
  syncAttrs(dst, src);
  if (dst.hasAttribute("data-static")) return;     // 自己管内容的容器（视频图库）：播放进度、焦点都不能被重绘打断
  morphChildren(dst, src);
}

export function patch(dstRoot, srcRoot) {
  morph(dstRoot, srcRoot);
  flush();
  firstPaint = false;
}

/* ── 事件委托：元素用 data-act / data-a1 / data-a2 声明动作，patch 后不需要重新绑定 ── */
const ACTIONS = {};
export const onAction = (name, fn) => { ACTIONS[name] = fn; };
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (!el || e.target.closest("a[href]")) return;
  const fn = ACTIONS[el.dataset.act];
  if (fn) fn(el.dataset.a1, el.dataset.a2, el, e);
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const el = e.target.closest && e.target.closest("[data-act][role=button]");
  if (el && ACTIONS[el.dataset.act]) { e.preventDefault(); ACTIONS[el.dataset.act](el.dataset.a1, el.dataset.a2, el, e); }
});

/* ── 格式化 ───────────────────────────────────────────────────────────────── */
export function fmtBytes(n) {
  if (n == null || isNaN(n)) return "-";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(n < 10 ? 2 : 1) : String(n)) + " " + u[i];
}
export function fmtDur(sec) {
  if (sec == null || !isFinite(sec)) return "-";
  sec = Math.round(sec);
  const d = Math.floor(sec / 86400), hh = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60), ss = sec % 60;
  if (d) return `${d}天${hh}时`;
  if (hh) return `${hh}时${m}分`;
  if (m) return `${m}分${ss}秒`;
  return `${ss}秒`;
}
export const fmtNum = (n) => (typeof n === "number" ? n.toLocaleString("en-US", { maximumFractionDigits: 4 }) : String(n));
export const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "-");
export const fmtPct = (p) => (p == null ? "--" : (p * 100).toFixed(p >= 0.9995 || p === 0 ? 0 : 1) + "%");
export function clip(str, maxUnits) {
  let w = 0, out = "";
  for (const ch of String(str ?? "")) {
    const cw = ch.charCodeAt(0) > 255 ? 2 : 1;
    if (w + cw > maxUnits) return out + "…";
    w += cw; out += ch;
  }
  return out;
}
export const assetUrl = (rel) => "/assets/" + String(rel).split("/").map(encodeURIComponent).join("/");

/* ── 中文标签 ─────────────────────────────────────────────────────────────── */
export const TASK_ST = { queued: "排队中", running: "运行中", done: "已完成", failed: "失败", cancelled: "已取消" };
export const NODE_ST = { pending: "待执行", running: "运行中", done: "已完成", failed: "失败", skipped: "已跳过",
                         ready: "就绪", building: "生成中", missing: "缺失", stale: "已过期" };
export const ITEM_ST = { pending: "待处理", running: "处理中", done: "已完成", failed: "失败", skipped: "已跳过" };
export const TASK_KIND = { train: "训练", eval: "评测", run: "流水线执行", job: "普通任务" };
export const PIPE_KIND = { source: "数据源", process: "处理", model: "模型", train: "训练", eval: "评测", output: "产出", gate: "校验" };
export const DATA_KIND = { raw: "原始数据", processed: "加工数据", derived: "衍生/结果", external: "外部数据", model: "模型/权重" };
export const LOC_ROLE = { primary: "主地址", backup: "备份", mirror: "镜像", remote: "远端", cache: "缓存" };
export const REPLICA = { consistent: "副本一致", mismatch: "副本不一致", missing_copy: "有副本缺失", unchecked: "副本未校验" };
export const META_LABEL = { size_bytes: "大小", size: "大小", count: "条数", format: "格式", version: "版本", updated: "更新时间",
                            storage: "存储", owner: "负责人", alias: "别名" };
export const META_HIDE = new Set(["auto_stat", "count_unit", "path"]);

export const badge = (st, map) => h("span", { class: `badge st-${st}` }, map[st] || st);

export function bar(p, status, o = {}) {
  const indet = p == null && status === "running";
  const pct = indet ? 100 : p == null ? 0 : Math.round(p * 1000) / 10;
  return h("div", { class: `bar ${o.sm ? "sm" : ""} ${indet ? "indet" : ""} st-${status} ${o.stale ? "stale" : ""}` },
    h("div", { class: "fill", "data-w": indet ? null : pct, style: { width: pct + "%" } }));
}

/* ── 卡片收纳：标题左侧的 ▾/▸ 按钮，收起后只留标题行（徽标仍显示，当摘要用） ──────────
   收起状态按 key 存在浏览器本地（localStorage），刷新页面后保持；默认全部展开。 */
const FOLD_KEY = "sb_fold";
const FOLD = (() => { try { return new Set(JSON.parse(localStorage.getItem(FOLD_KEY) || "[]")); } catch (e) { return new Set(); } })();
export const isFolded = (key) => FOLD.has(key);
export function toggleFold(key) {
  if (FOLD.has(key)) FOLD.delete(key); else FOLD.add(key);
  try { localStorage.setItem(FOLD_KEY, JSON.stringify([...FOLD].slice(-200))); } catch (e) { /* 存不了就只在本次页面有效 */ }
}
export function foldBtn(key) {
  const closed = FOLD.has(key);
  return h("button", { class: "fold", "data-act": "fold", "data-a1": key, "data-key": "fold:" + key,
                       title: closed ? "展开" : "收起", "aria-expanded": closed ? "false" : "true" }, closed ? "▸" : "▾");
}
/* 收起时返回 null，展开时原样返回内容：h("div", ..., head, foldBody(key, [正文...])) */
export const foldBody = (key, kids) => (FOLD.has(key) ? null : kids);
