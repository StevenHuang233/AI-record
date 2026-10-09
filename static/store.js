"use strict";
/* 页面共享状态。S.state 是服务端返回的整份状态；S.sel 记录每张图当前选中的节点。 */
export const S = {
  state: null,
  sel: {},          // "pipelines/<id>" -> 节点 id
  offline: false,
  lastOk: 0,
};

export const findGraph = (kind, gid) => (S.state ? S.state[kind].find((g) => g.id === gid) : null);

/* "<图id>/<节点id>" -> {g, n}；找不到返回 null */
export function resolveRef(kind, ref) {
  if (typeof ref !== "string" || !ref.includes("/")) return null;
  const i = ref.indexOf("/");
  const g = findGraph(kind, ref.slice(0, i));
  const n = g && !g.error ? g.nodes.find((x) => x.id === ref.slice(i + 1)) : null;
  return n ? { g, n } : null;
}
