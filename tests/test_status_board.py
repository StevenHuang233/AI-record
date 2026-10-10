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


def clip_row(i, cell="单图单题", turns="单轮", dur=10.0, pages=1, tp=0, asks=1, style="稳当拍摄", text=None):
    """按 video_eval.v4.jsonl 的结构造一行。"""
    ask_list = [{"turn": k + 1, "t": 2.0 + k, "page": 0, "kind": "first" if k == 0 else "followup",
                 "slot_id": f"s{i}#0", "instruction": f"指令{i}-{k}"} for k in range(asks)]
    return {"item_id": f"item-{i}", "cell": cell, "turns": turns,
            "images": [f"/x/handheld_v4/runs/release_t_v2.0/frames/{i}_1of1.jpg"],
            "video": f"/x/handheld_v4/runs/release_t_v2.0/videos/{i}.mp4", "instruction": text or f"指令{i}",
            "题面": f"题面{i}", "target": {"num": 1, "stem": f"题面{i}", "gold": "A"}, "gold_answer": "A",
            "n_pages": pages, "target_page": tp, "source": "demo", "asks": ask_list,
            "page_windows": [{"page": 0, "appear_s": 0.5, "disappear_s": dur - 1}],
            "scored": [{"slot_id": f"s{i}#0", "num": 1, "page": 0, "stem": f"题面{i}", "gold": "A",
                        "科目": "数学", "学段": "高中"}],
            "video_meta": {"duration": dur, "fps": 30, "width": 720, "height": 1280, "style": "steady",
                           "style_name": style, "optics": ["refocus"], "seed": 7, "render_version": "v4.7",
                           "stages": [[{"type": "hold", "name": "enter", "start": 0, "end": 1, "amount": 0.8,
                                        "direction": "right"}]],
                           "segments": [{"seconds": 4.0, "motion_seconds": 2.0, "enter_seconds": 1.0,
                                         "stage_count": 1, "rules_applied": []}],
                           "transitions": [],
                           "action_metrics": [[{"type": "hold", "name": "enter", "start": 0, "end": 1,
                                                "travel_px": 10, "zoom_percent": 1, "tilt_range": 0.1,
                                                "focus_peak": 0, "effective": None}]]}}


class ClipViewTests(unittest.TestCase):
    def test_media_map_anchor_rewrites_absolute_path(self):
        import clipview
        mm = {"anchor": "release_", "prefix": "releases/"}
        self.assertEqual(clipview.map_path("/data/a/runs/release_k_v2.0/videos/a.mp4", mm),
                         "releases/release_k_v2.0/videos/a.mp4")

    def test_media_map_after(self):
        import clipview
        self.assertEqual(clipview.map_path("/q/runs/r1/v.mp4", {"after": "/runs/", "prefix": "R/"}), "R/r1/v.mp4")

    def test_unmapped_absolute_path_is_dropped_not_leaked(self):
        import clipview
        self.assertIsNone(clipview.map_path("/secret/dir/a.mp4", {"anchor": "release_", "prefix": "releases/"}))
        self.assertIsNone(clipview.map_path("/secret/dir/a.mp4", {}))

    def test_relative_path_kept(self):
        import clipview
        self.assertEqual(clipview.map_path("videos/a.mp4", {}), "videos/a.mp4")

    def test_get_first_candidate_with_value_wins(self):
        import clipview
        self.assertEqual(clipview.get({"a": "", "b": {"c": 5}}, ["a", "b.c"]), 5)
        self.assertEqual(clipview.get({"l": [10, 20]}, ["l.1"]), 20)
        self.assertIsNone(clipview.get({}, ["x"]))
        self.assertEqual(clipview.get({"z": 0}, ["z"]), 0)

    def test_summarize_cells_and_styles(self):
        import clipview
        cfg = clipview.config({})
        gal = {1: clipview.compact(clip_row(1, dur=10), cfg), 2: clipview.compact(clip_row(2, dur=30), cfg),
               3: clipview.compact(clip_row(3, cell="多图单题", turns="多轮", dur=20, pages=3, asks=3,
                                            style="手抖"), cfg)}
        sm = clipview.summarize(gal)
        c = sm["cells"]["单图单题|单轮"]
        self.assertEqual((c["n"], c["dur_med"], c["dur_min"], c["dur_max"]), (2, 20.0, 10.0, 30.0))
        self.assertEqual(sm["cells"]["多图单题|多轮"]["asks_max"], 3)
        self.assertEqual(sm["styles"], {"稳当拍摄": 2, "手抖": 1})

    def test_select_filters_and_sort(self):
        import clipview
        cfg = clipview.config({})
        rows = [clip_row(1, dur=10, pages=1, asks=1), clip_row(2, dur=30, pages=3, asks=4, tp=2),
                clip_row(3, dur=20, pages=2, asks=2, tp=None, style="手抖", text="找一找蓝色的鸟")]
        gal = {i + 1: clipview.compact(r, cfg) for i, r in enumerate(rows)}
        self.assertEqual(clipview.select(gal, {"pages": "1"}), [1])
        self.assertEqual(clipview.select(gal, {"pages": "multi"}), [2, 3])
        self.assertEqual(clipview.select(gal, {"target": "known"}), [1, 2])
        self.assertEqual(clipview.select(gal, {"target": "unknown"}), [3])
        self.assertEqual(clipview.select(gal, {"style": "手抖"}), [3])
        self.assertEqual(clipview.select(gal, {"q": "蓝色"}), [3])
        self.assertEqual(clipview.select(gal, {"q": "ITEM-2"}), [2])
        self.assertEqual(clipview.select(gal, {"sort": "dur_desc"}), [2, 3, 1])
        self.assertEqual(clipview.select(gal, {"sort": "dur_asc"}), [1, 3, 2])
        self.assertEqual(clipview.select(gal, {"sort": "asks"}), [2, 3, 1])
        self.assertEqual(clipview.select(gal, {"cell": "不存在"}), [])

    def test_detail_has_everything_the_drawer_needs(self):
        import clipview
        cfg = clipview.config({"media_map": {"anchor": "release_", "prefix": "releases/"}})
        d = clipview.detail(clip_row(5, asks=2), 5, cfg)
        self.assertEqual(d["video"], "releases/release_t_v2.0/videos/5.mp4")
        self.assertEqual(d["frames"], ["releases/release_t_v2.0/frames/5_1of1.jpg"])
        self.assertEqual([a["turn"] for a in d["asks"]], [1, 2])
        self.assertEqual(d["detail"]["canvas"], "720×1280")
        self.assertEqual(d["detail"]["pages_stages"][0][0]["name"], "enter")
        self.assertEqual(d["detail"]["scored"][0]["asks"][0]["turn"], 1)
        self.assertEqual(d["gold"], "A")
        self.assertEqual(d["question"], "题面5")

    def test_gallery_config_validation(self):
        ok = {"title": "t", "description": "d", "kind": "cleaned_data", "source": "x.jsonl",
              "fields": [{"label": "a", "key": "a"}], "rules": [],
              "gallery": {"media_map": {"anchor": "release_", "prefix": "releases/"}, "group": "G",
                          "title": ["instruction", "题面"]}}
        self.assertIn("gallery", sbstore.clean_definition("reviews", ok))
        for bad in ({"nope": 1}, {"title": 5}, {"media_map": {"prefix": "/abs/"}}, {"media_map": {"prefix": "../x/"}},
                    {"media_map": {"zzz": "a"}}, {"cells": []}, {"title": []}, "str"):
            with self.assertRaises(ValueError, msg=str(bad)):
                sbstore.clean_definition("reviews", {**ok, "gallery": bad})


class GalleryApiTests(_StateCase):
    """图库接口：一页卡片 + 8 格统计 + 筛选 ∩ 校验视图；单条详情带标记。"""
    def setUp(self):
        super().setUp()
        import review
        self.review = review
        rows = [clip_row(1, dur=10), clip_row(2, turns="多轮", dur=30, asks=3),
                clip_row(3, cell="多图单题", dur=20, pages=3, tp=2), clip_row(4, dur=5, text="特别的一条")]
        rows[3]["cell"] = ""
        os.makedirs(sbstore.assets_dir(), exist_ok=True)
        with open(os.path.join(sbstore.assets_dir(), "c.jsonl"), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.d = {"title": "t", "description": "d", "kind": "cleaned_data", "source": "c.jsonl",
                  "fields": [{"label": "指令", "key": "instruction"}],
                  "rules": [{"id": "has_cell", "field": "cell", "op": "nonempty", "label": "格必须填"}],
                  "gallery": {"media_map": {"anchor": "release_", "prefix": "releases/"}}}
        sbstore.define("reviews", "r1", self.d)

    def page(self, **q):
        return self.review.gallery_page("r1", self.d, {"_wait": 5, **q})

    def test_page_lists_cards_with_mapped_media(self):
        out = self.page()
        self.assertEqual(out["count"], 4)
        first = out["items"][0]
        self.assertEqual(first["thumb"], "releases/release_t_v2.0/frames/1_1of1.jpg")
        self.assertTrue(first["has_video"])
        self.assertEqual(first["n_asks"], 1)

    def test_summary_is_independent_of_filters(self):
        a, b = self.page(), self.page(cell="多图单题")
        self.assertEqual(a["summary"], b["summary"])
        self.assertEqual(b["count"], 1)

    def test_pagination(self):
        out = self.page(limit=3, offset=0)
        self.assertEqual((len(out["items"]), out["count"]), (3, 4))
        self.assertEqual(len(self.page(limit=3, offset=3)["items"]), 1)

    def test_sort_and_search(self):
        self.assertEqual([i["id"] for i in self.page(sort="dur_desc")["items"]],
                         ["item-2", "item-3", "item-1", "item-4"])
        self.assertEqual([i["id"] for i in self.page(q="特别")["items"]], ["item-4"])

    def test_view_fail_intersects_gallery_filters(self):
        self.assertEqual([i["id"] for i in self.page(view="fail")["items"]], ["item-4"])
        self.assertEqual(self.page(view="fail", cell="多图单题")["count"], 0)
        self.assertEqual(self.page(rule="has_cell")["count"], 1)
        self.assertEqual(self.page(view="pass")["count"], 3)

    def test_marks_show_on_cards_and_in_views(self):
        sbstore.set_marks("r1", [{"line": 2, "v": "pass"}, {"line": 3, "v": "fail", "note": "糊"}])
        out = self.page()
        marks = {i["id"]: i.get("mark") for i in out["items"]}
        self.assertEqual(marks["item-2"], {"v": "pass"})
        self.assertEqual(marks["item-3"], {"v": "fail", "note": "糊"})
        self.assertIsNone(marks["item-1"])
        self.assertEqual(out["marked"], {"pass": 1, "weak": 0, "fail": 1, "doubt": 0})
        self.assertEqual([i["id"] for i in self.page(view="mfail")["items"]], ["item-3"])
        self.assertEqual(self.page(view="unmarked")["count"], 2)

    def test_item_detail_includes_mark_and_rule_failure(self):
        sbstore.set_marks("r1", [{"line": 4, "v": "doubt", "note": "再看看"}])
        self.page()
        out = self.review.gallery_item("r1", self.d, 4)
        self.assertEqual(out["mark"]["v"], "doubt")
        self.assertEqual(out["fails"], ["has_cell"])
        self.assertEqual(out["video"], "releases/release_t_v2.0/videos/4.mp4")

    def test_item_out_of_range(self):
        self.assertIn("error", self.review.gallery_item("r1", self.d, 99))

    def test_not_a_gallery_set(self):
        d = {k: v for k, v in self.d.items() if k != "gallery"}
        self.assertIn("error", self.review.gallery_page("r1", d, {}))
        self.assertIn("error", self.review.gallery_item("r1", d, 1))


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
