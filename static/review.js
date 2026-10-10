"use strict";
/* 成果校验工作台：翻页看样本、规则自动检查结果、人工标记（合格/不合格/存疑）、键盘快捷键。
   工作台有自己的状态（RV），不放进 S.state：全局轮询只刷新汇总，不会打断正在审阅的页面。 */
import { h, bar, badge, clip, fmtNum, fmtPct, fmtTime, assetUrl, TASK_ST } from "./util.js";
import { S } from "./store.js";

export const RV = {
  id: null,        // 当前校验集
  view: "all", rule: "", page: 0, size: null,
  picked: {},      // 分面筛选：{分面id: 取值}
  data: null,      // /api/review/<id> 的返回
  loading: false, error: null,
  sel: null,       // 当前选中的行号（键盘操作的对象）
  toast: null, pending: 0,
  only: false,     // 回放历史时：不显示样本
};

const VERDICT = { pass: "合格", fail: "不合格", doubt: "存疑" };
const VIEWS = [["all", "全部"], ["fail", "规则不通过"], ["pass", "规则通过"], ["unmarked", "未标记"], ["mpass", "已标合格"], ["mfail", "已标不合格"], ["mdoubt", "已标存疑"]];
let timer = null, onChange = () => {}, seq = 0;

export const setOnChange = (f) => { onChange = f; };

function resetView(id) {
  Object.assign(RV, { id, view: "all", rule: "", page: 0, size: null, picked: {}, data: null, loading: false, error: null, sel: null, toast: null });
}

/* enter 会在每次渲染时被调用：必须幂等——只在「换了校验集」时才发请求；失败后不自动重试（否则失败→重绘→再请求死循环）。
   定时器只建一次，用来在后台规则扫描完成前自动刷新。 */
export function enter(id) {
  if (RV.id !== id) { resetView(id); fetchPage(); }
  if (!timer) timer = setInterval(() => { if (RV.data && RV.data.scan && RV.data.scan.pending && !RV.loading) fetchPage(true); }, 1500);
}

export function leave() {
  if (timer) { clearInterval(timer); timer = null; }
  RV.id = null;                       // 离开后再回来要重新加载（样本文件可能已变化）
}

export async function fetchPage(silent) {
  if (!RV.id) return;
  const my = ++seq, id = RV.id;
  if (!silent) RV.loading = true;
  try {
    let q = `view=${encodeURIComponent(RV.view)}&page=${RV.page}`;      // 不用 URLSearchParams：构造放进 try，任何异常都显示为错误而不是卡在加载中
    if (RV.size) q += `&size=${RV.size}`;
    if (RV.rule) q += `&rule=${encodeURIComponent(RV.rule)}`;
    const pf = Object.entries(RV.picked).filter(([, v]) => v !== "" && v != null);
    if (pf.length) q += `&facets=${encodeURIComponent(JSON.stringify(Object.fromEntries(pf)))}`;
    const r = await fetch(`/api/review/${encodeURIComponent(id)}?${q}`, { cache: "no-store" });
    const d = await r.json();
    if (my !== seq || RV.id !== id) return;
    if (!r.ok) { RV.error = d.error || `请求失败 ${r.status}`; RV.data = null; }
    else {
      RV.data = d; RV.error = null; RV.size = d.size;
      if (!d.items.some((x) => x.line === RV.sel)) RV.sel = d.items.length ? d.items[0].line : null;
    }
  } catch (e) {
    if (my !== seq) return;
    RV.error = "连接失败：" + e.message;
  }
  RV.loading = false;
  onChange();
}

export function setView(view) { RV.view = view; RV.rule = ""; RV.page = 0; RV.sel = null; fetchPage(); }
export function setRule(rule) { RV.view = "fail"; RV.rule = rule; RV.page = 0; RV.sel = null; fetchPage(); }
export function setFacet(id, v) {
  if (v === "" || v == null) delete RV.picked[id]; else RV.picked[id] = v;
  RV.page = 0; RV.sel = null; fetchPage();
}
export function clearFacets() { RV.picked = {}; RV.page = 0; RV.sel = null; fetchPage(); }

export function gotoPage(p) {
  const d = RV.data; if (!d) return;
  const last = Math.max(0, Math.ceil(d.selected / d.size) - 1);
  RV.page = Math.max(0, Math.min(last, p)); RV.sel = null; fetchPage();
}

function flash(msg) {
  RV.toast = msg; onChange();
  setTimeout(() => { if (RV.toast === msg) { RV.toast = null; onChange(); } }, 3000);
}

/* 标记：乐观更新，失败回滚 */
export async function mark(line, v, note) {
  const d = RV.data; if (!d) return;
  const it = d.items.find((x) => x.line === line); if (!it) return;
  const prev = it.mark ? { ...it.mark } : null;
  const same = prev && prev.v === v && note === undefined;
  const nv = same ? null : v;                                   // 再按一次同一个结论 = 取消标记
  const bump = (m, s) => { if (m && d.marked[m.v] != null) d.marked[m.v] += s; };
  bump(prev, -1);
  if (nv) { it.mark = { v: nv, at: Date.now() / 1000, ...(note ? { note } : prev && prev.note ? { note: prev.note } : {}), ...(prev && prev.tags ? { tags: prev.tags } : {}) }; bump(it.mark, 1); }
  else delete it.mark;
  delete it.mark_stale;
  RV.pending++; onChange();
  try {
    const body = { marks: [{ line, v: nv, h: it.h, ...(note !== undefined ? { note } : it.mark && it.mark.note ? { note: it.mark.note } : {}) }] };
    const r = await fetch(`/api/review/${encodeURIComponent(RV.id)}/marks`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error((await r.json()).error || r.status);
  } catch (e) {
    bump(it.mark, -1);
    if (prev) { it.mark = prev; bump(prev, 1); } else delete it.mark;
    flash("标记失败，已撤回：" + e.message);
  }
  RV.pending--; onChange();
}

export function step(delta) {
  const d = RV.data; if (!d || !d.items.length) return;
  const i = d.items.findIndex((x) => x.line === RV.sel);
  const j = i + delta;
  if (j < 0) { if (RV.page > 0) { RV.page--; RV.sel = "last"; fetchPage().then(() => { if (RV.data && RV.data.items.length) { RV.sel = RV.data.items[RV.data.items.length - 1].line; onChange(); } }); } return; }
  if (j >= d.items.length) { const last = Math.ceil(d.selected / d.size) - 1; if (RV.page < last) gotoPage(RV.page + 1); return; }
  RV.sel = d.items[j].line; onChange();
  const el = document.querySelector(`[data-rvline="${RV.sel}"]`); if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

document.addEventListener("keydown", (e) => {
  if (!RV.id || !/^#\/review\//.test(location.hash) || e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target; if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
  const k = e.key.toLowerCase();
  if (k === "j" || k === "arrowdown") { e.preventDefault(); step(1); }
  else if (k === "k" || k === "arrowup") { e.preventDefault(); step(-1); }
  else if (RV.sel != null && (k === "1" || k === "2" || k === "3")) { e.preventDefault(); mark(RV.sel, { 1: "pass", 2: "fail", 3: "doubt" }[k]); }
  else if (RV.sel != null && k === "0") { e.preventDefault(); const it = RV.data && RV.data.items.find((x) => x.line === RV.sel); if (it && it.mark) mark(RV.sel, it.mark.v); }
  else if (k === "n" && RV.data) { e.preventDefault(); const it = RV.data.items.find((x) => x.line === RV.sel); if (it) { const txt = window.prompt(`第 ${it.line} 行的备注（留空则清除备注）`, (it.mark && it.mark.note) || ""); if (txt !== null) mark(it.line, (it.mark && it.mark.v) || "doubt", txt.slice(0, 500)); } }
});

/* ── 渲染 ─────────────────────────────────────────────────────────────────── */
function getPath(o, key) {
  let v = o;
  for (const p of String(key).split(".")) { if (v == null) return undefined; v = v[p]; }
  return v;
}

/* 逐字符 diff（LCS），长文本退化为整段对比，避免卡页面 */
function diffParts(a, b) {
  a = String(a ?? ""); b = String(b ?? "");
  if (a === b) return [{ t: "eq", s: a }];
  if (a.length * b.length > 400000) return [{ t: "del", s: a }, { t: "ins", s: b }];
  const n = a.length, m = b.length, dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out = []; let i = 0, j = 0;
  const push = (t, ch) => { const l = out[out.length - 1]; if (l && l.t === t) l.s += ch; else out.push({ t, s: ch }); };
  while (i < n && j < m) { if (a[i] === b[j]) { push("eq", a[i]); i++; j++; } else if (dp[i + 1][j] >= dp[i][j + 1]) { push("del", a[i]); i++; } else { push("ins", b[j]); j++; } }
  while (i < n) push("del", a[i++]);
  while (j < m) push("ins", b[j++]);
  return out;
}

function valueEl(v, type) {
  if (v === undefined) return h("span", { class: "id" }, "（没有这个字段）");
  if (v === null) return h("span", { class: "id" }, "null");
  if ((type === "image" || type === "audio" || type === "video") && typeof v === "string") {
    const url = assetUrl(v);
    if (type === "image") return h("img", { src: url, loading: "lazy", alt: v, class: "media rvimg" });
    return h(type, { src: url, controls: true, preload: "none", class: "media" });
  }
  if (typeof v === "string") return h("div", { class: type === "long" ? "longtext" : "text" }, v);
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return h("pre", { class: "jsonbox" }, JSON.stringify(v, null, 1).slice(0, 3000));
}

function fieldEl(f, row) {
  const label = f.label || f.key || "";
  if (f.type === "diff") {
    const a = getPath(row, f.a), b = getPath(row, f.b);
    const parts = diffParts(a, b);
    return h("div", { class: "sf" }, h("div", { class: "fk" }, `${label}（${f.a} → ${f.b}）`),
      h("div", { class: "diffbox" }, parts.map((p, i) => h("span", { class: "df-" + p.t, "data-key": "d" + i }, p.s))));
  }
  return h("div", { class: "sf" }, h("div", { class: "fk" }, label), valueEl(getPath(row, f.key), f.type));
}

function ruleLabel(rules, id) {
  if (id === "__bad_json__") return "该行不是合法 JSON 对象";
  const r = (rules || []).find((x) => x.id === id);
  return r ? (r.label || r.id) : id;
}

function sampleCard(it, def, sel) {
  const rules = def.scan && def.scan.per_rule ? def.scan.per_rule : [];
  const m = it.mark;
  const cls = `rvcard ${sel ? "sel" : ""} ${m ? "mk-" + m.v : ""} ${it.fails.length ? "has-fail" : ""}`;
  const btn = (v, key) => h("button", { class: `vbtn v-${v} ${m && m.v === v ? "on" : ""}`, "data-act": "rvmark", "data-a1": String(it.line), "data-a2": v, title: `${VERDICT[v]}（按 ${key}）` }, `${VERDICT[v]} `, h("kbd", {}, key));
  return h("div", { class: cls, "data-key": "rv:" + it.line, "data-rvline": String(it.line), "data-act": "rvsel", "data-a1": String(it.line) },
    h("div", { class: "rvhead" }, h("span", { class: "rvno" }, `第 ${it.line} 行`),
      it.fails.length ? it.fails.map((id) => h("span", { class: "badge st-failed", "data-key": "f:" + id, title: id }, ruleLabel(rules, id))) : h("span", { class: "badge st-done" }, "规则全通过"),
      h("span", { class: "spacer" }),
      m ? h("span", { class: `badge mkb-${m.v}` }, VERDICT[m.v]) : h("span", { class: "id" }, "未标记"),
      it.mark_stale ? h("span", { class: "badge stale", title: "标记之后源文件这一行变了，请重新确认" }, "标记可能过期") : null),
    it.bad !== undefined ? h("div", { class: "warn" }, "这一行没法解析成 JSON 对象：", h("pre", { class: "jsonbox" }, it.bad))
      : h("div", { class: "rvbody" }, def.fields.map((f, i) => h("div", { "data-key": "fld" + i }, fieldEl(f, it.row)))),
    h("div", { class: "rvact" }, btn("pass", "1"), btn("fail", "2"), btn("doubt", "3"),
      h("button", { class: "vbtn note", "data-act": "rvnote", "data-a1": String(it.line), title: "写备注（按 N）" }, m && m.note ? "改备注" : "备注"),
      m && m.note ? h("span", { class: "rvnote" }, "备注：" + m.note) : null));
}

function ruleSummary(def, scan) {
  if (!scan) return null;
  if (scan.pending) return h("div", { class: "card scanning", "data-key": "scan" }, h("div", { class: "pct-row" }, bar(null, "running", { sm: true }), h("span", { class: "id" }, "正在对全部样本跑规则检查…")));
  if (scan.error) return h("div", { class: "card err", "data-key": "scan" }, "规则扫描失败：" + scan.error);
  const rows = scan.per_rule.map((r) => {
    const p = scan.rows ? r.fail / scan.rows : 0;
    return h("tr", { class: "click", "data-key": "rr:" + r.id, "data-act": "rvrule", "data-a1": r.id, title: "点击只看不满足这条规则的样本" },
      h("td", {}, r.label, r.severity === "warn" ? h("span", { class: "tag" }, "提示") : null),
      h("td", { class: "num" }, r.fail ? h("b", { style: { color: "var(--bad)" } }, fmtNum(r.fail)) : h("span", { style: { color: "var(--ok)" } }, "0")),
      h("td", { style: { minWidth: "140px" } }, bar(p, r.fail ? "failed" : "done", { sm: true })),
      h("td", { class: "num" }, fmtPct(p)));
  });
  return h("div", { class: "card", "data-key": "scan" },
    h("div", { class: "g-head" }, h("h3", {}, "规则自动检查"),
      h("span", { class: "kv" }, `共 ${fmtNum(scan.rows)} 行`, scan.bad_json ? h("span", { style: { color: "var(--bad)" } }, ` · ${scan.bad_json} 行不是合法 JSON`) : null),
      h("span", { class: "badge st-failed" }, `${fmtNum(scan.fail_rows)} 行有问题`), h("span", { class: "badge st-done" }, `${fmtNum(scan.pass_rows)} 行全通过`)),
    scan.truncated ? h("div", { class: "warn" }, `文件很大，只扫描了前 ${fmtNum(scan.rows)} 行（上限由 SB_REVIEW_MAX_ROWS 控制），下面的统计不含其余部分。`) : null,
    rows.length ? h("div", { class: "tbl-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["规则", "不通过", "占比", ""].map((c) => h("th", {}, c)))), h("tbody", {}, rows)))
      : h("div", { class: "id" }, "没有配置自动规则，只能人工校验。"));
}

/* 分面筛选条：每个分面一行按钮，数字是当前其他筛选下这个取值有多少条（点一下筛选，再点取消） */
function facetBar(d) {
  if (!d.facets || !d.facets.length) return null;
  const rows = d.facets.map((f) => h("div", { class: "facet", "data-key": "fc:" + f.id },
    h("div", { class: "fname" }, f.label),
    h("div", { class: "fvals" },
      h("button", { class: `fv ${!(RV.picked[f.id] != null) ? "on" : ""}`, "data-act": "rvfacet", "data-a1": f.id, "data-a2": "" }, "全部"),
      f.values.map((v) => h("button", { class: `fv ${RV.picked[f.id] === v.v ? "on" : ""}`, "data-act": "rvfacet", "data-a1": f.id, "data-a2": String(v.v), title: `${v.n} 条` },
        String(v.v), h("span", { class: "fn" }, fmtNum(v.n)))))));
  const active = Object.keys(RV.picked).filter((k) => RV.picked[k] != null && RV.picked[k] !== "");
  return h("div", { class: "card facets", "data-key": "facets" },
    h("div", { class: "g-head" }, h("h3", {}, "按字段筛选（数字 = 当前筛选下的条数）"),
      active.length ? h("button", { class: "tl-btn", "data-act": "rvclearf" }, "清空筛选（" + active.length + "）") : null),
    rows);
}

/* 矩阵：两个分面交叉，每格显示条数 + 指标中位数，点格子同时筛这两个分面 */
function matrixCard(d) {
  const m = d.matrix;
  if (!m || !m.grid || !m.grid.length) return null;
  const pickedOf = (id) => (RV.picked[id] != null ? RV.picked[id] : null);
  return h("div", { class: "card", "data-key": "matrix" },
    h("div", { class: "g-head" }, h("h3", {}, `${m.col_label} × ${m.row_label}`),
      h("span", { class: "kv" }, m.metric ? "每格：条数 / " + m.metric + "中位数" : "每格：条数"),
      h("span", { class: "id" }, "点格子 = 同时按这两个字段筛选")),
    h("div", { class: "tbl-wrap" }, matrixTable(m)));
}

function matrixTable(m) {
  const pickedOf = (id) => (RV.picked[id] != null ? RV.picked[id] : null);
  return h("table", { class: "matrix" },
      h("thead", {}, h("tr", {}, h("th", {}, ""), m.cols.map((c) => h("th", {}, String(c))))),
      h("tbody", {}, m.grid.map((row, i) => {
        const rn = String(m.rows[i]);
        const cells = row.map((c, j) => {
          const cn = String(m.cols[j]);
          const on = pickedOf(m.rows_id) === rn && pickedOf(m.cols_id) === cn;
          const attrs = { class: `mc ${c.n ? "" : "empty"} ${on ? "on" : ""}`, "data-key": "mc:" + i + ":" + j,
            "data-act": "rvcell", "data-a1": rn, "data-a2": cn, role: "button", tabindex: 0,
            title: `${rn} × ${cn}：${c.n} 条` };
          return h("td", attrs, h("div", { class: "mn" }, fmtNum(c.n)),
            c.n ? h("div", { class: "md" }, (c.approx ? "≈" : "") + fmtNum(c.med)) : null);
        });
        return h("tr", { "data-key": "mr:" + i }, h("th", {}, rn), cells);
      })));
}

function pager(d) {
  const last = Math.max(0, Math.ceil(d.selected / d.size) - 1);
  const b = (txt, p, dis) => h("button", { class: "tl-btn", "data-act": "rvpage", "data-a1": String(p), disabled: dis }, txt);
  return h("div", { class: "pager", "data-key": "pager" }, b("«", 0, d.page <= 0), b("‹ 上一页", d.page - 1, d.page <= 0),
    h("span", { class: "id" }, `第 ${d.page + 1} / ${last + 1} 页 · 本视图共 ${fmtNum(d.selected)} 行 · 每页 ${d.size}`),
    b("下一页 ›", d.page + 1, d.page >= last), b("»", last, d.page >= last));
}

export function reviewWorkbench(def) {
  const d = RV.data;
  const m = d ? d.marked : def.marked;
  const total = (d && d.rows) || def.rows || 0;
  const done = m.pass + m.fail + m.doubt;
  const toolbar = h("div", { class: "rvbar", "data-key": "rvbar" },
    h("div", { class: "views" }, VIEWS.map(([k, l]) => h("button", { class: `vtab ${RV.view === k && !RV.rule ? "on" : ""}`, "data-key": "vt:" + k, "data-act": "rvview", "data-a1": k }, l))),
    RV.rule ? h("span", { class: "badge st-failed" }, "筛选规则：" + ruleLabel(d && d.scan && d.scan.per_rule, RV.rule), h("button", { class: "x", "data-act": "rvview", "data-a1": "fail", title: "取消规则筛选" }, "×")) : null,
    h("span", { class: "spacer" }),
    h("span", { class: "keys" }, h("kbd", {}, "J"), h("kbd", {}, "K"), " 上下 ", h("kbd", {}, "1"), "合格 ", h("kbd", {}, "2"), "不合格 ", h("kbd", {}, "3"), "存疑 ", h("kbd", {}, "N"), "备注"));
  const progress = h("div", { class: "card", "data-key": "mprog" },
    h("div", { class: "g-head" }, h("h3", {}, "人工校验进度"),
      h("span", { class: "kv" }, `已看 ${fmtNum(done)} / ${fmtNum(total)}`),
      h("span", { class: "badge mkb-pass" }, `合格 ${fmtNum(m.pass)}`), h("span", { class: "badge mkb-fail" }, `不合格 ${fmtNum(m.fail)}`), h("span", { class: "badge mkb-doubt" }, `存疑 ${fmtNum(m.doubt)}`),
      RV.pending ? h("span", { class: "id" }, "保存中…") : null),
    h("div", { class: "stack mstack" }, ["pass", "fail", "doubt"].map((v) => h("span", { class: "ms-" + v, "data-key": "ms" + v, style: { width: total ? (100 * m[v] / total) + "%" : "0%" }, title: `${VERDICT[v]} ${m[v]}` }))),
    done ? h("div", { class: "id" }, `已标记样本中：合格率 ${fmtPct(m.pass / done)}`) : h("div", { class: "id" }, "还没有标记。选中一条样本，按 1 / 2 / 3 标记合格 / 不合格 / 存疑。"));
  let body;
  if (RV.error) body = h("div", { class: "card err", "data-key": "rverr" }, RV.error);
  else if (!d) body = h("div", { class: "empty", "data-key": "rvload" }, "加载样本中…");
  else if (!d.items.length) body = h("div", { class: "empty", "data-key": "rvempty" }, d.scan && d.scan.pending && (RV.view === "fail" || RV.view === "pass" || RV.rule) ? "规则检查还没完成，完成后这里会出现结果。" : "这个视图下没有样本。");
  else body = h("div", { class: "rvlist", "data-key": "rvlist" }, d.items.map((it) => sampleCard(it, { ...def, scan: d.scan }, it.line === RV.sel)));
  return [progress, d ? facetBar(d) : null, d ? matrixCard(d) : null, ruleSummary(def, d && d.scan), toolbar, body, d && d.items.length ? pager(d) : null,
          RV.toast ? h("div", { class: "toast", "data-key": "toast" }, RV.toast) : null];
}
