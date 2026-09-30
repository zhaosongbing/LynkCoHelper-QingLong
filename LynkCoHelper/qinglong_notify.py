# -*- coding: utf-8 -*-
"""
青龙面板（Qinglong Panel）通知适配模块。

作用：把每日任务结果通过「青龙面板自带的通知渠道」推送出去（PushPlus /
Server 酱 / 钉钉 / 企业微信 / Telegram / Bark 等，取决于面板「系统设置 →
通知设置」里配了什么），与项目自带的 Bark 推送（lynkco_notify）互不冲突，
两者可以同时启用。

实现策略（全部失败均静默降级，绝不影响签到/分享主流程）：
    1. Python 通道：在青龙常见脚本目录里查找 sendNotify.py / notify.py，
       调用其 send(title, content)；
    2. Node 通道：调用青龙自带的 notify.js（部分版本只有 JS 版通知）；
    3. 全部不可用则跳过，脚本仍会通过 stdout 把结果写进青龙任务日志。

开关：环境变量 LYNKCO_QL_NOTIFY=0 关闭本模块（默认开启）。
"""
import os
import subprocess
import sys

# 青龙面板根目录（容器里固定为 /ql，宿主机直装可能为其它路径，允许覆盖）
QL_ROOT = os.environ.get("QL_DIR", "/ql")

# sendNotify / notify 模块可能的存放目录（按命中概率排序）
_PY_SEARCH_DIRS = [
    os.path.join(QL_ROOT, "scripts"),
    os.path.join(QL_ROOT, "scripts", "utils"),
    os.path.join(QL_ROOT, "data", "scripts"),
    os.path.join(QL_ROOT, "repo"),
    os.path.dirname(os.path.abspath(__file__)),
    "/ql/scripts",
]

# 通知模块名 -> 其内部的推送函数名候选
_PY_NOTIFY_MODULES = ("sendNotify", "notify", "ql_notify")
_PY_NOTIFY_FUNCS = ("send", "main", "push", "sendNotify")


def _enabled() -> bool:
    """LYNKCO_QL_NOTIFY=0/false/no/off 时关闭青龙通知。"""
    return os.environ.get("LYNKCO_QL_NOTIFY", "1").strip().lower() not in ("0", "false", "no", "off")


def _try_python_notify(title: str, content: str) -> bool:
    """尝试用青龙的 Python 通知模块推送，成功返回 True。"""
    saved_path = list(sys.path)
    try:
        for directory in _PY_SEARCH_DIRS:
            if not os.path.isdir(directory) or directory in sys.path:
                continue
            sys.path.insert(0, directory)
        for mod_name in _PY_NOTIFY_MODULES:
            try:
                module = __import__(mod_name)
            except Exception:
                continue
            for func_name in _PY_NOTIFY_FUNCS:
                func = getattr(module, func_name, None)
                if not callable(func):
                    continue
                try:
                    # 各版本签名不统一，逐个尝试，能跑通即可。
                    try:
                        func(title, content)
                    except TypeError:
                        func(f"{title}\n\n{content}")
                    print("[信息] 已通过青龙面板通知渠道推送结果。")
                    return True
                except Exception as e:
                    print(f"[警告] 青龙通知模块 {mod_name}.{func_name} 调用失败: {e}")
                    return False
    except Exception as e:
        print(f"[警告] 青龙通知模块加载失败: {e}")
    finally:
        sys.path[:] = saved_path
    return False


def _try_node_notify(title: str, content: str) -> bool:
    """尝试调用青龙自带的 Node 版通知脚本，成功返回 True。"""
    candidates = [
        os.path.join(QL_ROOT, "shell", "notify.js"),
        os.path.join(QL_ROOT, "scripts", "sendNotify.js"),
        os.path.join(QL_ROOT, "shell", "sendNotify.js"),
    ]
    for script in candidates:
        if not os.path.isfile(script):
            continue
        try:
            subprocess.run(
                ["node", script, title, content],
                timeout=60,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            print(f"[信息] 已调用青龙 Node 通知脚本: {script}")
            return True
        except Exception as e:
            print(f"[警告] 调用青龙 Node 通知脚本失败（{script}）: {e}")
    return False


def send(title: str, content: str) -> bool:
    """
    通过青龙面板自带通知渠道推送一条消息。返回是否成功。
    未启用 / 未找到可用通道时返回 False（只打印提示，不抛异常）。
    """
    if not _enabled():
        print("[提示] LYNKCO_QL_NOTIFY=0，已关闭青龙面板通知。")
        return False
    if not os.path.isdir(QL_ROOT):
        print(f"[提示] 未检测到青龙目录 {QL_ROOT}，跳过青龙面板通知（非青龙环境属正常现象）。")
        return False

    if _try_python_notify(title, content):
        return True
    if _try_node_notify(title, content):
        return True

    print("[提示] 未找到可用的青龙通知通道（sendNotify.py / notify.js），已跳过。"
          "结果仍可在青龙任务日志中查看，也可配置 LYNKCO_BARK_KEY 走 Bark 推送。")
    return False
