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
