/* 前端渲染的回归测试：需要 Node 18+（有 node 就跑，没有就跳过，Python 测试不依赖它）。
   用法：node tests/test_static.mjs

   为什么要有这个：static/*.js 是 ES 模块，浏览器里才会执行，写错（例如 const 先用后声明）只有打开页面才发现。
   这里把 util.js / store.js 换成最小桩，直接跑 drawGraph，能抓到语法错误和运行时错误。
   改动 static/ 之后请跑一次。 */
import { writeFileSync, mkdtempSync, rmSync, copyFileSync, readdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
let fails = 0;
const ok = (name, cond, extra = "") => {
  console.log(`${cond ? "ok  " : "FAIL"} ${name}${cond ? "" : "  " + extra}`);
  if (!cond) fails += 1;
};

/* util.js / store.js 的最小桩：只实现被 drawGraph 用到的部分，不引入任何 DOM。 */
const STUB = `
const flat = (kids) => kids.flatMap((k) => (Array.isArray(k) ? flat(k) : [k]))
  .filter((k) => k !== null && k !== undefined && k !== false && k !== "");
export const h = (tag, attrs, ...kids) => { if (tag == null || tag === false) return null;
  if (typeof tag !== "string") throw new Error("h: 标签不是字符串: " + tag);
  return { tag, attrs: attrs || {}, kids: flat(kids) }; };
export const s = h;
export const fmtBytes = (x) => String(x);
export const fmtNum = (x) => String(x);
export const fmtPct = (x) => String(x);
export const fmtDur = (x) => String(x);
export const fmtTime = (x) => String(x);
export const clip = (str, n) => String(str ?? "").slice(0, n);
export const badge = () => h("span", {}, "b");
export const bar = () => h("div", {});
export const tag = (x) => h("span", {}, String(x));
export const assetUrl = (x) => String(x);
export const FOLDED = new Set();                                    // 收纳：测试里用它模拟「已收起」
export const isFolded = (k) => FOLDED.has(k);
export const foldBtn = (k) => h("button", { class: "fold" }, FOLDED.has(k) ? "▸" : "▾");
export const foldBody = (k, kids) => (FOLDED.has(k) ? null : kids);
export const NODE_ST = { ready: "就绪", building: "生成中", stale: "过期", missing: "缺失", failed: "失败",
                         pending: "排队", running: "进行中", done: "完成", skipped: "跳过" };
export const ITEM_ST = { pending: "排队", running: "进行中", done: "完成", failed: "失败", skipped: "跳过" };
export const TASK_ST = {}; export const TASK_KIND = {}; export const PIPE_KIND = {}; export const DATA_KIND = {};
export const LOC_ROLE = {}; export const REPLICA = {}; export const META_LABEL = {}; export const META_HIDE = new Set();
export const S = { state: { pipelines: [], tasks: [], datasets: [] }, sel: {} };
export const resolveRef = () => null;
export const findGraph = () => null;
export const link = (k, id, t) => h("a", {}, String(t));
export const hrefOf = (k, id) => "#/" + k + "/" + id;
export const noteLine = (t) => h("span", {}, String(t));
export const exTag = () => null;
export const legend = () => h("div", {}, "legend");
`;

const dir = mkdtempSync(join(tmpdir(), "sb-static-"));
for (const f of readdirSync(join(ROOT, "static"))) {
  if (f.endsWith(".js")) copyFileSync(join(ROOT, "static", f), join(dir, f));
}
writeFileSync(join(dir, "__stub.js"), STUB);
writeFileSync(join(dir, "package.json"), '{"type":"module"}');
for (const f of ["graph.js", "views.js"]) {
  const p = join(dir, f);
  const src = (await import("node:fs")).readFileSync(p, "utf8")
    .replace(/from "\.\/util\.js"/g, 'from "./__stub.js"')
    .replace(/from "\.\/store\.js"/g, 'from "./__stub.js"');
  writeFileSync(p, src);
}

const G = await import(join(dir, "graph.js"));
const all = (el, out = []) => { if (!el || typeof el !== "object") return out; out.push(el);
  for (const k of el.kids || []) all(k, out); return out; };
const texts = (el) => all(el).flatMap((n) => (n.kids || []).filter((k) => typeof k === "string"));

const dataGraph = () => ({
  id: "g", title: "数据图", description: "d",
  groups: [{ id: "raw", name: "原始", desc: "只读", nodes: ["a"] },
           { id: "rel", name: "发布版本", desc: "final 才可用", nodes: ["c"] }],
  nodes: [
    { id: "a", name: "A 原始数据", kind: "raw", status: "ready", brief: "A：100 题",
      locations: [{ label: "主", role: "primary", path: "/tmp/a" }] },
    { id: "c", name: "C v2.0", kind: "processed", status: "ready", brief: "C v2.0：300 条视频",
      meta: { count: 300, count_unit: "条视频" }, locations: [{ label: "主", role: "primary", path: "/tmp/c" }] },
  ],
  edges: [{ from: "a", to: "c", label: "排页、渲染" }],
});

try {
  const out = G.drawGraph("datasets", dataGraph(), "a", "datasets/g");
  const t = texts(out.el);
  ok("数据图：带分组 + brief 能渲染", out.el != null);
  ok("数据图：节点上的 brief 显示在图上", t.some((x) => x.includes("A：100 题")), t.join(" / ").slice(0, 120));
  ok("数据图：返回按流向排好的节点顺序", Array.isArray(out.order) && out.order.length === 2);
} catch (e) {
  ok("数据图：带分组 + brief 能渲染", false, e.message);
}

try {
  const out = G.drawGraph("datasets", { id: "g2", title: "t", description: "d",
    nodes: [{ id: "x", name: "X", kind: "raw" }], edges: [] }, null, "datasets/g2");
  ok("数据图：旧格式（无分组、无 brief）不受影响", out.el != null);
} catch (e) {
  ok("数据图：旧格式（无分组、无 brief）不受影响", false, e.message);
}

try {
  const out = G.drawGraph("pipelines", { id: "p", title: "t", description: "d",
    nodes: [{ id: "s", name: "S", kind: "process", role: "作用" }], edges: [],
    groups: [{ id: "g", name: "阶段", nodes: ["s"] }] }, null, "pipelines/p");
  const t = texts(out.el);
  ok("Pipeline 图：结构与作用不受影响", out.el != null && t.some((x) => x.includes("作用")));
} catch (e) {
  ok("Pipeline 图：结构与作用不受影响", false, e.message);
}

/* 分组清单（layout: "list"）：一组一块，块里是表格；不画关系图 */
try {
  const V = await import(join(dir, "views.js"));
  const g = dataGraph();
  g.layout = "list";
  const el = V.datasetCard(g);
  const cards = all(el).filter((n) => n.attrs && String(n.attrs.class).includes("dgroup"));
  const t = texts(el);
  ok("分组清单：按 groups 分块", cards.length === 2, "块数 " + cards.length);
  ok("分组清单：块标题是分组名", t.includes("原始") && t.includes("发布版本"));
  ok("分组清单：每份数据带一句话说明", t.some((x) => x.includes("C v2.0：300 条视频")));
  ok("分组清单：不画关系图", all(el).every((n) => n.tag !== "svg"));
  ok("分组清单：提供切回关系图的入口", all(el).some((n) => n.attrs && n.attrs["data-act"] === "dsview"));

  const g2 = dataGraph();
  const el2 = V.datasetCard(g2);          // 默认仍是关系图
  ok("默认仍是关系图", all(el2).some((n) => n.tag === "svg"));
} catch (e) {
  ok("分组清单渲染", false, e.message);
}

/* 卡片收纳：收起后正文（关系图 / 分组清单表格）不渲染，标题行保留 */
try {
  const U = await import(pathToFileURL(join(dir, "__stub.js")).href);
  const V = await import(join(dir, "views.js"));
  const gk = { id: "gx", title: "收纳测试", nodes: [{ id: "a", name: "A", status: "ready" }], edges: [], groups: [{ name: "组一", nodes: ["a"] }] };
  U.FOLDED.add("ds:gx");
  const folded = V.datasetCard(gk);
  ok("收纳：数据图收起后不画关系图", !all(folded).some((n) => n.tag === "svg"));
  ok("收纳：数据图收起后标题仍在", JSON.stringify(folded).includes("收纳测试"));
  U.FOLDED.clear();
  ok("收纳：展开后恢复关系图", all(V.datasetCard(gk)).some((n) => n.tag === "svg"));
  U.FOLDED.add("dg:gx:组一");
  const lst = V.datasetList(gk, null, "datasets/gx", 1);
  ok("收纳：分组块收起后没有表格", !all(lst).some((n) => n.tag === "table") && JSON.stringify(lst).includes("组一"));
  U.FOLDED.clear();
} catch (e) {
  ok("收纳渲染", false, e.message);
}

/* 注：review.js 直接用 document 绑键盘事件，Node 里没有 DOM，前端渲染不在这里测。
   分面统计 / 矩阵 / 筛选的逻辑由 tests/test_status_board.py 覆盖。 */

rmSync(dir, { recursive: true, force: true });
console.log(fails ? `\n${fails} 项失败` : "\n全部通过");
process.exit(fails ? 1 : 0);
