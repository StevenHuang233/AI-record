"use strict";
/* 时间线控件：常驻 DOM（不参与 patch，拖动时不会被重建）。
   拖动/播放只改变「看哪个时刻的状态」，真正的动画由 patch + CSS 过渡完成。 */
import { h, fmtTime } from "./util.js";

export const TL = {
  events: [],      // 服务端事件（只含 i/ts/type/label/sig/note/tags）
  n: 0,            // 服务端事件总数
  idx: null,       // 当前查看的事件序号 i；null = 实时
  playing: false,
  speed: 1,
  onlyKey: false,
};

let E = {};          // 控件元素
let hooks = { load: async () => {} };
let timer = null;

const visible = () => (TL.onlyKey ? TL.events.filter((e) => e.sig) : TL.events);

function currentK() {
  const vis = visible();
  if (TL.idx == null) return vis.length;
  let k = -1;
  for (let j = 0; j < vis.length; j++) if (vis[j].i <= TL.idx) k = j;
  return Math.max(0, k);
}

async function gotoK(k) {
  const vis = visible();
  TL.idx = k >= vis.length ? null : vis[Math.max(0, k)].i;
  sync();
  await hooks.load();
}

function stop() {
  TL.playing = false;
  clearTimeout(timer);
  sync();
}

async function tick() {
  if (!TL.playing) return;
  const vis = visible(), k = currentK();
  if (k >= vis.length - 1) { stop(); return; }
  await gotoK(k + 1);
  if (TL.playing) timer = setTimeout(tick, 800 / TL.speed);
}

function play() {
  if (TL.playing) { stop(); return; }
  const vis = visible();
  if (!vis.length) return;
  if (TL.idx == null || currentK() >= vis.length - 1) TL.idx = vis[0].i;   // 从头开始播
  TL.playing = true;
  sync();
  hooks.load().then(() => { if (TL.playing) timer = setTimeout(tick, 800 / TL.speed); });
}

export function mount(root, h2) {
  hooks = h2;
  E.range = h("input", { type: "range", min: 0, max: 0, value: 0, step: 1, class: "tl-range", "aria-label": "时间线" });
  E.marks = h("div", { class: "tl-marks" });
  E.play = h("button", { class: "tl-btn", title: "播放 / 暂停" }, "▶");
  E.prev = h("button", { class: "tl-btn", title: "上一个事件" }, "◀");
  E.next = h("button", { class: "tl-btn", title: "下一个事件" }, "▶|");
  E.live = h("button", { class: "tl-btn live", title: "回到实时" }, "实时");
  E.speed = h("select", { class: "tl-sel", title: "播放速度" }, [0.5, 1, 2, 4, 8].map((v) => h("option", { value: v, selected: v === 1 }, v + "x")));
  E.key = h("input", { type: "checkbox", id: "tl-key" });
  E.info = h("div", { class: "tl-info" });
  E.chips = h("div", { class: "tl-chips" });
  root.replaceChildren(
    h("div", { class: "tl-row" }, E.prev, E.play, E.next, h("div", { class: "tl-track" }, E.range, E.marks), E.live, E.speed,
      h("label", { class: "tl-lbl", for: "tl-key" }, E.key, "仅关键事件")),
    h("div", { class: "tl-sub" }, E.info, E.chips));
  E.range.addEventListener("input", () => { if (TL.playing) stop(); gotoK(+E.range.value); });
  E.prev.addEventListener("click", () => { stop(); gotoK(Math.max(0, currentK() - 1)); });
  E.next.addEventListener("click", () => { stop(); gotoK(currentK() + 1); });
  E.play.addEventListener("click", play);
  E.live.addEventListener("click", () => { stop(); TL.idx = null; sync(); hooks.load(); });
  E.speed.addEventListener("change", () => { TL.speed = +E.speed.value; });
  E.key.addEventListener("change", () => { TL.onlyKey = E.key.checked; sync(); });
  sync();
}

export function setEvents(list, n, reset) {
  if (reset) TL.events = [];
  const have = TL.events.length ? TL.events[TL.events.length - 1].i : -1;
  for (const e of list) if (e.i > have) TL.events.push(e);
  TL.n = n;
  sync();
}

export function sync() {
  if (!E.range) return;
  const vis = visible(), k = currentK();
  E.range.max = vis.length;
  E.range.value = k;
  E.range.disabled = !vis.length;
  for (const b of [E.play, E.prev, E.next]) b.disabled = !vis.length;
  E.play.textContent = TL.playing ? "⏸" : "▶";
  E.live.classList.toggle("on", TL.idx == null);
  document.body.classList.toggle("hist", TL.idx != null);

  const marks = vis.map((e, j) => (e.type === "milestone" ? [e, j] : null)).filter(Boolean);
  E.marks.replaceChildren(...marks.map(([e, j]) => h("button", { class: "tl-flag", title: e.label, style: { left: (100 * j / Math.max(1, vis.length)) + "%" },
    onclick: () => { stop(); gotoK(j); } }, "⚑")));
  E.chips.replaceChildren(...marks.slice(-8).map(([e, j]) => h("button", { class: `tl-chip ${TL.idx === e.i ? "on" : ""}`, title: e.note || e.label, onclick: () => { stop(); gotoK(j); } }, e.label)));

  if (!vis.length) { E.info.textContent = "暂无历史：有任务、定义变化或里程碑后，这里会出现可回放的时间线。"; return; }
  if (TL.idx == null) { E.info.textContent = `实时 · 共 ${TL.n} 个事件 · 拖动滑块或点播放，回看变化过程`; return; }
  const ev = TL.events.find((e) => e.i === TL.idx) || vis[Math.max(0, k)];
  E.info.replaceChildren(h("b", {}, `${Math.min(k + 1, vis.length)} / ${vis.length}`), " · ", fmtTime(ev.ts), " · ", ev.type === "milestone" ? "⚑ " : "", ev.label,
    ev.note ? h("span", { class: "tl-note" }, " — " + ev.note) : null);
}
