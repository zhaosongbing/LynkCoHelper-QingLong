# -*- coding: utf-8 -*-
"""
把 LynkCoHelper 拆分的多个模块合并成一个「单文件版」青龙脚本，
方便在不方便拉库（或只想用青龙面板「新建脚本」粘贴）的场景下使用。

用法（在仓库根目录执行）：
    python3 LynkCoHelper/tools/build_qinglong_single.py
    python3 LynkCoHelper/tools/build_qinglong_single.py -o /path/to/output.py

产物默认写到 qinglong/lynkco_qinglong_single.py，可直接：
    · 在青龙面板「脚本管理 → 新建脚本」里粘贴；
    · 或用 `ql raw <该文件的 raw URL>` 添加。

合并规则：
    1. 按依赖顺序拼接：common -> notify -> share -> sign -> login -> daily_tasks
       -> qinglong_notify -> 青龙入口；
    2. 用 ast 剔除所有跨模块的本地 import（lynkco_* / qinglong_notify），
       以及各模块自带的 `if __name__ == "__main__":` 入口块；
    3. 末尾把当前模块注册为各子模块名并起别名，兼容 `import lynkco_common`
       与 `lynkco_common.NATIVE_APP_KEY` 两种引用方式；
    4. 统一保留一个 `if __name__ == "__main__":` 入口，调用青龙入口的 main()；
    5. 剥掉各模块自带的定时任务声明行（注释形式的 cron / name 标签）。面板拉库时扫脚本
       头部认这两行就会自动建任务；单文件版默认不带，否则整仓拉库时会与多模块版
       lynkco_qinglong.py 各建一条、同一份签到跑两遍。只想用单文件版、又想让面板自动
       建任务的场景，用 --cron / --name 显式打开。
"""
import argparse
import ast
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULE_DIR = os.path.join(REPO_ROOT, "LynkCoHelper")

# 需要内联的本地模块（顺序即依赖顺序，被依赖者在前）
LOCAL_MODULES = (
    "lynkco_common",
    "lynkco_notify",
    "lynkco_share",
    "lynkco_sign",
    "lynkco_login",
    "lynkco_daily_tasks",
    "qinglong_notify",
)

ENTRY_MODULE = "lynkco_qinglong"

HEADER = '''# -*- coding: utf-8 -*-
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

本文件默认不写定时任务声明：整仓拉库时多模块版 lynkco_qinglong.py 已经声明过，
再声明一次会建出两条任务、同一份签到跑两遍。只用这一个文件、又想让面板拉库自动建任务的，
在本文件头部（第 3 行起）加两行注释声明即可：第 1 行写 cron 标签加定时规则
（规则写法 8 8 * * *，即每天 08:08），第 2 行写 name 标签加任务名（领克·每日签到分享）。
"""
import sys
'''

ALIAS_BLOCK = '''

# ---------------------------------------------------------------------------
# 单文件兼容层：把本模块同时注册为各个子模块名，使 `import lynkco_common`、
# `lynkco_common.NATIVE_APP_KEY` 这类跨模块引用在合并后依然成立。
# ---------------------------------------------------------------------------
_THIS_MODULE = sys.modules[__name__]
for _alias in {alias_list}:
    sys.modules.setdefault(_alias, _THIS_MODULE)
{alias_assignments}
'''

RUNNER_FOOTER = '''

if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as _e:
        print(f"[错误] 脚本异常退出: {_e}")
        sys.exit(1)
'''


def strip_module_source(name: str, strip_imports: bool = True) -> str:
    """
    读取模块源码，剔除 shebang/coding 行与 __main__ 入口块。
    strip_imports=True 时再剔除本地模块 import（非入口模块需要，入口模块保留，
    由上一步注册到 sys.modules 的别名来满足）。
    """
    path = os.path.join(MODULE_DIR, name + ".py")
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()

    tree = ast.parse(source)
    drop_lines = set()

    if strip_imports:
        # 剔除 `import lynkco_xxx` / `from lynkco_xxx import ...`（含函数体内的延迟导入）
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                hit = any(alias.name.split(".")[0] in LOCAL_MODULES + (ENTRY_MODULE,) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                hit = (node.module or "").split(".")[0] in LOCAL_MODULES + (ENTRY_MODULE,)
            else:
                continue
            if hit:
                drop_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))

    # 剔除模块级 `if __name__ == "__main__":` 入口块
    for node in tree.body:
        if isinstance(node, ast.If) and _is_main_guard(node):
            drop_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))

    kept = []
    for i, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if i in drop_lines:
            continue
        if stripped.startswith("#!") or stripped.startswith("# -*- coding"):
            continue
        kept.append(line)

    return "\n".join(kept).strip("\n")


def _is_main_guard(node: ast.If) -> bool:
    test = node.test
    if not isinstance(test, ast.Compare) or not isinstance(test.left, ast.Name):
        return False
    if test.left.id != "__name__" or len(test.comparators) != 1:
        return False
    right = test.comparators[0]
    return isinstance(right, ast.Constant) and right.value == "__main__"


def _banner(name: str) -> str:
    return f"\n\n# {'=' * 74}\n# ↓↓↓ 以下内容来自 {name}.py\n# {'=' * 74}\n\n"


# 面板（青龙与呆呆面板等分支）拉库时扫描脚本头部，按下面两条正则认定时规则与任务名，
# 认出来就自动建定时任务。单文件版默认不声明，避免与多模块版重复建任务。
_CRON_DECL_RE = re.compile(r"^\s*#\s*@?cron\b\s*[:：]?\s*\S.*$")
_NAME_DECL_RE = re.compile(r"^\s*#\s*@?name\b\s*[:：]?\s*\S.*$")


def _strip_task_declarations(text: str) -> str:
    """去掉注释形式的 cron / name 声明行。"""
    return "\n".join(
        line for line in text.splitlines()
        if not _CRON_DECL_RE.match(line) and not _NAME_DECL_RE.match(line)
    )


def _declare_block(cron: str, name: str) -> str:
    """生成文件头部的定时任务声明块；cron 为空则不声明。"""
    if not cron:
        return ""
    lines = [f"# cron: {cron.strip()}"]
    if name:
        lines.append(f"# name: {name.strip()}")
    return "".join(line + "\n" for line in lines)


def build(output_path: str, cron: str = "", task_name: str = "") -> str:
    # 声明块插在 coding 行之后、模块 docstring 之前，保证落在面板扫描的前若干行内
    header_lines = HEADER.split("\n")
    chunks = [
        header_lines[0] + "\n",
        _declare_block(cron, task_name),
        "\n".join(header_lines[1:]),
    ]

    # 1) 业务模块：剔除跨模块 import，全部符号落在同一命名空间
    for name in LOCAL_MODULES:
        chunks.append(_banner(name) + _strip_task_declarations(strip_module_source(name, strip_imports=True)))

    # 2) 兼容层：注册子模块别名，供入口模块的 import 与 `lynkco_common.X` 使用
    aliases = list(LOCAL_MODULES) + [ENTRY_MODULE]
    alias_assignments = "\n".join(f"{a} = _THIS_MODULE" for a in aliases)
    chunks.append(ALIAS_BLOCK.format(alias_list=repr(tuple(aliases)), alias_assignments=alias_assignments))

    # 3) 青龙入口模块：保留其 import（由上面的 sys.modules 别名满足）
    chunks.append(_banner(ENTRY_MODULE) + _strip_task_declarations(strip_module_source(ENTRY_MODULE, strip_imports=False)))

    # 4) 统一入口
    chunks.append(RUNNER_FOOTER)

    content = "".join(chunks)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="生成青龙面板单文件版脚本")
    parser.add_argument("-o", "--output", default=os.path.join(REPO_ROOT, "qinglong", "lynkco_qinglong_single.py"),
                        help="输出文件路径，默认 qinglong/lynkco_qinglong_single.py")
    parser.add_argument("--cron", default="",
                        help="在文件头部写入定时任务声明（如 '8 8 * * *'），面板拉库时据此自动建任务；"
                             "默认不写，避免与多模块版 lynkco_qinglong.py 重复建任务")
    parser.add_argument("--name", default="领克·每日签到分享",
                        help="配合 --cron 使用，指定自动建出来的任务名")
    args = parser.parse_args()

    output = build(os.path.abspath(args.output), cron=args.cron, task_name=args.name)
    # 语法校验：合并产物必须能被编译通过
    with open(output, "r", encoding="utf-8") as f:
        compile(f.read(), output, "exec")
    print(f"已生成单文件版脚本: {output}")
    print(f"行数: {len(open(output, 'r', encoding='utf-8').read().splitlines())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
