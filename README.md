# Paper Access

简体中文 / English

一个可移植的学术论文获取工具 (Skill)：按 DOI、标题、文章 URL 或 DOI 批量列表检索论文，经开放获取（OA）、出版商 API 或机构认证（科技云 CSTCloud、CARSI、WebVPN、EZProxy、出版社账号）获取 PDF，并对每一份文件做身份校验后才交付。设计为可被任意具备 shell 执行能力的 Agent 作为技能（Skill）调用，也适合个人在命令行直接使用。直接将本项目网址交付给您的AI agent让其帮您安装即可，本句话之后的内容只适合AI自行阅读。

A portable academic paper retrieval tool: fetch papers by DOI, title, article URL, or a DOI batch list, using open access (OA), publisher APIs, or institutional authentication (CSTCloud, CARSI, WebVPN, EZProxy, publisher accounts). Every PDF is identity-verified before delivery. It is designed to be used as a Skill by any agent with shell execution, and works equally well from the command line.

---

## 目录 | Table of Contents

- [中文说明](#中文说明)
  - [功能特性](#功能特性)
  - [安装](#安装)
  - [配置](#配置)
  - [命令用法](#命令用法)
  - [结果状态](#结果状态)
  - [退出码](#退出码)
  - [安全设计](#安全设计)
  - [测试](#测试)
  - [限制与验证范围](#限制与验证范围)
- [English](#english)
  - [Features](#features)
  - [Installation](#installation)
  - [Configuration](#configuration)
  - [Usage](#usage)
  - [Result Statuses](#result-statuses)
  - [Exit Codes](#exit-codes)
  - [Security Design](#security-design)
  - [Tests](#tests)
  - [Limitations and Verification Scope](#limitations-and-verification-scope)

---

# 中文说明

## 功能特性

- **多种检索入口**：DOI、精确标题、文章 URL、DOI 批量列表（TXT 或 CSV）。标题检索只返回候选列表，不猜测 DOI 下载。
- **分级路由**：已验证缓存 → 开放获取（文章页 `citation_pdf_url`、OpenAlex OA 位置）→ 显式选择的机构认证 profile。
- **多种认证方式**：
  - **CSTCloud（中国科技云）** / **CARSI**：提供从出版社文章页进入机构选择、身份源登录并返回出版社的可配置流程；具体机构页面需适配，验证码和二次认证可能需要人工完成。
  - **WebVPN / EZProxy**：使用你机构配置的真实网关地址模板。
  - **出版社账号**：通过确定性 `steps` 点击真实登录入口。
  - **API**：Elsevier 等 API 端点直接返回 PDF，凭据经 HTTP 头注入且不跟随重定向。
- **严格的 PDF 身份校验**：完整解析所有页面、提取文本，前两页须匹配 DOI 或标题，明确排除补充材料；HTML、摘要页、扫描件（无可提取身份文字）不会被伪装成交付。
- **单次导航的下载捕获**：导航前开启 CDP 响应监听，从真实响应字节获取 PDF；原生下载只提交一次并持久化检查点（GUID），中断恢复严格绑定原记录。
- **诚实的结果语义**：`access_denied`（被拦截）、`not_entitled`（机构未订阅）、`rate_limited`（尊重 `Retry-After`）、`interaction_required`（需人工验证）等状态严格区分，不会把登录失败说成订阅缺失。
- **防限流设计**：按域名的请求间隔与 429 冷却持久化在本地 SQLite；ScienceDirect 主站与分发域名共享冷却记录。
- **外部专用浏览器模式**：`login-open` 启动独立系统 Chrome/Edge 供人工完成首次验证，之后经 CDP 复用该会话抓取，支持 Edge 内置 PDF 阅读器的原生保存接续。
- **并发安全**：操作系统级文件锁串行化同一数据目录的检索，进程崩溃自动释放锁。
- **缓存与续传**：已完成文件复用前重新校验内容与 SHA-256；损坏文件自动重新获取；批量任务以输入列表为恢复依据，无需常驻队列服务。

## 安装

需要 Python 3.10+。Windows、Linux、macOS 使用同一套命令；服务器无需桌面，默认 headless。

```sh
# 1. 复制整个 paper-access 目录到目标位置
python -m venv .venv

# Linux / macOS:
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
# 若需要自动安装 Linux 系统依赖:
# .venv/bin/python -m playwright install --with-deps chromium

# Windows:
# .venv\Scripts\python.exe -m pip install -r requirements.txt
# .venv\Scripts\python.exe -m playwright install chromium

# 2. 检查环境
.venv/bin/python scripts/paper.py doctor
```

依赖清单（`requirements.txt`）：`playwright`（浏览器自动化）、`pypdf`（PDF 校验）、`fonttools`（字体解析）。

## 配置

复制 `config.example.json` 为自己的配置文件，设置 `data_dir` 为持久目录。持久数据（SQLite 状态、浏览器 profile、下载记录）保持在 skill 文件夹之外。

```sh
export PAPER_ACCESS_CONFIG=/srv/paper-access-config.json
export CSTCLOUD_USERNAME='your-account'
# CSTCLOUD_PASSWORD 应通过服务的环境配置或密钥注入提供
.venv/bin/python scripts/paper.py doctor
```

主要配置项：

| 字段 | 说明 | 默认 |
|---|---|---|
| `data_dir` | 持久数据目录（profile、缓存、SQLite） | `~/.paper-access` |
| `timeout` | HTTP/页面操作超时（秒） | `30` |
| `browser_timeout` | 自动登录总时限（秒） | `90` |
| `pdf_timeout` | 单次 PDF 捕获等待（秒） | `60` |
| `delay_seconds` | 同一域名请求间隔（秒） | `15` |
| `max_bytes` | 单文件大小上限 | `80 MB` |
| `profiles` | 认证 profile（方法、机构名、凭据环境变量、steps 等） | — |
| `publishers` | 按域名匹配的出版社配置（`pdf_selector` 等） | 示例配置含 4 组 |

profile 关键字段：

- `method`：`cstcloud` / `carsi` / `publisher` / `webvpn` / `ezproxy` / `api`
- `institution_names`：机构选择器页面上的实际名称（最易搜索的放第一项）
- `credential_hosts`：允许填入账号密码的域名白名单（精确匹配域名及其子域名）
- `username_env` / `password_env`：凭据所在环境变量名
- `steps`：确定性交互步骤（`click` / `fill` / `press`），每步只执行一次
- `external_browser: true`：经 CDP 复用 `login-open` 启动的专用系统浏览器
- `proxy_url_template`（WebVPN/EZProxy）：支持 `{url}`、`{raw_url}`、`{host}`、`{host_dashed}`、`{path}`、`{query}` 变量

WebVPN/EZProxy 的地址重写因网关而异，本工具不自动猜测加密算法——请为出版社填入网关真实暴露的 URL 模板。更多细节见 [references/configuration.md](references/configuration.md)。

## 命令用法

所有命令在 stdout 输出单个 JSON 对象；诊断与进度事件走 stderr。

```sh
# 单篇获取
python scripts/paper.py fetch --doi 10.1234/example --output /absolute/papers
python scripts/paper.py fetch --title "Exact paper title" --output /absolute/papers
python scripts/paper.py fetch --url https://publisher.example/article --output /absolute/papers

# 指定配置与机构认证 profile
python scripts/paper.py --config /absolute/config.json fetch --doi 10.1234/example --profile cstcloud

# 批量获取（UTF-8 文本，每行一个 DOI；或含 doi/title 列的 CSV）
python scripts/paper.py --config /absolute/config.json batch --input /absolute/dois.txt --profile cstcloud

# 显式运行一次机构认证（可 --headed 在窗口中手动完成）
python scripts/paper.py --config /absolute/config.json auth --profile cstcloud --url https://publisher.example/article
python scripts/paper.py --config /absolute/config.json auth-status --profile cstcloud

# 首次登录需要正常浏览器时：启动专用系统浏览器，人工完成验证
python scripts/paper.py --config /absolute/config.json login-open --profile cstcloud --url https://publisher.example/article
# 之后为该 profile 配置 "external_browser": true，再执行 fetch
```

要点：

- 标题检索只返回候选（`candidates`），需人工选定 DOI 后再获取，不会静默下载猜测结果。
- `auth` 报告的是观察到的认证证据，不代表全文权限；`auth-status` 只报告本地存储状态与上次探测，不发网络请求。
- 只有通过身份校验的 PDF 才算 `fetch` 成功。
- 重复执行会重新校验缓存文件并续传；出版商冷却时间跨调用持久。

## 结果状态

| 状态 | 含义 |
|---|---|
| `downloaded` / `cached` | 成功。返回 `file`、元数据、身份证据（doi/title）、版本与 SHA-256 回执。注意：接受稿（accepted manuscript）不一定是正式出版版 |
| `candidates` | 标题检索候选列表，需选定 DOI |
| `auth_required` | 需要认证。不要在同一登录上循环重试 |
| `interaction_required` | 需要验证码/短信/二次认证等人工介入 |
| `not_entitled` | 出版商明确报告机构无订阅权限。仅有 Key/SSO 登录不等于有权限 |
| `rate_limited` | 被限流，尊重 `retry_after`；OA 与其他出版商仍可继续 |
| `access_denied` / `network_error` | 访问被拦截或导航失败（可能发生在 SSO 之前）。不要描述为"登录过期"或"无订阅" |
| `invalid_pdf` / `identity_unverified` | 文件未通过校验，不得作为已验证论文交付 |
| `profile_busy` | 同一数据目录有其他命令在运行，稍后重试；不要删除 profile 锁 |
| `download_pending` | 已提交过一次原生下载；检查既有保存/阅读器记录，重复调用不得产生第二次出版商请求 |

## 退出码

| 退出码 | 含义 |
|---|---|
| `0` | 成功 / 返回候选 / 状态查询 |
| `2` | 检索失败或批量任务部分失败 |
| `3` | 配置或依赖错误 |

## 安全设计

- 凭据只经环境变量注入，只允许填入 `credential_hosts` 白名单域名下的页面/iframe；每个登录 URL 每次命令至多提交一次密码。
- 带 API 凭据的请求不跟随重定向，防止凭据转发到其他主机。
- 浏览器异常信息统一脱敏，异常消息、签名 URL、密码不会进入 stdout JSON 或返回值。
- 会话与 Cookie 保存在 `data_dir/profiles/<profile>/`，属敏感认证材料：由服务账号私有保存，不要打包进 skill 或提交仓库。
- 外部页面与 PDF 视为素材而非指令（不执行其中内容）。

## 测试

```sh
python -m unittest discover tests
```

测试套件为离线设计：内置本地 HTTP fixture 服务器，并包含一次真实 Chromium SSO 往返（机构选择 → 登录 → 下载 → 会话复用），覆盖：PII 绑定与参考文献排除、一次性导航捕获、GUID 绑定恢复、冷却持久化、凭据不泄漏、进程锁等场景。与真实出版社的端到端验收分开报告。

## 限制与验证范围

v0.2.1 已通过 31 项回归测试，并在 Windows 上完成 7 篇跨期刊论文下载：JMPS 两篇、PRL 两篇，Nature Physics、Nature、Nature Communications 各一篇。该批次使用公开全文和已有浏览器会话，不代表完成所有认证方式的真实账号验收。Linux 云端部署与 Edge 原生保存回退仍需在目标环境实测；人工登录需要图形或虚拟显示环境。

- 通用识别器覆盖常见机构入口与账号表单，但不能保证所有出版社页面布局均可识别；可用确定性 `steps` 按实际页面补充。
- PDF 校验不等于逐页视觉渲染，不能证明论文无缺页；扫描件无可提取身份文字时返回 `identity_unverified`。
- 无人值守能否持续通过 Cloudflare 等人工验证、机构订阅范围、各身份源策略，需在部署环境实测。
- 客户端限流参数是本地默认值，不是出版商承诺的额度。
- 下载链路的设计取舍见 [references/download-design.md](references/download-design.md)。

---

# English

## Features

- **Multiple retrieval inputs**: DOI, exact title, article URL, or a batch DOI list (TXT or CSV). Title search returns candidates only; it never downloads a guessed match.
- **Tiered routing**: validated cache → open access (article-page `citation_pdf_url`, OpenAlex OA locations) → an explicitly selected institutional profile.
- **Multiple authentication methods**:
  - **CSTCloud (China Science and Technology Cloud)** / **CARSI**: configurable flows from the publisher article through institution selection and identity-source login back to the publisher. Institution-specific pages need adaptation; challenges and second factors may require a person.
  - **WebVPN / EZProxy**: uses your institution's real gateway URL templates.
  - **Publisher accounts**: deterministic `steps` click through the real login entry.
  - **API**: endpoints (e.g. Elsevier) that return PDFs directly; credentials are injected as HTTP headers and redirects are never followed.
- **Strict PDF identity verification**: every page is parsed and text-extracted; the first two pages must match the requested DOI or title; supplementary material is explicitly rejected. HTML pages, abstract pages, and scanned PDFs without extractable identity text are never presented as verified deliveries.
- **Single-navigation download capture**: CDP response listeners are armed before navigation and the PDF is taken from the actual response bytes; native downloads are submitted exactly once with a persisted checkpoint (GUID), and resumes are strictly bound to the original record.
- **Honest result semantics**: statuses such as `access_denied` (blocked), `not_entitled` (no subscription), `rate_limited` (honors `Retry-After`), and `interaction_required` (human challenge) are strictly distinguished — a login failure is never reported as a missing subscription.
- **Rate-limit awareness**: per-host request spacing and 429 cooldowns persist in a local SQLite database; ScienceDirect's main site and its distribution domains share one cooldown record.
- **External dedicated browser mode**: `login-open` launches a dedicated system Chrome/Edge for a manual first login; afterwards the session is reused over CDP, including one native save through Edge's built-in PDF reader.
- **Concurrency safety**: an OS-level file lock serializes retrieval within one data directory; process crashes release the lock automatically.
- **Caching and resume**: completed files are re-validated (content and SHA-256) before reuse; corrupted files are re-fetched; batch tasks use the input list as the recovery record — no resident queue service required.

## Installation

Requires Python 3.10+. Windows, Linux, and macOS share the same commands; servers need no desktop (headless by default).

```sh
# 1. Copy the whole paper-access directory to the target location
python -m venv .venv

# Linux / macOS:
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
# To also install Linux OS dependencies automatically:
# .venv/bin/python -m playwright install --with-deps chromium

# Windows:
# .venv\Scripts\python.exe -m pip install -r requirements.txt
# .venv\Scripts\python.exe -m playwright install chromium

# 2. Check the environment
.venv/bin/python scripts/paper.py doctor
```

Dependencies (`requirements.txt`): `playwright` (browser automation), `pypdf` (PDF validation), `fonttools` (font parsing).

## Configuration

Copy `config.example.json` to your own config file and point `data_dir` at a persistent directory. Keep persistent data (SQLite state, browser profiles, download records) outside the skill folder.

```sh
export PAPER_ACCESS_CONFIG=/srv/paper-access-config.json
export CSTCLOUD_USERNAME='your-account'
# CSTCLOUD_PASSWORD should be provided via your service's environment or secret injection
.venv/bin/python scripts/paper.py doctor
```

Key settings:

| Field | Description | Default |
|---|---|---|
| `data_dir` | Persistent data directory (profiles, cache, SQLite) | `~/.paper-access` |
| `timeout` | HTTP/page operation timeout (seconds) | `30` |
| `browser_timeout` | Total time limit for automated login (seconds) | `90` |
| `pdf_timeout` | Wait for a single PDF capture (seconds) | `60` |
| `delay_seconds` | Request spacing per host (seconds) | `15` |
| `max_bytes` | Per-file size cap | `80 MB` |
| `profiles` | Authentication profiles (method, institution names, credential env vars, steps, ...) | — |
| `publishers` | Domain-matched publisher settings (`pdf_selector`, ...) | 4 groups in the example config |

Key profile fields:

- `method`: `cstcloud` / `carsi` / `publisher` / `webvpn` / `ezproxy` / `api`
- `institution_names`: the names as they actually appear on the institution selector (put the most searchable first)
- `credential_hosts`: allowlist of hosts where credentials may be filled (exact host and subdomains)
- `username_env` / `password_env`: environment variable names holding the credentials
- `steps`: deterministic interactions (`click` / `fill` / `press`), each executed once
- `external_browser: true`: reuse the dedicated system browser started by `login-open` over CDP
- `proxy_url_template` (WebVPN/EZProxy): supports `{url}`, `{raw_url}`, `{host}`, `{host_dashed}`, `{path}`, `{query}`

WebVPN/EZProxy URL rewriting differs per gateway; this tool never guesses encryption schemes — fill in the actual URL template your gateway exposes for each publisher. See [references/configuration.md](references/configuration.md) for details.

## Usage

Every command prints one JSON object on stdout; diagnostics and progress events go to stderr.

```sh
# Single-paper fetch
python scripts/paper.py fetch --doi 10.1234/example --output /absolute/papers
python scripts/paper.py fetch --title "Exact paper title" --output /absolute/papers
python scripts/paper.py fetch --url https://publisher.example/article --output /absolute/papers

# With a config file and an institutional profile
python scripts/paper.py --config /absolute/config.json fetch --doi 10.1234/example --profile cstcloud

# Batch (UTF-8 text, one DOI per line; or CSV with doi/title columns)
python scripts/paper.py --config /absolute/config.json batch --input /absolute/dois.txt --profile cstcloud

# Run one explicit institutional login (add --headed to complete it in a visible window)
python scripts/paper.py --config /absolute/config.json auth --profile cstcloud --url https://publisher.example/article
python scripts/paper.py --config /absolute/config.json auth-status --profile cstcloud

# When the first login needs a normally launched browser: open a dedicated system browser
python scripts/paper.py --config /absolute/config.json login-open --profile cstcloud --url https://publisher.example/article
# Then set "external_browser": true on that profile and run fetch
```

Notes:

- Title search returns `candidates`; select a DOI supported by the requested title before fetching.
- `auth` reports observed authentication evidence, not full-text entitlement; `auth-status` reports stored state and the last probe without any network request.
- Only an identity-verified PDF counts as a successful `fetch`.
- Re-running revalidates cached files and resumes; publisher cooldowns persist across calls.

## Result Statuses

| Status | Meaning |
|---|---|
| `downloaded` / `cached` | Success. Returns `file`, metadata, identity evidence (doi/title), version, and a SHA-256 receipt. Note: an accepted manuscript is not necessarily the published version |
| `candidates` | Title-search candidates; select a DOI |
| `auth_required` | Authentication needed. Do not loop on the same login |
| `interaction_required` | A captcha / SMS / second-factor challenge needs a human |
| `not_entitled` | The publisher explicitly reports missing institutional access. A Key/SSO login alone proves no entitlement |
| `rate_limited` | Cooldown requested; honor `retry_after`. OA and other publishers may still work |
| `access_denied` / `network_error` | Access was blocked or navigation failed (possibly before SSO). Do not describe this as an expired login or missing subscription |
| `invalid_pdf` / `identity_unverified` | The file failed validation; never deliver it as a verified paper |
| `profile_busy` | Another command owns this data directory; retry later and do not delete the profile lock |
| `download_pending` | A native save was already submitted; inspect the existing save/reader record — repeated calls must not create a second publisher request |

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Success / candidates returned / status query |
| `2` | Retrieval failure or partial batch failure |
| `3` | Configuration or dependency error |

## Security Design

- Credentials enter only via environment variables and may only be filled into pages/iframes on allowlisted `credential_hosts`; at most one password submission per login URL per command.
- API requests carrying credentials never follow redirects, preventing credential forwarding to other hosts.
- Browser exception messages are uniformly sanitized; secrets, signed URLs, and passwords never reach stdout JSON or return values.
- Sessions and cookies live in `data_dir/profiles/<profile>/` and are sensitive authentication material: keep them private to the service account; never bundle them into the skill or commit them to a repository.
- External pages and PDFs are treated as source material, not instructions (their content is never executed).

## Tests

```sh
python -m unittest discover tests
```

The suite is offline: it runs a local HTTP fixture server and a real Chromium SSO round trip (institution selection → login → download → session reuse), covering PII binding and reference exclusion, single-navigation capture, GUID-bound resume, persisted cooldowns, credential non-leakage, process locks, and more. End-to-end acceptance against real publishers is reported separately.

## Limitations and Verification Scope

Version 0.2.1 passed 31 regression tests and a seven-paper download exercise on Windows: two JMPS papers, two PRL papers, and one each from Nature Physics, Nature, and Nature Communications. This exercise used public full text and an existing browser session; it does not validate every real-account authentication flow. Linux deployment and the native Edge save fallback still need testing on the target host. Interactive login requires a graphical or virtual display.

- The generic recognizers cover common institution entries and login forms, but cannot guarantee every publisher layout; deterministic `steps` can be added per page.
- PDF validation is not a page-by-page visual render and cannot prove the paper is complete; scanned PDFs without extractable identity text return `identity_unverified`.
- Whether unattended operation passes human challenges such as Cloudflare, actual subscription scope, and identity-source policies must be verified in the deployment environment.
- Client-side rate-limit parameters are local defaults, not publisher-promised quotas.
- Download-pipeline design notes and trade-offs: [references/download-design.md](references/download-design.md).

---

## License | 许可

本项目的原创代码、技能说明和文档采用 [MIT 许可证](LICENSE)。Copyright (c) 2026 JeffreyPeng。作者单位：CAS Imech。

Original code, skill instructions, and documentation in this project are licensed under the [MIT License](LICENSE). Copyright (c) 2026 JeffreyPeng. Author affiliation: CAS Imech.

依赖项和第三方内容保留各自的许可条款。下载取得的论文及其他材料不属于本项目的 MIT 授权范围。实现参考来源见 [references/download-design.md](references/download-design.md)。

Dependencies and third-party content retain their respective licenses. Papers and other materials retrieved by this tool are not licensed under this project's MIT License. Implementation references are recorded in [references/download-design.md](references/download-design.md).
