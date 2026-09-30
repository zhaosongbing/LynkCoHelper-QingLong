# -*- coding: utf-8 -*-
"""
领克 App 每日任务 · 青龙面板单文件版（由 tools/build_qinglong_single.py 自动生成，请勿手工编辑）。

功能与原仓库 LynkCoHelper/ 目录下的多模块版本完全一致：签到 + 分享 + 积分查询 + 通知。

环境变量（青龙面板 → 环境变量）：
    必需 LYNKCO_APP_SECRETS    签名密钥集合 JSON（单行）：
        {"nativeAppKey":"","nativeAppSecret":"","nativeAppCode":"","loginAppCode":"","glDevId":""}
    必需 LYNKCO_REFRESH_TOKEN + LYNKCO_DEVICE_ID（推荐）
        或 LYNKCO_TOKEN（静态 token，约 30 分钟有效）
    可选 LYNKCO_BARK_KEY / LYNKCO_BARK_ICON / LYNKCO_QL_NOTIFY / LYNKCO_DO_SHARE
         / LYNKCO_ENERGY_DELAY / LYNKCO_TIMEOUT / LYNKCO_ENV_FILE / LYNKCO_PIP_MIRROR

多账号：LYNKCO_REFRESH_TOKEN / LYNKCO_DEVICE_ID / LYNKCO_TOKEN 支持用换行或 & 分隔。

青龙定时任务命令示例：task lynkco_qinglong_single.py     定时规则：8 8 * * *
"""
import sys


# ==========================================================================
# ↓↓↓ 以下内容来自 lynkco_common.py
# ==========================================================================

"""
领克 App 相关脚本的公共基础模块：App 原生阿里云 API 网关签名算法
（build_native_signature），以及 env.json 读写辅助函数
（load_env_data / save_env_fields）。

env.json 结构（三个子对象）：
    {
      "user": {"username": "", "password": "", "token": "", "refreshToken": "", "deviceId": "",
                "tokenExpireAt": ""},
      "secrets": {"nativeAppKey": "", "nativeAppSecret": "", "nativeAppCode": "",
                  "loginAppCode": "", "glDevId": ""},
      "notify": {"barkKey": ""}
    }

密钥读取优先级：单独环境变量 > 整合环境变量 LYNKCO_APP_SECRETS（JSON 字符串，
结构同 env.json["secrets"]） > env.json["secrets"] 对应字段，均未配置时报错。
签名算法与密钥来源详见 docs/AppSecret_逆向分析记录.md。
"""
import base64
import hashlib
import hmac
import json
import os
import time
import uuid

import requests.exceptions


def _resolve_env_file() -> str:
    """
    确定 env.json 的路径，优先级：
        1. 环境变量 LYNKCO_ENV_FILE（显式指定，青龙面板推荐指向 /ql/data 这类不会被拉库覆盖的目录）；
        2. 检测到青龙面板环境（/ql/data 目录存在）时，自动改用 /ql/data/lynkco_env.json，
           避免 `ql repo` 拉库更新脚本时把回写的最新 token / refreshToken 冲掉；
        3. 默认与模块同目录（本地运行行为不变）。
    """
    override = os.environ.get("LYNKCO_ENV_FILE", "").strip()
    if override:
        return override
    if os.path.isdir("/ql/data"):
        return os.path.join("/ql/data", "lynkco_env.json")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "env.json")


# 青龙面板下默认落在 /ql/data/lynkco_env.json，本地仍为模块同级 env.json。
ENV_FILE = _resolve_env_file()

# 网络请求默认超时（秒），GitHub Actions runner 到领克服务器延迟较高，
# 15 秒不够。可通过环境变量覆盖。
DEFAULT_TIMEOUT = int(os.environ.get("LYNKCO_TIMEOUT", "30"))
# 超时/断连后自动重试次数（每次间隔 3 秒），重试时会重新生成签名。
DEFAULT_RETRIES = 2

# 密钥字段名 -> (环境变量名, env.json["secrets"] 字段名)
_SECRET_SPECS = {
    "NATIVE_APP_KEY": ("LYNKCO_NATIVE_APP_KEY", "nativeAppKey"),
    "NATIVE_APP_SECRET": ("LYNKCO_NATIVE_APP_SECRET", "nativeAppSecret"),
    "NATIVE_APP_CODE": ("LYNKCO_NATIVE_APP_CODE", "nativeAppCode"),
    "LOGIN_APP_CODE": ("LYNKCO_LOGIN_APP_CODE", "loginAppCode"),
    "NATIVE_GL_DEV_ID": ("LYNKCO_NATIVE_GL_DEV_ID", "glDevId"),
}


def _load_bundled_secrets() -> dict:
    """解析整合环境变量 LYNKCO_APP_SECRETS，未配置或解析失败时返回空 dict。"""
    raw = os.environ.get("LYNKCO_APP_SECRETS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _get_secret(name: str) -> str:
    """按“单独环境变量 > LYNKCO_APP_SECRETS > env.json[secrets] 字段”优先级取值。"""
    env_var, json_key = _SECRET_SPECS[name]
    value = (
        os.environ.get(env_var)
        or _load_bundled_secrets().get(json_key)
        or load_env_data().get("secrets", {}).get(json_key)
    )
    if not value:
        raise RuntimeError(
            f"缺少必需的签名密钥 {name}，请通过环境变量 {env_var}、整合环境变量 "
            f"LYNKCO_APP_SECRETS（JSON 字符串中的 {json_key} 字段）或 env.json 的 "
            f"secrets.{json_key} 字段配置（参考 env.json.example）。"
        )
    return value


BASE_URL = "https://app-api-gw-toc.lynkco.com"
NATIVE_BASE_URL = "https://app-services.lynkco.com.cn"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; sdk_gphone64_arm64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/109.0.5414.123 "
    "Mobile Safari/537.36"
)

NATIVE_ANDROID_UA = "ALIYUN-ANDROID-UA"

APP_VERSION = "4.2.7"
ANDROID_APP_BUILD = "402071520"


def _build_native_device_headers() -> dict:
    """设备指纹请求头，仅 gl_dev_id 来自配置，其余为固定机型字段。"""
    return {
        "gl_dev_name": "sdk_gphone64_arm64",
        "gl_dev_model": "sdk_gphone64_arm64",
        "gl_dev_brand": "Google",
        "gl_dev_platform": "android",
        "gl_os_version": "33",
        "gl_app_version": APP_VERSION,
        "gl_app_build": ANDROID_APP_BUILD,
        "gl_dev_id": _get_secret("NATIVE_GL_DEV_ID"),
    }


# 以下模块级“常量”通过 __getattr__（PEP 562）惰性求值，取值时才读取配置。
_LAZY_ATTRS = {
    "NATIVE_APP_KEY": lambda: _get_secret("NATIVE_APP_KEY"),
    "NATIVE_APP_SECRET": lambda: _get_secret("NATIVE_APP_SECRET"),
    "NATIVE_APP_CODE": lambda: _get_secret("NATIVE_APP_CODE"),
    "LOGIN_APP_CODE": lambda: _get_secret("LOGIN_APP_CODE"),
    "NATIVE_DEVICE_HEADERS": _build_native_device_headers,
}


def __getattr__(name: str):
    if name in _LAZY_ATTRS:
        return _LAZY_ATTRS[name]()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def request_with_retry(session, method: str, url: str, *, build_headers, retries: int = DEFAULT_RETRIES, timeout: int = DEFAULT_TIMEOUT, **kwargs) -> requests.Response:
    """带超时重试的请求封装。build_headers 是一个无参回调，每次尝试（含重试）
    时调用以重新生成签名头（刷新 nonce/timestamp），保证签名时效性。
    遇到 ReadTimeout / ConnectionError 时等待 3 秒后重试，最多 retries 次。"""
    last_exc = None
    for attempt in range(retries + 1):
        headers = build_headers()
        try:
            return session.request(method, url, headers=headers, timeout=timeout, **kwargs)
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            if attempt < retries:
                print(f"[警告] 请求超时/断连，3秒后重试（第 {attempt + 1}/{retries} 次）: {e}")
                time.sleep(3)
            else:
                print(f"[警告] 请求重试 {retries} 次后仍失败: {e}")
    raise last_exc


def _format_gmt_date() -> str:
    """生成 HTTP 标准 GMT 时间格式，如 'Wed, 08 Jul 2026 09:49:57 GMT'。"""
    return time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())


def build_native_signature(method: str, path: str, query: dict = None,
                            accept: str = "application/json; charset=utf-8",
                            content_type: str = "application/x-www-form-urlencoded; charset=utf-8",
                            signature_headers_order: str = "x-ca-nonce,x-ca-timestamp,x-ca-key",
                            body: bytes = None,
                            extra_ca_headers: dict = None,
                            signature_header_items: list = None) -> dict:
    """
    复刻领克 App 原生 SDK 访问 app-services.lynkco.com.cn 网关的签名逻辑，
    对照阿里云官方 SDK `SignUtil.buildStringToSign` 实现：

        METHOD\\n Accept\\n Content-MD5\\n Content-Type\\n Date\\n
        (参与签名的 header，每行 "name:value\\n") path(?排序后的query)

    参数说明：
        extra_ca_headers: 额外的 x-ca- 前缀头，与默认的 x-ca-key/nonce/timestamp
            一起按字典序排序参与签名。
        signature_header_items: 传入 [(name, value), ...] 可完全自定义参与签名
            的 header 集合/顺序/大小写（部分接口如 iOS 端登录需要），不传则用默认模式。
        body: 传入则计算 Content-MD5 = Base64(MD5(body))，部分登录接口会校验，
            默认接口（refresh/getShareCode）无需传。

    签名 = Base64(HMAC-SHA256(待签名字符串, appSecret))
    """
    nonce = str(uuid.uuid4())
    timestamp = str(int(time.time() * 1000))
    date_str = _format_gmt_date()

    content_md5 = ""
    if body:
        content_md5 = base64.b64encode(hashlib.md5(body).digest()).decode()

    parts = [method.upper(), "\n", accept, "\n", content_md5, "\n", content_type, "\n", date_str, "\n"]

    if signature_header_items is not None:
        header_items = signature_header_items
        result_headers = {}
        for name, value in header_items:
            parts.append(f"{name}:{value}")
            parts.append("\n")
            result_headers[name] = value
    else:
        ca_headers = {
            "x-ca-key": _get_secret("NATIVE_APP_KEY"),
            "x-ca-nonce": nonce,
            "x-ca-timestamp": timestamp,
        }
        if extra_ca_headers:
            ca_headers.update(extra_ca_headers)
        for k in sorted(ca_headers.keys()):
            parts.append(f"{k}:{ca_headers[k]}")
            parts.append("\n")
        result_headers = dict(ca_headers)

    parts.append(path)
    if query:
        sorted_query = "&".join(f"{k}={v}" for k, v in sorted(query.items()) if v is not None and v != "")
        if sorted_query:
            parts.append("?")
            parts.append(sorted_query)

    string_to_sign = "".join(parts)
    digest = hmac.new(_get_secret("NATIVE_APP_SECRET").encode(), string_to_sign.encode(), hashlib.sha256).digest()
    signature = base64.b64encode(digest).decode()

    result = dict(result_headers)
    result["x-ca-signature-headers"] = signature_headers_order
    result["x-ca-signature"] = signature
    result["date"] = date_str
    result["accept"] = accept
    result["content-type"] = content_type
    if content_md5:
        result["content-md5"] = content_md5
    result["_nonce"] = nonce
    result["_timestamp"] = timestamp
    return result


def load_env_data() -> dict:
    """读取 env.json，返回 {"user": {...}, "secrets": {...}, "notify": {...}} 结构；文件不存在或字段缺失时对应子对象为空 dict。"""
    if not os.path.exists(ENV_FILE):
        return {"user": {}, "secrets": {}, "notify": {}}
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {"user": {}, "secrets": {}, "notify": {}}
    return {
        "user": data.get("user") or {},
        "secrets": data.get("secrets") or {},
        "notify": data.get("notify") or {},
    }


def _env_write_disabled() -> bool:
    """环境变量 LYNKCO_DISABLE_ENV_WRITE=1 时禁止回写 env.json（多账号场景必须关闭，
    否则多个账号的 token 会互相覆盖同一个 user 段）。"""
    return os.environ.get("LYNKCO_DISABLE_ENV_WRITE", "").strip().lower() in ("1", "true", "yes", "on")


def save_env_fields(fields: dict, section: str = "user") -> None:
    """把 fields 写入/更新到 env.json 的指定子对象（默认 "user"），文件或子对象不存在时自动创建。"""
    if _env_write_disabled():
        return
    try:
        raw = load_env_data() if not os.path.exists(ENV_FILE) else json.load(open(ENV_FILE, "r", encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault(section, {})
        if not isinstance(raw[section], dict):
            raw[section] = {}
        raw[section].update(fields)
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# 打印接口响应到控制台/CI日志前需要脱敏的字段名（不区分大小写，同时匹配
# userId/user_id/accountId/account_id 等驼峰、下划线两种命名风格）。
_SENSITIVE_LOG_KEYS = {"userid", "user_id", "accountid", "account_id"}


def mask_sensitive(data):
    """
    递归遍历 dict/list，把键名命中 _SENSITIVE_LOG_KEYS 的值替换为掩码字符串，
    用于打印接口响应到控制台/CI日志前脱敏，避免泄露 userId/accountId。
    不修改原始数据，返回一份新的结构。
    """
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            if isinstance(k, str) and k.replace("-", "_").lower() in _SENSITIVE_LOG_KEYS and v is not None:
                result[k] = "***"
            else:
                result[k] = mask_sensitive(v)
        return result
    if isinstance(data, list):
        return [mask_sensitive(item) for item in data]
    return data

# ==========================================================================
# ↓↓↓ 以下内容来自 lynkco_notify.py
# ==========================================================================

"""
Bark 推送通知工具模块，提供两个通用能力（不含任何业务逻辑，供
lynkco_daily_tasks.py 按需调用）：
    - build_markdown_report(result)：把任务结果字典组装成 Bark Markdown 文案；
    - send_bark_notification(...)：把一段 Markdown 文案推送到 Bark。

配置方式：环境变量 LYNKCO_BARK_KEY，或 env.json 的 notify.barkKey 字段
（Bark App「我的」页面可查看），未配置时跳过推送并打印提示，不抛异常。
"""
import os

import requests


BARK_DEFAULT_BASE = "https://api.day.app"


def _extract_point(energy_resp: dict) -> str:
    """从 myEnergy 响应中安全地取出 point 字段，取不到时返回 '?'。"""
    return str((energy_resp.get("data") or {}).get("point", "?"))


def build_markdown_report(result: dict) -> str:
    """
    把 lynkco_daily_tasks.run_daily_tasks() 返回的结果字典组装成一段
    Bark markdown 推送内容。
    """
    lines = []

    # --- 签到 ---
    if result.get("already_signed"):
        lines.append("### ℹ️ 签到")
        lines.append("- 今日已签到，无需重复签到")
    else:
        sign_result = result.get("sign_result") or {}
        sign_ok = bool(sign_result.get("success"))
        sign_data = sign_result.get("data") or {}
        if sign_ok:
            lines.append("### ✅ 签到成功")
            reward = sign_data.get("rewardEnergyNumber")
            if reward is not None:
                lines.append(f"- 本次奖励能量体：**+{reward}**")
        else:
            lines.append("### ❌ 签到失败")
            lines.append(f"- {sign_result.get('message', '未知错误')}")

    continue_data = (result.get("continue_info") or {}).get("data") or {}
    continue_days = continue_data.get("continueDays")
    sign_card = continue_data.get("signCardNumber")
    if continue_days is not None:
        lines.append(f"- 连续签到：**{continue_days} 天**")
    if sign_card is not None:
        lines.append(f"- 签到卡剩余：**{sign_card} 张**")

    # --- 分享 ---
    share_result = result.get("share_result")
    if share_result is not None:
        lines.append("\n### 🔗 分享任务")
        if share_result.get("ok"):
            lines.append("- 状态：**上报成功**")
            article_title = share_result.get("articleTitle")
            if article_title:
                lines.append(f"- 分享文章：{article_title}")
        else:
            detail = share_result.get("detail") or {}
            lines.append(f"- 状态：**失败**（{detail.get('message', '详情见日志')}）")

    # --- 积分变化 ---
    point_before = _extract_point(result.get("energy_before") or {})
    point_after = _extract_point(result.get("energy_after") or {})
    lines.append("\n### 💰 积分变化")
    try:
        delta = int(point_after) - int(point_before)
        delta_str = f"（+{delta}）" if delta > 0 else (f"（{delta}）" if delta < 0 else "（无变化）")
    except (ValueError, TypeError):
        delta_str = ""
    lines.append(f"- {point_before} → **{point_after}** {delta_str}".rstrip())

    return "\n".join(lines)


def send_bark_notification(title: str, markdown_body: str, group: str = "LynkCo签到",
                            icon: str = None, level: str = "active",
                            bark_key: str = None) -> dict:
    """
    通过 Bark 发送一条 Markdown 格式的推送通知。level 可选
    "critical"/"active"/"timeSensitive"/"passive"。bark_key 不传则读取
    环境变量 LYNKCO_BARK_KEY，未配置时返回 {"skipped": True} 且不抛异常。
    """
    bark_key = bark_key or os.environ.get("LYNKCO_BARK_KEY", "").strip() or load_env_data().get("notify", {}).get("barkKey", "").strip()
    if not bark_key:
        print("[提示] 未配置 LYNKCO_BARK_KEY，跳过 Bark 推送。")
        return {"skipped": True}

    url = f"{BARK_DEFAULT_BASE}/{bark_key}"

    payload = {
        "title": title,
        "markdown": markdown_body,
        "group": group,
        "level": level,
    }
    if icon:
        payload["icon"] = icon

    resp = requests.post(
        url, json=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()

# ==========================================================================
# ↓↓↓ 以下内容来自 lynkco_share.py
# ==========================================================================

"""
领克App 分享任务脚本。

分享流程使用 App 原生签名体系；分享落地页仍需携带 H5 Origin/Referer，但这不
代表使用 H5 签名。接口协议细节、已知限制见
docs/分享任务接口说明.md。重要提醒：接口返回 success 不代表真正加分（每日
有次数上限，重复调用不会重复加分），需自行对比 myEnergy 的 point 字段判断。

do_share() 已封装好"优先简化两步法，失败/无 content_id 时可选回退完整三步法"的
逻辑。通过 lynkco_sign.py / lynkco_daily_tasks.py 运行时会自动执行一次分享；
也可直接运行本文件单独触发一次分享。
"""
import json
import sys
import time

import requests


# ------------------------- 分享任务相关端点，完整协议见 docs/分享任务接口说明.md -------------------------
EP_GET_SHARE_CODE = "/app/v1/task/getShareCode"          # 获取本次分享的一次性 shareCode（原生签名）
EP_SHARE_LOOKUP = "/app/v1/task/shareCodeToUserId"        # 通过 shareCode 反查分享人 userId
EP_SHARE_CHECK = "/app/v1/task/shareContentContectCheck"  # 分享前置校验，可选步骤
EP_SHARE_REPORT = "/app/v1/task/shareContentContectReporting"  # 完整版上报，另一账号点开链接后走的三步流程
# 简化版上报：拿到 getShareCode 返回的 shareCode 后，POST 到该接口即可让服务端
# 记为"已完成一次分享"，比 lookup+check+report 三步法更直接，单账号即可自己触发。
EP_SHARE_REPORTING_SIMPLE = "/app/v1/task/shareReporting"
# 探索广场首页文章流，用于动态取最新文章 id，避免固定文章失效。body 需带
# dynamicSort/uniqueId/refreshType/pageNo 分页参数；否则只返回首屏、命中"文章"
# 类型的概率很低。
EP_EXPLORE_SQUARE_INDEX = "/app/explore/home-page/square/index2"
# get_latest_article() 命中第一篇文章前最多尝试翻的页数。
EXPLORE_SQUARE_PAGE_COUNT = 5

# 分享落地页 H5 域名（与签到用的 h5.lynkco.cn 是两个不同域名/Origin，
# shareReporting 接口的签名 AppKey 虽然相同，但网关会校验 Origin/Referer）。
H5_SHARE_ORIGIN = "https://h5.lynkco.com"

# 典型文章 id（真实抓包样本），作为 get_latest_article() 获取失败时的兜底。
DEFAULT_SHARE_ARTICLE_ID = "2075054309774663680"


def _find_article(value) -> dict:
    """递归查找广场文章流中第一个“文章”类型内容，返回 {"articleId", "title"}，未找到则返回空 dict。"""
    if isinstance(value, dict):
        article_id = value.get("articleId")
        content_type = value.get("contentType") or value.get("contentTypeCode")
        if article_id and (not content_type or content_type in ("文章", "article")):
            return {"articleId": str(article_id), "title": str(value.get("title") or "")}
        for child in value.values():
            found = _find_article(child)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_article(item)
            if found:
                return found
    return {}


class LynkCoShareClient:
    def __init__(self, token: str):
        if not token:
            raise ValueError("token 不能为空")
        self.token = token if token.startswith("bearer") else f"bearer{token}"
        self.session = requests.Session()

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """走已验证的 App 原生签名体系请求分享/广场接口。"""
        extra_headers = kwargs.pop("extra_headers", {})
        json_body = kwargs.pop("json", None)
        body = None
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        url = BASE_URL + path
        resp = request_with_retry(
            self.session, method, url,
            build_headers=lambda: {
                **build_native_signature(
                    method, path, query=kwargs.get("params"),
                    accept="application/json; charset=utf-8",
                    content_type="application/json; charset=utf-8",
                    signature_headers_order="x-ca-nonce,x-ca-key,x-ca-timestamp",
                    body=body,
                ),
                "token": self.token,
                "ca_version": "1",
                "x-requiretoken": "false",
                "User-Agent": NATIVE_ANDROID_UA,
                **lynkco_common.NATIVE_DEVICE_HEADERS,
                # 分享上报仍要求 H5 落地页 Origin；这不是 H5 签名。
                "Origin": "https://h5.lynkco.cn",
                "Referer": "https://h5.lynkco.cn/",
                **extra_headers,
            },
            data=body,
            **kwargs,
        )
        try:
            resp.json()
        except ValueError:
            print(
                f"[警告][H5 {method} {path}] 接口未返回有效 JSON，HTTP {resp.status_code}，"
                f"响应体前200字符: {resp.text[:200]!r}"
            )
        return resp

    def _native_request(self, method: str, path: str, extra_headers: dict = None, **kwargs) -> requests.Response:
        """
        走 App 原生 SDK 签名体系（build_native_signature）请求
        app-api-gw-toc.lynkco.com 网关的部分端点（如 getShareCode）。
        """
        extra = extra_headers or {}
        url = BASE_URL + path
        resp = request_with_retry(
            self.session, method, url,
            build_headers=lambda: {
                **build_native_signature(
                    method, path, query=kwargs.get("params"),
                    signature_headers_order="x-ca-nonce,x-ca-key,x-ca-timestamp",
                ),
                "token": self.token,
                "svcsid": self.token,
                "ca_version": "1",
                "appversion": APP_VERSION,
                "appVersionCode": APP_VERSION,
                "appVersionName": ANDROID_APP_BUILD,
                "publicPlatform": "android",
                "User-Agent": NATIVE_ANDROID_UA,
                **lynkco_common.NATIVE_DEVICE_HEADERS,
                **extra,
            },
            **kwargs,
        )
        try:
            resp.json()
        except ValueError:
            print(
                f"[警告][Native {method} {path}] 接口未返回有效 JSON，HTTP {resp.status_code}，"
                f"响应体前200字符: {resp.text[:200]!r}"
            )
        return resp

    def get_latest_article(self) -> dict:
        """翻页查找探索广场文章流，命中第一篇"文章"即返回 {"articleId", "title"}，失败/未找到时返回空 dict 并打印警告日志。"""
        for page_no in range(1, EXPLORE_SQUARE_PAGE_COUNT + 1):
            try:
                body = {"dynamicSort": "new", "uniqueId": "", "refreshType": "MORE", "pageNo": page_no}
                resp = self._request("POST", EP_EXPLORE_SQUARE_INDEX, json=body)
                resp_json = resp.json()
                found = _find_article(resp_json)
                if found:
                    return found
                print(
                    f"[警告] get_latest_article: 第 {page_no} 页未命中\"文章\"类型内容，"
                    f"接口返回 code={resp_json.get('code')!r}"
                )
            except Exception as e:
                print(f"[警告] get_latest_article: 第 {page_no} 页请求异常: {e}")
                continue
        print(f"[警告] get_latest_article: 翻完 {EXPLORE_SQUARE_PAGE_COUNT} 页仍未找到文章，将回退到 DEFAULT_SHARE_ARTICLE_ID")
        return {}

    def get_share_code(self, article_id: str = None, account_id: str = None) -> dict:
        """
        获取本次分享专属的一次性 shareCode（GET /app/v1/task/getShareCode，走原生
        AppKey 签名）。请求头 risk_request_info 里携带了被分享文章的 id，之后必须原样
        作为 businessNo 传给 share_reporting()，网关会校验两者一致，否则虽返回 success
        但不会真正加分。

        参数:
            article_id: 被分享文章/内容的 id，不传则用 DEFAULT_SHARE_ARTICLE_ID 兜底（推荐
                通过 do_share() 调用，会自动取最新文章）。
            account_id: 当前账号 accountId，用于填充 gl_user_id 风控头，不传则留空。
        """
        article_id = article_id or DEFAULT_SHARE_ARTICLE_ID
        share_content_url = (
            "https://h5.lynkco.com/app-h5/dist/web/pages/exploration/article/index.html"
            f"?id={article_id}&isShare=lynkco%3A%2F%2Fwx%2F%3FrouteUrl%3D%2Fpages%2Fexploration%2Farticle%2Findex.js%3Fid%3D{article_id}"
        )
        risk_request_info = json.dumps(
            {
                "openTimeStamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "shareContentType": 1,
                "shareContentURL": share_content_url,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        sweet_security_info = json.dumps(
            {
                "appVersion": APP_VERSION, "platform": "android", "battery": "100",
                "isCharging": "4", "isSetProxy": "true", "isUsbDebug": "false",
                "isMockLocation": "false", "isRoot": "false",
                "appSignature": "4F8393A255313DE42799571ABDF33A60",
                # channel 为 URL-encoded 后的值（"%E5%90%89%E5%88%A9" = 吉利），
                # HTTP 头只能是 ASCII 字符，直接放中文会被 requests 库报编码错。
                "channel": "%E5%90%89%E5%88%A9", "screenResolution": "2400*1080", "brand": "google",
                "model": "sdk_gphone64_arm64",
                "geelyDeviceId": "0de2480e07cefcd852cf3a8dadc822cc", "os": "android",
                "osVersion": "13", "androidVersion": "33", "networkType": "WIFI",
                "ip": "10.0.2.16", "wifiName": "AndroidWifi", "wifiSignalLevel": "-50",
                "isLbsEnabled": "true", "lbsLatitude": "", "lbsLongitude": "",
                "deviceToken": "",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        extra_headers = {
            "risk_type": "1",
            "risk_request_info": risk_request_info,
            "sweet_security_info": sweet_security_info,
            "os": "13",
        }
        if account_id:
            extra_headers["gl_user_id"] = account_id
        resp = self._native_request("GET", EP_GET_SHARE_CODE, extra_headers=extra_headers)
        return resp.json()

    def share_reporting(self, share_code: str, business_no: str = None,
                         first_classification: str = "文章", second_classification: str = "") -> dict:
        """
        分享上报（POST /app/v1/task/shareReporting?shareCode=<shareCode>，Origin 需为
        https://h5.lynkco.com）。网关会校验 businessNo 与 getShareCode 时的文章 id 是否
        一致。返回 success 不代表真正加分成功，需自行对比 myEnergy 的 point 字段确认。
        """
        business_no = business_no or DEFAULT_SHARE_ARTICLE_ID
        body = {
            "businessNo": business_no,
            "eventData": {
                "firstClassification": first_classification,
                "secondClassification": second_classification,
            },
        }
        resp = self._request(
            "POST", EP_SHARE_REPORTING_SIMPLE,
            params={"shareCode": share_code},
            json=body,
            extra_headers={"Origin": H5_SHARE_ORIGIN, "Referer": H5_SHARE_ORIGIN + "/"},
        )
        return resp.json()

    def share_lookup(self, share_code: str) -> dict:
        """（完整三步法-第1步）通过 shareCode 反查分享人 userId"""
        resp = self._request("POST", EP_SHARE_LOOKUP, json={"shareCode": share_code})
        return resp.json()

    def share_check(self, content_id: str, share_code: str) -> dict:
        """（完整三步法-第2步）分享前置校验"""
        resp = self._request("POST", EP_SHARE_CHECK, json={"contentId": content_id, "shareCode": share_code})
        return resp.json()

    def share_report(self, content_id: str, share_code: str) -> dict:
        """（完整三步法-第3步）真正上报分享、加能量体"""
        resp = self._request("POST", EP_SHARE_REPORT, json={"contentId": content_id, "shareCode": share_code})
        return resp.json()

    def do_share(self, article_id: str = None, account_id: str = None,
                 content_id: str = None, use_simple: bool = True) -> dict:
        """
        执行一次完整的分享任务，自动编排 get_share_code -> share_reporting 两步法，
        use_simple=False 或两步法失败且提供了 content_id 时回退到 lookup->check->report
        完整三步法。返回 {"ok": bool, "via": "simple"|"full", "detail": {...}}。

        未显式传入 article_id 时自动调用 get_latest_article() 取最新文章，失败则回退到
        DEFAULT_SHARE_ARTICLE_ID（无标题），分享成功时返回值会带上 "articleId"/"articleTitle"。
        """
        article_title = ""
        if not article_id:
            latest = self.get_latest_article()
            article_id = latest.get("articleId") or DEFAULT_SHARE_ARTICLE_ID
            article_title = latest.get("title", "")
            if latest and not article_title:
                print(f"[警告] do_share: 动态获取到文章 articleId={article_id}，但该内容节点的 title 字段为空")
            elif not latest:
                print(f"[警告] do_share: 未获取到动态文章，回退使用 DEFAULT_SHARE_ARTICLE_ID={article_id}")

        if use_simple:
            try:
                code_info = self.get_share_code(article_id=article_id, account_id=account_id)
                share_code = code_info.get("data") or ""
                if not share_code:
                    raise RuntimeError(f"getShareCode 未返回有效 shareCode: {code_info}")

                result = self.share_reporting(share_code, business_no=article_id)
                if str(result.get("code")) in ("200", "success"):
                    return {
                        "ok": True, "via": "simple",
                        "articleId": article_id, "articleTitle": article_title,
                        "detail": {"getShareCode": code_info, "shareReporting": result},
                    }
                # 后端明确返回"已分享过"之类信息也视为成功（幂等）
                msg = result.get("message", "")
                if any(k in msg for k in ("已分享", "已领取", "今日已", "已结束")):
                    return {
                        "ok": True, "via": "simple",
                        "articleId": article_id, "articleTitle": article_title,
                        "detail": {"getShareCode": code_info, "shareReporting": result},
                    }
            except Exception as e:
                if not content_id:
                    return {"ok": False, "via": "simple", "detail": {"message": f"简化两步法失败: {e}"}}
                # 简化接口异常且提供了 content_id 时继续尝试完整三步法

        share_code_for_full = None
        try:
            share_code_for_full = (self.get_share_code(article_id=article_id, account_id=account_id).get("data") or "")
        except Exception:
            pass

        if not content_id:
            return {"ok": False, "via": "simple", "detail": {"message": "简化两步法未成功，且未提供 content_id 无法回退到完整三步法"}}

        lookup_result = self.share_lookup(share_code_for_full or "")
        check_result = self.share_check(content_id, share_code_for_full or "")
        report_result = self.share_report(content_id, share_code_for_full or "")
        ok = str(report_result.get("code")) in ("200", "success")
        msg = report_result.get("message", "")
        if not ok and any(k in msg for k in ("已分享", "已领取", "今日已", "已结束")):
            ok = True
        return {
            "ok": ok,
            "via": "full",
            "articleId": article_id, "articleTitle": article_title,
            "detail": {"lookup": lookup_result, "check": check_result, "report": report_result},
        }


def run_auto_share(token: str) -> dict:
    """供 lynkco_sign.py / lynkco_daily_tasks.py 调用的便捷封装：执行一次分享，打印结果。"""
    print("\n=== 执行分享任务 ===")
    client = LynkCoShareClient(token)
    try:
        share_result = client.do_share()
        print(json.dumps(mask_sensitive(share_result), ensure_ascii=False, indent=2))
        if share_result.get("ok"):
            print("\n分享任务成功！")
        else:
            print(f"\n分享任务失败: {share_result.get('detail')}")
        return share_result
    except Exception as e:
        print(f"[警告] 分享任务执行失败: {e}")
        return {"ok": False, "detail": {"message": str(e)}}


def main():
    """独立运行本文件即可单独触发一次分享任务（无需通过 lynkco_sign.py）。"""
    try:
        token = load_token()
    except RuntimeError as e:
        print(f"[错误] {e}")
        sys.exit(1)
    run_auto_share(token)

# ==========================================================================
# ↓↓↓ 以下内容来自 lynkco_sign.py
# ==========================================================================

"""
领克App 每日签到脚本。签名算法详见 lynkco_common.py / docs/AppSecret_逆向分析记录.md。

使用前提：需要有效 token，由 lynkco_login.py 的 load_token() 统一提供
（自动续期或人工获取），本脚本无需关心 token 具体来源。

2026-07 抓包更新：真正"执行签到"的接口路径已从 /up/api/v1/user/sign 变为
/up/api/v1/user/sign/upgrade，且改走原生 SDK 签名体系（build_native_signature，
NATIVE_APP_KEY/SECRET，签名头顺序 x-ca-nonce,x-ca-key,x-ca-timestamp，POST body
即使是空对象 "{}" 也要计算 Content-MD5）。2026-09-08 实测确认积分、签到状态和
连续签到查询也可使用同一原生签名体系；daily task 不再依赖 H5 签名密钥。
"""
import json
import sys

import requests


EP_SIGN_DAY_INFO = "/up/api/v1/user/sign/day/info"
EP_SIGN_UPGRADE = "/up/api/v1/user/sign/upgrade"  # 真正执行签到的接口（抓包确认，非 /up/api/v1/user/sign）
EP_CONTINUE_DAYS = "/up/api/v1/userReward/getContinueDaysAndSignCard"


class LynkCoSignClient:
    def __init__(self, token: str):
        if not token:
            raise ValueError("token 不能为空")
        # 领克接口的 token 头需要 "bearer" 前缀且中间无空格，若外部传入时已带前缀则不重复添加
        self.token = token if token.startswith("bearer") else f"bearer{token}"
        self.session = requests.Session()

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """走已验证的 App 原生签名体系，用于积分和签到查询接口。"""
        extra_headers = kwargs.pop("extra_headers", {})
        url = BASE_URL + path
        resp = request_with_retry(
            self.session, method, url,
            build_headers=lambda: {
                **build_native_signature(
                    method, path, query=kwargs.get("params"),
                    accept="application/json; charset=utf-8",
                    content_type="application/json; charset=utf-8",
                    signature_headers_order="x-ca-nonce,x-ca-key,x-ca-timestamp",
                ),
                "token": self.token,
                "ca_version": "1",
                "x-requiretoken": "false",
                "User-Agent": NATIVE_ANDROID_UA,
                **extra_headers,
            },
            **kwargs,
        )
        try:
            resp.json()
        except ValueError:
            raise RuntimeError(
                f"[{method} {path}] 接口未返回有效 JSON（可能被网关拦截，如境外 IP 访问限制"
                f"或 AppKey 未授权），HTTP {resp.status_code}，"
                f"响应体前200字符: {resp.text[:200]!r}"
            )
        return resp

    def _native_request(self, method: str, path: str, body: bytes = None, **kwargs) -> requests.Response:
        """走 App 原生 SDK 签名体系（build_native_signature），用于 sign/upgrade 等写操作接口。"""
        extra_headers = kwargs.pop("extra_headers", {})
        url = BASE_URL + path
        resp = request_with_retry(
            self.session, method, url,
            build_headers=lambda: {
                **build_native_signature(
                    method, path,
                    accept="application/json; charset=utf-8",
                    content_type="application/json; charset=utf-8",
                    signature_headers_order="x-ca-nonce,x-ca-key,x-ca-timestamp",
                    body=body,
                ),
                "token": self.token,
                "ca_version": "1",
                "x-requiretoken": "false",
                "User-Agent": NATIVE_ANDROID_UA,
                **extra_headers,
            },
            data=body,
            **kwargs,
        )
        try:
            resp.json()
        except ValueError:
            raise RuntimeError(
                f"[Native {method} {path}] 接口未返回有效 JSON，HTTP {resp.status_code}，"
                f"响应体前200字符: {resp.text[:200]!r}"
            )
        return resp

    def get_sign_day_info(self) -> dict:
        """查询今日签到状态"""
        resp = self._request("GET", EP_SIGN_DAY_INFO)
        return resp.json()

    def do_sign(self) -> dict:
        """执行签到（POST /up/api/v1/user/sign/upgrade，原生签名，body 固定为空对象 "{}"）"""
        resp = self._native_request("POST", EP_SIGN_UPGRADE, body=b"{}")
        return resp.json()

    def get_continue_days(self) -> dict:
        """查询连续签到天数和签到卡数量"""
        resp = self._request("GET", EP_CONTINUE_DAYS)
        return resp.json()


def main():
    try:
        token = load_token()
    except RuntimeError as e:
        print(f"[错误] {e}")
        sys.exit(1)

    client = LynkCoSignClient(token)

    print("=== 查询签到状态 ===")
    try:
        day_info = client.get_sign_day_info()
        print(json.dumps(mask_sensitive(day_info), ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"[错误] 查询签到状态失败: {e}")
        sys.exit(1)

    if not day_info.get("success"):
        print("[错误] token 可能已失效，请重新登录获取新 token。")
        sys.exit(1)

    already_signed = day_info.get("data", {}).get("signStatus") == 1
    if already_signed:
        print("\n今日已签到，无需重复签到。")
    else:
        print("\n=== 执行签到 ===")
        try:
            sign_result = client.do_sign()
            print(json.dumps(mask_sensitive(sign_result), ensure_ascii=False, indent=2))
            if sign_result.get("success"):
                print("\n签到成功！")
            else:
                print(f"\n签到失败: {sign_result.get('message')}")
        except Exception as e:
            print(f"[错误] 签到请求失败: {e}")
            sys.exit(1)

    print("\n=== 连续签到信息 ===")
    try:
        continue_info = client.get_continue_days()
        print(json.dumps(mask_sensitive(continue_info), ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"[警告] 查询连续签到信息失败: {e}")

    # 分享任务已独立到 lynkco_share.py，每次运行本脚本都会执行一次（接口
    # 每日有加分次数上限，重复调用不会重复加分，详见 docs/分享任务接口说明.md）。
    run_auto_share(token)

# ==========================================================================
# ↓↓↓ 以下内容来自 lynkco_login.py
# ==========================================================================

"""
领克App 登录 / Token 获取与续期模块。

两套获取 token 的方式：
    1. refreshToken 自动续期（推荐）：refresh_token()，内部依次尝试 AppCode
       静态认证、AppKey/AppSecret HMAC 签名认证两套方案。
    2. 账号密码/短信验证码全流程登录：get_security_config -> 用户完成极验
       滑块 -> validate_geetest -> send_login_sms -> login_by_mobile_code。

接口协议细节（请求路径、body 结构、签名要求等）见
docs/登录接口协议说明.md；签名密钥来源见 docs/AppSecret_逆向分析记录.md。

统一入口 load_token()：按优先级自动选择上述方式，供 lynkco_sign.py /
lynkco_share.py 等业务脚本直接调用。
"""
import json
import os
import sys
import time
import uuid

import requests


# ------------------------- refreshToken 续期 -------------------------

# iOS 请求仍沿用其对应的抓包 build 号；Android 版本与 build 统一由
# lynkco_common 的 APK 元数据常量提供。
IOS_APP_BUILD = "40203073"


def _parse_refresh_response(data: dict, refresh_token_value: str) -> dict:
    """解析 /auth/login/refresh 接口的响应体，两种认证方案返回结构一致，共用此解析逻辑。"""
    if data.get("code") != "success":
        raise RuntimeError(
            f"refreshToken 续期失败，接口返回: {data}。"
            "可能是 refreshToken 已过期(有效期约30天)，需要重新抓包获取。"
        )

    token_dto = (data.get("data") or {}).get("centerTokenDto") or {}
    token = token_dto.get("token")
    if not token:
        raise RuntimeError(f"refreshToken 续期响应异常，未找到 token 字段: {data}")

    return {
        "token": token,
        "refreshToken": token_dto.get("refreshToken", refresh_token_value),
        "expireAt": token_dto.get("expireAt"),
        "refreshExpireAt": token_dto.get("refreshExpireAt"),
    }


def refresh_token_by_appcode(refresh_token_value: str, device_id: str) -> dict:
    """
    方案一（优先，已验证）：使用 AppCode 静态认证换取新 token（无需任何签名运算）。

    只需固定的 `Authorization: APPCODE <NATIVE_APP_CODE>` 请求头即可通过
    阿里云网关校验，若未来失效上层会自动回退到方案二(HMAC 签名)。
    """
    path = "/auth/login/refresh"
    query = {
        "refreshToken": refresh_token_value,
        "deviceId": device_id,
        "deviceType": "IOS",
        "appVersion": APP_VERSION,
    }
    headers = {
        "Authorization": f"APPCODE {lynkco_common.NATIVE_APP_CODE}",
        "accept": "application/json",
        "content-type": "application/json; charset=UTF-8",
        "publicplatform": "iOS",
        "user-agent": "CA_iOS_SDK_2.0",
        "token": "",
        "gl_dev_id": device_id,
        "appversioncode": APP_VERSION,
        "appversionname": IOS_APP_BUILD,
        "gl_app_version": APP_VERSION,
        "gl_app_build": IOS_APP_BUILD,
        "x-ca-version": "1",
    }

    url = NATIVE_BASE_URL + path
    resp = requests.get(url, params=query, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return _parse_refresh_response(data, refresh_token_value)


def refresh_token_by_signature(refresh_token_value: str, device_id: str) -> dict:
    """方案二（兜底）：使用 AppKey/AppSecret HMAC 签名认证换取新 token。"""
    path = "/auth/login/refresh"
    query = {"deviceId": device_id, "refreshToken": refresh_token_value}

    # Accept / Content-Type 已参与签名运算，不要在下面覆盖它们，否则会与签名时的值不一致导致校验失败。
    headers = build_native_signature("GET", path, query)
    headers["ca_version"] = "1"
    headers["x-requiretoken"] = "false"
    headers["oauth"] = "false"
    headers["User-Agent"] = NATIVE_ANDROID_UA
    headers.update(lynkco_common.NATIVE_DEVICE_HEADERS)

    url = NATIVE_BASE_URL + path
    resp = requests.get(url, params=query, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return _parse_refresh_response(data, refresh_token_value)


def refresh_token(refresh_token_value: str, device_id: str) -> dict:
    """依次尝试 AppCode 静态认证、HMAC 签名认证换取新 token，两者均失败时抛出异常。"""
    try:
        return refresh_token_by_appcode(refresh_token_value, device_id)
    except Exception as e_appcode:
        try:
            return refresh_token_by_signature(refresh_token_value, device_id)
        except Exception as e_sig:
            raise RuntimeError(
                f"refreshToken 续期失败，两套认证方案均未成功。\n"
                f"  方案一(AppCode)报错: {e_appcode}\n"
                f"  方案二(HMAC签名)报错: {e_sig}"
            ) from e_sig


def _save_refreshed_token(refreshed: dict) -> None:
    """续期成功后，把最新的 token/refreshToken/expireAt 回写 env.json，便于排查、留档及本地缓存判断。"""
    fields = {"token": refreshed["token"]}
    if refreshed.get("refreshToken"):
        fields["refreshToken"] = refreshed["refreshToken"]
    if refreshed.get("expireAt"):
        fields["tokenExpireAt"] = refreshed["expireAt"]
    save_env_fields(fields)


# ------------------------- 账号密码/短信验证码全流程登录，完整链路见 docs/登录接口协议说明.md -------------------------

EP_SECURITY_CONFIG = "/auth/v1/security/config"
EP_GEETEST_VALIDATE = "/auth/v1/security/geeTestV4/validate"
EP_SEND_SMS = "/auth/login/sliding/sendSms"
EP_MOBILE_CODE_LOGIN = "/auth/login/mobileCodeLogin"
EP_PASSWORD_LOGIN = "/auth/login/sliding/login"

def get_security_config(device_id: str) -> dict:
    """第1步：获取极验(Geetest v4)配置（GET /auth/v1/security/config?type=GEE_TEST_V4）。"""
    path = EP_SECURITY_CONFIG
    query = {"type": "GEE_TEST_V4"}
    # loginAppCode 只服务于滑块登录；在实际调用接口时才读取，避免
    # refreshToken 续期/每日任务仅导入本模块就被这项可选配置阻塞。
    ca_headers = {"x-ca-appcode": lynkco_common.LOGIN_APP_CODE}
    headers = build_native_signature(
        "GET", path, query=query,
        accept="application/json; charset=utf-8",
        content_type="application/x-www-form-urlencoded; charset=utf-8",
        signature_headers_order="x-ca-appcode,x-ca-nonce,x-ca-key,x-ca-timestamp",
        extra_ca_headers=ca_headers,
    )
    headers["ca_version"] = "1"
    headers["tenantid"] = "569001643002"
    headers["x-refresh-token"] = "true"
    headers["User-Agent"] = NATIVE_ANDROID_UA
    headers["appVersionCode"] = APP_VERSION
    headers["appVersionName"] = ANDROID_APP_BUILD
    headers["publicPlatform"] = "android"
    headers.update(lynkco_common.NATIVE_DEVICE_HEADERS)
    headers["gl_dev_id"] = device_id  # 覆盖 NATIVE_DEVICE_HEADERS 里的默认设备id

    url = NATIVE_BASE_URL + path
    resp = requests.get(url, params=query, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def validate_geetest(device_id: str, lot_number: str, captcha_output: str,
                      pass_token: str, gen_time: str, scene: str) -> dict:
    """
    第3步：校验极验滑块结果，成功后返回 certifyId（=lot_number）。
    scene 取值："passwordLogin"（密码登录）/ "mobileLoginSendsms"（短信登录）。
    """
    path = EP_GEETEST_VALIDATE
    body_dict = {
        "passToken": pass_token,
        "lotNumber": lot_number,
        "genTime": gen_time,
        "captchaOutput": captcha_output,
        "scene": scene,
    }
    body_bytes = json.dumps(body_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    # loginAppCode 只服务于滑块登录；在实际调用接口时才读取，避免
    # refreshToken 续期/每日任务仅导入本模块就被这项可选配置阻塞。
    ca_headers = {"x-ca-appcode": lynkco_common.LOGIN_APP_CODE}
    headers = build_native_signature(
        "POST", path,
        accept="application/json; charset=utf-8",
        content_type="application/json; charset=utf-8",
        signature_headers_order="x-ca-appcode,x-ca-nonce,x-ca-key,x-ca-timestamp",
        body=body_bytes,
        extra_ca_headers=ca_headers,
    )
    headers["ca_version"] = "1"
    headers["tenantid"] = "569001643002"
    headers["x-refresh-token"] = "true"
    headers["User-Agent"] = NATIVE_ANDROID_UA
    headers["appVersionCode"] = APP_VERSION
    headers["appVersionName"] = ANDROID_APP_BUILD
    headers["publicPlatform"] = "android"
    headers.update(lynkco_common.NATIVE_DEVICE_HEADERS)
    headers["gl_dev_id"] = device_id  # 覆盖 NATIVE_DEVICE_HEADERS 里的默认设备id

    url = NATIVE_BASE_URL + path
    resp = requests.post(url, data=body_bytes, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def send_login_sms(device_id: str, mobile: str, certify_id: str) -> dict:
    """第4步：凭 certifyId（=lot_number）给手机号发送登录短信验证码。"""
    path = EP_SEND_SMS
    body_dict = {"mobile": mobile, "challenge": certify_id}
    body_bytes = json.dumps(body_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    headers = build_native_signature(
        "POST", path,
        accept="application/json; charset=utf-8",
        content_type="application/json; charset=utf-8",
        signature_headers_order="x-ca-nonce,x-ca-timestamp,x-ca-key",
        body=body_bytes,
    )
    headers["ca_version"] = "1"
    headers["User-Agent"] = NATIVE_ANDROID_UA
    headers["appVersionCode"] = APP_VERSION
    headers["appVersionName"] = ANDROID_APP_BUILD
    headers["publicPlatform"] = "android"
    headers.update(lynkco_common.NATIVE_DEVICE_HEADERS)
    headers["gl_dev_id"] = device_id  # 覆盖 NATIVE_DEVICE_HEADERS 里的默认设备id

    url = NATIVE_BASE_URL + path
    resp = requests.post(url, data=body_bytes, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def login_by_mobile_code(device_id: str, mobile: str, verification_code: str) -> dict:
    """
    第5步：手机号 + 短信验证码完成最终登录，返回值经
    _parse_refresh_response 同款结构解析后含 token/refreshToken。

    注意：该接口所有业务参数均通过 query string 传递，而非 JSON body；
    body 固定为空 JSON 对象 "{}"，但仍需据此计算 Content-MD5 参与签名。
    """
    path = EP_MOBILE_CODE_LOGIN
    query = {
        "deviceType": "ANDROID",
        "appVersion": APP_VERSION,
        "hardwareDeviceId": device_id,
        "mobile": mobile,
        "deviceModel": lynkco_common.NATIVE_DEVICE_HEADERS.get("gl_dev_model", "sdk_gphone64_arm64"),
        "verificationCode": verification_code,
    }
    body_bytes = b"{}"

    headers = build_native_signature(
        "POST", path, query=query,
        accept="application/json; charset=utf-8",
        content_type="application/json; charset=utf-8",
        signature_headers_order="x-ca-nonce,x-ca-timestamp,x-ca-key",
        body=body_bytes,
    )
    headers["certifyid"] = ""
    headers["ca_version"] = "1"
    headers["User-Agent"] = NATIVE_ANDROID_UA
    headers["appVersionCode"] = APP_VERSION
    headers["appVersionName"] = ANDROID_APP_BUILD
    headers["publicPlatform"] = "android"
    headers.update(lynkco_common.NATIVE_DEVICE_HEADERS)
    headers["gl_dev_id"] = device_id  # 覆盖 NATIVE_DEVICE_HEADERS 里的默认设备id

    url = NATIVE_BASE_URL + path
    resp = requests.post(url, params=query, data=body_bytes, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    result = _parse_refresh_response(data, refresh_token_value="")
    result["deviceId"] = device_id
    return result


def login_by_password(device_id: str, username: str, password_md5: str, certify_id: str,
                       hardware_device_id: str = None, device_type: str = "ANDROID") -> dict:
    """
    【参考实现，不建议依赖】账号密码登录（sliding/login）。陌生设备会被风控拦截为
    untrusted.device，需改走 send_login_sms + login_by_mobile_code 兜底；password_md5
    加密算法未完全逆向确认。device_type="IOS" 时使用不同的签名 header 顺序/大小写规则
    （详见 docs/登录接口协议说明.md）。
    """
    path = EP_PASSWORD_LOGIN
    is_ios = device_type.upper() == "IOS"
    query = {
        "deviceType": device_type.upper(),
        "appVersion": APP_VERSION,
        "password": password_md5,
        "hardwareDeviceId": hardware_device_id or device_id,
        "challenge": certify_id,
        "deviceModel": lynkco_common.NATIVE_DEVICE_HEADERS.get("gl_dev_model", "sdk_gphone64_arm64"),
        "username": username,
    }
    body_bytes = b"{}"

    if is_ios:
        nonce = str(uuid.uuid4())
        timestamp = str(int(time.time() * 1000))
        signature_header_items = [
            ("X-Ca-Key", lynkco_common.NATIVE_APP_KEY),
            ("X-Ca-Nonce", nonce),
            ("X-Ca-Signature-Method", "HmacSHA256"),
            ("X-Ca-Timestamp", timestamp),
            ("X-Ca-Version", "1"),
            ("token", ""),
        ]
        headers = build_native_signature(
            "POST", path, query=query,
            accept="application/json",
            content_type="application/json; charset=UTF-8",
            signature_headers_order="X-Ca-Key,X-Ca-Nonce,X-Ca-Signature-Method,X-Ca-Timestamp,X-Ca-Version,token",
            body=body_bytes,
            signature_header_items=signature_header_items,
        )
        headers.pop("_nonce", None)
        headers.pop("_timestamp", None)
        headers["certifyid"] = ""
        headers["User-Agent"] = "CA_iOS_SDK_2.0"
        headers["appVersionCode"] = APP_VERSION
        headers["appVersionName"] = IOS_APP_BUILD
        headers["publicPlatform"] = "iOS"
        headers["gl_dev_brand"] = "Apple"
        headers["gl_app_build"] = IOS_APP_BUILD
        headers["gl_dev_platform"] = "iOS"
        headers["gl_dev_name"] = "iPhone"
        headers["gl_os_version"] = "27.0"
        headers["gl_dev_model"] = "iPhone 15 Pro"
        headers["gl_dev_id"] = hardware_device_id or device_id
        headers["gl_app_version"] = APP_VERSION
        headers["gl_user_id"] = ""
    else:
        headers = build_native_signature(
            "POST", path, query=query,
            accept="application/json; charset=utf-8",
            content_type="application/json; charset=utf-8",
            signature_headers_order="x-ca-nonce,x-ca-timestamp,x-ca-key",
            body=body_bytes,
        )
        headers["certifyid"] = ""
        headers["ca_version"] = "1"
        headers["User-Agent"] = NATIVE_ANDROID_UA
        headers["appVersionCode"] = APP_VERSION
        headers["appVersionName"] = ANDROID_APP_BUILD
        headers["publicPlatform"] = "android"
        headers.update(lynkco_common.NATIVE_DEVICE_HEADERS)
        headers["gl_dev_id"] = device_id  # 覆盖 NATIVE_DEVICE_HEADERS 里的默认设备id

    url = NATIVE_BASE_URL + path
    resp = requests.post(url, params=query, data=body_bytes, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "success":
        # 未信任设备/密码错误/极验校验错误等预期内的业务分支，直接返回给调用方自行判断。
        return {"code": data.get("code"), "message": data.get("message"), "raw": data}
    result = _parse_refresh_response(data, refresh_token_value="")
    result["deviceId"] = device_id
    return result


def login_with_geetest_and_sms(mobile: str, verification_code: str,
                                device_id: str = None) -> dict:
    """
    完整登录流程的最后一步封装：给定手机号和用户收到的短信验证码，直接调用
    login_by_mobile_code 完成登录，并把结果（含 token/refreshToken/
    deviceId）写入 env.json。

    device_id 不传则自动生成一个随机值，只要求本次登录流程从获取极验配置到最终登录
    全程使用同一个值。
    """
    device_id = device_id or uuid.uuid4().hex[:16]
    result = login_by_mobile_code(device_id, mobile, verification_code)
    save_env_fields({
        "token": result["token"],
        "refreshToken": result.get("refreshToken", ""),
        "deviceId": device_id,
    })
    return result


# ------------------------- 统一 token 获取入口 -------------------------

# 本地缓存的 token 距过期时间小于该阈值(秒)时视为已失效，提前触发续期，避免请求发出后 token 恰好过期。
TOKEN_CACHE_SAFETY_MARGIN_SEC = 60


def _cached_token_valid(user_data: dict) -> str:
    """若 env.json 中缓存的 token 距 tokenExpireAt 仍有余量，返回该 token；已过期/缺失时返回空字符串。"""
    token = user_data.get("token")
    expire_at = user_data.get("tokenExpireAt")
    if not token or not expire_at:
        return ""
    try:
        remain_sec = (float(expire_at) - time.time() * 1000) / 1000
    except (TypeError, ValueError):
        return ""
    if remain_sec > TOKEN_CACHE_SAFETY_MARGIN_SEC:
        print(f"[信息] 命中本地缓存 token，距过期还剩约 {int(remain_sec)} 秒，跳过 refreshToken 续期请求。")
        return token.strip()
    return ""


def _token_cache_disabled() -> bool:
    """环境变量 LYNKCO_SKIP_TOKEN_CACHE=1 时跳过 env.json 中的缓存 token。

    多账号场景必须开启：env.json 的 user 段只有一份，多账号共用会串号，
    因此每个账号都应强制走 refreshToken 续期。
    """
    return os.environ.get("LYNKCO_SKIP_TOKEN_CACHE", "").strip().lower() in ("1", "true", "yes", "on")


def load_token() -> str:
    """
    token 获取优先级：
        1. env.json 中缓存的 token 若未过期（留 60 秒安全余量），直接复用，调试时避免
           频繁调用续期接口（LYNKCO_SKIP_TOKEN_CACHE=1 时跳过本步，多账号必需）；
        2. 环境变量 LYNKCO_REFRESH_TOKEN + LYNKCO_DEVICE_ID，或 env.json
           user.refreshToken + user.deviceId —— 自动向网关换取最新 token（成功后
           会把 token/expireAt 写回 env.json 供下次复用）；
        3. 环境变量 LYNKCO_TOKEN 或 env.json 的 user.token（静态兜底）。
    """
    user_data = load_env_data().get("user", {})

    if not _token_cache_disabled():
        cached = _cached_token_valid(user_data)
        if cached:
            return cached

    refresh_token_value = os.environ.get("LYNKCO_REFRESH_TOKEN") or user_data.get("refreshToken")
    device_id = os.environ.get("LYNKCO_DEVICE_ID") or user_data.get("deviceId")

    if refresh_token_value and device_id:
        try:
            refreshed = refresh_token(refresh_token_value.strip(), device_id.strip())
            print("[信息] 已使用 refreshToken 自动续期获取最新 token。")
            _save_refreshed_token(refreshed)
            return refreshed["token"]
        except Exception as e:
            print(f"[警告] refreshToken 自动续期失败: {e}，将退回使用静态 token。")

    env_token = os.environ.get("LYNKCO_TOKEN")
    if env_token:
        return env_token.strip()

    token = user_data.get("token")
    if token:
        return token.strip()

    raise RuntimeError(
        "未找到有效的 token。请设置环境变量 LYNKCO_TOKEN，或在 env.json 的 "
        "user.token 中添加（参考 readme.md / env.json.example）。"
    )


# ------------------------- 极验 GT4 本地滑块辅助页面 -------------------------
#
# 极验人机挑战无法自动化，本地生成一个 HTML 页面加载极验官方 GT4 Web SDK，
# 用户完成滑动后页面会把 lot_number/captcha_output/pass_token/gen_time
# 拼装成一行 JSON 供复制粘贴回终端，无需手动抄写 4 个字段。

_GEETEST_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>LynkCo 极验滑块辅助验证</title>
<script src="{api_server}/www/gt4.js"></script>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background: #0f172a; color: #e2e8f0; display: flex; flex-direction: column;
         align-items: center; padding: 40px 16px; }}
  h2 {{ margin-bottom: 4px; }}
  .hint {{ color: #94a3b8; margin-bottom: 24px; text-align: center; }}
  #captcha-box {{ margin: 16px 0; }}
  #result-box {{ width: 100%; max-width: 640px; margin-top: 24px; display: none; }}
  textarea {{ width: 100%; height: 120px; box-sizing: border-box; background: #1e293b;
             color: #4ade80; border: 1px solid #334155; border-radius: 8px; padding: 12px;
             font-family: Menlo, Consolas, monospace; font-size: 13px; }}
  button {{ margin-top: 12px; padding: 10px 20px; border: none; border-radius: 6px;
           background: #3b82f6; color: white; font-size: 14px; cursor: pointer; }}
  button:hover {{ background: #2563eb; }}
  #copied-tip {{ color: #4ade80; margin-left: 12px; display: none; }}
  #scene-label {{ color: #facc15; font-weight: bold; }}
  #error-box {{ color: #f87171; margin-top: 16px; display: none; max-width: 640px; text-align: center; }}
</style>
</head>
<body>
  <h2>LynkCo 领克 App 登录 · 极验滑块验证</h2>
  <p class="hint">
    场景：<span id="scene-label">{scene}</span> ｜ 请完成下方滑块拖动，
    成功后会自动生成结果 JSON，点击"复制"后粘贴回终端即可。
  </p>
  <div id="captcha-box"></div>
  <div id="result-box">
    <textarea id="result-text" readonly></textarea>
    <br />
    <button onclick="copyResult()">复制结果 JSON</button>
    <span id="copied-tip">已复制 ✓</span>
  </div>
  <div id="error-box">
    加载极验SDK失败，可能是浏览器拦截了本地文件跨域请求，
    请尝试更换浏览器（推荐 Chrome）重新打开本页面，或直接在领克 App 内触发一次
    登录流程走真实滑块。
  </div>

<script>
  // captcha4.geely.com/www/gt4.js 提供的是极验旧版(GT3 风格) SDK，入口函数为
  // window.initGeetest(config, callback)，config 结构为 {{captchaId, apiServers, protocol}}。
  // SDK 默认使用 "bind" 模式挂到 document.body 下的浮层且默认隐藏，需显式调用 showBox() 弹出。
  var geetestConfig = {{
    captchaId: "{captcha_id}",
    apiServers: ["{api_server_host}"],
    protocol: "https://",
  }};

  function onGeetestSuccess(captchaObj) {{
    captchaObj.onSuccess(function () {{
      var result = captchaObj.getValidate();
      // result: {{lot_number, captcha_output, pass_token, gen_time}}
      var payload = {{
        lotNumber: result.lot_number,
        captchaOutput: result.captcha_output,
        passToken: result.pass_token,
        genTime: result.gen_time,
        scene: "{scene}",
      }};
      var text = JSON.stringify(payload);
      document.getElementById("result-text").value = text;
      document.getElementById("result-box").style.display = "block";
    }});
    captchaObj.onError(function () {{
      document.getElementById("error-box").style.display = "block";
    }});
    captchaObj.onClose(function () {{
      // 面板被用户关闭后，提供一个入口可以重新打开，避免用户卡住。
      document.getElementById("captcha-box").innerHTML =
        '<button onclick="window.__captchaObj.showBox()">重新打开滑块验证</button>';
    }});
    window.__captchaObj = captchaObj;
    captchaObj.appendTo("#captcha-box");
    captchaObj.showBox();
  }}

  function copyResult() {{
    var textarea = document.getElementById("result-text");
    textarea.select();
    document.execCommand("copy");
    var tip = document.getElementById("copied-tip");
    tip.style.display = "inline";
    setTimeout(function () {{ tip.style.display = "none"; }}, 1500);
  }}

  if (typeof initGeetest === "function") {{
    initGeetest(geetestConfig, onGeetestSuccess);
  }} else {{
    document.getElementById("error-box").style.display = "block";
  }}
</script>
</body>
</html>
"""


def generate_geetest_html(scene: str, config: dict = None, device_id: str = None) -> str:
    """
    生成本地极验 GT4 滑块辅助页面，返回写入的 HTML 文件绝对路径。

    scene: "passwordLogin" 或 "mobileLoginSendsms"，会写入页面标题和最终生成
        的 JSON 结果中，方便直接传给 validate_geetest()。
    config: get_security_config() 的返回值；不传则内部自动请求一次
        （需要 device_id）。
    """
    if config is None:
        if not device_id:
            device_id = uuid.uuid4().hex[:16]
        config = get_security_config(device_id)

    data = config.get("data") or {}
    captcha_id = data.get("captchaId")
    # gt4.js 固定挂在 apiServer 域名下的 /www/gt4.js，不能用 staticServer 拼接
    # （那是 gt4.js 加载后再去请求其他资源用的基础路径，会得到 404）。
    api_server = data.get("apiServer") or "https://captcha4.geely.com"
    # apiServers 传给 initGeetest() 时必须是纯域名，带 "https://" 协议头会导致请求地址拼接错误。
    api_server_host = api_server.split("://", 1)[-1].rstrip("/")
    if not captcha_id:
        raise RuntimeError(f"极验配置响应中未找到 captchaId: {config}")

    html = _GEETEST_HTML_TEMPLATE.format(
        api_server=api_server,
        api_server_host=api_server_host,
        captcha_id=captcha_id,
        scene=scene,
    )

    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "geetest_helper.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    return html_path


def _parse_geetest_result_json(raw: str) -> dict:
    """解析用户从辅助页面复制粘贴回来的一行 JSON，容错处理首尾多余字符。"""
    raw = raw.strip()
    result = json.loads(raw)
    required = ("lotNumber", "captchaOutput", "passToken", "genTime", "scene")
    missing = [k for k in required if not result.get(k)]
    if missing:
        raise ValueError(f"粘贴的 JSON 缺少必要字段: {missing}，原始输入: {raw}")
    return result


def serve_geetest_html(html_path: str):
    """
    启动本机临时 HTTP 服务（仅监听 127.0.0.1）以 http:// 形式提供页面，避免
    file:// 协议被浏览器安全策略拦截极验 SDK 的跨域脚本请求。
    返回 (httpd, thread, url)，调用方结束后应调用 httpd.shutdown()。
    """
    import http.server
    import socketserver
    import threading

    directory = os.path.dirname(html_path)
    filename = os.path.basename(html_path)

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

        def log_message(self, format, *args):  # noqa: A002 - 屏蔽默认访问日志刷屏
            pass

    # port=0 由系统自动分配一个空闲端口，避免和其他本地服务冲突。
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{port}/{filename}"
    return httpd, thread, url


def main():
    """
    命令行辅助入口：交互式完成一次「极验滑块 + 短信验证码」登录。

    用法：python3 lynkco_login.py <手机号>，运行后会依次：生成本地极验滑块
    辅助页面并自动打开 -> 用户完成滑块后粘贴结果 JSON 回终端 -> 调用
    validate_geetest 换取 certifyId -> 发送短信验证码 -> 输入验证码完成登录并写入 env.json。
    """
    if len(sys.argv) < 2:
        print("用法: python3 lynkco_login.py <手机号>")
        sys.exit(1)

    mobile = sys.argv[1].strip()
    device_id = uuid.uuid4().hex[:16]

    print("=== 第1步：获取极验配置 ===")
    try:
        config = get_security_config(device_id)
        print(config)
    except Exception as e:
        print(f"[错误] 获取极验配置失败: {e}")
        sys.exit(1)

    print("\n=== 第2步：生成本地极验滑块辅助页面 ===")
    html_path = None
    httpd = None
    try:
        html_path = generate_geetest_html("mobileLoginSendsms", config=config)
        # file:// 直接打开会被部分浏览器拦截跨域脚本请求，改用本地 HTTP 服务器提供页面。
        httpd, _thread, url = serve_geetest_html(html_path)
        print(f"辅助页面已生成: {html_path}")
        print(f"本地服务已启动: {url}")
        import webbrowser
        webbrowser.open(url)
        print("已尝试用默认浏览器打开，若未自动打开请手动复制上面的地址在浏览器中打开。")
    except Exception as e:
        print(f"[警告] 自动生成/打开辅助页面失败（可退回手动输入模式）: {e}")

    try:
        print("\n请在打开的页面里完成滑块拖动，成功后点击「复制结果 JSON」，")
        print("然后粘贴到下方（一行 JSON，形如")
        print('  {"lotNumber":"...","captchaOutput":"...","passToken":"...","genTime":"...","scene":"mobileLoginSendsms"}')
        print("）：")
        raw_json = input("> ").strip()

        try:
            geetest_result = _parse_geetest_result_json(raw_json)
        except Exception as e:
            print(f"[错误] 解析粘贴内容失败: {e}")
            sys.exit(1)

        print("\n=== 第3步：校验极验结果 ===")
        try:
            validate_result = validate_geetest(
                device_id,
                geetest_result["lotNumber"],
                geetest_result["captchaOutput"],
                geetest_result["passToken"],
                geetest_result["genTime"],
                geetest_result["scene"],
            )
            print(validate_result)
            certify_id = (validate_result.get("data") or {}).get("certifyId")
            if not certify_id:
                print(f"[错误] 极验校验响应中未找到 certifyId: {validate_result}")
                sys.exit(1)
        except Exception as e:
            print(f"[错误] 极验校验失败: {e}")
            sys.exit(1)

        print("\n=== 第4步：发送登录短信验证码 ===")
        try:
            sms_result = send_login_sms(device_id, mobile, certify_id)
            print(sms_result)
        except Exception as e:
            print(f"[错误] 发送短信验证码失败: {e}")
            sys.exit(1)

        verification_code = input("\n请输入收到的短信验证码: ").strip()

        print("\n=== 第5步：登录 ===")
        try:
            result = login_with_geetest_and_sms(mobile, verification_code, device_id=device_id)
            safe_result = {k: v for k, v in result.items() if k != "token"}
            safe_result["token"] = "***"
            print("登录成功，已写入 env.json：")
            print(safe_result)
        except Exception as e:
            print(f"[错误] 登录失败: {e}")
            sys.exit(1)
    finally:
        _cleanup_geetest_resources(httpd, html_path)


def _cleanup_geetest_resources(httpd, html_path):
    """统一清理本地 HTTP 服务器和生成的辅助 HTML 文件，任何异常均静默忽略。"""
    if httpd is not None:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass
    if html_path and os.path.exists(html_path):
        try:
            os.remove(html_path)
        except OSError:
            pass

# ==========================================================================
# ↓↓↓ 以下内容来自 lynkco_daily_tasks.py
# ==========================================================================

"""
领克App 每日任务编排入口（签到 + 分享 + 积分查询 + 结果通知）。

- run_daily_tasks()：编排"查积分 -> 签到 -> 分享 -> 再查积分对比"，返回结构化结果字典。
- run_and_notify()：加载 token -> 执行每日任务 -> 组装 Markdown -> 推送到 Bark。

签到/分享成功后 myEnergy 接口的积分有几秒异步延迟，故查询"之后积分"前会先 sleep。

用法:
    python3 lynkco_daily_tasks.py    # 执行每日任务（签到+分享）并推送结果
"""
import json
import os
import sys
import time


# 签到/分享完成后，等待多久再查询"之后积分"，单位秒。经真机验证 3~5 秒足够
# 让服务端把能量体变化同步到 myEnergy 接口，可通过环境变量覆盖。
ENERGY_REFRESH_DELAY_SECONDS = float(os.environ.get("LYNKCO_ENERGY_DELAY", "5"))

EP_MY_ENERGY = "/app/energy/myEnergy"


def get_my_energy(client: LynkCoSignClient) -> dict:
    """查询当前账号的能量体积分（GET /app/energy/myEnergy，与签到共用同一网关/签名体系）。"""
    resp = client._request("GET", EP_MY_ENERGY)
    return resp.json()


def run_daily_tasks(token: str, do_share: bool = True) -> dict:
    """
    编排执行一次完整的"签到 + 分享"流程，返回汇总结果字典：
    {"energy_before", "day_info", "already_signed", "sign_result", "continue_info",
     "share_result", "energy_after"}（已签到/do_share=False 时对应字段为 None）。

    do_share 默认 True，供需要在测试/脚本中跳过分享的调用方使用；
    正式入口 run_and_notify() 始终以 do_share=True 调用。
    """
    result = {}

    sign_client = LynkCoSignClient(token)

    result["energy_before"] = get_my_energy(sign_client)

    day_info = sign_client.get_sign_day_info()
    result["day_info"] = day_info

    already_signed = (day_info.get("data") or {}).get("signStatus") == 1
    result["already_signed"] = already_signed

    if already_signed:
        result["sign_result"] = None
    else:
        result["sign_result"] = sign_client.do_sign()

    result["continue_info"] = sign_client.get_continue_days()

    if do_share:
        share_client = LynkCoShareClient(token)
        try:
            result["share_result"] = share_client.do_share()
        except Exception as e:
            result["share_result"] = {"ok": False, "detail": {"message": f"分享任务异常: {e}"}}
    else:
        result["share_result"] = None

    # 积分变化有异步延迟，等待片刻再查询，避免看到"没有变化"的假象。
    if not already_signed or (do_share and (result.get("share_result") or {}).get("ok")):
        time.sleep(ENERGY_REFRESH_DELAY_SECONDS)
    result["energy_after"] = get_my_energy(sign_client)

    return result


def run_and_notify() -> dict:
    """
    完整入口：加载 token -> 执行每日任务(run_daily_tasks) -> 组装 Markdown
    -> 推送到 Bark（lynkco_notify 模块）。

    每日任务固定包含"签到+分享"两项（分享接口每日有加分次数上限，重复
    调用不会重复加分，详见 docs/分享任务接口说明.md）。

    返回值同 run_daily_tasks()，并额外附带 "notify_result" 字段（Bark
    推送接口的响应，或 {"skipped": True}）。
    """
    token = load_token()

    print("=== 执行每日任务（签到+分享）===")
    result = run_daily_tasks(token, do_share=True)
    print(json.dumps(mask_sensitive(result), ensure_ascii=False, indent=2))

    markdown_body = build_markdown_report(result)
    print("\n=== 推送内容预览 ===")
    print(markdown_body)

    icon = os.environ.get(
        "LYNKCO_BARK_ICON"
    )
    try:
        notify_result = send_bark_notification(
            title="领克App · 每日任务",
            markdown_body=markdown_body,
            icon=icon,
        )
    except Exception as e:
        # 推送失败（网络问题/代理超时等）不应影响签到/分享本身已经成功执行
        # 这一事实，只记录警告，不让整个流程以异常状态退出。
        print(f"[警告] Bark 推送失败（不影响签到/分享结果）: {e}")
        notify_result = {"skipped": True, "error": str(e)}
    print("\n=== Bark 推送结果 ===")
    print(json.dumps(mask_sensitive(notify_result), ensure_ascii=False, indent=2))

    result["notify_result"] = notify_result
    return result


def main():
    try:
        run_and_notify()
    except RuntimeError as e:
        print(f"[错误] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[错误] 执行失败: {e}")
        sys.exit(1)

# ==========================================================================
# ↓↓↓ 以下内容来自 qinglong_notify.py
# ==========================================================================

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

# ---------------------------------------------------------------------------
# 单文件兼容层：把本模块同时注册为各个子模块名，使 `import lynkco_common`、
# `lynkco_common.NATIVE_APP_KEY` 这类跨模块引用在合并后依然成立。
# ---------------------------------------------------------------------------
_THIS_MODULE = sys.modules[__name__]
for _alias in ('lynkco_common', 'lynkco_notify', 'lynkco_share', 'lynkco_sign', 'lynkco_login', 'lynkco_daily_tasks', 'qinglong_notify', 'lynkco_qinglong'):
    sys.modules.setdefault(_alias, _THIS_MODULE)
lynkco_common = _THIS_MODULE
lynkco_notify = _THIS_MODULE
lynkco_share = _THIS_MODULE
lynkco_sign = _THIS_MODULE
lynkco_login = _THIS_MODULE
lynkco_daily_tasks = _THIS_MODULE
qinglong_notify = _THIS_MODULE
lynkco_qinglong = _THIS_MODULE


# ==========================================================================
# ↓↓↓ 以下内容来自 lynkco_qinglong.py
# ==========================================================================

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
    except Exception as _e:
        print(f"[错误] 脚本异常退出: {_e}")
        sys.exit(1)
