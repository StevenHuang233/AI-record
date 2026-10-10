/* 端到端：用 jsdom 加载真实页面 + 真实 JS（ES 模块），对着真实服务点一遍图库。
   不是浏览器（没有布局、没有视频解码），但 DOM 构建、事件、fetch、路由、patch 都是真的。
   用法：先起服务（带 --asset-root），再 node tests/e2e_gallery.mjs http://127.0.0.1:8767 <校验集id>
   需要 jsdom（npm i jsdom），不是仓库依赖：没装就跳过。 */
let JSDOM, VirtualConsole;
try { ({ JSDOM, VirtualConsole } = await import("jsdom")); }
catch (e) { console.log("没有安装 jsdom（npm i jsdom），跳过图库端到端测试"); process.exit(0); }

const base = process.argv[2] || "http://127.0.0.1:8767";
const rid = process.argv[3] || "gal-mmtutor-v2_0";
const errors = [];
let fails = 0;
const ok = (name, cond, extra = "") => { console.log(`${cond ? "ok  " : "FAIL"} ${name}${cond ? "" : "  " + extra}`); if (!cond) fails += 1; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms = 20000, step = 100) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { const v = fn(); if (v) return v; await sleep(step); }
  return null;
}

/* jsdom 不支持 <script type=module>，Node 也不能直接 import http 地址：
   把服务上真实的静态文件取回临时目录，把 jsdom 的 window / document 挂成全局，再 import 真实的 app.js。 */
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
const html = await (await fetch(base + "/")).text();
const modDir = mkdtempSync(join(tmpdir(), "sb-e2e-"));
writeFileSync(join(modDir, "package.json"), '{"type":"module"}');
for (const f of ["app.js", "util.js", "store.js", "graph.js", "views.js", "pages.js", "review.js", "gallery.js", "resources.js", "timeline.js"]) {
  const r = await fetch(`${base}/${f}`);
  if (r.ok) writeFileSync(join(modDir, f), await r.text());
}
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => errors.push("jsdomError: " + e.message));
vc.on("error", (e) => errors.push("console.error: " + e));
const dom = new JSDOM(html.replace(/<script[^>]*><\/script>/g, ""), { url: base + `/#/review/${rid}`, pretendToBeVisual: true, virtualConsole: vc });
const w = dom.window;
const def = (k, v) => { try { Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true }); } catch (e) { /* 只读的跳过 */ } };
for (const k of ["document", "location", "history", "HTMLElement", "Node", "Element", "navigator", "CSS", "requestAnimationFrame", "localStorage", "Event", "MouseEvent", "KeyboardEvent"]) def(k, w[k]);
def("window", w);
const nativeFetch = globalThis.fetch;
globalThis.fetch = (u, o) => nativeFetch(typeof u === "string" && u.startsWith("/") ? base + u : u, o);
w.fetch = globalThis.fetch;
if (w.HTMLDialogElement) {
  w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  w.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
}
let ct = new WeakMap();                                            // jsdom 的 <video> 没有真实播放：自己存 currentTime
Object.defineProperty(w.HTMLMediaElement.prototype, "currentTime", { configurable: true, get() { return ct.get(this) || 0; }, set(v) { ct.set(this, v); } });
w.HTMLMediaElement.prototype.play = () => Promise.resolve();
w.HTMLMediaElement.prototype.load = () => {};
w.Element.prototype.scrollIntoView = () => {};
w.prompt = () => null;

try {
  await import(join(modDir, "app.js"));
} catch (e) { errors.push("加载 app.js 失败：" + e.stack); }

const $ = (sel, root = w.document) => root.querySelector(sel);
const $$ = (sel, root = w.document) => [...root.querySelectorAll(sel)];
const click = (el) => el.dispatchEvent(new w.MouseEvent("click", { bubbles: true, cancelable: true }));

const gal = await until(() => $("#gal-root .glayout"), 30000);
ok("图库页渲染出来了（侧栏 + 主区）", !!gal, "没有 .glayout；errors=" + errors.slice(0, 2).join(" | "));
if (!gal) { console.log(errors.join("\n")); process.exit(1); }

const matrix = await until(() => $$("#gal-root table.matrix td.c").length >= 4, 30000);
ok("8 格矩阵渲染（含条数与时长中位）", !!matrix && $$("#gal-root table.matrix td.c .cn").length >= 4);
ok("风格分布出现", $$("#gal-root .bars .gbar").length > 0);
const cards = await until(() => $$("#gal-root .gcard").length >= 1, 30000);
ok("卡片网格渲染", !!cards, "没有 .gcard");
const n0 = $$("#gal-root .gcard").length;
ok("第一页 24 张", n0 === 24, String(n0));
ok("卡片带缩略图与时长角标", !!$("#gal-root .gcard .thumb img") && !!$("#gal-root .gcard .gbadge.br"));
ok("侧栏列出多个批次且当前高亮", $$("#gal-root aside .batch").length >= 5 && !!$("#gal-root aside .batch.on"));

/* 加载更多 */
const more = $("#gal-root .more button");
ok("有「加载更多」", more && !more.hidden);
click(more);
ok("点加载更多后卡片变多", !!(await until(() => $$("#gal-root .gcard").length > n0, 15000)), String($$("#gal-root .gcard").length));

/* 点格子筛选 */
const cell = $$("#gal-root table.matrix td.c:not(.empty)")[1];
const cellName = cell.dataset.cell + "·" + cell.dataset.turns;
click(cell);
await until(() => $("#gal-root .count")?.textContent.includes("（"), 15000);
ok("点矩阵格子后按格筛选（计数里带格名）", $("#gal-root .count").textContent.includes(cellName), $("#gal-root .count").textContent);
ok("被点的格子高亮", cell.classList.contains("on") || $$("#gal-root td.c.on").length === 1);
const tagsOk = $$("#gal-root .gcard .gtag.cell").every((t) => t.textContent === cellName.replace("·", " · "));
ok("筛选后每张卡都属于这个格", tagsOk);
click($("#gal-root .linkbtn"));
await until(() => !$("#gal-root .count").textContent.includes("（"), 15000);
ok("清空筛选后恢复", !$("#gal-root .count").textContent.includes("（"));

/* 页数分段、排序 */
click($$("#gal-root .filters .seg")[1].children[2]);        // 页数 → 多页
await sleep(1200);
const multi = $$("#gal-root .gcard .badge, #gal-root .gcard .gbadge.tl").map((b) => b.textContent);
ok("页数=多页：每张卡都显示 ≥2 页", multi.length > 0 && multi.every((t) => /^([2-9]|\d\d+) 页/.test(t)), multi.slice(0, 3).join("|"));
click($$("#gal-root .filters .seg")[1].children[0]);
const sort = $$("#gal-root .filters select")[1];
sort.value = "dur_desc"; sort.dispatchEvent(new w.Event("change", { bubbles: true }));
await sleep(1200);
const durs = $$("#gal-root .gcard .gbadge.br").map((b) => parseFloat(b.textContent));
ok("按时长降序", durs.length > 3 && durs.every((d, i) => i === 0 || durs[i - 1] >= d - 1e-9), durs.slice(0, 6).join(","));
sort.value = ""; sort.dispatchEvent(new w.Event("change", { bubbles: true }));
await sleep(800);

/* 搜索 */
const q = $("#gal-root #gq");
q.value = "difference of squares"; q.dispatchEvent(new w.Event("input", { bubbles: true }));
const wantQ = (await (await fetch(`${base}/api/review/${rid}/gallery?q=${encodeURIComponent("difference of squares")}&limit=1`)).json()).count;
await until(() => $("#gal-root .count")?.textContent.startsWith(`${wantQ} 条`), 15000);
ok(`搜索防抖后生效（和接口一致：${wantQ} 条）`, wantQ > 0 && $("#gal-root .count").textContent.startsWith(`${wantQ} 条`), $("#gal-root .count").textContent);
q.value = ""; q.dispatchEvent(new w.Event("input", { bubbles: true }));
await sleep(800);

/* 抽屉 */
click($("#gal-root .gcard"));
const drawer = await until(() => { const d = $(".gal-drawer"); return d && !d.hidden && $(".gal-drawer .player") ? d : null; }, 15000);
ok("点卡片打开详情抽屉", !!drawer);
ok("抽屉里有视频播放器", !!$(".gal-drawer .player video"));
ok("抽屉里有抽帧", $$(".gal-drawer .frames img").length >= 1);
ok("抽屉里有时间轴（动作块 / 可见区间 / 提问标记）", !!$(".gal-drawer .gtl .gblk") && !!$(".gal-drawer .gtl .gwin") && !!$(".gal-drawer .gtl .askm"));
ok("抽屉里有提问列表", $$(".gal-drawer .asks div").length >= 1);
ok("抽屉里有目标题与参考答案", $$(".gal-drawer .longtext").length >= 2);
ok("抽屉里有动作变化 / 每页段信息表", $$(".gal-drawer table.segs").length >= 3);
ok("抽屉里有拍摄参数", $$(".gal-drawer .gkv span").length >= 8);
ok("地址栏带 ?line=", w.location.hash.includes("?line="), w.location.hash);

/* 点提问 → 视频跳转 */
const video = $(".gal-drawer video");
const ask = $(".gal-drawer .asks div");
click(ask);
ok("点提问行 → 视频跳到提问时刻", Math.abs(video.currentTime - Number(ask.dataset.t)) < 0.01, `${video.currentTime} vs ${ask.dataset.t}`);

/* 键盘 → 下一条、标记 */
const title1 = $(".gal-drawer .dhead .t").textContent;
w.document.dispatchEvent(new w.KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
await until(() => $(".gal-drawer .dhead .t")?.textContent !== title1, 15000);
ok("→ 切到下一条", $(".gal-drawer .dhead .t").textContent !== title1);
w.document.dispatchEvent(new w.KeyboardEvent("keydown", { key: "1", bubbles: true }));
await until(() => $(".gal-drawer .gvbtn.on.v-pass"), 15000);
ok("按 1 标合格（按钮高亮）", !!$(".gal-drawer .gvbtn.on.v-pass"));
await sleep(500);
ok("卡片上出现「合格」角标", $$("#gal-root .gbadge.mk.mk-pass").length === 1);
const chk = await (await fetch(`${base}/api/review/${rid}/gallery?view=mpass&limit=5`)).json();
ok("服务端也记下了（已标合格 = 1）", chk.count === 1 && chk.marked.pass >= 1, JSON.stringify(chk.marked));
w.document.dispatchEvent(new w.KeyboardEvent("keydown", { key: "1", bubbles: true }));    // 再按一次 = 取消
await until(() => !$(".gal-drawer .gvbtn.on.v-pass"), 15000);
await sleep(600);
const chk2 = await (await fetch(`${base}/api/review/${rid}/gallery?view=mpass&limit=5`)).json();
ok("再按一次取消标记（服务端归零）", chk2.count === 0, JSON.stringify(chk2.marked));

/* 关闭 */
w.document.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
ok("Esc 关闭抽屉", $(".gal-drawer").hidden === true);

/* 看板每 2.5 秒轮询重绘：图库里的内容不能被洗掉 */
const before = $$("#gal-root .gcard").length;
await sleep(6000);
ok("看板轮询重绘后图库内容还在", $$("#gal-root .gcard").length === before && !!$("#gal-root table.matrix td.c"), `${before} → ${$$("#gal-root .gcard").length}`);

/* 切换批次 */
const other = $$("#gal-root aside .batch").find((b) => !b.classList.contains("on"));
click(other);
await sleep(300);
ok("点侧栏切换批次（地址栏变了）", !w.location.hash.endsWith(rid), w.location.hash);

/* 页面层面：不能有未捕获的脚本错误 */
const real = errors.filter((e) => !/Not implemented|navigation|Could not load/i.test(e));
ok("没有脚本错误", real.length === 0, real.slice(0, 3).join(" | "));
console.log(fails ? `\n${fails} 项失败` : "\n全部通过");
process.exit(fails ? 1 : 0);
