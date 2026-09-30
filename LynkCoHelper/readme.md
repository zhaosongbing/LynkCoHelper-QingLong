---
# 郑重告知：本程序源码仅供学习研究使用，使用该程序造成的一切后果与程序作者无关
---

## 简介

领克 App「我的-签到」自动化脚本，支持每日签到、分享任务、积分查询，并可通过 Bark 推送结果。

模块划分：

| 文件 | 作用 |
| --- | --- |
| `lynkco_common.py` | 签名算法 + 公共常量 + `env.json` 读写 |
| `lynkco_login.py` | token 获取统一入口 `load_token()`，含账号密码/短信验证码全流程登录 |
| `lynkco_sign.py` | 每日签到逻辑 |
| `lynkco_share.py` | 分享任务逻辑（可选功能） |
| `lynkco_notify.py` | Bark 推送工具 |
| `lynkco_daily_tasks.py` | 顶层入口：编排签到 + 分享 + 积分查询并推送结果 |
| `lynkco_qinglong.py` | 青龙面板专用入口：依赖自检 + 多账号 + 双通道通知 + 退出码 |
| `qinglong_notify.py` | 青龙面板自带通知渠道适配（sendNotify.py / notify.js） |
| `lynkco_ql_tasks.py` + `qinglong_tasks.json` | 拉库后按清单自动在青龙面板建定时任务：OpenAPI 幂等同步 + cron/间隔校验 + 去重 + 清理 |
| `tests/` | `lynkco_ql_tasks.py` 的单元测试（内含模拟面板，无需真实青龙即可跑） |

原理、签名算法、接口协议等技术细节见 `docs/` 目录，此处只介绍如何使用。

## AppSecret 自动提取

从模拟器内运行的领克 App 中自动提取 API 密钥（`nativeAppKey` / `nativeAppSecret`），写入 `env.json` 的 `secrets` 段。原理：x86_64 系统镜像（自带 libndk，可翻译执行 arm64 原生库）+ KVM 加速冷启动 + jdb 在加固壳常量类 `<clinit>` 设断点读取字段。

### 按平台入口（`tools/`）

| 脚本 | 平台 | 说明 |
| --- | --- | --- |
| `tools/extract_appsecret_mac.py` | macOS（HVF） | 原版交互式流程 |
| `tools/extract_appsecret_ubuntu.py` | WSL2 Ubuntu（KVM） | 一键式，环境缺失自动下载 |
| `tools/extract_appsecret_windows.py` | Windows 原生编排 | 模拟器跑在 WSL2，Windows 侧驱动 |
| `tools/extract_appsecret_github_action.py` | GitHub Actions | 全自动无人值守 |

### CI workflows（`.github/workflows/`）

| Workflow | 触发 | 作用 |
| --- | --- | --- |
| `extract-appsecret` | 手动 | 全自动提取密钥 → 运行日志输出**一次性取件链接**（仅可打开 1 次、10 分钟失效），需手动填入 `LYNKCO_APP_SECRETS` 供 daily-tasks 使用 |
| `fetch-lynkco-apk` | 每日 + 手动 | 拉取最新版领克 APK 上传 Release（`apk-v VERSION` 留历史 + `apk-latest` 稳定资产），提取 CI 优先使用 |

### 安全说明

- 所有日志输出均脱敏（密钥值仅显示前 3 后 2 位），明文只写入本地 `env.json`（gitignore）；CI 提取时通过一次性链接送达明文（仅可打开 1 次，10 分钟失效）
- CI 使用的 x86_64 模拟器镜像与 APK 均托管在仓库 Release（`sysimg-x86_64-33-r09` / `apk-latest`），版本经实测钉死，不随上游变动漂移

详细排障（镜像版本坑、IPv6 坑、forward 生命周期坑等）见 `docs/本地一键提取指南.md`。

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置账号与密钥

复制 `env.json.example` 为 `env.json`，按需填写：

```json
{
  "user": {
    "username": "",
    "password": "",
    "token": "bearerXXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX",
    "refreshToken": "",
    "deviceId": ""
  },
  "secrets": {
    "nativeAppKey": "",
    "nativeAppSecret": "",
    "nativeAppCode": "",
    "loginAppCode": "",
    "glDevId": ""
  },
  "notify": {
    "barkKey": ""
  }
}
```

- `user`：账号相关凭据（token 至少需要一个，见下）。
- `secrets`：领克 App 的应用级签名密钥（非个人凭证，但代码中不内置，必须自行配置），获取方式见 `docs/AppSecret_逆向分析记录.md`。每个字段都支持用同名大写环境变量覆盖（如 `LYNKCO_NATIVE_APP_KEY`）；也可用一个整合环境变量 `LYNKCO_APP_SECRETS`（值为与 `secrets` 结构相同的 JSON 字符串）一次性提供全部 5 个字段，适合 CI 只想配置一个 Secret 的场景（优先级：单独字段环境变量 > `LYNKCO_APP_SECRETS` > `env.json`）。均未配置时程序会直接报错退出。
- `notify`：推送相关配置，`barkKey` 为 Bark 推送 Key（可选，也可用环境变量 `LYNKCO_BARK_KEY` 覆盖）。

### 3. 获取 token

登录环节含极验滑块验证，无法完全自动化，需人工/半自动获取一次：

- **token**（约 30 分钟有效）：写入 `user.token`，或设置环境变量 `LYNKCO_TOKEN`。
- **refreshToken + deviceId**（约 30 天有效，推荐）：写入 `user.refreshToken` / `user.deviceId`，或设置环境变量 `LYNKCO_REFRESH_TOKEN` / `LYNKCO_DEVICE_ID`。配置后脚本每次运行会自动续期 token 并回写 `env.json`，无需频繁手动登录。

获取方式：抓包登录一次领克 App，或运行 `python3 lynkco_login.py <手机号>` 交互式完成滑块 + 短信登录（会自动写入 `env.json`）。详见 `docs/登录接口协议说明.md`。

### 4. 执行

```bash
python3 lynkco_helper.py         # 签到 + 分享
python3 lynkco_daily_tasks.py    # 签到 + 分享 + 积分查询 + Bark 推送
```

分享任务（`lynkco_share.py`）每日有加分次数上限，接口返回成功不代表真正加分，重复调用不会重复加分，详见 `docs/分享任务接口说明.md`。

可选环境变量：

| 变量 | 说明 |
| --- | --- |
| `LYNKCO_BARK_KEY` | Bark 推送的 Key，未配置则跳过推送（也可写入 `env.json` 的 `notify.barkKey`） |
| `LYNKCO_ENERGY_DELAY` | 签到/分享后查询积分前的等待秒数，默认 5 |
| `LYNKCO_BARK_ICON` | Bark 推送使用的图标 URL，默认使用领克官方图标 |

## 部署到青龙面板（Qinglong Panel）

完整说明见 [`docs/青龙面板部署指南.md`](docs/青龙面板部署指南.md)，这里只给最短路径：

1. 拉库（青龙 → 定时任务 / 订阅管理）：

   ```bash
   ql repo https://github.com/zhaosongbing/LynkCoHelper-QingLong.git "LynkCoHelper" "docs|previews|tools" "requirements.txt" "main"
   ```

   > 青龙版只存在于 `LynkCoHelper-QingLong` 仓库，原仓库 `LynkCoHelper` 没有 `lynkco_qinglong.py`。

   不方便拉库时可用单文件版：`qinglong/lynkco_qinglong_single.py`
   （由 `tools/build_qinglong_single.py` 生成），粘贴到面板「新建脚本」或用 `ql raw` 添加。

2. 配置环境变量（**关闭「自动拆分」**）：

   | 变量 | 必需 | 说明 |
   | --- | --- | --- |
   | `LYNKCO_APP_SECRETS` | ✅ | 单行 JSON：`{"nativeAppKey":"","nativeAppSecret":"","nativeAppCode":"","loginAppCode":"","glDevId":""}` |
   | `LYNKCO_REFRESH_TOKEN` | ✅（推荐） | 约 30 天有效，可自动续期 |
   | `LYNKCO_DEVICE_ID` | ✅（配合上） | 与 refreshToken 同一次抓包 |
   | `LYNKCO_TOKEN` | 备选 | 静态 token，约 30 分钟有效 |
   | `LYNKCO_BARK_KEY` | 可选 | Bark 推送 Key |
   | `LYNKCO_QL_NOTIFY` | 可选 | 默认 1，走青龙自带通知渠道；0 关闭 |
   | `LYNKCO_DO_SHARE` | 可选 | 默认 1，0 只签到不分享 |

3. 新建定时任务：命令 `task LynkCoHelper-QingLong/LynkCoHelper/lynkco_qinglong.py`，定时规则 `8 8 * * *`。

   多账号：`LYNKCO_REFRESH_TOKEN` / `LYNKCO_DEVICE_ID` 用换行或 `&` 分隔多组即可，脚本会逐个执行并汇总。

### 拉库时自动创建定时任务（免手工建任务）

不想手动建任务的话，用 `lynkco_ql_tasks.py sync` 按仓库里的清单 `qinglong_tasks.json`
幂等注册到面板（任务名/规则/启用状态都由清单决定，重复拉库不会建重，青龙自建的任务也会被校正）：

```bash
# 订阅「执行后」填这一条，拉完库就自动注册
task LynkCoHelper-QingLong/LynkCoHelper/lynkco_ql_tasks.py sync

# 或手动跑一次；先自检凭据与清单
python3 /ql/data/scripts/LynkCoHelper-QingLong/LynkCoHelper/lynkco_ql_tasks.py status
```

需要青龙 OpenAPI 凭据：环境变量 `QL_TOKEN`（或 `QL_CLIENT_ID` + `QL_CLIENT_SECRET`，
应用需勾选 `crons` 权限）。详见 [`docs/青龙自动创建定时任务.md`](docs/青龙自动创建定时任务.md)。

## 部署到 GitHub Actions 定时执行

项目内置 `.github/workflows/daily-tasks.yml`，默认每天北京时间 8:00（UTC 0:00，GitHub 调度可能有延迟）自动运行。

1. Fork 本仓库。
2. 进入 `Settings → Secrets and variables → Actions`，新增 Secret：
   - 必需：`LYNKCO_TOKEN`（或 `LYNKCO_REFRESH_TOKEN` + `LYNKCO_DEVICE_ID`，推荐后者，可自动续期）。
   - 必需：`LYNKCO_APP_SECRETS`，一个 JSON 字符串，整合了 `env.json` 中 `secrets` 段的全部 5 个字段，形如：
     ```json
     {"nativeAppKey":"...","nativeAppSecret":"...","nativeAppCode":"...","loginAppCode":"...","glDevId":"..."}
     ```
     （如果不想合并配置，也可仍改用 5 个独立的 `LYNKCO_NATIVE_APP_KEY` 等 Secret，同时修改 workflow 中的 `env` 字段）。
   - 可选：`LYNKCO_BARK_KEY`（daily-tasks 的 Bark 推送）。
3. 可在 `Actions` 页面手动触发一次 workflow 测试。
4. 仅配置 `LYNKCO_TOKEN` 时，token 失效后需要手动更新；配置 `refreshToken` 后可自动续期，仅需在其过期（约 30 天）时才需人工干预。

## 已知限制

- 登录环节（滑块验证码、可能的短信验证）无法完全自动化。
- 已确认的 iPhone 使用场景可在安装并完全信任代理证书后抓包；桌面助手按此流程设计。不同 App/系统版本仍需真机验收，不能仅因系统是 iOS 就判定无法抓包。
- `token`/`refreshToken`/`secrets` 均为敏感信息，请勿提交到公开仓库（`env.json` 已在 `.gitignore` 中忽略），应通过 GitHub Secrets 或本地 `env.json` 传递。

## 更多文档

- `docs/本地一键提取指南.md`：AppSecret 自动提取的完整流程、各平台坑位与排障速查。
- `docs/AppSecret_逆向分析记录.md`：签名密钥的逆向分析过程与获取方式。
- `docs/登录接口协议说明.md`：登录/续期相关接口协议细节。
- `docs/分享任务接口说明.md`：分享任务接口协议与限制说明。
