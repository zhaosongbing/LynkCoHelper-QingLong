#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lynkco_ql_tasks.py（青龙定时任务自动注册）的单元测试。

不依赖真实青龙面板：用 FakePanel 替换模块内的 _http_json，模拟 /open/crons 等
接口的真实返回结构，覆盖 cron 校验、间隔折算、清单解析、幂等同步、去重、
清理、重试、鉴权失败、状态持久化等路径。

运行：
    cd LynkCoHelper && python3 -m unittest discover -s tests -v
或：
    python3 tests/test_ql_tasks.py
"""
import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_DIR = os.path.dirname(_HERE)
if _PACKAGE_DIR not in sys.path:
    sys.path.insert(0, _PACKAGE_DIR)

import lynkco_ql_tasks as m  # noqa: E402


class FakePanel:
    """模拟青龙面板的 /open/* 接口（含 token 校验与任务表）。"""

    def __init__(self, token: str = "fake-token-abcdefgh", tz: str = "Asia/Shanghai"):
        self.token = token
        self.tz = tz
        self.crons = []
        self.calls = []
        self.next_id = 1
        self.fail_first = 0          # 前 N 次请求直接抛网络异常
        self.force_status = None     # 强制返回某个 HTTP 状态码
        self.force_code = None       # 强制返回某个业务 code
        self.fail_post = False       # POST /open/crons 固定失败（模拟面板写入异常）

    # ---------------------------------------------------------------- 工具

    def _auth_ok(self, headers) -> bool:
        auth = (headers or {}).get("Authorization", "")
        return auth == f"Bearer {self.token}"

    def count(self, method: str, path: str) -> int:
        """统计某接口的调用次数（url 需去掉面板地址前缀再比对）。"""
        hits = 0
        for call_method, url, _payload in self.calls:
            call_path = url.split("?", 1)[0].replace("http://127.0.0.1:5700", "")
            if call_method == method.upper() and call_path == path:
                hits += 1
        return hits

    # ---------------------------------------------------------------- 接口

    def handle(self, method: str, url: str, headers: dict = None, payload=None, timeout: int = 15):
        self.calls.append((method.upper(), url, payload))
        if self.fail_first > 0:
            self.fail_first -= 1
            raise RuntimeError("模拟网络异常")

        path = url.split("?", 1)[0].replace("http://127.0.0.1:5700", "")
        query = url.split("?", 1)[1] if "?" in url else ""

        if path == "/open/auth/token":
            if "client_id=fake" in query:
                return 200, {"code": 200, "data": {"token": self.token, "expiration": 9999999999}}
            return 401, {"code": 401, "message": "client_id/secret 无效"}

        if self.force_status:
            return self.force_status, {"code": self.force_code or self.force_status, "message": "模拟错误"}
        if not self._auth_ok(headers):
            return 401, {"code": 401, "message": "token 无效"}

        if path == "/open/envs":
            return 200, {"code": 200, "data": {"data": [{"name": "TZ", "value": self.tz}], "total": 1}}

        if path == "/open/crons":
            if method.upper() == "GET":
                return 200, {"code": 200, "data": {"data": list(self.crons), "total": len(self.crons)}}
            if method.upper() == "POST":
                if self.fail_post:
                    return 500, {"code": 500, "message": "模拟写入失败"}
                if not isinstance(payload, dict):
                    return 400, {"code": 400, "message": "创建任务需要单个对象，不接受数组"}
                cron = dict(payload)
                cron["id"] = self.next_id
                cron.setdefault("isDisabled", 0)
                self.next_id += 1
                self.crons.append(cron)
                return 200, {"code": 200, "data": {"id": cron["id"]}}
            if method.upper() == "PUT":
                for cron in self.crons:
                    if cron.get("id") == (payload or {}).get("id"):
                        cron.update({k: v for k, v in payload.items() if k != "id"})
                        return 200, {"code": 200}
                return 404, {"code": 404, "message": "任务不存在"}
            if method.upper() == "DELETE":
                ids = set(payload or [])
                self.crons = [c for c in self.crons if c.get("id") not in ids]
                return 200, {"code": 200}

        if path.startswith("/open/crons/") and method.upper() == "PUT":
            return 200, {"code": 200}

        return 404, {"code": 404, "message": f"未知接口 {path}"}


def write_json(path: str, data) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


class CronValidationTest(unittest.TestCase):
    """cron 表达式与间隔折算的校验。"""

    def test_valid_expressions(self):
        for expr in ("8 8 * * *", "*/5 * * * *", "0 0 1 1 *", "30 6 * * 1-5",
                     "0 8 * * MON", "0 0 1 JAN *", "*/30 * * * * *", "0 0 L * *".replace("L", "28")):
            self.assertEqual(m.validate_cron(expr), expr, f"{expr} 应合法")

    def test_invalid_field_count(self):
        for expr in ("8 8 * *", "* * * * * * *", ""):
            with self.assertRaises(m.TaskConfigError):
                m.validate_cron(expr)

    def test_out_of_range(self):
        for expr in ("60 8 * * *", "8 24 * * *", "8 8 32 * *", "8 8 * 13 *", "8 8 * * 8"):
            with self.assertRaises(m.TaskConfigError):
                m.validate_cron(expr)

    def test_bad_step(self):
        for expr in ("*/0 * * * *", "*/-1 * * * *", "*/70 * * * *"):
            with self.assertRaises(m.TaskConfigError):
                m.validate_cron(expr)

    def test_bad_token(self):
        with self.assertRaises(m.TaskConfigError):
            m.validate_cron("abc 8 * * *")
        with self.assertRaises(m.TaskConfigError):
            m.validate_cron("8 8 * FOO *")

    def test_interval_to_cron(self):
        self.assertEqual(m.interval_to_cron(45), "*/45 * * * * *")
        self.assertEqual(m.interval_to_cron("30m"), "*/30 * * * *")
        self.assertEqual(m.interval_to_cron("2h"), "0 */2 * * *")
        self.assertEqual(m.interval_to_cron("1d"), "0 0 */1 * *")
        self.assertEqual(m.interval_to_cron(1800), "*/30 * * * *")

    def test_interval_invalid(self):
        for value in (0, -1, "abc", True, "40d"):
            with self.assertRaises(m.TaskConfigError):
                m.interval_to_cron(value)


class ManifestTest(unittest.TestCase):
    """任务清单解析与校验。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.scripts = os.path.join(self.tmp.name, "scripts")
        os.makedirs(self.scripts, exist_ok=True)
        self._saved_dirs = m.SCRIPTS_DIRS
        self._saved_repo_env = os.environ.get("LYNKCO_QL_REPO")
        m.SCRIPTS_DIRS = [self.scripts]
        self.addCleanup(lambda: setattr(m, "SCRIPTS_DIRS", self._saved_dirs))
        self.addCleanup(lambda: os.environ.pop("LYNKCO_QL_REPO", None)
                        if self._saved_repo_env is None else os.environ.__setitem__("LYNKCO_QL_REPO", self._saved_repo_env))

    def test_placeholders_and_defaults(self):
        os.environ["LYNKCO_QL_REPO"] = "LynkCoHelper-QingLong"
        path = write_json(os.path.join(self.tmp.name, "tasks.json"), {
            "defaults": {"labels": ["LynkCo"], "timezone": "Asia/Shanghai"},
            "tasks": [{
                "name": "签到",
                "command": "task {repo}/LynkCoHelper/lynkco_qinglong.py",
                "script": "{repo}/LynkCoHelper/lynkco_qinglong.py",
                "schedule": "8 8 * * *",
            }],
        })
        tasks = m.load_tasks_file(path)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["command"], "task LynkCoHelper-QingLong/LynkCoHelper/lynkco_qinglong.py")
        self.assertEqual(tasks[0]["labels"], ["LynkCo"])
        self.assertEqual(tasks[0]["isDisabled"], 0)
        self.assertEqual(tasks[0]["timezone"], "Asia/Shanghai")

    def test_duplicate_name_or_command_rejected(self):
        base = {"schedule": "8 8 * * *", "command": "task x.py"}
        path = write_json(os.path.join(self.tmp.name, "dup.json"),
                          [dict(base, name="A"), dict(base, name="A")])
        with self.assertRaises(m.TaskConfigError):
            m.load_tasks_file(path)
        path2 = write_json(os.path.join(self.tmp.name, "dup2.json"),
                           [{"name": "A", **base}, {"name": "B", **base}])
        with self.assertRaises(m.TaskConfigError):
            m.load_tasks_file(path2)

    def test_missing_trigger_rejected(self):
        path = write_json(os.path.join(self.tmp.name, "bad.json"), [{"name": "A", "command": "task x.py"}])
        with self.assertRaises(m.TaskConfigError):
            m.load_tasks_file(path)

    def test_both_trigger_rejected(self):
        path = write_json(os.path.join(self.tmp.name, "bad2.json"),
                          [{"name": "A", "command": "task x.py", "schedule": "8 8 * * *", "interval": "1h"}])
        with self.assertRaises(m.TaskConfigError):
            m.load_tasks_file(path)

    def test_validate_reports_missing_script(self):
        path = write_json(os.path.join(self.tmp.name, "v.json"),
                          [{"name": "A", "command": "task 不存在.py", "schedule": "8 8 * * *"}])
        report = m.validate_tasks_file(path)
        self.assertEqual(report["total"], 1)
        self.assertEqual(report["valid"], 0)
        self.assertEqual(report["issues"][0]["level"], "warning")

    def test_validate_passes_when_script_exists(self):
        os.makedirs(os.path.join(self.scripts, "repo", "LynkCoHelper"), exist_ok=True)
        open(os.path.join(self.scripts, "repo", "LynkCoHelper", "lynkco_qinglong.py"), "w").close()
        path = write_json(os.path.join(self.tmp.name, "ok.json"), [
            {"name": "A", "command": "task repo/LynkCoHelper/lynkco_qinglong.py",
             "script": "repo/LynkCoHelper/lynkco_qinglong.py", "schedule": "8 8 * * *"}])
        report = m.validate_tasks_file(path)
        self.assertEqual(report["issues"], [])


class SyncTest(unittest.TestCase):
    """同步主流程：幂等、去重、更新、清理、dry-run。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.scripts = os.path.join(self.tmp.name, "scripts")
        os.makedirs(self.scripts, exist_ok=True)
        self.state = os.path.join(self.tmp.name, "state.json")
        self._saved_dirs = m.SCRIPTS_DIRS
        m.SCRIPTS_DIRS = [self.scripts]
        self.addCleanup(lambda: setattr(m, "SCRIPTS_DIRS", self._saved_dirs))
        self._saved_http = m._http_json
        self.addCleanup(lambda: setattr(m, "_http_json", self._saved_http))

        self.panel = FakePanel()
        m._http_json = self.panel.handle
        self.api = m.QingLongAPI(token=self.panel.token, retries=1)

    def _manifest(self, tasks):
        return write_json(os.path.join(self.tmp.name, "tasks.json"), {"tasks": tasks})

    def _daily(self, **kwargs):
        spec = {"name": "领克·每日签到", "command": "task repo/LynkCoHelper/lynkco_qinglong.py",
                "script": "repo/LynkCoHelper/lynkco_qinglong.py", "schedule": "8 8 * * *"}
        spec.update(kwargs)
        return spec

    def test_create_then_idempotent(self):
        path = self._manifest([self._daily()])
        first = m.sync(m.load_tasks_file(path), api=self.api, state_path=self.state)
        self.assertEqual((first["created"], first["updated"], first["unchanged"]), (1, 0, 0))
        self.assertEqual(self.panel.count("POST", "/open/crons"), 1)

        second = m.sync(m.load_tasks_file(path), api=self.api, state_path=self.state)
        self.assertEqual((second["created"], second["unchanged"]), (0, 1))
        self.assertEqual(self.panel.count("POST", "/open/crons"), 1, "重复同步不应再次创建")
        self.assertEqual(len(self.panel.crons), 1)

    def test_dedup_by_command_even_if_name_differs(self):
        """青龙「自动添加任务」先用默认规则建出来的任务，应按 command 认出来并校正。"""
        self.panel.crons.append({"id": 99, "name": "lynkco_qinglong", "command": "task repo/LynkCoHelper/lynkco_qinglong.py",
                                 "schedule": "0 3 * * *", "isDisabled": 0, "labels": []})
        path = self._manifest([self._daily()])
        result = m.sync(m.load_tasks_file(path), api=self.api, state_path=self.state)
        self.assertEqual((result["created"], result["updated"]), (0, 1))
        self.assertEqual(len(self.panel.crons), 1, "不应产生重复任务")
        self.assertEqual(self.panel.crons[0]["schedule"], "8 8 * * *")
        self.assertEqual(self.panel.crons[0]["name"], "领克·每日签到")

    def test_no_update_keeps_existing(self):
        self.panel.crons.append({"id": 5, "name": "领克·每日签到", "command": "task repo/LynkCoHelper/lynkco_qinglong.py",
                                 "schedule": "0 3 * * *", "isDisabled": 0, "labels": ["LynkCo"]})
        path = self._manifest([self._daily()])
        result = m.sync(m.load_tasks_file(path), api=self.api, update=False, state_path=self.state)
        self.assertEqual(result["created"], 0)
        self.assertEqual(self.panel.crons[0]["schedule"], "0 3 * * *")

    def test_prune_removes_stale_task(self):
        self.panel.crons.append({"id": 7, "name": "旧任务", "command": "task repo/old.py",
                                 "schedule": "0 3 * * *", "isDisabled": 0, "labels": []})
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"synced_at": None, "tasks": {"旧任务": {"id": 7, "command": "task repo/old.py"}}}, f)
        path = self._manifest([self._daily()])
        result = m.sync(m.load_tasks_file(path), api=self.api, prune=True, state_path=self.state)
        self.assertEqual(result["pruned"], 1)
        self.assertEqual([c["command"] for c in self.panel.crons], ["task repo/LynkCoHelper/lynkco_qinglong.py"])

    def test_dry_run_makes_no_write(self):
        path = self._manifest([self._daily()])
        result = m.sync(m.load_tasks_file(path), api=self.api, dry_run=True, state_path=self.state)
        self.assertEqual(result["created"], 1)
        self.assertEqual(len(self.panel.crons), 0, "dry-run 不能真的创建")
        self.assertFalse(os.path.exists(self.state), "dry-run 不应写状态文件")

    def test_disabled_task_created_disabled(self):
        spec = {"name": "停用示例", "command": "task repo/LynkCoHelper/lynkco_qinglong.py",
                "script": "repo/LynkCoHelper/lynkco_qinglong.py", "interval": "6h", "enabled": False}
        path = self._manifest([spec])
        m.sync(m.load_tasks_file(path), api=self.api, state_path=self.state)
        self.assertEqual(self.panel.crons[0]["isDisabled"], 1)
        self.assertEqual(self.panel.crons[0]["schedule"], "0 */6 * * *")

    def test_run_now_triggers_execution(self):
        path = self._manifest([self._daily()])
        m.sync(m.load_tasks_file(path), api=self.api, run_now=True, state_path=self.state)
        self.assertEqual(self.panel.count("PUT", "/open/crons/run"), 1)

    def test_state_persists_ids(self):
        path = self._manifest([self._daily()])
        m.sync(m.load_tasks_file(path), api=self.api, state_path=self.state)
        with open(self.state, "r", encoding="utf-8") as f:
            state = json.load(f)
        self.assertIn("领克·每日签到", state["tasks"])
        self.assertEqual(state["tasks"]["领克·每日签到"]["id"], self.panel.crons[0]["id"])
        self.assertIsNotNone(state["synced_at"])

    def test_failed_task_does_not_abort_batch(self):
        self.panel.fail_post = True
        path = self._manifest([self._daily(), self._daily(name="第二条", command="task repo/b.py", script="repo/b.py")])
        result = m.sync(m.load_tasks_file(path), api=self.api, state_path=self.state)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(len(result["details"]), 2, "单条失败不应中断整批")


class APITest(unittest.TestCase):
    """OpenAPI 客户端：重试、鉴权、错误处理。"""

    def setUp(self):
        self._saved_http = m._http_json
        self.addCleanup(lambda: setattr(m, "_http_json", self._saved_http))
        self.panel = FakePanel()
        m._http_json = self.panel.handle

    def test_retry_on_network_error(self):
        self.panel.fail_first = 2
        api = m.QingLongAPI(token=self.panel.token, retries=2)
        self.assertEqual(len(api.list_crons()), 0)
        self.assertGreaterEqual(self.panel.count("GET", "/open/crons"), 3)

    def test_retry_exhausted_raises(self):
        self.panel.fail_first = 9
        api = m.QingLongAPI(token=self.panel.token, retries=1)
        with self.assertRaises(m.QingLongAPIError):
            api.list_crons()

    def test_invalid_token_raises_auth_error(self):
        api = m.QingLongAPI(token="wrong-token")
        with self.assertRaises(m.QingLongAuthError):
            api.list_crons()

    def test_client_credentials_exchange(self):
        api = m.QingLongAPI(client_id="fake-id", client_secret="fake-secret")
        token = api.ensure_token()
        self.assertEqual(token, self.panel.token)

    def test_bad_client_credentials(self):
        api = m.QingLongAPI(client_id="bad", client_secret="bad")
        with self.assertRaises(m.QingLongAuthError):
            api.ensure_token()

    def test_missing_credentials_gives_guidance(self):
        saved = m.AUTH_FILE
        m.AUTH_FILE = os.path.join(tempfile.gettempdir(), "不存在的-auth.json")
        self.addCleanup(lambda: setattr(m, "AUTH_FILE", saved))
        with self.assertRaises(m.QingLongAuthError) as ctx:
            m.QingLongAPI().ensure_token()
        self.assertIn("QL_CLIENT_ID", str(ctx.exception))

    def test_business_code_error_raises(self):
        self.panel.force_status = 200
        self.panel.force_code = 400
        api = m.QingLongAPI(token=self.panel.token)
        with self.assertRaises(m.QingLongAPIError):
            api.list_crons()

    def test_panel_timezone(self):
        api = m.QingLongAPI(token=self.panel.token)
        self.assertEqual(api.panel_timezone(), "Asia/Shanghai")


class CLITest(unittest.TestCase):
    """命令行入口：validate / status 的退出码。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._saved_http = m._http_json
        self.addCleanup(lambda: setattr(m, "_http_json", self._saved_http))
        self.panel = FakePanel()
        m._http_json = self.panel.handle

    def test_validate_clean_file_exit_zero(self):
        path = write_json(os.path.join(self.tmp.name, "ok.json"),
                          [{"name": "A", "command": "task a.py", "schedule": "8 8 * * *"}])
        self.assertEqual(m.main(["validate", "--file", path]), 0)

    def test_validate_bad_file_exit_one(self):
        path = write_json(os.path.join(self.tmp.name, "bad.json"),
                          [{"name": "A", "command": "task a.py", "schedule": "8 8 * * * * *"}])
        self.assertEqual(m.main(["validate", "--file", path]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
