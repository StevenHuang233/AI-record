"""看板存储层与体检的测试：只用标准库，python3 tests/test_status_board.py 直接跑。

覆盖的是「写进去的东西是否成立」，不涉及页面渲染（前端靠 sb.py validate + 人眼）。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import sbstore  # noqa: E402


def graph(nodes, groups=None, edges=None, coll="datasets"):
    obj = {"title": "t", "description": "d", "nodes": nodes, "edges": edges or []}
    if groups is not None:
        obj["groups"] = groups
    if coll == "pipelines":
        for n in nodes:
            n.setdefault("role", "作用")
    return obj


def data_node(nid, **kw):
    base = {"id": nid, "name": nid, "kind": "raw",
            "desc": "含义", "source": "来源", "purpose": "用处", "processing": ["处理"],
            "locations": [{"label": "主", "role": "primary", "path": "/tmp/x"}]}
    base.update(kw)
    return base


class DefinitionValidationTests(unittest.TestCase):
    def test_dataset_groups_accepted(self):
        """数据图支持 groups：和 Pipeline 一样按 id 引用节点，不会当成错误。"""
        obj = graph([data_node("a", brief="x"), data_node("b", brief="y")],
                    groups=[{"id": "raw", "name": "原始", "desc": "分组说明", "nodes": ["a", "b"]}])
        self.assertIn("groups", sbstore.clean_definition("datasets", obj))

    def test_dataset_groups_reject_unknown_node(self):
        obj = graph([data_node("a")], groups=[{"id": "raw", "name": "原始", "nodes": ["a", "nope"]}])
        with self.assertRaises(ValueError):
            sbstore.clean_definition("datasets", obj)

    def test_pipeline_groups_still_validated(self):
        obj = graph([{"id": "a", "kind": "process", "desc": "d", "role": "r"},
                     {"id": "b", "kind": "process", "desc": "d", "role": "r"}],
                    groups=[{"id": "p", "name": "准备", "nodes": ["a", "b"]}], coll="pipelines")
        self.assertEqual(len(sbstore.clean_definition("pipelines", obj)["groups"]), 1)


class _StateCase(unittest.TestCase):
    """每个用例用一个临时状态目录：sbstore 的读写都跟着 SB_STATE_DIR 走。"""
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sb-test-")
        self._old = os.environ.get("SB_STATE_DIR")
        os.environ["SB_STATE_DIR"] = self.dir
        sbstore.ensure_dirs()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("SB_STATE_DIR", None)
        else:
            os.environ["SB_STATE_DIR"] = self._old
        shutil.rmtree(self.dir, ignore_errors=True)

    def _define(self, coll, gid, obj):
        return sbstore.define(coll, gid, obj)


class ValidateReportTests(_StateCase):
    def test_missing_brief_is_warned_for_datasets(self):
        self._define("datasets", "g", graph([data_node("a", brief="有说明"), data_node("b")]))
        warns = [m for lv, m in sbstore.lint() if lv == "warn"]
        self.assertTrue(any("brief" in m and "g:b" in m for m in warns), warns)

    def test_brief_present_means_no_warning(self):
        self._define("datasets", "g", graph([data_node("a", brief="x"), data_node("b", brief="y")]))
        warns = [m for lv, m in sbstore.lint() if "brief" in m]
        self.assertEqual(warns, [])

    def test_brief_warning_names_the_group(self):
        self._define("datasets", "g", graph([data_node("a"), data_node("b")],
                                            groups=[{"id": "raw", "name": "原始", "nodes": ["a", "b"]}]))
        warns = [m for lv, m in sbstore.lint() if "brief" in m]
        self.assertTrue(all("原始" in m for m in warns), warns)

    def test_dataset_groups_do_not_create_cycle_error(self):
        """分组只是显示归类，不能影响由 edges 决定的分层判定。"""
        self._define("datasets", "g", graph(
            [data_node("a"), data_node("b"), data_node("c")],
            groups=[{"id": "x", "name": "X", "nodes": ["a", "b", "c"]}],
            edges=[{"from": "a", "to": "b"}, {"from": "b", "to": "c"}]))
        errs = [m for lv, m in sbstore.lint() if lv == "error"]
        self.assertEqual(errs, [], errs)


class LayoutTests(unittest.TestCase):
    """layout：list = 按分组的模块化清单，graph = 关系图（默认）。"""
    def test_layout_list_accepted(self):
        obj = graph([data_node("a", brief="x")], groups=[{"id": "raw", "name": "原始", "nodes": ["a"]}])
        obj["layout"] = "list"
        self.assertEqual(sbstore.clean_definition("datasets", obj)["layout"], "list")

    def test_layout_graph_accepted_and_absent_by_default(self):
        obj = graph([data_node("a", brief="x")])
        obj["layout"] = "graph"
        self.assertEqual(sbstore.clean_definition("datasets", obj)["layout"], "graph")
        self.assertNotIn("layout", sbstore.clean_definition("datasets", graph([data_node("a", brief="x")])))

    def test_layout_rejects_other_values(self):
        obj = graph([data_node("a", brief="x")])
        obj["layout"] = "mindmap"
        with self.assertRaises(ValueError):
            sbstore.clean_definition("datasets", obj)


class FacetDefinitionTests(unittest.TestCase):
    """分面（facets）与矩阵（matrix）的定义校验。"""
    def review(self, **kw):
        base = {"title": "t", "description": "d", "kind": "cleaned_data", "source": "x.jsonl",
                "fields": [{"label": "格", "key": "cell"}], "rules": []}
        base.update(kw)
        return base

    def test_facets_accepted(self):
        obj = self.review(facets=[{"id": "cell", "label": "格", "field": "cell"},
                                  {"id": "ds", "label": "数据集", "field": "数据集"}])
        self.assertEqual(len(sbstore.clean_definition("reviews", obj)["facets"]), 2)

    def test_facet_needs_field(self):
        with self.assertRaises(ValueError):
            sbstore.clean_definition("reviews", self.review(facets=[{"id": "cell", "label": "格"}]))

    def test_duplicate_facet_id(self):
        with self.assertRaises(ValueError):
            sbstore.clean_definition("reviews", self.review(facets=[{"id": "a", "field": "x"}, {"id": "a", "field": "y"}]))

    def test_matrix_must_reference_declared_facets(self):
        obj = self.review(facets=[{"id": "cell", "field": "cell"}],
                          matrix={"rows": "cell", "cols": "nope", "metric": "时长秒"})
        with self.assertRaises(ValueError):
            sbstore.clean_definition("reviews", obj)

    def test_matrix_accepted(self):
        obj = self.review(facets=[{"id": "cell", "field": "cell"}, {"id": "t", "field": "turns"}],
                          matrix={"rows": "cell", "cols": "t", "metric": "时长秒"})
        self.assertEqual(sbstore.clean_definition("reviews", obj)["matrix"]["metric"], "时长秒")


class FacetScanTests(unittest.TestCase):
    """分面统计与矩阵：扫描一遍 jsonl，取值条数、交叉矩阵、按分面筛选后的行数都要对。"""
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sb-review-")
        self.assets = os.path.join(self.dir, "assets")
        os.makedirs(self.assets, exist_ok=True)
        self.rows = [{"数据集": "A", "cell": "单图单题", "turns": "单轮", "时长秒": 10},
                     {"数据集": "A", "cell": "单图单题", "turns": "多轮", "时长秒": 20},
                     {"数据集": "A", "cell": "多图单题", "turns": "单轮", "时长秒": 30},
                     {"数据集": "B", "cell": "多图单题", "turns": "多轮", "时长秒": 40}]
        self.src = os.path.join(self.assets, "s.jsonl")
        with open(self.src, "w", encoding="utf-8") as f:
            for r in self.rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def scanned(self):
        import review
        conf = {"facets": [{"id": "ds", "label": "数据集", "field": "数据集"},
                           {"id": "cell", "label": "格", "field": "cell"},
                           {"id": "turns", "label": "轮次", "field": "turns"}],
                "matrix": {"rows": "cell", "cols": "turns", "metric": "时长秒"}}
        res = {}
        review._scan(self.src, [], ("t",), conf=conf)
        return review, conf, review._scans[("t",)]

    def test_facet_value_counts(self):
        review, conf, scan = self.scanned()
        got = {f["id"]: {v["v"]: v["n"] for v in f["values"]} for f in review.facet_summary(conf, scan, {}, {})}
        self.assertEqual(got["ds"], {"A": 3, "B": 1})
        self.assertEqual(got["cell"], {"单图单题": 2, "多图单题": 2})

    def test_facet_counts_respect_other_filters(self):
        review, conf, scan = self.scanned()
        picked, idx = review._facet_filter(conf, scan, {"facets": json.dumps({"ds": "A"})})
        got = {f["id"]: {v["v"]: v["n"] for v in f["values"]} for f in review.facet_summary(conf, scan, picked, idx)}
        self.assertEqual(got["cell"], {"单图单题": 2, "多图单题": 1})     # 数据集=A 的 3 条里：单图 2、多图 1
        self.assertEqual(got["turns"], {"单轮": 2, "多轮": 1})

    def test_matrix_cell_counts_and_median(self):
        review, conf, scan = self.scanned()
        m = review.matrix_summary(conf, scan, {})
        cells = {(r, c): m["grid"][i][j] for i, r in enumerate(m["rows"]) for j, c in enumerate(m["cols"])}
        self.assertEqual(cells[("单图单题", "单轮")]["n"], 1)
        self.assertEqual(cells[("单图单题", "单轮")]["med"], 10)
        self.assertEqual(cells[("多图单题", "多轮")]["med"], 40)
        self.assertEqual(m["rows_id"], "cell")
        self.assertEqual(m["cols_id"], "turns")


class ReviewPageTests(_StateCase):
    """整页接口：分面筛选和「视图 + 规则」是「并且」的关系。"""
    def setUp(self):
        super().setUp()
        self.assets = sbstore.assets_dir()
        os.makedirs(self.assets, exist_ok=True)
        with open(os.path.join(self.assets, "s.jsonl"), "w", encoding="utf-8") as f:
            for r in [{"数据集": "A", "cell": "单图单题", "时长秒": 10}, {"数据集": "A", "cell": "多图单题", "时长秒": 20},
                      {"数据集": "B", "cell": "多图单题", "时长秒": 30}]:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.defn = {"title": "t", "description": "d", "source": "s.jsonl",
                     "fields": [{"label": "数据集", "key": "数据集"}], "rules": [],
                     "facets": [{"id": "ds", "label": "数据集", "field": "数据集"}],
                     "matrix": {"rows": "ds", "cols": "ds", "metric": "时长秒"}}

    def test_facet_filter_narrows_selection(self):
        import review
        out = review.page("r1", self.defn, {"view": "all", "page": 0, "size": 10, "_wait": 5, "facets": json.dumps({"ds": "B"})})
        self.assertEqual(out["selected"], 1)
        self.assertEqual([i["row"]["数据集"] for i in out["items"]], ["B"])
        self.assertEqual(out["picked"], {"ds": "B"})

    def test_without_facets_all_rows(self):
        import review
        out = review.page("r1", self.defn, {"view": "all", "page": 0, "size": 10, "_wait": 5})
        self.assertEqual(out["selected"], 3)
        self.assertEqual(out["picked"], {})

    def test_unknown_facet_value_is_ignored(self):
        import review
        out = review.page("r1", self.defn, {"view": "all", "page": 0, "size": 10, "_wait": 5, "facets": json.dumps({"ds": "不存在"})})
        self.assertEqual(out["selected"], 3)


class AssetRootTests(_StateCase):
    """额外只读资源根（SB_ASSET_ROOTS）：视频留在共享盘，不用复制进 state/assets。"""
    def setUp(self):
        super().setUp()
        self.ext = tempfile.mkdtemp(prefix="sb-ext-")
        self.secret = tempfile.mkdtemp(prefix="sb-secret-")
        os.makedirs(os.path.join(self.ext, "videos"), exist_ok=True)
        with open(os.path.join(self.ext, "videos", "a.mp4"), "wb") as f:
            f.write(b"x")
        with open(os.path.join(self.secret, "pw.txt"), "w") as f:
            f.write("secret")
        os.symlink(self.secret, os.path.join(self.ext, "escape"))              # 根里指向根外的软链
        self._old_roots = os.environ.get("SB_ASSET_ROOTS")
        os.environ["SB_ASSET_ROOTS"] = f"rel={self.ext}"

    def tearDown(self):
        if self._old_roots is None:
            os.environ.pop("SB_ASSET_ROOTS", None)
        else:
            os.environ["SB_ASSET_ROOTS"] = self._old_roots
        shutil.rmtree(self.ext, ignore_errors=True)
        shutil.rmtree(self.secret, ignore_errors=True)
        super().tearDown()

    def test_extra_root_file_resolves_in_place(self):
        got = sbstore.resolve_asset("rel/videos/a.mp4")
        self.assertEqual(got, os.path.realpath(os.path.join(self.ext, "videos", "a.mp4")))

    def test_dotdot_rejected(self):
        self.assertIsNone(sbstore.resolve_asset("rel/../etc/passwd"))
        self.assertIsNone(sbstore.resolve_asset("rel/videos/../../x"))

    def test_symlink_escaping_extra_root_rejected(self):
        self.assertIsNone(sbstore.resolve_asset("rel/escape/pw.txt"))

    def test_extra_root_is_read_only(self):
        self.assertIsNone(sbstore.resolve_asset("rel/videos/a.mp4", writable=True))
        self.assertIsNotNone(sbstore.resolve_asset("plain/x.jsonl", writable=True))     # 主根可写

    def test_unknown_alias_falls_back_to_primary_root(self):
        got = sbstore.resolve_asset("nope/a.mp4")
        self.assertTrue(got.startswith(sbstore.assets_dir()))

    def test_alias_alone_is_not_a_file(self):
        """只写别名、没有子路径：落到主根（不会把整个外部根暴露成一个路径）。"""
        got = sbstore.resolve_asset("rel")
        self.assertTrue(got.startswith(sbstore.assets_dir()))

    def test_bad_registrations_ignored(self):
        os.environ["SB_ASSET_ROOTS"] = f"../bad={self.ext}:ok={self.ext}:missing=/no/such/dir:=/tmp:noeq"
        self.assertEqual(sorted(sbstore.asset_roots()), ["ok"])

    def test_review_rows_cannot_write_into_extra_root(self):
        import sb
        with self.assertRaises(ValueError):
            sb.review_rows([{"a": 1}], "rel/out.jsonl")
        self.assertFalse(os.path.exists(os.path.join(self.ext, "out.jsonl")))

    def test_review_source_may_live_in_extra_root(self):
        with open(os.path.join(self.ext, "s.jsonl"), "w", encoding="utf-8") as f:
            f.write('{"a": 1}\n')
        obj = {"title": "t", "description": "d", "kind": "cleaned_data", "source": "rel/s.jsonl",
               "fields": [{"label": "a", "key": "a"}], "rules": []}
        self.assertEqual(sbstore.clean_definition("reviews", obj)["source"], "rel/s.jsonl")


class RoundTripTests(_StateCase):
    def test_dataset_groups_and_brief_survive_roundtrip(self):
        self._define("datasets", "g", graph([data_node("a", brief="一句话说明")],
                                            groups=[{"id": "raw", "name": "原始", "desc": "d", "nodes": ["a"]}]))
        path = os.path.join(self.dir, "datasets", "g.json")
        with open(path, encoding="utf-8") as f:
            back = json.load(f)
        self.assertEqual(back["groups"][0]["name"], "原始")
        self.assertEqual(back["nodes"][0]["brief"], "一句话说明")


if __name__ == "__main__":
    unittest.main(verbosity=2)
