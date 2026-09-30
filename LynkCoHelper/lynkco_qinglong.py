#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
领克 App 每日任务 —— 青龙面板（Qinglong Panel）专用入口。

与 lynkco_daily_tasks.py 的区别（针对青龙运行环境做了适配）：
    1. 自包含路径：把脚本所在目录插入 sys.path，无论青龙以何种 cwd / 何种命令
       （`task LynkCoHelper/lynkco_qinglong.py` 或 `python3 /ql/scripts/...`）启动，
       都能正确 import 同目录的 lynkco_*.py 模块。
    2. 依赖自检：requests 缺失时自动用 pip 安装（优先走国内镜像），避免因青龙
       依赖管理没装 requests 而直接报错退出。
    3. 配置自愈：env.json 默认落在 /ql/data/lynkco_env.json（见 lynkco_common），
       不会被 `ql repo` 拉库覆盖；可用 LYNKCO_ENV_FILE 自定义。
    4. 多账号：LYNKCO_REFRESH_TOKEN / LYNKCO_DEVICE_ID / LYNKCO_TOKEN 支持用
       换行或 & 分隔配置多个账号，逐个执行并汇总结果（多账号自动关闭 env.json
       缓存与回写，防止串号）。
    5. 双通道通知：Bark（项目自带）+ 青龙面板自带通知渠道（qinglong_notify）。
    6. 明确的退出码：全部账号成功 exit 0，任一失败 exit 1，便于青龙标记任务状态
       并按「任务失败时推送」发送告警。

必需环境变量（青龙面板 → 环境变量）：
    LYNKCO_APP_SECRETS   签名密钥集合 JSON：
                         {"nativeAppKey":"","nativeAppSecret":"","nativeAppCode":"",
                          "loginAppCode":"","glDevId":""}
                         （也可用 5 个独立的 LYNKCO_NATIVE_APP_KEY 等变量）
    LYNKCO_REFRESH_TOKEN refreshToken（推荐，约 30 天有效，可自动续期）
    LYNKCO_DEVICE_ID     与 refreshToken 配套的设备 id
    或 LYNKCO_TOKEN      静态 token（约 30 分钟有效，需频繁手动更新，不推荐）

可选环境变量：
    LYNKCO_BARK_KEY      Bark 推送 Key，未配置则跳过 Bark 推送
    LYNKCO_BARK_ICON     Bark 推送图标 URL
    LYNKCO_QL_NOTIFY     是否走青龙面板自带通知，默认 1（0 关闭）
    LYNKCO_DO_SHARE      是否执行分享任务，默认 1（0 只签到）
    LYNKCO_ENERGY_DELAY  签到/分享后查询积分前的等待秒数，默认 5
    LYNKCO_TIMEOUT       单次请求超时秒数，默认 30
    LYNKCO_ENV_FILE      env.json 路径，默认 /ql/data/lynkco_env.json
    LYNKCO_PIP_MIRROR    自动安装 requests 时使用的 pip 镜像

青龙定时任务命令：
    task LynkCoHelper/LynkCoHelper/lynkco_qinglong.py
定时规则（每天 08:08）：
    8 8 * * *
"""
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

# 青龙容器的 locale 可能未设置，强制 stdout 用 UTF-8，避免日志里的中文抛
# UnicodeEncodeError 导致任务莫名其妙失败。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass


def log(msg: str = "") -> None:
    """带时间戳的日志输出，青龙会把 stdout 完整收进任务日志。"""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- 依赖自检

DEFAULT_PIP_MIRROR = os.environ.get("LYNKCO_PIP_MIRROR", "https://pypi.tuna.tsinghua.edu.cn/simple")


def ensure_requests() -> None:
    """确保 requests 可用；缺失时自动 pip 安装（青龙「依赖管理 → Python3 → requests」也可预先安装）。"""
    try:
        import requests  # noqa: F401
        return
    except ImportError:
        pass

    log("未检测到 requests，开始自动安装 …")
    requirements = os.path.join(_HERE, "requirements.txt")
    attempts = []
    if os.path.isfile(requirements):
        attempts.append([sys.executable, "-m", "pip", "install", "-q", "-r", requirements, "-i", DEFAULT_PIP_MIRROR])
        attempts.append([sys.executable, "-m", "pip", "install", "-q", "-r", requirements])
    attempts.append([sys.executable, "-m", "pip", "install", "-q", "requests", "-i", DEFAULT_PIP_MIRROR])
    attempts.append([sys.executable, "-m", "pip", "install", "-q", "requests"])

    for cmd in attempts:
        try:
            subprocess.run(cmd, timeout=300, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            import requests  # noqa: F401
            log("requests 安装成功。")
            return
        except Exception as e:
            log(f"[警告] 安装尝试失败（{' '.join(cmd[-3:])}）: {e}")

    raise SystemExit("[错误] requests 安装失败，请在青龙「依赖管理 → Python3」中手动添加 requests 后重试。")


ensure_requests()

# 依赖就绪后再导入业务模块
import lynkco_common  # noqa: E402
from lynkco_common import mask_sensitive  # noqa: E402
from lynkco_login import load_token  # noqa: E402
from lynkco_notify import build_markdown_report, send_bark_notification  # noqa: E402
from lynkco_daily_tasks import run_daily_tasks  # noqa: E402

try:
    import qinglong_notify  # noqa: E402
except Exception:  # 单文件版 / 独立部署时该模块可能不存在
    qinglong_notify = None  # type: ignore[assignment]


# ---------------------------------------------------------------- 工具函数

def _mask(value: str) -> str:
    """密钥类字段脱敏：只留前 3 后 2 位，避免日志泄露。"""
    value = (value or "").strip()
    if not value:
        return "<未配置>"
    if len(value) <= 8:
        return f"{value[:3]}***({len(value)}位)"
    return f"{value[:3]}***{value[-2:]}({len(value)}位)"


def _split_multi(raw: str) -> list:
    """把环境变量值拆成多个账号：支持换行分隔或 & 分隔（青龙社区常见写法）。"""
    raw = (raw or "").strip()
    if not raw:
        return []
    parts = re.split(r"[\r\n]+|@@|&", raw)
    return [p.strip() for p in parts if p.strip()]


def _pick(values: list, index: int) -> str:
    """按账号序号取值：数量不足时复用第一个（单 deviceId 配多 token 的场景）。"""
    if not values:
        return ""
    if index < len(values):
        return values[index]
    return values[0]


def build_accounts() -> list:
    """
    根据环境变量构造账号列表。每个账号是一个 dict，键为要注入的环境变量名。
    未配置任何 token 时返回 [{}]，退化为「读 env.json」的单账号模式。
    """
    refresh_tokens = _split_multi(os.environ.get("LYNKCO_REFRESH_TOKEN", ""))
    device_ids = _split_multi(os.environ.get("LYNKCO_DEVICE_ID", ""))
    static_tokens = _split_multi(os.environ.get("LYNKCO_TOKEN", ""))

    accounts = []
    if refresh_tokens:
        for i, rt in enumerate(refresh_tokens):
            account = {"LYNKCO_REFRESH_TOKEN": rt}
            dev = _pick(device_ids, i)
            if dev:
                account["LYNKCO_DEVICE_ID"] = dev
            accounts.append(account)
    elif static_tokens:
        for st in static_tokens:
            accounts.append({"LYNKCO_TOKEN": st})

    if not accounts:
        accounts = [{}]
    return accounts


def apply_account_env(account: dict) -> None:
    """把某个账号的凭据写入 os.environ（先清空其它账号的，避免串号）。"""
    for key in ("LYNKCO_REFRESH_TOKEN", "LYNKCO_DEVICE_ID", "LYNKCO_TOKEN"):
        os.environ.pop(key, None)
    for key, value in account.items():
        if value:
            os.environ[key] = value


def print_config_summary(accounts: list) -> None:
    """启动自检：把关键配置的「已配置/未配置」打印到日志，方便在青龙里排障。"""
    log("=" * 56)
    log("领克 App 每日任务 · 青龙面板入口")
    log("=" * 56)
    offset_hour = -time.timezone / 3600
    log(f"脚本目录: {_HERE}")
    log(f"env 文件 : {lynkco_common.ENV_FILE}")
    log(f"Python   : {sys.version.split()[0]} ({sys.executable})")
    log(f"当前时间 : {time.strftime('%Y-%m-%d %H:%M:%S')} (UTC{offset_hour:+g})，"
        f"若与北京时间不符请在青龙设置环境变量 TZ=Asia/Shanghai")
    log(f"账号数量 : {len(accounts)}")
    log(f"执行分享 : {'是' if DO_SHARE else '否（LYNKCO_DO_SHARE=0）'}")
    log("-" * 56)
    app_secrets = os.environ.get("LYNKCO_APP_SECRETS", "").strip()
    if app_secrets:
        try:
            keys = sorted(json.loads(app_secrets).keys())
            log(f"LYNKCO_APP_SECRETS    : 已配置（{len(app_secrets)} 位，字段 {len(keys)} 个：{','.join(keys)}）")
        except Exception:
            log(f"LYNKCO_APP_SECRETS    : 已配置但 JSON 解析失败（{len(app_secrets)} 位），请检查是否为单行合法 JSON")
    else:
        log("LYNKCO_APP_SECRETS    : <未配置>")
    for key in ("LYNKCO_NATIVE_APP_KEY", "LYNKCO_NATIVE_APP_SECRET", "LYNKCO_NATIVE_APP_CODE",
                "LYNKCO_LOGIN_APP_CODE", "LYNKCO_NATIVE_GL_DEV_ID"):
        if os.environ.get(key):
            log(f"{key:<20}: {_mask(os.environ.get(key, ''))}")
    log(f"LYNKCO_REFRESH_TOKEN  : {len(_split_multi(os.environ.get('LYNKCO_REFRESH_TOKEN', '')))} 个")
    log(f"LYNKCO_DEVICE_ID      : {len(_split_multi(os.environ.get('LYNKCO_DEVICE_ID', '')))} 个")
    log(f"LYNKCO_TOKEN          : {len(_split_multi(os.environ.get('LYNKCO_TOKEN', '')))} 个")
    log(f"LYNKCO_BARK_KEY       : {_mask(os.environ.get('LYNKCO_BARK_KEY', ''))}")
    log(f"青龙通知              : {'启用' if (qinglong_notify is not None and QL_NOTIFY) else '未启用'}")
    if not os.environ.get("LYNKCO_APP_SECRETS") and not os.environ.get("LYNKCO_NATIVE_APP_KEY"):
        log("[警告] 未检测到 LYNKCO_APP_SECRETS，也未检测到独立的签名密钥变量，"
            "脚本很可能在计算签名时报「缺少必需的签名密钥」。")
    log("-" * 56)


# ---------------------------------------------------------------- 运行参数

DO_SHARE = os.environ.get("LYNKCO_DO_SHARE", "1").strip().lower() not in ("0", "false", "no", "off")
QL_NOTIFY = os.environ.get("LYNKCO_QL_NOTIFY", "1").strip().lower() not in ("0", "false", "no", "off")
BARK_ICON = os.environ.get("LYNKCO_BARK_ICON", "").strip() or None


# ---------------------------------------------------------------- 单账号执行

def run_one_account(index: int, total: int, account: dict) -> dict:
    """执行单个账号的「签到 + 分享 + 积分查询 + 通知」，返回 {"ok": bool, "report": str, "name": str}。"""
    name = f"账号{index}/{total}" if total > 1 else "账号"
    log(f"===== 开始执行 {name} =====")
    apply_account_env(account)

    summary = {"ok": False, "name": name, "report": ""}

    try:
        token = load_token()
    except Exception as e:
        msg = f"[错误] {name} 获取 token 失败: {e}"
        log(msg)
        summary["report"] = msg
        if qinglong_notify is not None and QL_NOTIFY:
            try:
                qinglong_notify.send("领克App · 每日任务失败", msg)
            except Exception:
                pass
        return summary

    try:
        result = run_daily_tasks(token, do_share=DO_SHARE)
        log("任务原始结果（已脱敏）：")
        print(json.dumps(mask_sensitive(result), ensure_ascii=False, indent=2))
    except Exception as e:
        msg = f"[错误] {name} 执行每日任务失败: {e}"
        log(msg)
        summary["report"] = msg
        if qinglong_notify is not None and QL_NOTIFY:
            try:
                qinglong_notify.send("领克App · 每日任务失败", msg)
            except Exception:
                pass
        return summary

    markdown_body = build_markdown_report(result)
    log("\n===== 结果摘要 =====")
    print(markdown_body)

    # 判断本账号是否算成功：签到完成（或今日已签到）且没有明显失败项。
    sign_ok = bool(result.get("already_signed")) or bool((result.get("sign_result") or {}).get("success"))
    share_result = result.get("share_result")
    share_ok = True
    if DO_SHARE and share_result is not None:
        share_ok = bool(share_result.get("ok"))
    summary["ok"] = bool(sign_ok and share_ok)

    title = f"领克App · 每日任务{' ✅' if summary['ok'] else ' ❌'}"
    if total > 1:
        title = f"{title}（{name}）"

    # 通道一：Bark（项目自带，读 LYNKCO_BARK_KEY，未配置则内部跳过）
    try:
        bark_result = send_bark_notification(title=title, markdown_body=markdown_body, icon=BARK_ICON)
        log(f"Bark 推送结果: {mask_sensitive(bark_result)}")
    except Exception as e:
        log(f"[警告] {name} Bark 推送失败（不影响签到/分享结果）: {e}")

    # 通道二：青龙面板自带通知渠道
    if qinglong_notify is not None and QL_NOTIFY:
        try:
            qinglong_notify.send(title, markdown_body)
        except Exception as e:
            log(f"[警告] {name} 青龙通知推送失败: {e}")

    summary["report"] = markdown_body
    log(f"===== {name} 执行结束：{'成功' if summary['ok'] else '存在失败项'} =====\n")
    return summary


# ---------------------------------------------------------------- 主流程

def main() -> int:
    accounts = build_accounts()
    print_config_summary(accounts)

    # 多账号：env.json 的 user 段只有一份，必须关闭缓存读取与回写，防止串号。
    if len(accounts) > 1:
        os.environ["LYNKCO_SKIP_TOKEN_CACHE"] = "1"
        os.environ["LYNKCO_DISABLE_ENV_WRITE"] = "1"
        log("[信息] 检测到多账号，已自动关闭 env.json 的 token 缓存与回写（防串号）。")

    summaries = []
    for i, account in enumerate(accounts, start=1):
        try:
            summaries.append(run_one_account(i, len(accounts), account))
        except Exception as e:
            log(f"[错误] 账号{i} 执行过程中出现未捕获异常: {e}")
            summaries.append({"ok": False, "name": f"账号{i}", "report": str(e)})

    success = sum(1 for s in summaries if s.get("ok"))
    failed = len(summaries) - success

    log("=" * 56)
    log(f"全部执行完毕：共 {len(summaries)} 个账号，成功 {success} 个，失败 {failed} 个。")
    if len(summaries) > 1:
        log("===== 汇总 =====")
        for s in summaries:
            log(f"{s['name']}: {'成功' if s.get('ok') else '失败'}")
        if qinglong_notify is not None and QL_NOTIFY:
            try:
                qinglong_notify.send(
                    f"领克App · 每日任务汇总（{success}/{len(summaries)}）",
                    "\n\n".join(f"**{s['name']}**：{'成功' if s.get('ok') else '失败'}\n{s.get('report', '')}"
                                for s in summaries),
                )
            except Exception as e:
                log(f"[警告] 汇总通知推送失败: {e}")
    log("=" * 56)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"[错误] 脚本异常退出: {e}")
        sys.exit(1)
