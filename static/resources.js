"use strict";
/* 资源管理视图：API Key / GPU / CPU / 磁盘 / 服务。所有文本经 textContent 写入。
   密钥永远不在页面上出现——这里只显示「从哪取、有没有、额度多少、什么时候过期」。 */
import { h, bar, badge, clip, fmtBytes, fmtNum, fmtPct, fmtTime, foldBtn, foldBody, isFolded } from "./util.js";
import { S } from "./store.js";

export const RES_KIND = { apikey: "API Key", gpu: "GPU", cpu: "CPU", disk: "磁盘 / 存储", service: "服务", other: "其他" };
const ICON = { apikey: "⚿", gpu: "▦", cpu: "▣", disk: "⛁", service: "⇄", other: "◇" };
const KIND_ORDER = ["apikey", "gpu", "cpu", "disk", "service", "other"];
const HEALTH = { ok: "正常", warn: "需关注", bad: "异常", off: "已停用" };
const HBAR = { ok: "done", warn: "stale", bad: "failed", off: "pending" };
const STATE_CN = { active: "启用", disabled: "已停用", revoked: "已撤销", maintenance: "维护中" };
const PERIOD = { day: "每日", month: "每月", year: "每年", total: "总量" };
const SECRET_ST = { set: "已设置", unset: "未设置（空）", missing: "文件不存在", empty: "文件为空", unreadable: "文件不可读", external: "由密钥库管理，无法检查", unregistered: "未登记来源", history: "回放中不检查" };
const SECRET_OK = new Set(["set", "external"]);

const asArr = (x) => (Array.isArray(x) ? x : []);
const tag = (txt, cls = "") => h("span", { class: `tag ${cls}` }, txt);
const href = (kind, ref) => `#/${kind}/${String(ref).split("/").map(encodeURIComponent).join("/")}`;
export const resHref = (gid, iid) => href("resources", iid ? `${gid}/${iid}` : gid);
const level = (p, warn = 0.85, bad = 0.95) => (p >= bad ? "failed" : p >= warn ? "stale" : "done");

/* 一行「标题 + 进度条 + 文字」 */
function meter(label, p, text, st, key) {
  return h("div", { class: "meter", "data-key": "mt:" + key }, h("span", { class: "ml" }, label), bar(p, st || level(p || 0), { sm: true }), h("span", { class: "mv" }, text));
}

function daysText(d) {
  if (d == null) return "";
  return d < 0 ? `已过期 ${-d} 天` : d === 0 ? "今天到期" : `还剩 ${d} 天`;
}

function secretLine(it) {
  const s = it.secret;
  if (!s) return h("div", { class: "id", "data-key": "sec" }, "没有登记密钥来源（secret_ref）");
  const src = { env: "环境变量", file: "密钥文件", vault: "密钥库", none: "" }[s.kind] || s.kind;
  const ok = SECRET_OK.has(s.state);
  return h("div", { class: "secline", "data-key": "sec" }, h("span", { class: "ml" }, "密钥来源"),
    h("span", {}, src, s.name ? [" ", h("code", {}, s.name)] : null),
    h("span", { class: `badge ${ok ? "st-done" : s.state === "history" ? "st-pending" : "st-failed"}` }, SECRET_ST[s.state] || s.state),
    s.loose_perm ? h("span", { class: "badge st-stale", title: "同组或其他用户能读写这个密钥文件，请 chmod 600" }, "权限过宽") : null);
}

function allocTable(it, full) {
  const al = asArr(it.allocations);
  if (!al.length) return full ? h("div", { class: "id", "data-key": "al" }, "当前没有登记的分配（谁在用多少，可用 sb.resource 上报 allocations）") : null;
  const unit = it.kind === "disk" ? "" : it.kind === "gpu" ? " 卡" : it.kind === "cpu" ? " 核" : "";
  return h("div", { class: "tbl-wrap", "data-key": "al" }, h("table", {}, h("thead", {}, h("tr", {}, ["占用方", "任务", "数量", "到期"].map((c) => h("th", {}, c)))),
    h("tbody", {}, al.map((a, i) => h("tr", { "data-key": "a" + i },
      h("td", {}, a.label || "-", a.note ? h("div", { class: "id" }, a.note) : null),
      h("td", {}, a.task ? (full ? h("a", { class: "xlink", href: href("task", a.task) }, a.task) : a.task) : "-"),
      h("td", { class: "num" }, fmtNum(a.amount) + (a.unit ? " " + a.unit : unit)),
      h("td", {}, a.until ? [a.until, a.expired ? h("span", { class: "badge st-stale" }, "已过期") : null] : "-"))))));
}

function gpuCards(p) {
  const cards = asArr(p && p.cards);
  if (!cards.length) return null;
  return h("div", { class: "gpus", "data-key": "gpus" }, cards.slice(0, 16).map((c) => h("div", { class: "gpu", "data-key": "c" + c.index, title: c.name },
    h("div", { class: "gh" }, h("b", {}, "#" + c.index), h("span", { class: "id" }, fmtPct(c.util))),
    bar(c.util, level(c.util, 0.9, 0.99), { sm: true }),
    h("div", { class: "id" }, `显存 ${fmtBytes(c.mem_used)} / ${fmtBytes(c.mem_total)}`),
    bar(c.mem_total ? c.mem_used / c.mem_total : 0, level(c.mem_total ? c.mem_used / c.mem_total : 0), { sm: true }))),
    cards.length > 16 ? h("div", { class: "id" }, `…共 ${cards.length} 张`) : null);
}

/* 一个资源的主体：不同类型显示不同的量 */
function itemBody(it, full) {
  const out = [];
  const k = it.kind, u = it.usage || {}, pr = it.probed;
  if (k === "apikey") {
    if (it.provider) out.push(h("div", { class: "kv", "data-key": "pv" }, h("span", {}, "服务商 ", h("b", {}, it.provider)), it.scopes ? h("span", {}, "权限 ", h("b", {}, it.scopes)) : null, it.rate_limit ? h("span", {}, "限速 ", h("b", {}, it.rate_limit)) : null));
    out.push(secretLine(it));
    const q = it.quota;
    if (q) {
      const used = u.used, p = it.quota_pct;
      out.push(used == null ? h("div", { class: "id", "data-key": "q" }, `额度 ${fmtNum(q.limit)} ${q.unit || ""} / ${PERIOD[q.period || "month"]}（还没有用量上报）`)
        : meter(`额度（${PERIOD[q.period || "month"]}）`, Math.min(1, p), `${fmtNum(used)} / ${fmtNum(q.limit)} ${q.unit || ""} · ${fmtPct(p)}`, level(p, 0.9, 1), "q"));
    }
    if (it.expires) out.push(h("div", { class: "kv", "data-key": "ex" }, h("span", {}, "到期 ", h("b", {}, it.expires)), it.days_left != null ? h("span", { class: it.days_left < 0 ? "bad" : it.days_left <= 14 ? "warnc" : "" }, daysText(it.days_left)) : null,
      it.rotated ? h("span", {}, "上次轮换 ", h("b", {}, it.rotated)) : null, it.rotate_due ? h("span", {}, "应轮换于 ", h("b", {}, it.rotate_due)) : null));
  } else if (k === "gpu") {
    const cap = it.capacity;
    out.push(h("div", { class: "kv", "data-key": "cap" }, h("span", {}, "共 ", h("b", {}, cap != null ? `${fmtNum(cap)} 张` : "未知")), it.model ? h("span", {}, it.model) : null,
      it.mem_gb ? h("span", {}, "显存 ", h("b", {}, `${it.mem_gb} GB/张`)) : null, it.host ? h("span", {}, "主机 ", h("b", {}, it.host)) : null));
    if (cap) out.push(meter("已分配", Math.min(1, (it.allocated || 0) / cap), `${fmtNum(it.allocated || 0)} / ${fmtNum(cap)} 张 · 空闲 ${fmtNum(it.free || 0)}`, level((it.allocated || 0) / cap, 0.8, 1.0001), "al"));
    if (it.util_avg != null) out.push(meter("实时利用率", it.util_avg, `平均 ${fmtPct(it.util_avg)} · 显存 ${fmtPct(it.mem_pct)}`, level(it.util_avg, 0.9, 0.99), "ut"));
    if (full || (pr && pr.cards && pr.cards.length <= 8)) out.push(gpuCards(pr));
  } else if (k === "cpu") {
    out.push(h("div", { class: "kv", "data-key": "cap" }, h("span", {}, "共 ", h("b", {}, it.capacity != null ? `${fmtNum(it.capacity)} 核` : "未知")), it.model ? h("span", {}, it.model) : null,
      it.mem_gb ? h("span", {}, "内存 ", h("b", {}, `${it.mem_gb} GB`)) : null, it.host ? h("span", {}, "主机 ", h("b", {}, it.host)) : null));
    if (it.capacity) out.push(meter("已分配", Math.min(1, (it.allocated || 0) / it.capacity), `${fmtNum(it.allocated || 0)} / ${fmtNum(it.capacity)} 核 · 空闲 ${fmtNum(it.free || 0)}`, level((it.allocated || 0) / it.capacity, 0.8, 1.0001), "al"));
    if (pr && pr.load) out.push(meter("实时负载", Math.min(1, it.load_pct || 0), `1/5/15 分钟：${pr.load.map((x) => x.toFixed(1)).join(" / ")}`, level(it.load_pct || 0, 0.7, 1), "ld"));
    if (pr && pr.mem_total) { const used = pr.mem_total - (pr.mem_avail || 0); out.push(meter("内存", used / pr.mem_total, `${fmtBytes(used)} / ${fmtBytes(pr.mem_total)}`, level(used / pr.mem_total, 0.85, 0.95), "mem")); }
  } else if (k === "disk") {
    out.push(h("div", { class: "kv", "data-key": "cap" }, h("span", {}, "容量 ", h("b", {}, it.capacity ? fmtBytes(it.capacity) : "未知")), it.fs ? h("span", {}, it.fs) : null,
      it.readonly ? tag("只读") : null, it.host ? h("span", {}, "主机 ", h("b", {}, it.host)) : null), it.path ? h("div", { "data-key": "path" }, h("code", {}, it.path)) : null);
    if (it.used_pct != null) out.push(meter("已用", it.used_pct, `${fmtBytes(it.used_bytes)} / ${fmtBytes(it.capacity)} · 剩余 ${fmtBytes(Math.max(0, it.capacity - it.used_bytes))}`, level(it.used_pct), "du"));
    if (it.capacity && it.allocated) out.push(meter("已分配", Math.min(1, it.allocated / it.capacity), `${fmtBytes(it.allocated)} / ${fmtBytes(it.capacity)}`, level(it.allocated / it.capacity, 0.8, 1.0001), "al"));
  } else if (k === "service") {
    out.push(h("div", { class: "kv", "data-key": "svc" }, it.endpoint ? h("span", {}, "地址 ", h("code", {}, it.endpoint)) : null, it.protocol ? h("span", {}, "协议 ", h("b", {}, it.protocol)) : null, it.host ? h("span", {}, "主机 ", h("b", {}, it.host)) : null));
  } else {
    out.push(h("div", { class: "kv", "data-key": "cap" }, it.count != null ? h("span", {}, "数量 ", h("b", {}, fmtNum(it.count) + (it.unit ? " " + it.unit : ""))) : null, it.model ? h("span", {}, it.model) : null, it.host ? h("span", {}, "主机 ", h("b", {}, it.host)) : null));
  }
  return out;
}

const issueList = (it) => (it.issues && it.issues.length
  ? h("ul", { class: "alist", "data-key": "iss" }, it.issues.map((x, i) => h("li", { "data-key": "i" + i, class: `st-${x.level === "bad" ? "failed" : x.level === "warn" ? "stale" : "pending"}` }, h("i"), x.text))) : null);

function head(g, it, link, foldKey) {
  return h("div", { class: "task-head", "data-key": "hd" }, foldKey ? foldBtn(foldKey) : null, h("span", { class: "rico" }, ICON[it.kind] || "◇"), tag(RES_KIND[it.kind] || it.kind),
    link ? h("span", { class: "name", title: it.name || it.id }, it.name || it.id) : h("span", { class: "name" }, it.name || it.id),
    it.state && it.state !== "active" ? h("span", { class: "badge st-stale" }, STATE_CN[it.state]) : null,
    h("span", { class: `badge hl-${it.health}` }, HEALTH[it.health] || it.health));
}

/* 池页里的紧凑卡片：整张可点；内部不放链接（避免嵌套 <a>） */
export function itemMini(g, it) {
  return h("a", { class: `card mini rescard hc-${it.health}`, href: resHref(g.id, it.id), "data-key": "ri:" + it.id, "data-fade": 1 },
    head(g, it, true), it.desc ? h("p", { class: "desc", style: { margin: "4px 0 6px" } }, clip(it.desc, 90)) : null,
    itemBody(it, false), it.issues && it.issues.length ? h("div", { class: "issue1" }, it.issues[0].text, it.issues.length > 1 ? ` 等 ${it.issues.length} 项` : "") : null,
    it.owner ? h("div", { class: "id" }, "负责人 " + it.owner) : null);
}

export function poolMini(g) {
  if (g.error) return h("div", { class: "card err", "data-key": "rp:" + g.id }, `${g.id}: ${g.error}`);
  const sm = g.summary, bk = sm.by_kind;
  return h("a", { class: "card mini", href: resHref(g.id), "data-key": "rp:" + g.id, "data-fade": 1 },
    h("div", { class: "task-head" }, h("span", { class: "name" }, g.title || g.id), g._example ? tag("范例", "ex") : null,
      sm.bad ? h("span", { class: "badge st-failed" }, `${sm.bad} 项异常`) : sm.warn ? h("span", { class: "badge st-stale" }, `${sm.warn} 项需关注`) : h("span", { class: "badge st-done" }, "全部正常")),
    h("p", { class: "desc", style: { margin: "6px 0" } }, clip(g.description || "（未填写说明）", 150)),
    h("div", { class: "tags" }, KIND_ORDER.filter((k) => bk[k]).map((k) => tag(`${ICON[k]} ${RES_KIND[k]} ${bk[k].n}`))),
    h("div", { class: "kv" }, h("span", {}, h("b", {}, `${sm.total} 项资源`))));
}

/* 池详情：分类汇总 + 各类资源卡 */
export function poolPage(g) {
  const sm = g.summary, bk = sm.by_kind;
  const cap = (k) => {
    const b = bk[k];
    if (!b || !b.capacity) return null;
    if (k === "disk") return `${fmtBytes(b.capacity)}（空闲 ${fmtBytes(b.free)}）`;
    return `共 ${fmtNum(b.capacity)}${k === "gpu" ? " 张" : k === "cpu" ? " 核" : ""}，空闲 ${fmtNum(b.free)}`;
  };
  const tiles = KIND_ORDER.filter((k) => bk[k]).map((k) => h("div", { class: `rtile ${bk[k].bad ? "hc-bad" : bk[k].warn ? "hc-warn" : ""}`, "data-key": "rt:" + k },
    h("div", { class: "rtn" }, h("span", { class: "rico" }, ICON[k]), String(bk[k].n)), h("div", { class: "tile-name" }, RES_KIND[k]),
    h("div", { class: "tile-sub" }, cap(k) || (bk[k].bad || bk[k].warn ? `${bk[k].bad} 异常 · ${bk[k].warn} 需关注` : "全部正常"))));
  const sections = KIND_ORDER.filter((k) => bk[k]).map((k) => h("section", { "data-key": "rs:" + k }, h("h2", {}, foldBtn(`rs:${g.id}:${k}`), `${ICON[k]} ${RES_KIND[k]}`, h("small", {}, `${bk[k].n} 项`)),
    foldBody(`rs:${g.id}:${k}`, [h("div", { class: "tasks" }, g.items.filter((x) => x.kind === k).map((it) => itemMini(g, it)))])));
  const hk = "rp:" + g.id;
  return [h("div", { class: "card" + (isFolded(hk) ? " folded" : ""), "data-key": "rphead" }, h("div", { class: "g-head" }, foldBtn(hk), h("h3", {}, g.title || g.id), g._example ? tag("范例", "ex") : null,
    sm.bad ? h("span", { class: "badge st-failed" }, `${sm.bad} 项异常`) : null, sm.warn ? h("span", { class: "badge st-stale" }, `${sm.warn} 项需关注`) : null),
    foldBody(hk, [g.description ? h("p", { class: "desc" }, g.description) : null,
    g.live_probe ? null : h("div", { class: "warn" }, "正在回放历史：不显示本机实时探测（GPU / CPU / 磁盘），也不检查密钥是否存在于当前环境。")])),
    h("div", { class: "rtiles", "data-key": "rtiles" }, tiles), sections];
}

/* 单个资源的子页面 */
export function itemPage(g, it) {
  const rel = [];
  asArr(it.used_by).forEach((u, i) => {
    if (u.task) rel.push(h("span", { "data-key": "ub" + i }, "任务 ", h("a", { class: "xlink", href: href("task", u.task) }, (S.state.tasks.find((t) => t.id === u.task) || {}).name || u.task), u.label ? ` · ${u.label}` : ""));
    else if (u.dataset) rel.push(h("span", { "data-key": "ub" + i }, "数据 ", h("a", { class: "xlink", href: href("datasets", u.dataset) }, u.dataset), u.label ? ` · ${u.label}` : ""));
    else if (u.label) rel.push(h("span", { "data-key": "ub" + i }, u.label));
  });
  const other = (ref) => { const [gid, iid] = String(ref).split("/"); const gg = (S.state.resources || []).find((x) => x.id === gid); const ii = gg && !gg.error ? gg.items.find((x) => x.id === iid) : null; return { gid, iid, it: ii }; };
  const links = [];
  if (it.auth_ref) { const o = other(it.auth_ref); links.push(h("div", { "data-key": "auth" }, "调用凭据：", o.it ? h("a", { class: "xlink", href: resHref(o.gid, o.iid) }, o.it.name || o.iid) : h("code", {}, it.auth_ref))); }
  if (it.replica_of) { const o = other(it.replica_of); links.push(h("div", { "data-key": "rep" }, "这是以下资源的副本 / 备份：", o.it ? h("a", { class: "xlink", href: resHref(o.gid, o.iid) }, o.it.name || o.iid) : h("code", {}, it.replica_of))); }
  const me = `${g.id}/${it.id}`, back = [];
  for (const gg of S.state.resources || []) if (!gg.error) for (const x of gg.items) {
    if (x.replica_of === me) back.push(h("a", { class: "xlink", href: resHref(gg.id, x.id), "data-key": "bk" + gg.id + x.id }, x.name || x.id));
    if (x.auth_ref === me) back.push(h("a", { class: "xlink", href: resHref(gg.id, x.id), "data-key": "au" + gg.id + x.id }, `${x.name || x.id}（使用此凭据）`));
  }
  if (back.length) links.push(h("div", { "data-key": "back" }, "被以下资源引用：", back));
  const fld = (label, c) => (c ? h("div", { class: "fld", "data-key": "f:" + label }, h("div", { class: "fk" }, label), h("div", { class: "fv" }, c)) : null);
  const ik = `ri:${g.id}/${it.id}`;
  return h("div", { class: `card rescard hc-${it.health}` + (isFolded(ik) ? " folded" : ""), "data-key": "ricard" }, head(g, it, false, ik),
    foldBody(ik, [it.desc ? h("p", { class: "desc" }, it.desc) : null, itemBody(it, true), issueList(it),
    fld("怎么调用 / 申请", it.access), fld("负责人", it.owner), fld("备注", it.note),
    fld("动态备注", it.usage && it.usage.note ? `${it.usage.note}${it.usage.at ? "（更新于 " + fmtTime(it.usage.at) + "）" : ""}` : null),
    fld("正在使用它的", rel.length ? h("div", { class: "kv" }, rel) : null), links.length ? fld("关联", links) : null,
    it.kind !== "apikey" ? fld("分配情况", allocTable(it, true)) : null,
    it.tags && it.tags.length ? fld("标签", it.tags.map((x) => tag(x))) : null,
    it.usage && it.usage.metrics ? fld("指标", h("div", { class: "metrics" }, Object.entries(it.usage.metrics).map(([k, v]) => h("span", { class: "metric", "data-key": "m" + k }, `${k}: ${typeof v === "number" ? fmtNum(v) : v}`)))) : null]));
}

/* 首页：需要关注 + 入口卡片副标题 */
export function resourceAttention(st) {
  const out = [];
  for (const g of st.resources || []) {
    if (g.error) { out.push([`资源池 ${g.id} 解析失败`, resHref(g.id), "failed"]); continue; }
    for (const it of g.items) {
      if (it.health === "bad" || it.health === "warn") {
        const first = it.issues.find((x) => x.level === it.health) || it.issues[0];
        out.push([`${RES_KIND[it.kind]}「${it.name || it.id}」${first ? "：" + first.text : ""}`, resHref(g.id, it.id), it.health === "bad" ? "failed" : "stale"]);
      }
    }
  }
  return out.sort((a, b) => (a[2] === "failed" ? 0 : 1) - (b[2] === "failed" ? 0 : 1));
}

export function resourceTileSub(st) {
  const rs = (st.resources || []).filter((g) => !g.error);
  if (!rs.length) return "API Key · GPU · CPU · 磁盘";
  const bad = rs.reduce((a, g) => a + g.summary.bad, 0), warn = rs.reduce((a, g) => a + g.summary.warn, 0);
  return bad || warn ? `${bad} 异常 · ${warn} 需关注` : "全部正常";
}

export const resourceCount = (st) => (st.resources || []).reduce((a, g) => a + (g.items ? g.items.length : 0), 0);
