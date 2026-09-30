# -*- coding: utf-8 -*-
r"""
按青龙 / 呆呆面板（Dumb-Panel）拉库时的口径，检查仓库里哪些脚本会被自动建定时任务。

面板的扫描逻辑（对应 daidai-panel server/service/subscription.go）：
    · 只读脚本前 120 行（subscriptionScriptHeadLines）；
    · 定时规则：`^([\s#*@/]*)@?cron\b\s*[:：]?\s*(\S.*)$`，
      取到的值能通过 5 段（分 时 日 月 周）或 6 段（秒 分 时 日 月 周）cron 校验才算数；
    · 任务名：`^\s*(?:<!--|//+|#+|\*+|--+|@)\s*@?name\s*[:：]\s*(\S.*)$`，
      其次才是 `new Env('名称')`，都没有就回退成文件名；
    · 认不出定时规则的脚本不建任务（除非订阅里填了「默认 Cron 规则」）。

本脚本用同一套正则复算一遍，用来在提交前自查：该建的建了、不该建的没建。

用法：
    python3 LynkCoHelper/tools/check_cron_decl.py            # 扫描仓库
    python3 LynkCoHelper/tools/check_cron_decl.py <文件>      # 只看单个文件
"""
import os
import re
import sys

# 与面板保持一致的常量与正则
HEAD_LINES = 120

CRON_LABEL_RE = re.compile(r"^([\s#*@/]*)@?cron\b\s*[:：]?\s*(\S.*)$", re.IGNORECASE | re.MULTILINE)
NAME_LABEL_RE = re.compile(r"^\s*(?:<!--|//+|#+|\*+|--+|@)\s*@?name\s*[:：]\s*(\S.*)$",
                           re.IGNORECASE | re.MULTILINE)
ENV_NAME_RE = re.compile(r"""new\s+Env\s*\(\s*['"`]([^'"`]+)['"`]\s*\)""")

# 各字段取值范围：6 段 = 秒 分 时 日 月 周；5 段 = 分 时 日 月 周
_FIELD_RANGE = ((0, 59), (0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def cron_valid(expr: str) -> bool:
    """与面板 pkg/cron.Parse 的字段数口径一致：只校验 5 段或 6 段，不做语义细分。"""
    fields = expr.split()
    if len(fields) not in (5, 6):
        return False
    ranges = _FIELD_RANGE if len(fields) == 6 else _FIELD_RANGE[1:]
    for raw, (low, high) in zip(fields, ranges):
        if raw in ("*", "?"):
            continue
        for part in raw.split(","):
            step = part.split("/")
            if len(step) > 2:
                return False
            body = step[0]
            if body in ("*", "?"):
                continue
            for value in body.split("-"):
                if value.isdigit():
                    if not low <= int(value) <= high:
                        return False
                # JAN-DEC / MON-FRI 等英文别名交给面板判定；中文（"每天"这类说明文字）
                # 在面板的解析器里不合法，这里同样算非法，否则会被当成第 6 段吃进去
                elif value.isascii() and value.replace("-", "").isalpha():
                    continue
                else:
                    return False
    return True


def extract_cron(line: str) -> str:
    """复刻 extractSubscriptionCronExpressionFromLabel 的值解析。"""
    match = CRON_LABEL_RE.match(line)
    if not match:
        return ""
    rest = match.group(2).strip()
    if not rest:
        return ""
    if cron_valid(rest):
        return rest
    # 成对引号包住整个值时剥掉（仅当 cron 前面带注释标记或顶格）
    prefix = match.group(1)
    if prefix == "" or any(ch in prefix for ch in "#*@/"):
        if len(rest) >= 2 and rest[0] in "\"'" and rest[-1] == rest[0]:
            unquoted = rest[1:-1].strip()
            if unquoted and cron_valid(unquoted):
                return unquoted
    # 行尾跟着文件名/说明时，只取前 6 或 5 个字段
    parts = rest.split()
    for count in (6, 5):
        if len(parts) < count:
            continue
        expr = " ".join(parts[:count])
        if cron_valid(expr):
            return expr
    return ""


def scan_file(path: str) -> dict:
    """返回 {'cron': 定时规则, 'name': 任务名, 'fallback': 文件名}。"""
    result = {"cron": "", "name": "", "fallback": os.path.splitext(os.path.basename(path))[0]}
    env_name = ""
    label_name = ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()[:HEAD_LINES]
    except OSError:
        return result
    for line in lines:
        if not result["cron"]:
            result["cron"] = extract_cron(line)
        if not env_name:
            found = ENV_NAME_RE.search(line)
            if found:
                env_name = found.group(1).strip()
        if not label_name:
            found = NAME_LABEL_RE.match(line)
            if found:
                label_name = found.group(1).strip().strip("\"'`")
    result["name"] = env_name or label_name or result["fallback"]
    return result


def scan_repo(root: str) -> list:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", ".github"}]
        for filename in sorted(filenames):
            if not filename.endswith((".py", ".js", ".ts", ".sh", ".mjs")):
                continue
            path = os.path.join(dirpath, filename)
            info = scan_file(path)
            if info["cron"]:
                found.append((os.path.relpath(path, root), info["cron"], info["name"]))
    return sorted(found)


def main() -> int:
    args = sys.argv[1:]
    if args:
        for target in args:
            info = scan_file(target)
            state = f"cron={info['cron']!r} name={info['name']!r}" if info["cron"] else "未识别到定时规则 → 不会自动建任务"
            print(f"{target}: {state}")
        return 0

    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    found = scan_repo(repo_root)
    print(f"扫描仓库：{repo_root}")
    if not found:
        print("没有脚本声明定时规则，拉库后面板不会自动创建任何定时任务。")
        return 1
    print(f"会被自动建任务的脚本 {len(found)} 个：")
    for rel, cron, name in found:
        print(f"  · {rel}\n      定时规则 {cron}    任务名 {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
