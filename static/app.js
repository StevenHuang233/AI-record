"use strict";
/* 入口：取状态 → 按 URL hash 路由出当前子页面 → 生成离线 DOM → patch 进页面。
   实时模式每 2.5 秒轮询；时间线模式取历史时刻的状态。切换子页面时清空旧页面再绘制（新页面淡入）。 */
import { h, patch, onAction, fmtTime } from "./util.js";
import { S } from "./store.js";
import { parseRoute, navKeyOf, renderPage } from "./pages.js";
import { TL, mount, setEvents } from "./timeline.js";

const POLL_MS = 2500;
const NAV = [["home", "首页", "#/"], ["train", "训练", "#/train"], ["run", "流水线执行", "#/run"], ["eval", "评测", "#/eval"],
             ["pipelines", "Pipeline", "#/pipelines"], ["datasets", "数据管理", "#/datasets"], ["modules", "定制模块", "#/modules"]];
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
window.addEventListener("hashchange", render);

async function loop() {
  await pullTimeline();
  if (TL.idx == null && !TL.playing) await load();
  setTimeout(loop, document.hidden ? POLL_MS * 4 : POLL_MS);
}

mount(document.getElementById("timeline"), { load });
load().then(loop);
