# -*- coding: utf-8 -*-
r"""
定时任务声明（cron / name）的回归测试。

面板拉库时靠脚本头部的注释声明认定时规则与任务名，认不出就不建任务。这组测试锁住两件事：
    1. 入口脚本 lynkco_qinglong.py 的声明要能被面板的正则认出来；
    2. 其余脚本（尤其单文件版）不能带声明，否则整仓拉库会建出多条重复任务。

判定口径与 tools/check_cron_decl.py 一致，对应 daidai-panel server/service/subscription.go
的 cronLabelPrefixRe / subscriptionTaskNameLabelRe 与 pkg/cron 的 5 段、6 段规则。
"""
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_HERE)                 # LynkCoHelper/
_TOOLS = os.path.join(_PKG, "tools")
_REPO_ROOT = os.path.dirname(_PKG)
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import check_cron_decl as decl  # noqa: E402
import build_qinglong_single as builder  # noqa: E402


class CronExpressionTest(unittest.TestCase):
    """cron 取值与合法性判定。"""

    def test_five_fields(self):
        self.assertTrue(decl.cron_valid("8 8 * * *"))
        self.assertTrue(decl.cron_valid("0 */6 * * *"))

    def test_six_fields(self):
        self.assertTrue(decl.cron_valid("0 8 8 * * *"))
        self.assertTrue(decl.cron_valid("0 0 0 * * 1"))

    def test_invalid_field_count(self):
        self.assertFalse(decl.cron_valid("8 8 * *"))
        self.assertFalse(decl.cron_valid("0 8 8 * * * *"))

    def test_invalid_range(self):
        self.assertFalse(decl.cron_valid("99 8 * * *"))   # 分钟 99
        self.assertFalse(decl.cron_valid("0 25 * * *"))   # 小时 25


class ExtractCronTest(unittest.TestCase):
    """各种注释写法都要认得出来（面板注释里列出的写法）。"""

    def test_python_hash(self):
        self.assertEqual(decl.extract_cron("# cron: 8 8 * * *"), "8 8 * * *")

    def test_hash_without_space_and_colon(self):
        self.assertEqual(decl.extract_cron("#cron 8 9,10,11 * * *"), "8 9,10,11 * * *")

    def test_jsdoc_star(self):
        self.assertEqual(decl.extract_cron(" * cron: 12 8 * * *"), "12 8 * * *")

    def test_at_cron(self):
        self.assertEqual(decl.extract_cron(" * @cron 0 0 * * *"), "0 0 * * *")

    def test_double_slash(self):
        self.assertEqual(decl.extract_cron("// cron: 0 0 * * *"), "0 0 * * *")

    def test_quoted_value(self):
        self.assertEqual(decl.extract_cron('# cron: "0 8 * * *"'), "0 8 * * *")

    def test_trailing_comment(self):
        # 行尾跟了说明文字时，只取前 5 个字段
        self.assertEqual(decl.extract_cron("# cron: 8 8 * * * 每天 08:08 执行一次"), "8 8 * * *")

    def test_not_a_declaration(self):
        # 普通代码行不能误判成声明
        self.assertEqual(decl.extract_cron("                cron = dict(payload)"), "")
        self.assertEqual(decl.extract_cron("定时任务规则 8 8 * * *"), "")


class TaskNameTest(unittest.TestCase):
    """任务名取值：name 标签 > 文件名回退。"""

    def _scan(self, text: str) -> dict:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(text)
            path = f.name
        try:
            return decl.scan_file(path)
        finally:
            os.unlink(path)

    def test_name_label(self):
        info = self._scan("# cron: 8 8 * * *\n# name: 领克·每日签到分享\nprint(1)\n")
        self.assertEqual(info["cron"], "8 8 * * *")
        self.assertEqual(info["name"], "领克·每日签到分享")

    def test_fallback_to_filename(self):
        info = self._scan("# cron: 8 8 * * *\nprint(1)\n")
        self.assertEqual(info["name"], info["fallback"])
        self.assertTrue(info["name"])

    def test_env_name_wins(self):
        info = self._scan("const $ = new Env('青龙风格任务名')\n# name: 注释里的名字\n")
        self.assertEqual(info["name"], "青龙风格任务名")

    def test_only_first_120_lines(self):
        body = ["print(%d)" % i for i in range(200)]
        body.append("# cron: 8 8 * * *")
        info = self._scan("\n".join(body))
        self.assertEqual(info["cron"], "")


class RepoDeclarationTest(unittest.TestCase):
    """仓库层面：该建的建、不该建的不建。"""

    def test_entry_declares_cron(self):
        entry = os.path.join(_PKG, "lynkco_qinglong.py")
        info = decl.scan_file(entry)
        self.assertEqual(info["cron"], "8 8 * * *")
        self.assertEqual(info["name"], "领克·每日签到分享")

    def test_only_entry_declares(self):
        declared = [rel for rel, _cron, _name in decl.scan_repo(_REPO_ROOT)]
        expected = os.path.join("LynkCoHelper", "lynkco_qinglong.py")
        self.assertEqual([p.replace("\\", "/") for p in declared], [expected.replace("\\", "/")])

    def test_single_file_has_no_declaration(self):
        single = os.path.join(_REPO_ROOT, "qinglong", "lynkco_qinglong_single.py")
        if not os.path.exists(single):
            self.skipTest("单文件版未生成")
        self.assertEqual(decl.scan_file(single)["cron"], "")


class BuilderStripTest(unittest.TestCase):
    """打包器要剥掉声明，也要能按 --cron 生成。"""

    def test_strip(self):
        text = "# cron: 8 8 * * *\n# name: 领克·每日签到分享\nimport os\n"
        self.assertEqual(builder._strip_task_declarations(text), "import os")

    def test_declare_block_empty_by_default(self):
        self.assertEqual(builder._declare_block("", "x"), "")

    def test_declare_block(self):
        self.assertEqual(builder._declare_block("8 8 * * *", "领克"), "# cron: 8 8 * * *\n# name: 领克\n")

    def test_build_without_cron(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "single.py")
            builder.build(out)
            self.assertEqual(decl.scan_file(out)["cron"], "")

    def test_build_with_cron(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "single.py")
            builder.build(out, cron="0 8 8 * * *", task_name="领克·单文件版")
            info = decl.scan_file(out)
            self.assertEqual(info["cron"], "0 8 8 * * *")
            self.assertEqual(info["name"], "领克·单文件版")


if __name__ == "__main__":
    unittest.main()
