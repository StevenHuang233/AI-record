"use strict";
/* 子页面（嵌套）：用 URL hash 做路由，页面之间用链接/面包屑跳转，浏览器前进后退可用。
   #/                          首页总览
   #/train | run | eval | job  某类任务列表          #/task/<任务id>        任务详情
   #/pipelines                 Pipeline 列表          #/pipelines/<图>       结构图详情     #/pipelines/<图>/<步骤>  步骤子页面
   #/datasets                  数据图列表             #/datasets/<图>        数据图详情     #/datasets/<图>/<资产>   数据资产子页面
   #/modules                   展示模块列表           #/modules/<id>         模块详情 */
import { h, bar, badge, clip, fmtBytes, fmtDur, fmtNum, fmtPct, fmtTime, TASK_ST, NODE_ST, TASK_KIND, DATA_KIND } from "./util.js";
import { S, findGraph } from "./store.js";
import { orderedNodes, nodeSize } from "./graph.js";
import { taskCard, pipelineCard, datasetCard, moduleCard, pipeNodeDetail, dataNodeDetail, hrefOf, exTag } from "./views.js";
import { TL } from "./timeline.js";
import { reviewWorkbench, enter as reviewEnter, leave as reviewLeave } from "./review.js";

const TASK_KINDS = ["train", "run", "eval", "job"];
const LISTS = { train: "训练", run: "流水线执行", eval: "评测", job: "其他任务", pipelines: "Pipeline", datasets: "数据管理", modules: "定制模块", review: "成果校验" };
const REVIEW_KIND = { model_output: "模型生成结果", cleaned_data: "清洗后的数据", generic: "成果" };

export function parseRoute(hash) {
  const raw = (hash || "").replace(/^#\/?/, "");
  const seg = raw ? raw.split("/").map((x) => { try { return decodeURIComponent(x); } catch (e) { return x; } }) : [];
  const [a, b, c] = seg;
  if (!a) return { page: "home", key: "home" };
  if (TASK_KINDS.includes(a) && !b) return { page: "tasks", kind: a, key: a };
  if (a === "task" && b) return { page: "task", id: b, key: `task/${b}` };
  if (a === "review") return b ? { page: "review", id: b, key: `review/${b}` } : { page: "reviews", key: "review" };
  if (["pipelines", "datasets", "modules"].includes(a)) {
    if (!b) return { page: "list", kind: a, key: a };
    if (c && a !== "modules") return { page: "node", kind: a, gid: b, nid: c, key: `${a}/${b}/${c}` };
    return { page: "detail", kind: a, id: b, key: `${a}/${b}` };
  }
  return { page: "notfound", key: "404" };
}

/* 一级导航：用于顶栏高亮 */
export function navKeyOf(r) {
  if (r.page === "home") return "home";
  if (r.page === "tasks") return r.kind;
  if (r.page === "task") { const t = S.state && S.state.tasks.find((x) => x.id === r.id); return t ? (t.kind || "job") : "home"; }
  if (r.page === "reviews" || r.page === "review") return "review";
  if (r.kind) return r.kind;
  return "home";
}

/* ── 通用部件 ─────────────────────────────────────────────────────────────── */
const tag = (txt, cls = "") => h("span", { class: `tag ${cls}` }, txt);
const empty = (...k) => h("div", { class: "empty" }, k);

function crumbs(items) {
  return h("nav", { class: "crumbs", "data-key": "crumbs" }, items.map(([label, href], i) => [
    i ? h("span", { class: "sep" }, "›") : null,
    href ? h("a", { href }, label) : h("span", { class: "cur" }, label)]));
}

function pageShell(r, crumbItems, ...kids) {
  return h("div", { class: "page", "data-key": "page:" + r.key }, crumbs(crumbItems), kids);
}

function notFound(r, what) {
  return pageShell(r, [["首页", "#/"], ["未找到", null]],
    h("div", { class: "card" }, h("h3", {}, "找不到" + what),
      h("p", { class: "desc" }, S.state && !S.state.live ? "你正在回放历史：这个对象在所选时间点还不存在（或已被删除）。拖动底部时间线，或点「实时」回到现在。" : "它可能已被删除，或链接有误。"),
      h("a", { class: "xlink", href: "#/" }, "← 回到首页")));
}

function pageTitle(text, small) { return h("h2", { class: "page-title" }, text, small ? h("small", {}, small) : null); }

/* ── 卡片摘要（列表页/首页用，整张卡可点击）──────────────────────────────── */
function taskMini(t) {
  const kind = t.kind || "job";
  const facts = [];
  if (t.model && t.model.name) facts.push(h("span", { "data-key": "f-model" }, "模型 ", h("b", {}, clip(t.model.name, 28))));
  if (t.datasets && t.datasets.length) facts.push(h("span", { "data-key": "f-ds" }, "数据 ", h("b", {}, `${t.datasets.length} 项`)));
  if (t.total) facts.push(h("span", { "data-key": "f-prog" }, h("b", {}, `${fmtNum(Math.round(t.done || 0))} / ${fmtNum(t.total)}${t.unit ? " " + t.unit : ""}`)));
  facts.push(h("span", { "data-key": "f-el" }, "已用 ", h("b", {}, fmtDur(t.elapsed))));
  if (t.status === "running" && t.eta != null) facts.push(h("span", { "data-key": "f-eta" }, "剩余 ", h("b", {}, fmtDur(t.eta))));
  return h("a", { class: `card mini task st-${t.status}`, href: hrefOf("task", t.id), "data-key": "tm:" + t.id, "data-fade": 1 },
    h("div", { class: "task-head" }, tag(TASK_KIND[kind] || kind, `kind-${kind}`), h("span", { class: "name", title: t.name || t.id }, t.name || t.id),
      t.stale ? h("span", { class: "badge stale" }, "疑似停滞") : null, badge(t.status, TASK_ST)),
    h("div", { class: "pct-row" }, bar(t.progress, t.status, { stale: t.stale }), h("span", { class: "pct" }, fmtPct(t.progress))),
    h("div", { class: "kv" }, facts),
    t.message ? h("div", { class: "msg" }, clip(t.message, 90)) : null);
}

const nodeStrip = (g) => h("div", { class: "strip" }, orderedNodes(g).map((n) => h("i", { class: `st-${n.status}`, title: `${n.name || n.id}：${NODE_ST[n.status] || n.status}`, "data-key": "d:" + n.id })));

function graphProgress(g) {
  const n = g.nodes.length;
  const prog = n ? g.nodes.reduce((a, x) => a + (["done", "skipped"].includes(x.status) ? 1 : x.status === "running" ? (x.progress || 0) : 0), 0) / n : null;
  const st = g.nodes.some((x) => x.status === "failed") ? "failed" : g.nodes.some((x) => x.status === "running") ? "running" : prog === 1 ? "done" : "pending";
  return { prog, st, done: g.nodes.filter((x) => ["done", "skipped"].includes(x.status)).length };
}

function pipelineMini(g) {
  if (g.error) return h("div", { class: "card err", "data-key": "pm:" + g.id }, `${g.id}: ${g.error}`);
  const { prog, st, done } = graphProgress(g);
  return h("a", { class: "card mini", href: hrefOf("pipelines", g.id), "data-key": "pm:" + g.id, "data-fade": 1 },
    h("div", { class: "task-head" }, h("span", { class: "name" }, g.title || g.id), exTag(g), badge(st, NODE_ST)),
    h("p", { class: "desc", style: { margin: "6px 0" } }, clip(g.description || "（未填写说明）", 150)),
    nodeStrip(g),
    h("div", { class: "pct-row" }, bar(prog, st, { sm: true }), h("span", { class: "pct" }, fmtPct(prog))),
    h("div", { class: "kv" }, h("span", {}, "共 ", h("b", {}, `${g.nodes.length} 步`), " · 已完成 ", h("b", {}, String(done)))));
}

function dsIssues(g) {
  return g.nodes.filter((n) => ["missing", "failed", "stale"].includes(n.status) || n.replica === "mismatch" || n.replica === "missing_copy").length;
}

function datasetMini(g) {
  if (g.error) return h("div", { class: "card err", "data-key": "dm:" + g.id }, `${g.id}: ${g.error}`);
  const total = g.nodes.reduce((a, n) => a + (nodeSize(n) || 0), 0), issues = dsIssues(g);
  const nLoc = g.nodes.reduce((a, n) => a + (n.locations || []).length, 0);
  return h("a", { class: "card mini", href: hrefOf("datasets", g.id), "data-key": "dm:" + g.id, "data-fade": 1 },
    h("div", { class: "task-head" }, h("span", { class: "name" }, g.title || g.id), exTag(g), issues ? h("span", { class: "badge st-failed" }, `${issues} 项异常`) : badge("ready", NODE_ST)),
    h("p", { class: "desc", style: { margin: "6px 0" } }, clip(g.description || "（未填写说明）", 150)),
    h("div", { class: "tags" }, g.nodes.slice(0, 8).map((n) => tag(n.name || n.id))),
    h("div", { class: "kv" }, h("span", {}, h("b", {}, `${g.nodes.length} 个数据资产`)), h("span", {}, "存放地址 ", h("b", {}, String(nLoc))), total ? h("span", {}, "已知总量 ", h("b", {}, fmtBytes(total))) : null));
}

function moduleMini(m) {
  if (m.error) return h("div", { class: "card err", "data-key": "mm:" + m.id }, `${m.id}: ${m.error}`);
  const types = [...new Set((m.blocks || []).map((b) => b.type))];
  return h("a", { class: "card mini", href: hrefOf("modules", m.id), "data-key": "mm:" + m.id, "data-fade": 1 },
    h("div", { class: "task-head" }, h("span", { class: "name" }, m.title || m.id), exTag(m)),
    h("p", { class: "desc", style: { margin: "6px 0" } }, clip(m.description || "（未填写说明）", 150)),
    h("div", { class: "tags" }, types.map((x) => tag(x))), h("div", { class: "kv" }, h("span", {}, h("b", {}, `${(m.blocks || []).length} 个展示块`))));
}

const tasksGrid = (ts, key) => (ts.length ? h("div", { class: "tasks", "data-key": key }, ts.map(taskMini)) : null);
const sortActive = (ts) => [...ts].sort((a, b) => ((a.status === "running" || a.status === "queued") ? 0 : 1) - ((b.status === "running" || b.status === "queued") ? 0 : 1));

/* ── 首页 ─────────────────────────────────────────────────────────────────── */
function attention(st) {
  const out = [];
  for (const t of st.tasks) {
    if (t.status === "failed") out.push([`${TASK_KIND[t.kind || "job"]}「${t.name || t.id}」失败${t.message ? "：" + clip(t.message, 50) : ""}`, hrefOf("task", t.id), "failed"]);
    else if (t.stale) out.push([`${TASK_KIND[t.kind || "job"]}「${t.name || t.id}」疑似停滞（${fmtDur(t.idle)} 无心跳）`, hrefOf("task", t.id), "stale"]);
  }
  for (const g of st.datasets) {
    if (g.error) { out.push([`数据图 ${g.id} 解析失败`, hrefOf("datasets", g.id), "failed"]); continue; }
    for (const n of g.nodes) {
      const href = hrefOf("datasets", `${g.id}/${n.id}`), nm = n.name || n.id;
      if (n.status === "missing") out.push([`数据「${nm}」路径缺失`, href, "failed"]);
      else if (n.replica === "mismatch") out.push([`数据「${nm}」主地址与备份不一致`, href, "failed"]);
      else if (n.replica === "missing_copy") out.push([`数据「${nm}」有副本地址不存在`, href, "failed"]);
      else if (!(n.locations || []).length) out.push([`数据「${nm}」未记录存放地址`, href, "stale"]);
    }
  }
  for (const g of st.pipelines) if (!g.error) for (const n of g.nodes) if (n.status === "failed") out.push([`Pipeline「${g.title || g.id}」步骤「${n.name || n.id}」失败`, hrefOf("pipelines", `${g.id}/${n.id}`), "failed"]);
  for (const d of st.reviews || []) {
    if (d.error) { out.push([`校验集 ${d.id} 解析失败`, hrefOf("review", d.id), "failed"]); continue; }
    if (d.source_error) out.push([`校验集「${d.title || d.id}」${d.source_error}`, hrefOf("review", d.id), "failed"]);
    else if (d.scan && !d.scan.pending && !d.scan.error && d.scan.fail_rows) out.push([`校验集「${d.title || d.id}」有 ${fmtNum(d.scan.fail_rows)} 行规则不通过`, hrefOf("review", d.id), "stale"]);
    if (d.marked && d.marked.fail) out.push([`校验集「${d.title || d.id}」已标 ${fmtNum(d.marked.fail)} 条不合格`, hrefOf("review", d.id), "failed"]);
  }
  return out;
}

function reviewTileSub(st) {
  const rs = (st.reviews || []).filter((d) => !d.error);
  if (!rs.length) return "生成结果 · 清洗数据的校验";
  const bad = rs.reduce((a, d) => a + ((d.marked && d.marked.fail) || 0), 0);
  const seen = rs.reduce((a, d) => a + (d.marked ? d.marked.pass + d.marked.fail + d.marked.doubt : 0), 0);
  return `已看 ${fmtNum(seen)} 条${bad ? ` · ${fmtNum(bad)} 条不合格` : ""}`;
}

function homePage(r) {
  const st = S.state, bk = st.by_kind, sm = st.summary;
  const runningOf = (k) => st.tasks.filter((t) => (t.kind || "job") === k && t.status === "running").length;
  const nAssets = st.datasets.reduce((a, g) => a + (g.nodes ? g.nodes.length : 0), 0);
  const tile = (key, icon, name, n, sub, href) => h("a", { class: "tile", href, "data-key": "tile:" + key },
    h("div", { class: "tile-ic" }, icon), h("div", { class: "tile-n" }, String(n)), h("div", { class: "tile-name" }, name), h("div", { class: "tile-sub" }, sub));
  const running = sortActive(st.tasks.filter((t) => t.status === "running" || t.status === "queued"));
  const att = attention(st);
  const ms = TL.events.filter((e) => e.type === "milestone").slice(-6).reverse();
  return h("div", { class: "page", "data-key": "page:home" },
    h("div", { class: "tiles" },
      tile("train", "◔", "训练", bk.train, runningOf("train") ? `${runningOf("train")} 个运行中` : "模型 · 数据 · 曲线", "#/train"),
      tile("run", "⇶", "流水线执行", bk.run, runningOf("run") ? `${runningOf("run")} 个运行中` : "要素走到哪一步", "#/run"),
      tile("eval", "✓", "评测", bk.eval, runningOf("eval") ? `${runningOf("eval")} 个运行中` : "评测集 · 模型 · 分数", "#/eval"),
      tile("pipelines", "⌘", "Pipeline", st.pipelines.length, "结构图与各步骤作用", "#/pipelines"),
      tile("datasets", "▤", "数据管理", nAssets, `${st.datasets.length} 张数据图 · 地址与来源`, "#/datasets"),
      tile("modules", "❖", "定制模块", st.modules.length, "数据集与效果展示", "#/modules"),
      tile("review", "☑", "成果校验", (st.reviews || []).length, reviewTileSub(st), "#/review")),
    h("section", { "data-key": "sec:running" }, h("h2", {}, "正在运行", h("small", {}, `${running.length} 个任务`)),
      running.length ? tasksGrid(running, "run-grid") : empty("当前没有运行中的任务。")),
    h("div", { class: "two" },
      h("section", { "data-key": "sec:att" }, h("h2", {}, "需要关注", h("small", {}, `${att.length} 项`)),
        h("div", { class: "card" }, att.length ? h("ul", { class: "alist" }, att.slice(0, 12).map(([txt, href, lv], i) => h("li", { "data-key": "a" + i, class: `st-${lv === "failed" ? "failed" : "stale"}` }, h("i"), h("a", { href }, txt))))
          : h("div", { class: "okline" }, "没有失败、停滞或数据缺失的项目。"))),
      h("section", { "data-key": "sec:ms" }, h("h2", {}, "最近迭代", h("small", {}, "时间线里程碑")),
        h("div", { class: "card" }, ms.length ? h("ul", { class: "alist ms" }, ms.map((e) => h("li", { "data-key": "m" + e.i }, h("b", {}, "⚑ " + e.label), h("span", { class: "id" }, " " + fmtTime(e.ts)), e.note ? h("div", { class: "id" }, e.note) : null)))
          : h("div", { class: "id" }, "还没有里程碑。用 sb.milestone(...) 记录版本/重大变更。")))),
    h("div", { class: "id foot", "data-key": "foot" }, `共 ${sm.running + sm.queued + sm.done + sm.failed + sm.cancelled} 条任务记录 · 已完成 ${sm.done} · 失败 ${sm.failed}`));
}

/* ── 列表页 ───────────────────────────────────────────────────────────────── */
function tasksPage(r) {
  const label = LISTS[r.kind], ts = sortActive(S.state.tasks.filter((t) => (t.kind || "job") === r.kind));
  const hint = { train: "每条显示模型、使用的数据、训练曲线", run: "每个数据要素走到了 Pipeline 的哪一步", eval: "评测集及地址、被测模型、进度与分数", job: "不属于以上三类的任务" }[r.kind];
  return pageShell(r, [["首页", "#/"], [label, null]], pageTitle(label, `${ts.length} 条 · ${hint}`),
    ts.length ? tasksGrid(ts, "grid") : empty(`暂无${label}记录。`));
}

function listPage(r) {
  const st = S.state, label = LISTS[r.kind];
  const items = st[r.kind];
  const mini = { pipelines: pipelineMini, datasets: datasetMini, modules: moduleMini }[r.kind];
  const hint = { pipelines: "点开查看结构图，点击节点进入各步骤子页面", datasets: "点开查看数据血缘；每份数据记录地址（含备份）、来源、用处、处理情况", modules: "数据集展示、效果展示等自定义内容" }[r.kind];
  const how = { pipelines: "python3 sb.py define pipelines <id> file.json", datasets: "python3 sb.py define datasets <id> file.json", modules: "python3 sb.py define modules <id> file.json" }[r.kind];
  return pageShell(r, [["首页", "#/"], [label, null]], pageTitle(label, `${items.length} 个 · ${hint}`),
    items.length ? h("div", { class: "tasks", "data-key": "grid" }, items.map(mini)) : h("div", { class: "empty" }, `暂无${label}。用 sb.define 写入：`, " ", h("code", {}, how)));
}

/* ── 详情页 ───────────────────────────────────────────────────────────────── */
function taskPage(r) {
  const t = S.state.tasks.find((x) => x.id === r.id);
  if (!t) return notFound(r, `任务「${r.id}」`);
  const kind = t.kind || "job";
  const extra = [];
  if (t.pipeline) {
    const g = findGraph("pipelines", t.pipeline);
    if (g && !g.error) {
      extra.push(h("a", { class: "xlink", href: hrefOf("pipelines", g.id), "data-key": "x1" }, `所属 Pipeline：${g.title || g.id}`));
      const n = t.node && g.nodes.find((x) => x.id === t.node);
      if (n) extra.push(h("a", { class: "xlink", href: hrefOf("pipelines", `${g.id}/${n.id}`), "data-key": "x2" }, `对应步骤：${n.name || n.id}`));
    }
  }
  return pageShell(r, [["首页", "#/"], [LISTS[kind], "#/" + kind], [t.name || t.id, null]],
    pageTitle(t.name || t.id, `${TASK_KIND[kind]} · ${t.id}`), h("div", { class: "single" }, taskCard(t)), extra.length ? h("div", { class: "rel" }, extra) : null);
}

const tasksOfPipeline = (gid) => sortActive(S.state.tasks.filter((t) => t.pipeline === gid));
const tasksOfDataset = (gid) => sortActive(S.state.tasks.filter((t) => (t.datasets || []).some((d) => typeof d.ref === "string" && d.ref.split("/")[0] === gid)));

function relSection(title, ts, key) {
  return ts.length ? h("section", { "data-key": key }, h("h2", {}, title, h("small", {}, `${ts.length} 个`)), tasksGrid(ts, key + "-g")) : null;
}

function detailPage(r) {
  const g = findGraph(r.kind, r.id) || (r.kind === "modules" ? S.state.modules.find((m) => m.id === r.id) : null);
  if (!g) return notFound(r, `「${r.id}」`);
  const label = LISTS[r.kind], title = g.title || g.id;
  if (r.kind === "modules") return pageShell(r, [["首页", "#/"], [label, "#/modules"], [title, null]], moduleCard(g));
  const card = r.kind === "pipelines" ? pipelineCard(g) : datasetCard(g);
  const rel = r.kind === "pipelines" ? relSection("该 Pipeline 的任务", tasksOfPipeline(g.id), "rel-t") : relSection("使用了这里数据的任务", tasksOfDataset(g.id), "rel-t");
  return pageShell(r, [["首页", "#/"], [label, "#/" + r.kind], [title, null]], card, rel);
}

function stepNav(kind, g, nid) {
  const seq = orderedNodes(g), i = seq.findIndex((n) => n.id === nid);
  const link = (n, dir) => (n ? h("a", { class: `pn ${dir}`, href: hrefOf(kind, `${g.id}/${n.id}`), "data-key": "pn" + dir }, dir === "prev" ? "← 上一步：" : "下一步：", n.name || n.id, dir === "next" ? " →" : "") : h("span", { "data-key": "pn" + dir }));
  return h("div", { class: "pnav", "data-key": "pnav" }, link(seq[i - 1], "prev"), h("span", { class: "id" }, `${i + 1} / ${seq.length}`), link(seq[i + 1], "next"));
}

function nodePage(r) {
  const g = findGraph(r.kind, r.gid);
  const n = g && !g.error ? g.nodes.find((x) => x.id === r.nid) : null;
  if (!n) return notFound(r, `「${r.gid}/${r.nid}」`);
  const label = LISTS[r.kind], gt = g.title || g.id;
  const isP = r.kind === "pipelines";
  const ts = isP ? sortActive((n.tasks || []).map((id) => S.state.tasks.find((t) => t.id === id)).filter(Boolean))
    : sortActive(S.state.tasks.filter((t) => (t.datasets || []).some((d) => d.ref === `${g.id}/${n.id}`)));
  return pageShell(r, [["首页", "#/"], [label, "#/" + r.kind], [gt, hrefOf(r.kind, g.id)], [n.name || n.id, null]],
    pageTitle(n.name || n.id, isP ? "Pipeline 步骤" : `数据资产 · ${DATA_KIND[n.kind] || n.kind || ""}`),
    h("div", { class: "card", "data-key": "ncard" }, isP ? pipeNodeDetail(g, n, true) : dataNodeDetail(g, n, true)),
    stepNav(r.kind, g, n.id),
    relSection(isP ? "该步骤的关联任务" : "使用了这份数据的任务", ts, "rel-t"));
}

/* ── 成果校验 ─────────────────────────────────────────────────────────────── */
function reviewMini(d) {
  if (d.error) return h("div", { class: "card err", "data-key": "rm:" + d.id }, `${d.id}: ${d.error}`);
  const m = d.marked || { pass: 0, fail: 0, doubt: 0 }, done = m.pass + m.fail + m.doubt, total = d.rows || 0;
  const sc = d.scan && !d.scan.pending && !d.scan.error ? d.scan : null;
  return h("a", { class: "card mini", href: hrefOf("review", d.id), "data-key": "rm:" + d.id, "data-fade": 1 },
    h("div", { class: "task-head" }, tag(REVIEW_KIND[d.kind] || REVIEW_KIND.generic, "kind-eval"), h("span", { class: "name" }, d.title || d.id), exTag(d),
      d.source_error ? h("span", { class: "badge st-failed" }, d.source_error) : null),
    h("p", { class: "desc", style: { margin: "6px 0" } }, clip(d.description || "（未填写说明）", 150)),
    h("div", { class: "pct-row" }, bar(total ? done / total : null, "running", { sm: true }), h("span", { class: "pct" }, total ? fmtPct(done / total) : "--")),
    h("div", { class: "kv" }, h("span", {}, "样本 ", h("b", {}, total ? fmtNum(total) : "-")), h("span", {}, "已看 ", h("b", {}, fmtNum(done))),
      h("span", { style: { color: "var(--ok)" } }, "合格 ", h("b", {}, fmtNum(m.pass))), h("span", { style: { color: "var(--bad)" } }, "不合格 ", h("b", {}, fmtNum(m.fail))),
      h("span", { style: { color: "var(--warn)" } }, "存疑 ", h("b", {}, fmtNum(m.doubt))),
      sc ? h("span", {}, "规则不通过 ", h("b", { style: { color: sc.fail_rows ? "var(--bad)" : "var(--ok)" } }, fmtNum(sc.fail_rows))) : null));
}

function reviewsPage(r) {
  const items = S.state.reviews || [];
  return pageShell(r, [["首页", "#/"], [LISTS.review, null]],
    pageTitle(LISTS.review, `${items.length} 个 · 看模型生成的结果、清洗出来的数据：规则自动检查 + 人工逐条标记`),
    items.length ? h("div", { class: "tasks", "data-key": "grid" }, items.map(reviewMini))
      : h("div", { class: "empty" }, "暂无校验集。用 sb.define 写入：", " ", h("code", {}, "python3 sb.py define reviews <id> file.json")));
}

function reviewPage(r) {
  const d = (S.state.reviews || []).find((x) => x.id === r.id);
  if (!d) { reviewLeave(); return notFound(r, `校验集「${r.id}」`); }
  if (d.error) return pageShell(r, [["首页", "#/"], [LISTS.review, "#/review"], [d.id, null]], h("div", { class: "card err" }, d.error));
  const live = S.state.live;
  const meta = [];
  const f = d.from || {};
  if (f.task) {
    const t = S.state.tasks.find((x) => x.id === f.task);
    meta.push(h("span", { "data-key": "m-task" }, "来自任务 ", t ? h("a", { class: "xlink", href: hrefOf("task", t.id) }, t.name || t.id) : h("b", {}, f.task)));
  }
  if (f.dataset) {
    const [gid, nid] = String(f.dataset).split("/");
    const g = findGraph("datasets", gid), n = g && !g.error ? g.nodes.find((x) => x.id === nid) : null;
    meta.push(h("span", { "data-key": "m-ds" }, "来自数据 ", n ? h("a", { class: "xlink", href: hrefOf("datasets", f.dataset) }, n.name || nid) : h("b", {}, f.dataset)));
  }
  if (f.model) meta.push(h("span", { "data-key": "m-model" }, "模型 ", h("b", {}, f.model)));
  if (f.note) meta.push(h("span", { "data-key": "m-note" }, f.note));
  const head = h("div", { class: "card", "data-key": "rvhead" },
    h("div", { class: "g-head" }, h("h3", {}, d.title || d.id), tag(REVIEW_KIND[d.kind] || REVIEW_KIND.generic, "kind-eval"), exTag(d)),
    d.description ? h("p", { class: "desc" }, d.description) : null,
    meta.length ? h("div", { class: "kv" }, meta) : null);
  let body;
  if (!live) {
    reviewLeave();
    const m = d.marked || {};
    body = h("div", { class: "card", "data-key": "rvhist" }, h("div", { class: "warn" }, "正在回放历史：样本明细读的是当前文件，和历史时刻对不上，所以这里只显示当时的人工校验汇总。"),
      h("div", { class: "kv" }, h("span", {}, "合格 ", h("b", {}, fmtNum(m.pass || 0))), h("span", {}, "不合格 ", h("b", {}, fmtNum(m.fail || 0))), h("span", {}, "存疑 ", h("b", {}, fmtNum(m.doubt || 0)))),
      h("div", { class: "id" }, "点底部「实时」回到现在，继续校验。"));
  } else {
    reviewEnter(d.id);
    body = reviewWorkbench(d);
  }
  return pageShell(r, [["首页", "#/"], [LISTS.review, "#/review"], [d.title || d.id, null]], head, body);
}

export function renderPage(r) {
  if (r.page !== "review") reviewLeave();
  try {
    switch (r.page) {
      case "home": return homePage(r);
      case "tasks": return tasksPage(r);
      case "task": return taskPage(r);
      case "list": return listPage(r);
      case "detail": return detailPage(r);
      case "node": return nodePage(r);
      case "reviews": return reviewsPage(r);
      case "review": return reviewPage(r);
      default: return notFound(r, "页面");
    }
  } catch (e) {
    return pageShell(r, [["首页", "#/"], ["出错", null]], h("div", { class: "card err" }, `页面渲染失败：${e.message}`));
  }
}
