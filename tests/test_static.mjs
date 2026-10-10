/* 前端渲染的回归测试：需要 Node 18+（有 node 就跑，没有就跳过，Python 测试不依赖它）。
   用法：node tests/test_static.mjs

   为什么要有这个：static/*.js 是 ES 模块，浏览器里才会执行，写错（例如 const 先用后声明）只有打开页面才发现。
   这里把 util.js / store.js 换成最小桩，直接跑 drawGraph，能抓到语法错误和运行时错误。
   改动 static/ 之后请跑一次。 */
import { writeFileSync, mkdtempSync, rmSync, copyFileSync, readdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

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
for (const f of ["graph.js", "views.js", "transition.js"]) {
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

/* 注：review.js 直接用 document 绑键盘事件，Node 里没有 DOM，前端渲染不在这里测。
   分面统计 / 矩阵 / 筛选的逻辑由 tests/test_status_board.py 覆盖。 */

/* 热力图 / 状态流转图：几何是纯函数，块能渲染成 SVG，切标签页 / 选项有效 */
try {
  const Tr = await import(join(dir, "transition.js"));
  const { S } = await import(join(dir, "__stub.js"));
  const hm = {
    type: "heatmap", title: "转移矩阵", rows: [{ label: "START" }, { label: "甲" }, { label: "乙" }, { label: "空行" }],
    cols: [{ label: "甲" }, { label: "乙" }, { label: "END" }, { label: "空列" }],
    matrices: [
      { id: "all", name: "全部", values: [[0.7, 0.3, 0, 0], [0.2, 0.1, 0.7, 0], [0, 0, 1, 0], [0, 0, 0, 0]],
        counts: [[7, 3, 0, 0], [2, 1, 7, 0], [0, 0, 5, 0], [0, 0, 0, 0]], row_totals: [10, 10, 5, 0], n_dialogues: 10 },
      { id: "sub", name: "子集", values: [[1, 0, 0, 0], [0, 0, 1, 0], [0, 0, 1, 0], [0, 0, 0, 0]],
        counts: [[2, 0, 0, 0], [0, 0, 2, 0], [0, 0, 1, 0], [0, 0, 0, 0]], row_totals: [2, 2, 1, 0], n_dialogues: 2 }],
  };
  const M = Tr.heatmapModel(hm, "all");
  ok("热力图：全零的行和列默认隐藏", M.rows.length === 3 && M.cols.length === 3, `${M.rows.length}x${M.cols.length}`);
  ok("热力图：每格有数值、颜色和提示", M.cells.length === 9 && M.cells.every((c) => c.fill.startsWith("rgb(") && c.tip.includes("→")));
  const hi = M.cells.find((c) => c.v === 1), lo = M.cells.find((c) => c.v === 0.1);
  ok("热力图：值越大颜色越深、字变白", hi.fill !== lo.fill && hi.ink === "#ffffff" && lo.ink === "#222222");
  ok("热力图：格内数字两位小数", M.cells.find((c) => c.v === 0.7).text === "0.70");
  ok("热力图：切到「次数」显示计数", Tr.heatmapModel(hm, "all", "n").cells.find((c) => c.v === 0.7).text === "7");
  ok("热力图：色条最大值 = 矩阵最大值", M.vmax === 1);
  ok("热力图：不存在的矩阵返回 null", Tr.heatmapModel(hm, "nope") === null);
  ok("热力图：hide_empty=false 保留全部行列", Tr.heatmapModel({ ...hm, hide_empty: false }, "all").rows.length === 4);
  ok("色阶两端：0 近白，1 近深蓝", Tr.heatColor(0) === "rgb(247,251,255)" && Tr.heatColor(1) === "rgb(8,48,107)" && Tr.heatColor(NaN) === Tr.heatColor(0));

  S.sel = {};
  const el = Tr.heatmapBlock(hm, "md-0");
  ok("热力图块：渲染出 svg + 标签页 + 下载按钮", all(el).some((n) => n.tag === "svg") && all(el).filter((n) => n.attrs && n.attrs["data-act"] === "bltab").length === 2
    && all(el).filter((n) => n.attrs && n.attrs["data-act"] === "bldl").length === 2);
  ok("热力图块：svg 自带尺寸和白底（下载后能独立打开）", all(el).some((n) => n.tag === "svg" && n.attrs.width > 0 && n.attrs.xmlns) && all(el).some((n) => n.tag === "rect" && n.attrs.fill === "#ffffff"));
  const t1 = texts(el);
  ok("热力图块：显示对话段数", t1.some((x) => x.includes("10 段对话")), t1.slice(-4).join("|"));
  S.sel["md-0#tab"] = "sub";
  ok("热力图块：切标签页后换成另一张矩阵", texts(Tr.heatmapBlock(hm, "md-0")).some((x) => x.includes("2 段对话")));
  S.sel["md-0#tab"] = "不存在";
  ok("热力图块：标签页不存在时回到第一张", texts(Tr.heatmapBlock(hm, "md-0")).some((x) => x.includes("10 段对话")));

  const fl = {
    type: "flow", title: "拓扑", threshold: 0.05,
    nodes: [{ id: "S", label: "START", col: 0, row: 1 }, { id: "A", label: "甲", col: 1, row: 0 }, { id: "B", label: "乙", col: 1, row: 1 },
            { id: "C", label: "丙", col: 2, row: 0 }, { id: "E", label: "END", col: 3, row: 1 }],
    groups: [{ id: "G", label: "甲乙", nodes: ["A", "B"] }],
    edges: [{ from: "S", to: "A", w: { all: 0.6, x: 0 } }, { from: "S", to: "B", w: { all: 0.01, x: 0 } }, { from: "A", to: "C", w: { all: 0.5, x: 0 } },
            { from: "C", to: "C", w: { all: 0.3, x: 0 } }, { from: "G", to: "E", w: { all: 0.8, x: 0 }, bend: 50, note: "说明" }],
    phases: [{ col: 0, label: "开始" }], matrices: [{ id: "all", name: "全部" }, { id: "x", name: "无" }],
  };
  const F = Tr.flowModel(fl, "all");
  ok("流转图：节点、分组、边都排出来了", F.nodes.length === 5 && F.groups.length === 1 && F.edges.length === 5);
  ok("流转图：分组框把成员节点包住", (() => { const g = F.groups[0], a = F.nodes[1]; return g.x < a.x && g.y < a.y && g.x + g.w > a.x + a.w && g.y + g.h > a.y + a.h; })());
  ok("流转图：低于阈值的边不标数字，但仍画出（极细）", F.edges[1].shown === false && F.edges[1].dashed === false);
  ok("流转图：概率越大线越粗", F.edges[0].width > F.edges[2].width && F.edges[2].width > F.edges[1].width);
  ok("流转图：自环画在节点上方", F.edges[3].self && /^M[\d.]+,[\d.]+ C/.test(F.edges[3].d));
  ok("流转图：连到分组的边用分组框的边缘", F.edges[4].d.startsWith(`M${F.groups[0].x + F.groups[0].w},`));
  ok("流转图：这张矩阵里为 0 的边画成虚线", Tr.flowModel(fl, "x").edges.every((e) => e.dashed));
  ok("流转图：边带提示文字", F.edges[4].tip.includes("说明") && F.edges[4].tip.includes("80.0%"));
  const fe = Tr.flowBlock(fl, "md-1");
  ok("流转图块：渲染出 svg、箭头、阈值选项", all(fe).some((n) => n.tag === "svg") && all(fe).some((n) => n.tag === "marker")
    && all(fe).filter((n) => n.attrs && n.attrs["data-act"] === "blopt").length === 5);
  S.sel["md-1#thr"] = 0.7;
  ok("流转图块：提高阈值后标注的边变少", all(Tr.flowBlock(fl, "md-1")).filter((n) => n.tag === "text" && n.attrs["paint-order"]).length === 1);
} catch (e) {
  ok("热力图 / 流转图", false, e.stack || e.message);
}

try {
  const V = await import(join(dir, "views.js"));
  const m = { id: "md", title: "模块", blocks: [{ type: "heatmap", rows: [{ label: "a" }], cols: [{ label: "b" }], matrices: [{ id: "all", name: "x", values: [[0.5]] }] },
    { type: "flow", nodes: [{ id: "n", label: "N", col: 0, row: 0 }], edges: [], matrices: [{ id: "all", name: "x" }] }] };
  const el = V.moduleCard(m);
  ok("模块卡片里能放热力图和流转图", all(el).filter((n) => n.tag === "svg").length === 2 && !texts(el).some((x) => x.includes("未知的模块类型")));
} catch (e) {
  ok("模块卡片里能放热力图和流转图", false, e.stack || e.message);
}

try {
  const Tr2 = await import(join(dir, "transition.js"));
  const V2 = await import(join(dir, "views.js"));
  const tree = { type: "flow", no_threshold: true, threshold: 0, nodes: [{ id: "a", label: "A", col: 0, row: 0 }, { id: "b", label: "B", col: 1, row: 0 }],
    edges: [{ from: "a", to: "b", w: 0.5, label: "28%", tip: "自定义提示" }], matrices: [] };
  const M = Tr2.flowModel(tree, "x");
  ok("流转图：边可以自带标签和提示", M.edges[0].label === "28%" && M.edges[0].tip === "自定义提示");
  ok("流转图块：no_threshold 时不画阈值选项", all(Tr2.flowBlock(tree, "md-t")).filter((n) => n.attrs && n.attrs["data-act"] === "blopt").length === 0);
  const g = { id: "g", nodes: [], groups: [], edges: [] };
  const node = { id: "n", name: "步骤", blocks: [{ type: "text", text: "逻辑树说明" }, tree] };
  const el = V2.pipeNodeDetail(g, node, true);
  ok("Pipeline 步骤详情：步骤里的展示块被渲染", texts(el).includes("逻辑树说明") && all(el).some((n) => n.tag === "svg"));
} catch (e) {
  ok("流转图自带标签 / 步骤内展示块", false, e.stack || e.message);
}

rmSync(dir, { recursive: true, force: true });
console.log(fails ? `\n${fails} 项失败` : "\n全部通过");
process.exit(fails ? 1 : 0);
