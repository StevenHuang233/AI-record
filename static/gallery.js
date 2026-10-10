"use strict";
/* 视频图库视图：照旧浏览站（gallery.html）搬过来。侧栏 → 批次头 → 8 格矩阵 + 风格分布 → 筛选条 → 卡片网格 → 详情抽屉
   （播放器、抽帧、拍摄参数、时间轴、提问、题目与答案、计分题、动作变化、每页段信息）。
   在旧站基础上多了校验：每张卡和抽屉里都能标 合理 / 偏弱 / 判错 / 不确定（键盘 1 / 2 / 3 / 4，N 写备注），
   视图可以按「规则不通过 / 未标记 / 已标…」筛。

   这块有自己的 DOM 和状态，不走看板的增量 patch（播放进度、时间轴游标、输入框焦点都不能被重绘打断）：
   页面里只放一个带 data-static 的容器，patch 不会碰它里面的内容。 */
import { h, assetUrl, fmtNum } from "./util.js";
import { S } from "./store.js";

export const ACTION = {   // 动作颜色与中文名（同旧站）
  enter: ["#9a9a9a", "入场"], raise: ["#6d7fb0", "举起对准"], hold: ["#a7a294", "稳住停留"], zoom: ["#4f8f6d", "推近"],
  pan: ["#c08a3e", "移动视线"], scan: ["#b5653f", "逐行扫读"], settle: ["#7c6aa6", "回修"], reframe: ["#5a9aa8", "重新构图"],
  tilt: ["#a65b7c", "透视倾斜"], refocus: ["#6b8e3a", "原地对焦"],
};
export const SYNTH = { real: "真实图", crop: "裁切横带", concat: "多页拼接", page: "排版纸面", video: "视频", handheld: "手持帧" };
const PAGE = 24;
const VERDICT = { pass: "合理", weak: "偏弱", fail: "判错", doubt: "不确定" };
const VIEWS = [["", "全部"], ["fail", "规则不通过"], ["unmarked", "未标记"], ["mpass", "已标合理"], ["mweak", "已标偏弱"], ["mfail", "已标判错"], ["mdoubt", "已标不确定"]];
const PAGES_SEG = [["", "全部"], ["1", "单页"], ["multi", "多页"]];
const TARGET_SEG = [["", "全部"], ["known", "已知"], ["unknown", "未知"]];
const SORTS = [["", "默认顺序"], ["dur_desc", "时长：长 → 短"], ["dur_asc", "时长：短 → 长"], ["asks", "提问次数多的优先"]];


/* DOM 原生的 append / replaceChildren 会把 null 渲染成文字 "null"（看板自己的 h() 会忽略 null，原生的不会）：统一走这两个 */
const kids = (xs) => xs.flat(Infinity).filter((x) => x !== null && x !== undefined && x !== false);
const fill = (el, ...xs) => { el.replaceChildren(...kids(xs)); return el; };
const add = (el, ...xs) => { el.append(...kids(xs)); return el; };

const actionOf = (s) => ACTION[s.name === "enter" ? "enter" : s.type] || [null, s.type];

/* ── 时间轴的几何：纯函数，不碰 DOM，方便测试 ─────────────────────────────── */
export function timelineModel(it) {
  const d = it.detail || {};
  const wins = it.page_windows || [], asks = it.asks || [];
  const total = Number(it.duration) || Math.max(1, ...wins.map((w) => w.disappear_s || 0), ...asks.map((a) => a.t || 0));
  const pct = (t) => `${(100 * t / total).toFixed(3)}%`;
  const starts = [0];
  for (const tr of d.transitions || []) starts.push(tr.start + tr.seconds);
  const blocks = [];
  (d.pages_stages || []).forEach((page, p) => {
    for (const s of page) {
      const [color, name] = ACTION[s.name === "enter" ? "enter" : s.type] || ["#999", s.type];
      const a = (starts[p] || 0) + s.start, b = (starts[p] || 0) + s.end;
      blocks.push({ left: pct(a), width: pct(b - a), color, name,
        title: `第${p + 1}页 · ${name} · ${a.toFixed(2)}–${b.toFixed(2)}s · 幅度 ${s.amount} · ${s.direction}` });
    }
  });
  const trs = (d.transitions || []).map((t) => ({ left: pct(t.start), width: pct(t.seconds), title: `换页过渡 ${t.seconds}s` }));
  const windows = wins.map((w) => ({ left: pct(w.appear_s), width: pct(w.disappear_s - w.appear_s), tgt: w.page === it.target_page,
    title: `第${w.page + 1}页可见 ${w.appear_s}–${w.disappear_s}s` }));
  const marks = asks.map((a) => ({ left: pct(a.t), turn: a.turn }));
  const ticks = Array.from({ length: 6 }, (_, i) => `${(total * i / 5).toFixed(1)}s`);
  const used = new Set((d.pages_stages || []).flat().map((s) => (s.name === "enter" ? "enter" : s.type)));
  const legend = Object.entries(ACTION).filter(([k]) => used.has(k)).map(([, [c, n]]) => ({ color: c, name: n }));
  legend.push({ color: "#86bb98", name: "目标页可见" }, { color: "#c9dfd0", name: "其他页可见" }, { color: "#c0473a", name: "提问", thin: true });
  if (trs.length) legend.push({ color: "#d8d4cc", name: "换页" });
  return { total, blocks, trs, windows, marks, ticks, legend };
}

/* ── 一张卡片 ────────────────────────────────────────────────────────────── */
export function cardEl(it, index, onOpen) {
  const tl = [it.n_pages ? `${it.n_pages} 页` : "", it.n_asks ? `问 ${it.n_asks} 次` : ""].filter(Boolean).join(" · ");
  const warn = it.n_pages > 1 && it.target_page == null ? h("span", { class: "gtag warn" }, "目标页未知") : null;
  const thumb = it.thumb ? h("img", { src: assetUrl(it.thumb), loading: "lazy", alt: "" })
    : (it.has_video ? h("div", { class: "muted", style: { color: "#aaa", padding: "40% 0", textAlign: "center" } }, "▶ 视频") : null);
  const mk = it.mark && it.mark.v;
  return h("article", { class: `gcard ${mk ? "mk-" + mk : ""}`, "data-i": index, onclick: () => onOpen(index) },
    h("div", { class: "thumb" }, thumb,
      tl ? h("span", { class: "gbadge tl" }, tl) : null,
      mk ? h("span", { class: `gbadge mk mk-${mk}` }, VERDICT[mk]) : null,
      it.has_video ? h("span", { class: "gbadge tr" }, "▶") : null,
      it.duration ? h("span", { class: "gbadge br" }, `${Number(it.duration).toFixed(1)}s`) : null),
    h("div", { class: "cbody" },
      h("div", { class: "tags" },
        it.cell ? h("span", { class: "gtag cell" }, `${it.cell} · ${it.turns}`) : null,
        it.style_name || it.style ? h("span", { class: "gtag style" }, it.style_name || it.style) : null,
        it.data_source && it.data_source !== "None" ? h("span", { class: "gtag" }, it.data_source) : null,
        it.fails && it.fails.length ? h("span", { class: "gtag warn", title: it.fails.join("、") }, `${it.fails.length} 条规则不通过`) : null,
        warn),
      h("div", { class: "instr" }, it.instruction || it.question || it.id),
      it.instruction && it.question ? h("div", { class: "gq" }, `题：${it.question}`) : null));
}

/* ── 抽屉里的时间轴 / 各个区块 ───────────────────────────────────────────── */
function timelineEl(it) {
  const m = timelineModel(it);
  const lane = (name, ...kids) => h("div", { class: "lane" }, h("span", { class: "lname" }, name), kids);
  const trEls = () => m.trs.map((t) => h("div", { class: "gtr", style: { left: t.left, width: t.width }, title: t.title }));
  return [h("div", { class: "gtl", "data-total": String(m.total) },
    h("div", { class: "ghead", style: { left: "0" } }),
    m.blocks.length ? lane("", m.blocks.map((b) => h("div", { class: "gblk", style: { left: b.left, width: b.width, background: b.color }, title: b.title }, b.name)), trEls()) : null,
    lane(m.windows.length ? "" : "可见区间", m.windows.map((w) => h("div", { class: `gwin ${w.tgt ? "tgt" : ""}`, style: { left: w.left, width: w.width }, title: w.title })), trEls(),
      m.marks.map((a) => h("div", { class: "askm", style: { left: a.left } }, h("b", {}, `Q${a.turn}`)))),
    h("div", { class: "ticks" }, m.ticks.map((t) => h("span", {}, t)))),
  h("div", { class: "legend2" }, m.legend.map((l) => h("span", {}, h("i", { style: { background: l.color, ...(l.thin ? { width: "3px" } : {}) } }), l.name)))];
}

const sec = (title, ...kids) => h("div", { class: "sec" }, h("h4", {}, title), kids);
const kvRows = (pairs) => pairs.filter(([, v]) => v !== undefined && v !== null && v !== "").flatMap(([k, v]) => [h("span", {}, k), h("span", {}, v)]);
const tbl = (head, rows) => h("table", { class: "segs" }, h("tr", {}, head.map((c) => h("th", {}, c))), rows.map((r) => h("tr", {}, r.map((c) => (c instanceof Node ? c : h("td", {}, c))))));
const td = (kids, cls) => h("td", cls ? { class: cls } : {}, kids);

export function detailLeft(it, onFrame) {
  const d = it.detail || {};
  const fp = it.frame_pages || [];
  const frames = (it.frames || []).map((m, i) => h("img", { src: assetUrl(m), alt: "",
    onclick: (e) => onFrame(e.target.src, `首问前抽帧 ${i + 1}${fp[i] != null ? ` · 第 ${fp[i] + 1} 页` : ""}`) }));
  const player = it.video ? h("video", { src: assetUrl(it.video), controls: true, autoplay: true, muted: true, loop: true, playsinline: true })
    : (it.frames && it.frames[0] ? h("img", { src: assetUrl(it.frames[0]), alt: "" }) : h("div", { class: "muted" }, "没有媒体"));
  const left = [h("div", { class: "player" }, player)];
  if (frames.length) left.push(h("div", { class: "sec", style: { marginTop: "12px" } }, h("h4", {}, it.video ? "首问前抽帧" : "帧序列"), h("div", { class: "frames" }, frames)));
  const target = it.n_pages ? `${it.n_pages} 页 · 目标页 ${it.target_page == null ? "未知（整条视频都可问）" : "第 " + (it.target_page + 1) + " 页"}` : "";
  left.push(sec("拍摄参数", h("div", { class: "gkv" }, kvRows([
    ["来源", it.data_source && it.data_source !== "None" ? it.data_source : ""],
    ["风格", it.style_name || it.style], ["光学算子", (it.optics || []).join(" / ")],
    ["时长", it.duration ? `${Number(it.duration).toFixed(2)} 秒` : ""],
    ["规格", d.canvas ? `${d.canvas} · ${d.fps} fps` : ""], ["页数", target],
    ["原组帧方式", SYNTH[it.frame_synth_src] || it.frame_synth_src || SYNTH[it.frame_synth] || it.frame_synth],
    ["指认方式", [it.cue_kind, it.tone].filter(Boolean).join(" · ")], ["候选题数", d.n_candidates],
    ["种子", d.seed], ["渲染版本", d.render_version],
    ["模糊峰值 / 上限", d.blur_limit_sigma != null ? `${Number(d.blur_peak_sigma || 0).toFixed(2)} / ${Number(d.blur_limit_sigma).toFixed(2)} px（等效 σ）` : ""],
    ["ID", h("span", { class: "mono", style: { fontSize: "11px", wordBreak: "break-all" } }, it.id)]]))));
  return left;
}

export function detailRight(it, ruleLabel) {
  const d = it.detail || {};
  const out = [];
  if (it.mark && it.mark.note) out.push(h("div", { class: "note" }, `备注：${it.mark.note}`));
  if (it.mark_stale) out.push(h("div", { class: "note" }, "标记之后源文件这一行变了，请重新确认。"));
  if (it.fails && it.fails.length) out.push(sec("规则不通过", h("div", {}, it.fails.map((f) => h("span", { class: "gtag warn", style: { marginRight: "6px" } }, ruleLabel(f))))));
  if ((it.asks && it.asks.length) || (d.pages_stages && d.pages_stages.length)) {
    out.push(sec([`时间轴 `, h("span", { class: "muted", style: { fontWeight: 400 } }, "点任意位置跳转")], timelineEl(it)));
  }
  if (it.asks && it.asks.length) {
    out.push(sec(`提问（${it.asks.length} 次）`, h("div", { class: "asks" }, it.asks.map((a) => h("div", { "data-t": String(a.t) },
      h("b", {}, `Q${a.turn}`), " ", h("span", { class: "muted" }, `${a.t}s · 第 ${(a.page ?? 0) + 1} 页`), "　",
      a.turn === 1 ? (a.instruction || it.instruction)
        : [h("span", { class: "muted" }, "（意图，台词评测时生成）"), a.intent || (d.intents || [])[a.turn - 2] || ""])))));
  } else if (it.instruction) out.push(sec("学生指令", h("div", {}, it.instruction)));
  if (it.question || it.gold) {
    out.push(sec(`目标题${d.target_num ? ` · 第 ${d.target_num} 题` : ""}${d["科目"] ? ` · ${d["学段"] || ""}${d["科目"]}` : ""}`,
      h("div", { class: "longtext" }, it.question || "—"),
      h("h4", { style: { marginTop: "10px" } }, "参考答案"), h("div", { class: "longtext" }, it.gold || "—")));
  }
  if (d.scored && d.scored.length) {
    out.push(sec(`计分题（${d.scored.length} 道）`, tbl(["题号", "页", "科目", "被问到的时刻", "题目", "参考答案"],
      d.scored.map((s) => [s.num ?? "—", String((s.page ?? 0) + 1), `${s.stage || ""}${s.subject || ""}`,
        (s.asks || []).map((a) => `Q${a.turn} @ ${Number(a.t ?? 0).toFixed(1)}s`).join("、") || "—",
        td(s.stem || "—", "longtext"), td(s.gold || "—", "longtext")]))));
  }
  if (d.action_metrics && d.action_metrics.length) {
    const rows = d.action_metrics.flatMap((page, p) => page.map((m) => [String(p + 1), actionOf({ name: m.name, type: m.type })[1],
      `${Number(m.travel_px).toFixed(1)} px`, `${Number(m.zoom_percent).toFixed(1)}%`,
      `${Number(m.tilt_range).toFixed(2)} / ${Number(m.focus_peak).toFixed(2)}`, m.effective == null ? "停留" : m.effective ? "达到阈值" : "轻微"]));
    out.push(sec("实际动作变化", h("p", { class: "muted" }, "数值来自最终轨迹；低于阈值会标为轻微，不等同于人工确认可读性。"),
      tbl(["页", "动作", "中心移动", "缩放变化", "透视 / 合焦", "效果"], rows)));
  }
  if (d.segments && d.segments.length) {
    out.push(sec("每页段信息", tbl(["页", "段长", "运动时长", "入场", "动作数", "动作序列", "约束命中"],
      d.segments.map((s, i) => [[String(i + 1), s.target ? h("span", { class: "gtag" }, "目标") : ""].map((x) => x),
        `${s.seconds}s`, `${s.motion_seconds}s`, s.enter_seconds ? s.enter_seconds + "s" : "—", String(s.stage_count),
        ((d.pages_stages || [])[i] || []).map((x) => actionOf(x)[1]).join(" → "),
        (s.rules_applied || []).filter((r) => r.includes("stage") || r.includes("duration")).join("、") || "—"]))));
  }
  if (d.bench) {
    out.push(sec("原版出分", h("div", { class: "gkv" }, kvRows([["评测集", d.bench], ["计分器", h("span", { class: "mono" }, d.scorer)],
      ["官方题型", [d.keyword, d.question_type].filter(Boolean).join(" · ")], ["分值", d.full_score]]))));
  }
  return out;
}

/* ── 图库本体 ────────────────────────────────────────────────────────────── */
let current = null;

export function galleryLeave() {
  if (current) { current.destroy(); current = null; }
  if (typeof document !== "undefined") document.body.classList.remove("gal-mode");
}

const isGallery = (d) => d && !d.error && d.gallery;
/* 规则不通过的行数：扫描里「超长行」也记成失败行，但那是读不了，不是规则判的，分开说 */
const ruleFails = (scan) => (scan ? Math.max(0, (scan.fail_rows || 0) - (scan.too_long || 0) - (scan.bad_json || 0)) : 0);
export const galleryReviews = () => (S.state.reviews || []).filter(isGallery);

export function mountGallery(root, id, openLine) {
  if (typeof document !== "undefined") document.body.classList.add("gal-mode");
  if (!current || current.root !== root) {
    if (current) current.destroy();
    current = createGallery(root);
  }
  current.renderSide();
  current.select(id, openLine);
}

function createGallery(root) {
  const G = { id: null, def: null, list: [], total: 0, opened: -1, filt: {}, data: null, seq: 0, itemSeq: 0, timer: null, detail: null };
  const els = {};
  const $ = (tag, attrs, ...kids) => h(tag, attrs, ...kids);

  /* 骨架：只建一次；控件（输入框、下拉）之后只改值、不重建，输入时焦点不会丢 */
  const seg = (key, options) => {
    const box = $("div", { class: "seg" });
    for (const [v, label] of options) {
      box.append($("button", { "data-v": v, class: v === "" ? "on" : "", onclick: () => {
        G.filt[key] = v;
        if (key === "view") G.filt.rule = "";
        for (const b of box.children) b.classList.toggle("on", b.dataset.v === v);
        load(false);
      } }, label));
    }
    return box;
  };
  els.side = $("aside");
  els.bname = $("h1", {}, "加载中…");
  els.bmeta = $("span", { class: "meta" });
  els.matrix = $("table", { class: "matrix" });
  els.styles = $("div", { class: "bars" });
  els.help = $("div", { class: "help", style: { marginTop: "10px" } });
  els.rules = $("div", { class: "rules", hidden: true });
  els.fView = seg("view", VIEWS);
  els.fPages = seg("pages", PAGES_SEG);
  els.fTarget = seg("target", TARGET_SEG);
  els.fStyle = $("select", { onchange: () => { G.filt.style = els.fStyle.value; load(false); } }, $("option", { value: "" }, "全部风格"));
  els.fSort = $("select", { onchange: () => { G.filt.sort = els.fSort.value; load(false); } }, SORTS.map(([v, l]) => $("option", { value: v }, l)));
  let qTimer = null;
  els.q = $("input", { id: "gq", placeholder: "搜索指令 / 题干 / id", oninput: () => { clearTimeout(qTimer); qTimer = setTimeout(() => { G.filt.q = els.q.value.trim(); load(false); }, 250); } });
  els.count = $("span", { class: "count" });
  els.grid = $("div", { class: "grid" });
  els.empty = $("div", { class: "gempty", hidden: true }, "没有匹配的数据。");
  els.more = $("button", { hidden: true, onclick: () => load(true) }, "加载更多");
  const clear = $("button", { class: "linkbtn", onclick: () => { resetControls(); load(false); } }, "清空筛选");

  const main = $("main", {},
    $("div", { class: "bhead" }, els.bname, els.bmeta),
    $("div", { class: "overview" },
      $("div", { class: "gpanel" }, $("h3", {}, "8 格分布（点格子筛选；数字 = 条数，下方 = 时长中位 / 范围）"), els.matrix),
      $("div", { class: "gpanel" }, $("h3", {}, "风格分布"), els.styles, els.help, els.rules)),
    $("div", { class: "filters" },
      $("span", { class: "flabel" }, "校验"), els.fView,
      $("span", { class: "flabel" }, "页数"), els.fPages, $("span", { class: "flabel" }, "目标页"), els.fTarget,
      els.fStyle, els.fSort, els.q, clear, $("span", { class: "spacer" }), els.count),
    els.grid, els.empty, $("div", { class: "more" }, els.more));
  fill(root, $("div", { class: "glayout" }, els.side, main));

  /* 抽屉、遮罩、灯箱放在 body 上（固定定位，盖住整个页面） */
  els.mask = $("div", { class: "gal-mask", hidden: true, onclick: () => closeDetail() });
  els.dtitle = $("span", { class: "t" });
  els.prev = $("button", { title: "上一条（←）", onclick: () => step(-1) }, "‹");
  els.next = $("button", { title: "下一条（→）", onclick: () => step(1) }, "›");
  els.marks = $("span", { style: { display: "inline-flex", gap: "6px" } });
  els.dbody = $("div", { class: "dbody", onclick: onBodyClick });
  els.drawer = $("div", { class: "gal-drawer", hidden: true },
    $("div", { class: "dhead" }, els.prev, els.next, els.dtitle, $("span", { class: "spacer" }), els.marks,
      $("span", { class: "muted", style: { fontSize: "12px" } }, "← → 切换 · 1 2 3 标记 · Esc 关闭"), $("button", { onclick: () => closeDetail() }, "关闭")),
    els.dbody);
  els.lbImg = $("img", { alt: "" });
  els.lbCap = $("div", { class: "cap" });
  els.lightbox = $("dialog", { class: "gal-lightbox", onclick: () => els.lightbox.close() }, els.lbImg, els.lbCap);
  els.toast = null;
  add(document.body, els.mask, els.drawer, els.lightbox);

  function flash(msg) {
    if (els.toast) els.toast.remove();
    els.toast = $("div", { class: "gal-toast" }, msg);
    document.body.append(els.toast);
    setTimeout(() => { if (els.toast) { els.toast.remove(); els.toast = null; } }, 3000);
  }

  function resetControls() {
    G.filt = {};
    for (const box of [els.fView, els.fPages, els.fTarget]) for (const b of box.children) b.classList.toggle("on", b.dataset.v === "");
    els.fStyle.value = ""; els.fSort.value = ""; els.q.value = "";
  }

  const api = (path, params) => {
    const p = new URLSearchParams(params || {});
    return fetch(`/api/review/${encodeURIComponent(G.id)}/${path}?${p}`, { cache: "no-store" });
  };

  /* ── 侧栏：按数据集分组，当前批次高亮 ── */
  let sideSig = "";
  function renderSide() {
    const sig = JSON.stringify([G.id, galleryReviews().map((d) => [d.id, d.title, d.rows, d.marked, d.scan && [d.scan.fail_rows, d.scan.too_long], d.gallery])]);
    if (sig === sideSig) return;
    sideSig = sig;
    const by = {};
    for (const d of galleryReviews()) (by[d.gallery.group || "其他"] ||= []).push(d);
    fill(els.side,
      $("div", { class: "help" }, "成果校验 · 按数据集展示", $("br"), "每条视频可播放、可标记"),
      ...Object.entries(by).sort(([a], [b]) => a.localeCompare(b)).map(([name, items]) =>
        $("div", { class: "ogroup" }, $("div", { class: "ohead" }, name),
          items.sort((a, b) => String(b.gallery.version || "").localeCompare(String(a.gallery.version || ""))).map((d) => {
            const m = d.marked || {}, marked = (m.pass || 0) + (m.weak || 0) + (m.fail || 0) + (m.doubt || 0);
            return $("button", { class: `batch ${d.id === G.id ? "on" : ""}`, "data-key": d.id, onclick: () => { location.hash = `#/review/${encodeURIComponent(d.id)}`; } },
              $("div", { class: "bn" }, d.title || d.id, d.gallery.version ? $("span", { class: "ver" }, d.gallery.version) : null,
                $("span", { class: "vid" }, "视频"),
                ruleFails(d.scan) ? $("span", { class: "bad" }, `${fmtNum(ruleFails(d.scan))} 条规则不通过`) : null,
                d.scan && d.scan.too_long ? $("span", { class: "bad", title: "单行超过上限没有读取，调大 SB_REVIEW_LINE_MAX 可看全" }, `${fmtNum(d.scan.too_long)} 行超长未读`) : null),
              $("div", { class: "bm" }, `${d.rows != null ? fmtNum(d.rows) : "—"} 条${marked ? ` · 已标 ${fmtNum(marked)}` : ""}`));
          }))));
  }

  /* ── 选批次 ── */
  function select(id, openLine) {
    if (G.id === id && G.def) return;
    G.openLine = openLine || null;
    const def = galleryReviews().find((d) => d.id === id);
    G.id = id; G.def = def || null; G.data = null; G.list = []; G.total = 0;
    closeDetail(true);
    resetControls();
    while (els.fStyle.options.length > 1) els.fStyle.remove(1);
    renderSide();
    if (!def) { els.bname.textContent = "这个校验集没有配置 gallery"; fill(els.grid); return; }
    els.bname.textContent = def.title || def.id;
    fill(els.bmeta, "读取中…");
    clearTimeout(G.timer);
    load(false, true);
  }

  /* ── 取一页 ── */
  function params(offset) {
    const f = G.filt, p = { offset, limit: PAGE };
    for (const k of ["cell", "turns", "pages", "target", "style", "sort", "q", "view", "rule"]) if (f[k]) p[k] = f[k];
    return p;
  }

  async function load(append, first) {
    if (!G.id) return;
    const my = ++G.seq, id = G.id;
    let data;
    try {
      const r = await api("gallery", params(append ? G.list.length : 0));
      data = await r.json();
      if (!r.ok) throw new Error(data.error || r.status);
    } catch (e) {
      if (my === G.seq) { fill(els.bmeta, `加载失败：${e.message}`); }
      return;
    }
    if (my !== G.seq || id !== G.id) return;
    if (data.pending) {                              // 后台还在扫描：先显示进度，过一会儿再取
      fill(els.bmeta, `正在扫描 ${data.rows != null ? fmtNum(data.rows) + " 行" : ""}…`);
      G.timer = setTimeout(() => load(append, first), 1500);
      return;
    }
    G.data = data;
    G.list = append ? G.list.concat(data.items) : data.items;
    G.total = data.count;
    if (first || !els.matrix.firstChild) renderSummary();
    renderHead();
    const start = append ? G.list.length - data.items.length : 0;
    const cards = data.items.map((it, i) => cardEl(it, start + i, openDetail));
    if (append) add(els.grid, cards); else fill(els.grid, cards);
    if (G.openLine) { const ln = G.openLine; G.openLine = null; openDetail(null, ln); }
    els.empty.hidden = G.total > 0;
    els.more.hidden = G.list.length >= G.total;
    els.more.textContent = `加载更多（还有 ${G.total - G.list.length} 条）`;
    const parts = [];
    if (G.filt.cell) parts.push(`${G.filt.cell}·${G.filt.turns}`);
    els.count.textContent = `${fmtNum(G.total)} 条` + (parts.length ? `（${parts.join("，")}）` : "");
    for (const td0 of els.matrix.querySelectorAll("td.c")) td0.classList.toggle("on", td0.dataset.cell === G.filt.cell && td0.dataset.turns === G.filt.turns);
    for (const rr of els.rules.querySelectorAll(".rr")) rr.classList.toggle("on", rr.dataset.rule === (G.filt.rule || "\u0000"));
  }

  function renderHead() {
    const d = G.data, def = G.def, m = d.marked || {};
    const marked = (m.pass || 0) + (m.weak || 0) + (m.fail || 0) + (m.doubt || 0);
    fill(els.bmeta,
      $("span", { class: "gtag", style: { marginRight: "6px" } }, def.gallery.badge || "校验集"),
      def.gallery.version ? `版本 ${def.gallery.version} · ` : "", `${fmtNum(d.rows)} 条 · 有视频`,
      ` · 已标 ${fmtNum(marked)}（合理 ${fmtNum(m.pass || 0)} / 偏弱 ${fmtNum(m.weak || 0)} / 判错 ${fmtNum(m.fail || 0)} / 不确定 ${fmtNum(m.doubt || 0)}）`,
      d.scan && d.scan.too_long ? $("span", { style: { color: "#b4532a", marginLeft: "8px" } },
        `· ${fmtNum(d.scan.too_long)} 行超过单行上限没有读取（启动时设 SB_REVIEW_LINE_MAX 调大）`) : null);
  }

  /* ── 8 格矩阵 + 风格分布 + 规则 ── */
  function renderSummary() {
    const d = G.data, sum = d.summary, cells = d.cells, turns = d.turn_names;
    const rows = [$("tr", {}, $("th"), turns.map((t) => $("th", {}, t)))];
    for (const c of cells) {
      rows.push($("tr", {}, $("th", {}, c), turns.map((t) => {
        const n = sum.cells[`${c}|${t}`];
        if (!n) return $("td", { class: "c empty" }, $("div", { class: "cn" }, "0"), $("div", { class: "cd" }, "—"));
        const dur = n.dur_med != null ? `${n.dur_med}s（${n.dur_min}–${n.dur_max}）` : "无时长";
        return $("td", { class: "c", "data-cell": c, "data-turns": t, onclick: () => {
          const same = G.filt.cell === c && G.filt.turns === t;
          G.filt.cell = same ? "" : c; G.filt.turns = same ? "" : t; load(false);
        } }, $("div", { class: "cn" }, fmtNum(n.n)), $("div", { class: "cd" }, dur));
      })));
    }
    const other = Object.entries(sum.cells).filter(([k]) => !cells.some((c) => k.startsWith(c + "|")));
    if (other.length) rows.push($("tr", {}, $("th", {}, "无格子"), $("td", { class: "c", colspan: "2" },
      $("div", { class: "cn" }, fmtNum(other.reduce((a, [, v]) => a + v.n, 0))), $("div", { class: "cd" }, "这些行没有格子信息"))));
    fill(els.matrix, rows);
    const styles = Object.entries(sum.styles).sort((a, b) => b[1] - a[1]);
    const max = Math.max(1, ...styles.map((s) => s[1]));
    fill(els.styles, ...(styles.length ? styles.map(([k, v]) => $("div", { class: "gbar" }, $("span", {}, k),
      $("i", { style: { width: `${(100 * v / max).toFixed(0)}%` } }), $("span", { class: "muted" }, String(v))))
      : [$("div", { class: "muted", style: { fontSize: "12px" } }, "这批数据没有风格信息")]));
    const have = new Set([...els.fStyle.options].map((o) => o.value));
    for (const [k] of styles) if (!have.has(k)) els.fStyle.append($("option", { value: k }, k));
    fill(els.help, "点卡片看详情：", $("b", {}, "视频"), "、", $("b", {}, "时间轴"), "（每页动作、可见区间、提问时刻，点任意位置跳转）、",
      $("b", {}, "题目与答案"), "、", $("b", {}, "每页段信息"), "、", $("b", {}, "拍摄参数"), "。在详情里按 ", $("b", {}, "1 / 2 / 3 / 4"), " 标合理 / 偏弱 / 判错 / 不确定。");
    const per = (d.scan && d.scan.per_rule) || [];
    els.rules.hidden = !per.length;
    fill(els.rules, ...(per.length ? [$("div", { class: "muted", style: { fontSize: "12px", marginBottom: "4px" } }, "规则自动检查（点一条只看它不通过的）"),
      ...per.map((r) => $("div", { class: "rr", "data-rule": r.id, onclick: () => {
        G.filt.rule = G.filt.rule === r.id ? "" : r.id;
        for (const b of els.fView.children) b.classList.toggle("on", b.dataset.v === "");
        G.filt.view = ""; load(false);
      } }, r.fail ? $("b", { class: "bad0" }, fmtNum(r.fail)) : $("b", { class: "ok0" }, "0"), r.label,
      r.severity === "warn" ? $("span", { class: "gtag" }, "提示") : null))] : []));
  }

  /* ── 详情抽屉 ── */
  const ruleLabel = (rid) => {
    if (rid === "__bad_json__") return "该行不是合法 JSON 对象";
    const r = ((G.data && G.data.scan && G.data.scan.per_rule) || []).find((x) => x.id === rid);
    return r ? r.label : rid;
  };

  async function openDetail(index, lineOverride) {
    const light = index != null && index >= 0 ? G.list[index] : null;
    const line = lineOverride || (light && light.line);
    if (!line) return;
    const my = ++G.itemSeq;
    let it;
    try {
      const r = await api("item", { line });
      if (!r.ok) throw new Error((await r.json()).error || r.status);
      it = await r.json();
    } catch (e) { flash(`打开失败：${e.message}`); return; }
    if (my !== G.itemSeq) return;
    G.opened = index ?? -1; G.detail = it;
    history.replaceState(null, "", `#/review/${encodeURIComponent(G.id)}?line=${line}`);
    renderDetail();
    els.mask.hidden = false; els.drawer.hidden = false;
    els.prev.disabled = G.opened <= 0; els.next.disabled = G.opened < 0 || G.opened >= G.list.length - 1;
  }

  function renderDetail() {
    const it = G.detail;
    els.dtitle.textContent = [it.cell && `${it.cell} · ${it.turns}`, it.style_name || it.style, it.id].filter(Boolean).join("　");
    fill(els.dbody, $("div", {}, ...detailLeft(it, (src, cap) => { els.lbImg.src = src; els.lbCap.textContent = cap; els.lightbox.showModal(); })),
      $("div", {}, ...detailRight(it, ruleLabel)));
    const mk = it.mark && it.mark.v;
    fill(els.marks, ...["pass", "weak", "fail", "doubt"].map((v, i) => $("button", { class: `gvbtn v-${v} ${mk === v ? "on" : ""}`,
      title: `${VERDICT[v]}（按 ${i + 1}）`, onclick: () => mark(v) }, VERDICT[v])),
      $("button", { class: "gvbtn", title: "写备注（按 N）", onclick: () => note() }, mk && it.mark.note ? "改备注" : "备注"));
    const video = els.dbody.querySelector("video");
    if (video) video.addEventListener("timeupdate", () => {
      const head = els.dbody.querySelector(".ghead");
      const dur = Number(it.duration) || video.duration || 1;
      if (head) head.style.left = `${(100 * video.currentTime / dur).toFixed(2)}%`;
    });
  }

  function closeDetail(silent) {
    els.mask.hidden = true; els.drawer.hidden = true; fill(els.dbody); G.detail = null; G.opened = -1; G.itemSeq++;
    if (!silent && G.id) history.replaceState(null, "", `#/review/${encodeURIComponent(G.id)}`);
  }

  function step(delta) {
    const n = G.opened + delta;
    if (G.opened < 0 || n < 0) return;
    if (n >= G.list.length) { if (G.list.length < G.total) load(true).then(() => { if (n < G.list.length) openDetail(n); }); return; }
    openDetail(n);
  }

  function onBodyClick(e) {
    const video = els.dbody.querySelector("video");
    const jump = e.target.closest("[data-t]");
    if (jump && video) { video.currentTime = Number(jump.dataset.t); video.play(); return; }
    const lane = e.target.closest(".lane");
    if (lane && video) {
      const tl = lane.closest(".gtl"), r = tl.getBoundingClientRect();
      video.currentTime = Math.max(0, (e.clientX - r.left) / r.width * Number(tl.dataset.total)); video.play();
    }
  }

  /* ── 标记：乐观更新，失败回滚 ── */
  async function mark(v, noteText) {
    const it = G.detail; if (!it) return;
    const prev = it.mark ? { ...it.mark } : null;
    const same = prev && prev.v === v && noteText === undefined;
    const nv = same ? null : v;
    const m = G.data.marked;
    if (prev && m[prev.v] != null) m[prev.v] -= 1;
    if (nv) { it.mark = { v: nv, ...(noteText ? { note: noteText } : prev && prev.note ? { note: prev.note } : {}) }; m[nv] += 1; } else delete it.mark;
    delete it.mark_stale;
    paintMark(it);
    try {
      const body = { marks: [{ line: it.line, v: nv, h: it.h, ...(noteText !== undefined ? { note: noteText } : it.mark && it.mark.note ? { note: it.mark.note } : {}) }] };
      const r = await fetch(`/api/review/${encodeURIComponent(G.id)}/marks`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!r.ok) throw new Error((await r.json()).error || r.status);
    } catch (e) {
      if (nv && m[nv] != null) m[nv] -= 1;
      if (prev) { it.mark = prev; m[prev.v] += 1; } else delete it.mark;
      paintMark(it);
      flash(`标记失败，已撤回：${e.message}`);
    }
  }

  function paintMark(it) {
    renderDetail();
    renderHead();
    const light = G.list.find((x) => x.line === it.line);
    if (light) {
      if (it.mark) light.mark = { v: it.mark.v, ...(it.mark.note ? { note: it.mark.note } : {}) }; else delete light.mark;
      const i = G.list.indexOf(light), old = els.grid.children[i];
      if (old) old.replaceWith(cardEl(light, i, openDetail));
    }
    renderSide();
  }

  function note() {
    const it = G.detail; if (!it) return;
    const txt = window.prompt(`第 ${it.line} 行的备注（留空则清除备注）`, (it.mark && it.mark.note) || "");
    if (txt !== null) mark((it.mark && it.mark.v) || "doubt", txt.slice(0, 500));
  }

  /* 键盘：只在抽屉打开时响应，不抢输入框和灯箱 */
  function onKey(e) {
    if (els.drawer.hidden || els.lightbox.open || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
    if (e.key === "Escape") closeDetail();
    else if (e.key === "ArrowLeft") step(-1);
    else if (e.key === "ArrowRight") step(1);
    else if (e.key === "1") mark("pass");
    else if (e.key === "2") mark("weak");
    else if (e.key === "3") mark("fail");
    else if (e.key === "4") mark("doubt");
    else if (e.key.toLowerCase() === "n") { e.preventDefault(); note(); }
    else return;
  }
  document.addEventListener("keydown", onKey);

  function destroy() {
    clearTimeout(G.timer); clearTimeout(qTimer);
    document.removeEventListener("keydown", onKey);
    for (const k of ["mask", "drawer", "lightbox", "toast"]) if (els[k]) els[k].remove();
    G.seq++; G.itemSeq++;
  }

  return { root, renderSide, select, destroy, G };
}
