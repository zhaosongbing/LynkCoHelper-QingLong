#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
领克 App 脚本 · 青龙面板定时任务自动注册工具。

解决的问题：`ql repo` / 订阅拉库只负责把脚本文件拉到 /ql/data/scripts，
不会按我们期望的「任务名 + 定时规则 + 启用状态」建任务；青龙自带的
「自动添加任务」又是按文件批量建的，规则统一走 config.sh 的 DefaultCronRule
（默认 `0 3 * * *`），建出来的任务名与规则都不受控，重复拉库还容易建重。

本模块的做法：仓库里放一份声明式清单（qinglong_tasks.json），拉库后由青龙
执行一次本脚本（订阅「执行后」命令 / 定时任务 / 容器内命令行均可），把清单里
的任务幂等地同步进青龙面板：

    - 幂等：以 command（任务命令）为主键、name 为辅键匹配已有任务，
      已存在且规则一致 → 不动；规则变了 → 更新；不存在 → 新建。
      青龙「自动添加任务」先建出来的任务也会被识别为同一条并校正，不会建重。
    - 校验：cron 支持 5 段（分 时 日 月 周）与 6 段（秒 分 时 日 月 周，
      青龙支持秒级），逐字段校验范围/步长/月份与星期别名；固定间隔会折算成
      最接近的 cron（见 interval_to_cron）；任务名、命令、清单内部重名都会查。
    - 时区：青龙按面板所在时区解释 cron，清单里声明的 timezone 会与面板时区
      比对，不一致只告警（可通过容器环境变量 TZ=Asia/Shanghai 对齐）。
    - 重试：API 调用失败按 LYNKCO_QL_RETRY 退避重试，仍失败则计入 failed 汇总。
    - 状态：同步结果写入状态文件（默认 /ql/data/lynkco_ql_tasks_state.json），
      记录任务名 → 青龙任务 id，供下次幂等比对与 --prune 清理，面板重启/重新
      拉库后都能恢复，不会重复创建。

鉴权（青龙 OpenAPI，全部走 /open/*，路径必须小写）：
    1. QL_TOKEN                直接可用的 Bearer Token（最简单）
    2. /ql/config/auth.json    读其中的 token 字段（老版本面板）
    3. QL_CLIENT_ID + QL_CLIENT_SECRET（或 auth.json 里的同名字段）
       → GET /open/auth/token?client_id=..&client_secret=.. 换 token
       应用需具备 crons 权限（面板「系统设置 → 安全设置 → 应用」创建）。

环境变量：
    QL_BASE_URL                面板地址，默认 http://127.0.0.1:5700
    QL_TOKEN / QL_CLIENT_ID / QL_CLIENT_SECRET   鉴权凭据
    QL_AUTH_FILE               auth.json 路径，默认 /ql/config/auth.json
    LYNKCO_QL_TASKS_FILE       任务清单，默认模块同级 qinglong_tasks.json
    LYNKCO_QL_STATE_FILE       同步状态文件，默认 /ql/data/lynkco_ql_tasks_state.json
    LYNKCO_QL_SCRIPTS_DIR      脚本目录（用于校验 command 指向的脚本存在）
    LYNKCO_QL_LABEL            给创建的任务打的标签，默认 LynkCo
    LYNKCO_QL_RETRY            API 失败重试次数，默认 2
    LYNKCO_QL_TIMEOUT          API 超时秒数，默认 15
    LYNKCO_QL_NOTIFY           同步失败时是否走面板通知渠道，默认 0（1 开启）

命令行：
    python3 lynkco_ql_tasks.py status                      # 连通性/凭据/时区自检
    python3 lynkco_ql_tasks.py sync [--dry-run] [--prune] [--no-update]
    python3 lynkco_ql_tasks.py validate [清单文件]          # 只校验不联网
    python3 lynkco_ql_tasks.py list [--json]                # 列出面板上已有任务
    python3 lynkco_ql_tasks.py add --name 领克签到 --command "task xxx.py" --schedule "8 8 * * *"
    python3 lynkco_ql_tasks.py remove 领克签到
    python3 lynkco_ql_tasks.py run 领克签到                 # 立即触发一次
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

# ---------------------------------------------------------------- 路径与编码

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


BASE_URL = (os.environ.get("QL_BASE_URL", "").strip() or "http://127.0.0.1:5700").rstrip("/")
AUTH_FILE = os.environ.get("QL_AUTH_FILE", "").strip() or "/ql/config/auth.json"
TASKS_FILE = os.environ.get("LYNKCO_QL_TASKS_FILE", "").strip() or os.path.join(_HERE, "qinglong_tasks.json")
STATE_FILE = os.environ.get("LYNKCO_QL_STATE_FILE", "").strip() or (
    os.path.join("/ql/data", "lynkco_ql_tasks_state.json") if os.path.isdir("/ql/data")
    else os.path.join(_HERE, "lynkco_ql_tasks_state.json")
)
DEFAULT_LABEL = os.environ.get("LYNKCO_QL_LABEL", "").strip() or "LynkCo"
API_RETRIES = _env_int("LYNKCO_QL_RETRY", 2)
API_TIMEOUT = _env_int("LYNKCO_QL_TIMEOUT", 15)
RETRY_INTERVAL = 2
NOTIFY_ON_FAILURE = os.environ.get("LYNKCO_QL_NOTIFY", "0").strip().lower() not in ("0", "false", "no", "off")

# 青龙脚本目录（用于校验 command 里 `task xxx.py` 指向的脚本是否真的被拉下来了）
_SCRIPTS_DIRS = [os.environ.get("LYNKCO_QL_SCRIPTS_DIR", "").strip()] + [
    "/ql/data/scripts", "/ql/scripts", os.path.join(os.getcwd(), "scripts")
]
SCRIPTS_DIRS = [d for d in _SCRIPTS_DIRS if d]


def repo_prefix() -> str:
    """
    本脚本在青龙脚本目录下的「仓库目录名」，例如 LynkCoHelper-QingLong。

    清单里的命令写成 `task {repo}/LynkCoHelper/lynkco_qinglong.py` 即可自动适配
    仓库改名 / 换 Fork 的场景，不必手工改命令路径。本地运行（不在脚本目录下）时
    返回 LYNKCO_QL_REPO 环境变量或空串。
    """
    override = os.environ.get("LYNKCO_QL_REPO", "").strip()
    if override:
        return override
    here = os.path.abspath(_HERE)
    for directory in SCRIPTS_DIRS:
        root = os.path.abspath(directory)
        if here.startswith(root + os.sep):
            return os.path.relpath(here, root).split(os.sep)[0]
    return ""


def _apply_placeholders(spec: dict) -> dict:
    """
    把任务定义里的 {repo} 占位符替换成实际仓库目录名。

    本地直接运行（脚本不在 /ql/data/scripts 下）时拿不到仓库名，保留原样并告警，
    可用 LYNKCO_QL_REPO=<仓库目录名> 手工指定。
    """
    prefix = repo_prefix()
    if not isinstance(spec, dict):
        return spec
    for key in ("command", "script", "name"):
        value = spec.get(key)
        if isinstance(value, str) and "{repo}" in value:
            spec[key] = value.replace("{repo}", prefix) if prefix else value
            if not prefix:
                log(f"[警告] 无法推断仓库目录名，{key} 里的 {{repo}} 未替换：{value}"
                    f"（青龙里由脚本目录自动推断，本地可设 LYNKCO_QL_REPO）")
    return spec


def log(msg: str = "") -> None:
    """带时间戳输出，青龙会把 stdout 收进任务日志。"""
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- 异常

class QingLongError(Exception):
    """青龙交互异常基类。"""


class QingLongAuthError(QingLongError):
    """鉴权失败（缺凭据 / token 无效 / 应用缺 crons 权限）。"""


class QingLongAPIError(QingLongError):
    """API 返回非 200 或网络异常。"""


class TaskConfigError(QingLongError):
    """任务清单/参数非法。"""


# ---------------------------------------------------------------- cron 校验

_MONTH_NAMES = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
_DOW_NAMES = {d: i for i, d in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"])}
# 字段名 -> (最小值, 最大值)
_FIELD_LIMITS = {
    "second": (0, 59), "minute": (0, 59), "hour": (0, 23),
    "dom": (1, 31), "month": (1, 12), "dow": (0, 6),
}
_FIELD_ORDERS = {
    5: ("minute", "hour", "dom", "month", "dow"),
    6: ("second", "minute", "hour", "dom", "month", "dow"),
}


def _parse_cron_value(field: str, token: str) -> int:
    low, high = _FIELD_LIMITS[field]
    upper = token.upper()
    if field == "month" and upper in _MONTH_NAMES:
        return _MONTH_NAMES[upper]
    if field == "dow" and upper in _DOW_NAMES:
        return _DOW_NAMES[upper]
    if not re.fullmatch(r"\d{1,2}", token):
        raise TaskConfigError(
            f"cron 字段 {field} 的取值 {token!r} 不合法（范围 {low}-{high}"
            + ("，或 JAN-DEC 别名" if field == "month" else "")
            + ("，或 SUN-SAT 别名" if field == "dow" else "")
            + "）"
        )
    value = int(token)
    if field == "dow" and value == 7:      # 周字段 7 等同周日
        value = 0
    if not low <= value <= high:
        raise TaskConfigError(f"cron 字段 {field} 的取值 {value} 超出范围 {low}-{high}。")
    return value


def validate_cron(expr: str) -> str:
    """
    校验 cron 表达式（5 段或 6 段），返回规范化后的表达式（多余空白折叠）。

    支持 `*`、`a`、`a-b`、`a,b`、`*/n`、`a-b/n`，月份/星期英文别名，周字段 7=周日。
    只做合法性校验，不判断未来是否有触发点（面板侧用 cron-parser 自行解析）。
    """
    raw = (expr or "").strip()
    if not raw:
        raise TaskConfigError("定时规则 schedule 为空。")
    parts = raw.split()
    if len(parts) not in _FIELD_ORDERS:
        raise TaskConfigError(
            f"cron 字段数错误：应为 5 段（分 时 日 月 周）或 6 段（秒 分 时 日 月 周），"
            f"实际 {len(parts)} 段：{raw!r}"
        )

    for field, token in zip(_FIELD_ORDERS[len(parts)], parts):
        low, high = _FIELD_LIMITS[field]
        for item in token.split(","):
            item = item.strip()
            if not item:
                raise TaskConfigError(f"cron 字段 {field} 存在空取值项：{token!r}")
            step = 1
            range_token = item
            if "/" in item:
                range_token, _, step_token = item.partition("/")
                if not re.fullmatch(r"\d+", step_token.strip()):
                    raise TaskConfigError(f"cron 字段 {field} 的步长 {step_token!r} 不是正整数：{token!r}")
                step = int(step_token)
                if step < 1 or step > (high - low + 1):
                    raise TaskConfigError(
                        f"cron 字段 {field} 的步长 {step} 非法，应为 1-{high - low + 1}：{token!r}")
                range_token = range_token.strip()
                if range_token in ("", "*"):
                    range_token = f"{low}-{high}"
            if "-" in range_token:
                start_token, _, end_token = range_token.partition("-")
                start = _parse_cron_value(field, start_token.strip())
                end = _parse_cron_value(field, end_token.strip())
                if start > end:
                    raise TaskConfigError(f"cron 字段 {field} 的区间 {range_token!r} 起点大于终点。")
            elif range_token == "*":
                continue
            else:
                _parse_cron_value(field, range_token)
    return " ".join(parts)


def interval_to_cron(value) -> str:
    """
    把固定间隔折算成 cron（青龙 cron 任务只认 cron 表达式，不认 interval）。

    支持 1800 / "30m" / "2h" / "1d" / "45s"，按量级落到秒/分/时/日字段：
        45s  -> */45 * * * * *      2m  -> */2 * * * *
        2h   -> 0 */2 * * *         3d  -> 0 0 */3 * *
    不能整除时（如 45m）仍会生成表达式但实际触发并不均匀，此时给出告警提示改用 cron。
    """
    if isinstance(value, bool):
        raise TaskConfigError("间隔不能是布尔值。")
    if isinstance(value, (int, float)):
        seconds = int(value)
    else:
        text = str(value).strip().lower()
        matched = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd]?)", text)
        if not matched:
            raise TaskConfigError(f"间隔 {value!r} 无法解析，请填整数秒或 45s/30m/2h/1d 形式。")
        seconds = int(float(matched.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[matched.group(2)])

    if seconds <= 0:
        raise TaskConfigError(f"间隔必须大于 0 秒，实际 {seconds}。")
    if seconds < 60:
        if 60 % seconds:
            log(f"[警告] 间隔 {seconds} 秒不能整除 60，折算出的 */{seconds} 秒级 cron 触发并不均匀，建议直接写 cron。")
        return f"*/{seconds} * * * * *"
    if seconds < 3600:
        minutes = seconds // 60
        if seconds % 60 or 60 % minutes:
            log(f"[警告] 间隔 {seconds} 秒折算为 */{minutes} 分并不均匀，建议直接写 cron。")
        return f"*/{minutes} * * * *"
    if seconds < 86400:
        hours = seconds // 3600
        if seconds % 3600 or 24 % hours:
            log(f"[警告] 间隔 {seconds} 秒折算为 */{hours} 时并不均匀，建议直接写 cron。")
        return f"0 */{hours} * * *"
    days = seconds // 86400
    if seconds % 86400:
        log(f"[警告] 间隔 {seconds} 秒折算为每 {days} 天，不足一天的部分被丢弃。")
    if days > 31:
        raise TaskConfigError(f"间隔 {seconds} 秒（{days} 天）超出 cron 可表达范围（最多 31 天）。")
    return f"0 0 */{days} * *"


def _utc_offset_text(name: str):
    """把时区名解析成 UTC 偏移描述（用于与面板时区比对），失败返回 None。"""
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime

        return datetime.now(ZoneInfo(name)).utcoffset()
    except Exception:
        return None


# ---------------------------------------------------------------- 清单

def _as_bool(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "none", "null")
    return bool(value)


def normalize_task(spec: dict, defaults: dict = None) -> dict:
    """
    校验并补全单条任务定义，返回可提交给青龙的结构：
    {"name", "command", "schedule", "labels", "isDisabled", "script", "description", "timezone"}

    触发方式二选一：schedule（cron）或 interval（秒/30m/2h/1d）。
    """
    if not isinstance(spec, dict):
        raise TaskConfigError(f"任务定义必须是 JSON 对象，实际为 {type(spec).__name__}。")
    defaults = defaults or {}

    name = str(spec.get("name") or "").strip()
    if not name:
        raise TaskConfigError("缺少必填字段 name（任务名称）。")
    if len(name) > 100:
        raise TaskConfigError(f"任务名 {name!r} 超过 100 字符上限（青龙面板字段限制）。")

    command = str(spec.get("command") or "").strip()
    if not command:
        raise TaskConfigError(f"任务 {name!r} 缺少必填字段 command（任务命令）。")

    schedule = spec.get("schedule")
    interval = spec.get("interval")
    if schedule is None and interval is None:
        raise TaskConfigError(f"任务 {name!r} 未指定触发方式：请填 schedule（cron）或 interval（间隔）。")
    if schedule is not None and interval is not None:
        raise TaskConfigError(f"任务 {name!r} 同时填了 schedule 与 interval，只能二选一。")
    schedule = validate_cron(schedule) if schedule is not None else interval_to_cron(interval)

    labels = spec.get("labels")
    if labels is None:
        labels = defaults.get("labels") or [DEFAULT_LABEL]
    elif isinstance(labels, str):
        labels = [labels]
    elif isinstance(labels, list):
        labels = [str(x) for x in labels]
    else:
        raise TaskConfigError(f"任务 {name!r} 的 labels 必须是字符串或字符串数组。")

    return {
        "name": name,
        "command": command,
        "schedule": schedule,
        "labels": labels,
        "isDisabled": 0 if _as_bool(spec.get("enabled", defaults.get("enabled", True)), True) else 1,
        "script": str(spec.get("script") or "").strip() or None,
        "description": str(spec.get("description") or "").strip(),
        "timezone": str(spec.get("timezone") or defaults.get("timezone") or "").strip() or None,
    }


def _script_path_from_command(command: str):
    """从 `task xxx.py` / `python3 xxx.py` 这类命令里取出脚本相对路径。"""
    parts = (command or "").split()
    for token in parts:
        if token.endswith((".py", ".js", ".ts", ".sh")):
            return token
    return None


def check_script_exists(task: dict) -> bool:
    """校验任务命令指向的脚本是否已在青龙脚本目录里（拉库失败的常见症状）。"""
    relative = task.get("script") or _script_path_from_command(task["command"])
    if not relative:
        return True
    for directory in SCRIPTS_DIRS:
        if os.path.isfile(os.path.join(directory, relative)):
            return True
    return False


def load_tasks_file(path: str = None) -> list:
    """
    读取任务清单，支持三种结构：
        {"tasks": [...], "defaults": {...}}   推荐
        [ {...}, {...} ]                      数组
        {...}                                 单条任务
    返回 normalize_task 后的任务列表（顺序保持清单顺序）。
    """
    path = path or TASKS_FILE
    if not os.path.isfile(path):
        raise TaskConfigError(f"任务清单不存在：{path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise TaskConfigError(f"任务清单 {path} 不是合法 JSON：{e}")

    defaults = {}
    if isinstance(data, dict):
        defaults = data.get("defaults") or {}
        specs = data.get("tasks")
        if specs is None:
            specs = [data]
    elif isinstance(data, list):
        specs = data
    else:
        raise TaskConfigError(f"任务清单 {path} 顶层必须是数组或对象，实际为 {type(data).__name__}。")
    if not isinstance(specs, list):
        raise TaskConfigError(f"任务清单 {path} 的 tasks 必须是数组。")

    tasks = []
    seen_names = set()
    seen_commands = set()
    for index, spec in enumerate(specs):
        try:
            task = normalize_task(_apply_placeholders(spec), defaults)
        except QingLongError as e:
            raise TaskConfigError(f"清单第 {index + 1} 条：{e}")
        if task["name"] in seen_names:
            raise TaskConfigError(f"清单第 {index + 1} 条：任务名 {task['name']!r} 在清单内重复。")
        if task["command"] in seen_commands:
            raise TaskConfigError(f"清单第 {index + 1} 条：任务命令 {task['command']!r} 在清单内重复，"
                                  f"青龙以 command 为主键，重复会导致互相覆盖。")
        seen_names.add(task["name"])
        seen_commands.add(task["command"])
        tasks.append(task)
    return tasks


def validate_tasks_file(path: str = None) -> dict:
    """只校验清单（不联网），返回 {"total", "valid", "issues"}。"""
    try:
        tasks = load_tasks_file(path)
    except QingLongError as e:
        return {"total": 0, "valid": 0, "issues": [{"name": "-", "error": str(e), "level": "error"}]}
    issues = []
    for task in tasks:
        if not check_script_exists(task):
            issues.append({
                "name": task["name"],
                "error": f"脚本 {task.get('script') or _script_path_from_command(task['command'])} "
                         f"在脚本目录 {SCRIPTS_DIRS} 下不存在，请确认已拉库且路径/白名单正确",
                "level": "warning",
            })
    return {"total": len(tasks), "valid": len(tasks) - len(issues), "issues": issues}


# ---------------------------------------------------------------- 同步状态

def load_state(path: str = None) -> dict:
    path = path or STATE_FILE
    if not os.path.exists(path):
        return {"synced_at": None, "tasks": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {"synced_at": None, "tasks": {}}
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), dict):
        return {"synced_at": None, "tasks": {}}
    return data


def save_state(state: dict, path: str = None) -> None:
    path = path or STATE_FILE
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    state["synced_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- OpenAPI

def _http_json(method: str, url: str, headers: dict = None, payload=None, timeout: int = API_TIMEOUT):
    """
    发一次 JSON 请求，返回 (status_code, body_dict)。单独抽出来方便测试替换。
    依赖 requests；青龙容器内一般已装（项目 requirements.txt 也声明了）。
    """
    import requests

    response = requests.request(
        method.upper(), url,
        headers={"Content-Type": "application/json", **(headers or {})},
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        timeout=timeout,
    )
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:500]}
    return response.status_code, body


class QingLongAPI:
    """青龙 OpenAPI 客户端（/open/*，路径必须全小写，否则面板返回 400 Invalid path format）。"""

    def __init__(self, base_url: str = None, token: str = None,
                 client_id: str = "", client_secret: str = "", timeout: int = None, retries: int = None):
        self.base_url = (base_url or BASE_URL).rstrip("/")
        self.token = (token or "").strip()
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.timeout = timeout or API_TIMEOUT
        self.retries = API_RETRIES if retries is None else retries

    # ------------------------------------------------------------ 鉴权

    def _load_auth_file(self) -> dict:
        """读取 /ql/config/auth.json（老版本面板的 token / 新版的应用凭据都可能在里面）。"""
        if not os.path.isfile(AUTH_FILE):
            return {}
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return {}

    def ensure_token(self) -> str:
        """
        取得可用 token，优先级：显式参数 > 环境变量 QL_TOKEN > auth.json 的 token >
        QL_CLIENT_ID/QL_CLIENT_SECRET（或 auth.json 同名字段）换 token。
        都拿不到时抛出带操作指引的 QingLongAuthError。
        """
        if self.token:
            return self.token
        env_token = os.environ.get("QL_TOKEN", "").strip()
        if env_token:
            self.token = env_token
            log("[信息] 使用环境变量 QL_TOKEN 中的 token。")
            return self.token
        auth = self._load_auth_file()
        if auth.get("token"):
            self.token = str(auth["token"])
            log("[信息] 使用 auth.json 中的 token。")
            return self.token
        self.client_id = self.client_id or os.environ.get("QL_CLIENT_ID", "").strip() or str(auth.get("client_id") or "")
        self.client_secret = self.client_secret or os.environ.get("QL_CLIENT_SECRET", "").strip() or str(auth.get("client_secret") or "")
        if self.client_id and self.client_secret:
            self.token = self.fetch_token()
            return self.token
        raise QingLongAuthError(
            "未找到青龙面板凭据。任选一种：\n"
            "  1) 环境变量 QL_TOKEN=<面板 token>\n"
            "  2) 环境变量 QL_CLIENT_ID + QL_CLIENT_SECRET（面板「系统设置 → 安全设置 → 应用」创建，"
            "需勾选 crons 权限）\n"
            "  3) 容器内 /ql/config/auth.json 里存在 token 或 client_id/client_secret"
        )

    def fetch_token(self) -> str:
        """GET /open/auth/token?client_id=..&client_secret=.. 换取 token。"""
        url = f"{self.base_url}/open/auth/token?client_id={self.client_id}&client_secret={self.client_secret}"
        status, body = self._request("GET", url, auth_required=False, expect_code=True)
        if status == 401:
            raise QingLongAuthError("换取 token 失败（401）：client_id / client_secret 不正确或应用已被删除。")
        data = body.get("data") or {}
        token = data.get("token") or data.get("value") or (data if isinstance(data, str) else None)
        if not token:
            raise QingLongAuthError(f"换取 token 失败，响应未包含 token：{body}")
        log("[信息] 已通过 client_id/client_secret 换取 token。")
        return str(token)

    # ------------------------------------------------------------ 请求

    def _request(self, method: str, url: str, payload=None, *, auth_required: bool = True,
                 expect_code: bool = True, retries: int = None):
        """
        统一请求入口：带 Bearer 头、失败退避重试、非 2xx 转异常。
        返回 (status_code, body_dict)。
        """
        retries = self.retries if retries is None else retries
        headers = {}
        if auth_required:
            headers["Authorization"] = f"Bearer {self.ensure_token()}"

        last_exc = None
        for attempt in range(retries + 1):
            try:
                status, body = _http_json(method, url, headers=headers, payload=payload, timeout=self.timeout)
            except Exception as e:
                last_exc = e
                if attempt < retries:
                    log(f"[警告] 请求异常（{method} {url}）：{e}，{RETRY_INTERVAL} 秒后重试"
                        f"（{attempt + 1}/{retries}）")
                    time.sleep(RETRY_INTERVAL)
                    continue
                raise QingLongAPIError(f"请求 {method} {url} 失败：{last_exc}")

            if status in (401, 403):
                raise QingLongAuthError(
                    f"青龙面板拒绝访问（HTTP {status}）：token 无效或应用缺少 crons 权限。"
                    f"响应：{str(body)[:200]}"
                )
            if expect_code and isinstance(body, dict) and body.get("code") not in (200, None):
                message = body.get("message") or body.get("error") or str(body)[:200]
                if attempt < retries and status >= 500:
                    log(f"[警告] 面板返回 {body.get('code')}（{message}），{RETRY_INTERVAL} 秒后重试")
                    time.sleep(RETRY_INTERVAL)
                    continue
                raise QingLongAPIError(f"青龙面板返回 code={body.get('code')}：{message}")
            if status >= 400:
                raise QingLongAPIError(f"青龙面板返回 HTTP {status}：{str(body)[:200]}")
            return status, body

        raise QingLongAPIError(f"请求 {method} {url} 重试 {retries} 次后仍失败：{last_exc}")

    # ------------------------------------------------------------ 任务接口

    def list_crons(self, search: str = "") -> list:
        """列出面板上的定时任务，返回任务 dict 列表（兼容 {data:[...]} 与 {data:{data:[...]}}）。"""
        url = f"{self.base_url}/open/crons?page=1&size=100"
        if search:
            url += f"&searchValue={search}"
        _, body = self._request("GET", url)
        data = (body or {}).get("data")
        if isinstance(data, dict):
            data = data.get("data")
        if not isinstance(data, list):
            return []
        return [x for x in data if isinstance(x, dict)]

    def find_cron(self, task: dict, crons: list = None):
        """
        按 command 优先、name 其次匹配已有任务（防重复注册的核心）。
        命中返回面板上的任务 dict（含 id），未命中返回 None。
        """
        crons = self.list_crons() if crons is None else crons
        command = (task.get("command") or "").strip()
        name = (task.get("name") or "").strip()
        for cron in crons:
            if (cron.get("command") or "").strip() == command:
                return cron
        for cron in crons:
            if (cron.get("name") or "").strip() == name:
                return cron
        return None

    def create_cron(self, task: dict):
        """POST /open/crons 新建任务（单对象，传数组会被面板校验拒绝）。返回新建任务 id。"""
        payload = {
            "name": task["name"],
            "command": task["command"],
            "schedule": task["schedule"],
            "labels": task.get("labels") or [],
            "isDisabled": int(task.get("isDisabled") or 0),
        }
        _, body = self._request("POST", f"{self.base_url}/open/crons", payload)
        data = (body or {}).get("data")
        if isinstance(data, dict):
            return data.get("id")
        return None

    def update_cron(self, cron_id, task: dict) -> None:
        """PUT /open/crons 更新已有任务（必须带 id）。"""
        payload = {
            "id": cron_id,
            "name": task["name"],
            "command": task["command"],
            "schedule": task["schedule"],
            "labels": task.get("labels") or [],
            "isDisabled": int(task.get("isDisabled") or 0),
        }
        self._request("PUT", f"{self.base_url}/open/crons", payload)

    def delete_crons(self, ids: list) -> None:
        """DELETE /open/crons 批量删除（body 为 id 数组）。"""
        if not ids:
            return
        self._request("DELETE", f"{self.base_url}/open/crons", [int(i) for i in ids])

    def run_crons(self, ids: list) -> None:
        """PUT /open/crons/run 立即触发一次。"""
        if not ids:
            return
        self._request("PUT", f"{self.base_url}/open/crons/run", [int(i) for i in ids])

    def set_disabled(self, ids: list, disabled: bool) -> None:
        """PUT /open/crons/{disable|enable} 启停任务。"""
        if not ids:
            return
        action = "disable" if disabled else "enable"
        self._request("PUT", f"{self.base_url}/open/crons/{action}", [int(i) for i in ids])

    def get_env(self, name: str):
        """读取面板环境变量（用于比对时区），取不到或没权限时返回 None。"""
        try:
            _, body = self._request("GET", f"{self.base_url}/open/envs?searchValue={name}")
        except QingLongError:
            return None
        data = (body or {}).get("data")
        if isinstance(data, dict):
            data = data.get("data")
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("name") == name:
                    return item.get("value")
        return None

    def panel_timezone(self) -> str:
        """猜测面板时区：优先面板 TZ 环境变量，其次容器 TZ，最后系统时区名。"""
        return self.get_env("TZ") or os.environ.get("TZ", "") or time.tzname[0]


# ---------------------------------------------------------------- 同步

def _cron_diff(task: dict, cron: dict) -> list:
    """比较清单任务与面板任务，返回差异字段列表（空列表表示一致）。"""
    diffs = []
    if (cron.get("schedule") or "").strip() != task["schedule"]:
        diffs.append(f"schedule: {cron.get('schedule')} -> {task['schedule']}")
    if (cron.get("name") or "").strip() != task["name"]:
        diffs.append(f"name: {cron.get('name')} -> {task['name']}")
    if int(cron.get("isDisabled") or 0) != int(task.get("isDisabled") or 0):
        diffs.append(f"isDisabled: {cron.get('isDisabled')} -> {task.get('isDisabled')}")
    labels = cron.get("labels") or []
    wanted = task.get("labels") or []
    if sorted(str(x) for x in labels) != sorted(str(x) for x in wanted):
        diffs.append(f"labels: {labels} -> {wanted}")
    return diffs


def sync(tasks: list, *, api: QingLongAPI = None, dry_run: bool = False, update: bool = True,
         prune: bool = False, run_now: bool = False, state_path: str = None) -> dict:
    """
    把清单任务幂等同步进青龙面板。

    dry_run：只打印将要做的变更，不发写请求。
    update：是否把已有任务的规则/名称/标签校正为清单值（False 则只补建缺失任务）。
    prune：删除「状态文件里记录过、但本次清单已不含」的任务（对应仓库删任务的场景）。
    run_now：新建/更新完成后立即触发一次。

    返回 {"created", "updated", "unchanged", "failed", "pruned", "details": [...]}
    """
    api = api or QingLongAPI()
    result = {"created": 0, "updated": 0, "unchanged": 0, "failed": 0, "pruned": 0, "details": []}
    state = load_state(state_path)
    state.setdefault("tasks", {})

    log("=" * 60)
    log("领克 App 脚本 · 青龙定时任务自动注册")
    log(f"面板地址: {api.base_url}")
    log(f"任务清单: {len(tasks)} 条" + ("（dry-run，只预演不写入）" if dry_run else ""))
    log("=" * 60)

    # 时区一致性提示（青龙按面板时区解释 cron）
    declared_tz = next((t.get("timezone") for t in tasks if t.get("timezone")), None)
    if declared_tz:
        panel_tz = api.panel_timezone() or "未知"
        if panel_tz and panel_tz != declared_tz:
            same_offset = _utc_offset_text(declared_tz) is not None and \
                _utc_offset_text(panel_tz) is not None and \
                _utc_offset_text(declared_tz) == _utc_offset_text(panel_tz)
            if not same_offset:
                log(f"[警告] 清单声明时区 {declared_tz}，面板时区为 {panel_tz}，cron 触发时刻会按面板时区解释，"
                    f"建议在青龙设置环境变量 TZ={declared_tz} 后重启面板。")

    try:
        crons = api.list_crons()
    except QingLongError as e:
        if not dry_run:
            raise
        log(f"[警告] 无法读取面板现有任务（{e}），dry-run 将按「全部新建」预演。")
        crons = []
    log(f"[信息] 面板现有定时任务 {len(crons)} 条。")

    matched_commands = set()
    for task in tasks:
        detail = {"name": task["name"], "command": task["command"], "action": None, "error": None}
        try:
            if not check_script_exists(task):
                log(f"[警告] 任务 {task['name']!r} 的脚本未找到："
                    f"{task.get('script') or _script_path_from_command(task['command'])}"
                    f"（脚本目录 {SCRIPTS_DIRS}），请确认已拉库。")

            existing = api.find_cron(task, crons)
            if existing is None:
                if dry_run:
                    log(f"[预演] 将新建任务 {task['name']!r}：{task['schedule']} | {task['command']}")
                else:
                    cron_id = api.create_cron(task)
                    log(f"[新建] 任务 {task['name']!r}（id={cron_id}）：{task['schedule']} | {task['command']}")
                    state["tasks"][task["name"]] = {
                        "id": cron_id, "command": task["command"], "schedule": task["schedule"],
                        "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    if run_now and cron_id:
                        api.run_crons([cron_id])
                        log(f"[信息] 已立即触发一次：{task['name']!r}")
                result["created"] += 1
                detail["action"] = "created"
            else:
                matched_commands.add((existing.get("command") or "").strip())
                diffs = _cron_diff(task, existing)
                if not diffs:
                    log(f"[一致] 任务 {task['name']!r} 已存在且规则一致（id={existing.get('id')}），跳过。")
                    result["unchanged"] += 1
                    detail["action"] = "unchanged"
                elif not update:
                    log(f"[跳过] 任务 {task['name']!r} 存在差异但未开启更新：{'; '.join(diffs)}")
                    result["unchanged"] += 1
                    detail["action"] = "unchanged"
                elif dry_run:
                    log(f"[预演] 将更新任务 {task['name']!r}（id={existing.get('id')}）：{'; '.join(diffs)}")
                    result["updated"] += 1
                    detail["action"] = "updated"
                else:
                    api.update_cron(existing.get("id"), task)
                    log(f"[更新] 任务 {task['name']!r}（id={existing.get('id')}）：{'; '.join(diffs)}")
                    state["tasks"][task["name"]] = {
                        "id": existing.get("id"), "command": task["command"], "schedule": task["schedule"],
                        "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    if run_now:
                        api.run_crons([existing.get("id")])
                    result["updated"] += 1
                    detail["action"] = "updated"
        except QingLongError as e:
            result["failed"] += 1
            detail["error"] = str(e)
            log(f"[错误] 任务 {task['name']!r} 同步失败：{e}")
        result["details"].append(detail)

    if prune:
        wanted_commands = {t["command"].strip() for t in tasks}
        stale = [(name, info) for name, info in state.get("tasks", {}).items()
                 if (info.get("command") or "").strip() not in wanted_commands and info.get("id")]
        for name, info in stale:
            if dry_run:
                log(f"[预演] 将删除已下线任务 {name!r}（id={info.get('id')}）")
            else:
                try:
                    api.delete_crons([info["id"]])
                    log(f"[删除] 已下线任务 {name!r}（id={info.get('id')}）已从面板移除。")
                except QingLongError as e:
                    result["failed"] += 1
                    log(f"[错误] 删除任务 {name!r} 失败：{e}")
                    continue
            state["tasks"].pop(name, None)
            result["pruned"] += 1

    if not dry_run:
        try:
            save_state(state, state_path)
            log(f"[信息] 同步状态已写入 {state_path or STATE_FILE}")
        except OSError as e:
            log(f"[警告] 状态文件写入失败（不影响已创建的任务）：{e}")

    log("=" * 60)
    log(f"同步完成：新建 {result['created']}，更新 {result['updated']}，一致 {result['unchanged']}，"
        f"清理 {result['pruned']}，失败 {result['failed']}。")
    log("=" * 60)

    if result["failed"] and NOTIFY_ON_FAILURE:
        _notify_sync_failure(result)
    return result


def _notify_sync_failure(result: dict) -> None:
    """同步失败时走青龙面板通知渠道（LYNKCO_QL_NOTIFY=1 开启），失败只告警。"""
    try:
        import qinglong_notify

        lines = "\n".join(f"- {d['name']}: {d['error']}" for d in result["details"] if d.get("error"))
        qinglong_notify.send("领克脚本 · 青龙任务注册失败", f"共 {result['failed']} 条失败：\n{lines}")
    except Exception as e:
        log(f"[警告] 通知推送失败（不影响结果）: {e}")


# ---------------------------------------------------------------- 自检

def status(api: QingLongAPI = None) -> int:
    """面板连通性 / 凭据 / 时区自检，返回退出码（0 正常）。"""
    api = api or QingLongAPI()
    log("=" * 60)
    log("青龙面板连接自检")
    log("=" * 60)
    log(f"面板地址 : {api.base_url}")
    log(f"auth.json : {AUTH_FILE}（{'存在' if os.path.isfile(AUTH_FILE) else '不存在'}）")
    log(f"脚本目录 : {SCRIPTS_DIRS}")
    log(f"任务清单 : {TASKS_FILE}（{'存在' if os.path.isfile(TASKS_FILE) else '不存在'}）")
    log(f"状态文件 : {STATE_FILE}")

    ok = True
    try:
        token = api.ensure_token()
        log(f"凭据      : 已获取 token（{len(token)} 位，脱敏 {token[:3]}***{token[-2:]}）")
    except QingLongError as e:
        log(f"凭据      : ❌ {e}")
        return 1

    try:
        crons = api.list_crons()
        log(f"任务列表  : ✅ 可读取，面板现有 {len(crons)} 条定时任务")
        for cron in crons[:20]:
            log(f"   - [{cron.get('id')}] {cron.get('name')} | {cron.get('schedule')} | "
                f"{'停用' if cron.get('isDisabled') else '启用'} | {cron.get('command')}")
        if len(crons) > 20:
            log(f"   … 其余 {len(crons) - 20} 条已省略")
    except QingLongError as e:
        ok = False
        log(f"任务列表  : ❌ {e}")

    tz = api.panel_timezone()
    log(f"面板时区  : {tz or '未知'}（cron 按此时区解释，建议 TZ=Asia/Shanghai）")

    if os.path.isfile(TASKS_FILE):
        report = validate_tasks_file(TASKS_FILE)
        log(f"清单校验  : {report['valid']}/{report['total']} 条通过")
        for issue in report["issues"]:
            log(f"   - [{issue['level']}] {issue['name']}: {issue['error']}")
        if report["total"] == 0:
            ok = False
    else:
        log(f"清单校验  : ⚠️ 未找到 {TASKS_FILE}")

    return 0 if ok else 1


# ---------------------------------------------------------------- 命令行

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lynkco_ql_tasks.py",
        description="把仓库里的任务清单幂等注册到青龙面板（拉库后自动建任务）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "典型用法（青龙订阅的「执行后」命令）：\n"
            "  task LynkCoHelper-QingLong/LynkCoHelper/lynkco_ql_tasks.py sync\n"
            "一次性拉库并注册：\n"
            "  ql repo https://github.com/zhaosongbing/LynkCoHelper-QingLong.git "
            "\"LynkCoHelper\" \"docs|previews|tools\" \"requirements.txt\" \"main\" && \\\n"
            "  python3 /ql/data/scripts/LynkCoHelper-QingLong/LynkCoHelper/lynkco_ql_tasks.py sync\n"
        ),
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("status", help="连通性/凭据/时区/清单自检")

    p_sync = sub.add_parser("sync", help="按清单同步任务到面板（幂等）")
    p_sync.add_argument("--file", help=f"任务清单路径，默认 {TASKS_FILE}")
    p_sync.add_argument("--dry-run", action="store_true", dest="dry_run", help="只预演，不写入面板")
    p_sync.add_argument("--prune", action="store_true", help="删除清单中已移除的旧任务")
    p_sync.add_argument("--no-update", action="store_true", dest="no_update",
                        help="已存在的任务不校正（只补建缺失的）")
    p_sync.add_argument("--run-now", action="store_true", dest="run_now", help="注册后立即触发一次")

    p_validate = sub.add_parser("validate", help="只校验清单，不联网")
    p_validate.add_argument("--file", help=f"任务清单路径，默认 {TASKS_FILE}")

    p_list = sub.add_parser("list", help="列出面板上的定时任务")
    p_list.add_argument("--json", action="store_true", dest="as_json")
    p_list.add_argument("--search", default="", help="按名称/命令过滤")

    p_add = sub.add_parser("add", help="注册单条任务")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--command", required=True, help='任务命令，如 "task LynkCoHelper-QingLong/LynkCoHelper/lynkco_qinglong.py"')
    p_add.add_argument("--schedule", help='cron，如 "8 8 * * *"')
    p_add.add_argument("--interval", help="固定间隔，如 30m / 2h / 1d（会折算为 cron）")
    p_add.add_argument("--labels", default=DEFAULT_LABEL, help=f"逗号分隔的标签，默认 {DEFAULT_LABEL}")
    p_add.add_argument("--disabled", action="store_true", help="注册为停用状态")
    p_add.add_argument("--run-now", action="store_true", dest="run_now", help="注册后立即执行一次")
    p_add.add_argument("--dry-run", action="store_true", dest="dry_run")

    p_remove = sub.add_parser("remove", help="删除任务（按名称或 id）")
    p_remove.add_argument("target", help="任务名称或任务 id")
    p_remove.add_argument("--dry-run", action="store_true", dest="dry_run")

    p_run = sub.add_parser("run", help="立即触发任务（按名称或 id）")
    p_run.add_argument("target")

    return parser


def main(argv: list = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if not args.cmd:
        build_parser().print_help()
        return 0

    api = QingLongAPI()

    try:
        if args.cmd == "status":
            return status(api)

        if args.cmd == "sync":
            tasks = load_tasks_file(args.file)
            result = sync(tasks, api=api, dry_run=args.dry_run, update=not args.no_update,
                          prune=args.prune, run_now=args.run_now)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1 if result["failed"] else 0

        if args.cmd == "validate":
            report = validate_tasks_file(args.file)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            # 脚本缺失只是 warning（可能还没拉库），只有配置错误才算失败
            errors = [i for i in report["issues"] if i.get("level") == "error"]
            return 1 if errors else 0

        if args.cmd == "list":
            crons = api.list_crons(args.search)
            if args.as_json:
                print(json.dumps(crons, ensure_ascii=False, indent=2))
            else:
                log(f"面板现有 {len(crons)} 条定时任务：")
                for cron in crons:
                    log(f"[{cron.get('id')}] {cron.get('name')} | {cron.get('schedule')} | "
                        f"{'停用' if cron.get('isDisabled') else '启用'} | {cron.get('command')}")
            return 0

        if args.cmd == "add":
            spec = {
                "name": args.name,
                "command": args.command,
                "schedule": args.schedule,
                "interval": args.interval,
                "labels": [x.strip() for x in (args.labels or "").split(",") if x.strip()],
                "enabled": not args.disabled,
            }
            tasks = [normalize_task(spec)]
            result = sync(tasks, api=api, dry_run=args.dry_run, run_now=args.run_now)
            return 1 if result["failed"] else 0

        if args.cmd == "remove":
            crons = api.list_crons()
            target = str(args.target).strip()
            hit = [c for c in crons if str(c.get("id")) == target or (c.get("name") or "").strip() == target]
            if not hit:
                log(f"[提示] 未找到任务 {target!r}。")
                return 1
            ids = [c["id"] for c in hit]
            if args.dry_run:
                log(f"[预演] 将删除 {len(ids)} 条任务：{[c.get('name') for c in hit]}")
                return 0
            api.delete_crons(ids)
            log(f"[删除] 已删除 {len(ids)} 条任务：{[c.get('name') for c in hit]}")
            return 0

        if args.cmd == "run":
            crons = api.list_crons()
            target = str(args.target).strip()
            hit = [c for c in crons if str(c.get("id")) == target or (c.get("name") or "").strip() == target]
            if not hit:
                log(f"[提示] 未找到任务 {target!r}。")
                return 1
            api.run_crons([c["id"] for c in hit])
            log(f"[执行] 已触发 {len(hit)} 条任务：{[c.get('name') for c in hit]}")
            return 0

    except TaskConfigError as e:
        print(f"[配置错误] {e}")
        return 1
    except QingLongAuthError as e:
        print(f"[鉴权失败] {e}")
        return 1
    except QingLongAPIError as e:
        print(f"[面板错误] {e}")
        return 1
    except QingLongError as e:
        print(f"[错误] {e}")
        return 1

    build_parser().print_help()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("[信息] 已中断。")
        sys.exit(130)
    except Exception as e:
        print(f"[错误] 脚本异常退出: {e}")
        sys.exit(1)
