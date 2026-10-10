"use strict";
/* 入口：取状态 → 按 URL hash 路由出当前子页面 → 生成离线 DOM → patch 进页面。
   实时模式每 2.5 秒轮询；时间线模式取历史时刻的状态。切换子页面时清空旧页面再绘制（新页面淡入）。 */
import { h, patch, onAction, fmtTime } from "./util.js";
import { S } from "./store.js";
import { parseRoute, navKeyOf, renderPage } from "./pages.js";
import { TL, mount, setEvents } from "./timeline.js";
import { setOnChange, setView, setRule, gotoPage, mark, setFacet, clearFacets, fetchPage, RV } from "./review.js";
import { downloadBlock } from "./transition.js";

const POLL_MS = 2500;
const NAV = [["home", "首页", "#/"], ["train", "训练", "#/train"], ["run", "流水线执行", "#/run"], ["eval", "评测", "#/eval"],
             ["pipelines", "Pipeline", "#/pipelines"], ["datasets", "数据管理", "#/datasets"], ["review", "成果校验", "#/review"], ["resources", "资源管理", "#/resources"], ["modules", "定制模块", "#/modules"]];
let seq = 0;
let lastRoute = null;

function render() {
  const st = S.state;
  if (!st) return;
  const r = parseRoute(location.hash);
  document.title = st.title;
  const sm = st.summary, bk = st.by_kind;
  const hist = !st.live;
  const chip = (label, n, cls) => h("span", { class: `chip st-${cls}`, "data-key": "chip:" + label }, h("b", {}, n), label);
  const active = navKeyOf(r);

  const header = h("header", { class: "top", "data-key": "top" },
    h("div", { class: "top-row" }, h("a", { class: "brand", href: "#/" }, h("h1", {}, st.title)), st.subtitle ? h("span", { class: "subtitle" }, st.subtitle) : null, h("span", { class: "spacer" }),
      h("span", { class: "live" }, h("span", { class: `dot ${S.offline ? "off" : hist ? "hist" : ""}` }),
        S.offline ? "连接中断，显示的是旧数据" : hist ? `历史回放 · ${fmtTime(st.now)}` : `实时 · 更新于 ${fmtTime(S.lastOk / 1000)}`)),
    h("nav", { class: "mainnav" }, NAV.map(([k, label, href]) => h("a", { href, class: k === active ? "on" : "", "data-key": "nav:" + k }, label))),
    h("div", { class: "chips" }, chip("运行中", sm.running, "running"), chip("排队", sm.queued, "queued"), chip("已完成", sm.done, "done"), chip("失败", sm.failed, "failed"), chip("疑似停滞", sm.stale, "stale"),
      chip("训练", bk.train, "plain"), chip("流水线执行", bk.run, "plain"), chip("评测", bk.eval, "plain")));

  const root = h("div", { id: "app", "data-key": "app" },
    h("div", { class: "wrap", "data-key": "wrap" }, header, S.offline ? h("div", { class: "banner", "data-key": "banner" }, "无法连接看板服务，正在重试…") : null,
      hist ? h("div", { class: "banner hist", "data-key": "histbanner" }, `正在回放历史（事件 #${st.at}）：${st.event ? st.event.label : ""}`) : null,
      renderPage(r)));

  if (lastRoute !== null && lastRoute !== r.key) {         // 换子页面：移除旧页面，避免与新页面叠在一起淡出
    const old = document.querySelector("#app .page");
    if (old) old.remove();
    window.scrollTo(0, 0);
  }
  lastRoute = r.key;
  patch(document.getElementById("app"), root);
}

export async function load() {
  const my = ++seq;
  const url = TL.idx == null ? "/api/state" : `/api/state?at=${TL.idx}`;
  try {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    const data = await r.json();
    if (my !== seq) return;          // 已有更新的请求，丢弃过期响应
    S.state = data; S.offline = false; S.lastOk = Date.now();
  } catch (e) {
    if (my !== seq) return;
    S.offline = true;
  }
  render();
}

async function pullTimeline() {
  try {
    const r = await fetch(`/api/timeline?since=${TL.events.length ? TL.events[TL.events.length - 1].i + 1 : 0}`, { cache: "no-store" });
    if (!r.ok) return;
    const d = await r.json();
    if (d.events.length || d.n !== TL.n || d.reset) setEvents(d.events, d.n, d.reset);
  } catch (e) { /* 忽略，下个周期重试 */ }
}

onAction("selnode", (key, id) => { S.sel[key] = id; render(); });
/* 数据图：在「分组清单」和「关系图」之间切换（记住每张图的选择） */
onAction("dsview", (key, mode) => { S.sel[`${key}#view`] = mode; render(); });
/* 模块里的热力图 / 流转图：切标签页、切选项、下载 */
onAction("bltab", (key, id) => { S.sel[`${key}#tab`] = id; render(); });
onAction("blopt", (k, v) => { S.sel[k] = /^[\d.]+$/.test(v) ? Number(v) : v; render(); });
onAction("bldl", (key, fmt) => downloadBlock(key, fmt));
/* 成果校验工作台的交互 */
onAction("rvview", (k) => setView(k));
onAction("rvrule", (id) => setRule(id));
onAction("rvpage", (p) => gotoPage(parseInt(p, 10)));
onAction("rvsel", (line, _a2, el, e) => {
  if (e.target.closest("button, a, video, audio, input, textarea")) return;       // 点按钮/媒体时不改选中
  RV.sel = parseInt(line, 10); render();
});
onAction("rvmark", (line, v, _el, e) => { e.stopPropagation(); RV.sel = parseInt(line, 10); mark(parseInt(line, 10), v); });
onAction("rvfacet", (id, v) => setFacet(id, v === "" ? "" : v));
onAction("rvclearf", () => clearFacets());
onAction("rvcell", (rowVal, colVal) => {           // 点矩阵格子：同时筛行分面和列分面
  const m = RV.data && RV.data.matrix;
  if (!m) return;
  const same = RV.picked[m.rows_id] === rowVal && RV.picked[m.cols_id] === colVal;
  RV.picked = {};
  if (!same) { RV.picked[m.rows_id] = rowVal; RV.picked[m.cols_id] = colVal; }
  RV.page = 0; RV.sel = null; fetchPage();
});
onAction("rvnote", (line, _a2, _el, e) => {
  e.stopPropagation();
  const ln = parseInt(line, 10), it = RV.data && RV.data.items.find((x) => x.line === ln);
  if (!it) return;
  const txt = window.prompt(`第 ${ln} 行的备注（留空则清除备注）`, (it.mark && it.mark.note) || "");
  if (txt !== null) mark(ln, (it.mark && it.mark.v) || "doubt", txt.slice(0, 500));
});
setOnChange(render);
window.addEventListener("hashchange", render);

async function loop() {
  await pullTimeline();
  if (TL.idx == null && !TL.playing) await load();
  setTimeout(loop, document.hidden ? POLL_MS * 4 : POLL_MS);
}

mount(document.getElementById("timeline"), { load });
load().then(loop);
