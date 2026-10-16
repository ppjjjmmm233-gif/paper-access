# 配置与部署

此 skill 不依赖具体 agent 产品。运行环境需要 Python 3.10+、依赖及 Chromium；Windows、Linux、macOS 使用同一套命令。服务器无需桌面，默认 headless。首次安装 Linux 浏览器系统依赖可能需要管理员权限。

## 最小部署

将整个 `paper-access` 目录复制到服务器。在 skill 目录执行：

```sh
python -m venv .venv
# Linux/macOS:
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install --with-deps chromium
# Windows 使用 .venv/Scripts/python.exe
```

将 `config.example.json` 复制成自己的配置文件，设置 `data_dir` 为持久目录。Linux 示例：

```sh
export PAPER_ACCESS_CONFIG=/srv/paper-access-config.json
export CSTCLOUD_USERNAME='your-account'
# CSTCLOUD_PASSWORD 应通过服务的环境配置或密钥注入提供。
.venv/bin/python scripts/paper.py doctor
.venv/bin/python scripts/paper.py fetch --doi 10.1234/example --profile cstcloud
```

将 skill 文件夹交给 agent 的技能加载机制；若框架不自动读取 SKILL.md，在其工具说明中指定本文档入口和 CLI 的绝对路径即可。没有常驻服务、端口、agent SDK 或 MCP 依赖。命令的工作目录可以不同，配置中相对路径以配置文件所在目录为准，`--output` 相对路径以调用工作目录为准。

## 科技云认证

`cstcloud` 从当前出版社文章页开始，点击机构登录、选择科技云、进入真实身份源、填写账号密码、返回出版社，再获取原论文 PDF。默认不访问科技云门户。若出版社选择器有不同名称，在 `institution_names` 配置其页面实际名称，最适合搜索的名称放第一项。

通用识别器覆盖常见机构入口、搜索框和账号表单，但不能保证所有出版社页面布局均可识别。出现中间地区选择、额外确认或不同按钮时，增加确定性的 `steps`，不需要修改下载逻辑：

```json
{
  "method": "cstcloud",
  "institution_names": ["中国科技云通行证", "CSTCloud ID (CAS)"],
  "credential_hosts": ["passport.escience.cn"],
  "username_env": "CSTCLOUD_USERNAME",
  "password_env": "CSTCLOUD_PASSWORD",
  "institution_login_selector": "a.actual-institution-login",
  "institution_search_selector": "input.actual-institution-search",
  "username_selector": "input[name=userName]",
  "password_selector": "input[type=password]",
  "submit_selector": "button[type=submit]",
  "steps": [
    {"hosts": ["publisher.example"], "action": "click", "selector": "button.actual-region-choice"}
  ]
}
```

上述 `.actual-*` 是配置示意，必须替换成真实页面选择器，不是已验证的出版社接口。每个 step 只执行一次；支持 `click`、`fill`、`press`。`fill` 可设 `value` 或 `value_env`，秘密环境变量只能填入 `credential_hosts` 下的页面/iframe。`press` 用 `key` 指定按键，默认 Enter。`hosts` 精确匹配该域名及其子域名。多阶段表单按实际顺序增加步骤，并将域名限制为身份源。

可设置 `success_selector` 指向实际机构登录成功后的元素；它只提供认证观察证据。是否取得全文仍以 PDF 身份校验为准。`auth-status` 不发网络请求，只报告本地 profile 与上次观察。

默认在每个登录 URL 只提交一次密码，整个自动登录限时 `browser_timeout` 秒。遇到验证码、短信/动态码或二次认证返回 `interaction_required`。普通账号表单和仍有效的 SSO 会话可自动处理；持续无人值守能否成立需在真实身份源验证，不能通过延长 Cookie 本地时间替代远端授权。

首次会话可显式运行 `auth --headed`，在专用窗口手动完成官方认证。该模式不会因缺少密码环境变量或出现验证页立即关闭窗口，最多等待 `browser_timeout` 秒；可在首次配置时调大此值。后续默认无头运行。若出版社在无头环境返回 403，仍会如实报告访问受阻；人工登录不会自动解决浏览器模式或网络出口限制。

若首次验证需要正常启动的系统浏览器，可运行：

```sh
python scripts/paper.py --config /absolute/config.json login-open --profile cstcloud --url https://publisher.example/article
```

该命令正常启动专用 Chrome/Edge，返回 `awaiting_login`，等待用户在网站完成验证与登录。此阶段不接入浏览器自动化。确认登录后，为该 profile 配置 `"external_browser": true`，再执行原 `fetch`；程序通过本机连接复用这一专用窗口。`browser_channel` 可设 `msedge` 或 `chrome`，也可用 `browser_executable` 指定系统浏览器绝对路径。

系统浏览器窗口保持打开，用户可自行关闭。它使用独立的 `system-browser` 数据目录，不读取日常浏览器 profile；连接信息存在同一私有目录，调试入口仅用于本机。该可选模式需要图形环境，与默认无头服务模式分开；它不保证网站验证一定通过，也不自动解答验证码。

会话保存在 `data_dir/profiles/<profile>/`（Chromium profile 与 Cookie 快照）。目录包含敏感认证材料；由服务账号私有保存，不能打包进 skill 或提交仓库。操作系统权限负责静态文件保护，本实现不声称对 Cookie 做了加密。

## 其他认证

- CARSI：设置学校的 `institution_names` 和实际学校 IdP 的 `credential_hosts`，其余联邦流程共用。
- 出版社账号：`method=publisher`，通过 `steps` 点击真实账号登录入口，设置其凭据域名和表单选择器。已有专用浏览器会话可以直接复用。
- EZProxy：`proxy_url_template` 可为 `https://proxy.example/login?url={url}` 或 `https://{host_dashed}.proxy.example{path}?{query}`；仅使用你的机构提供的实际网关。
- WebVPN：设置 `entry_url` 和 `proxy_url_template`。不同 WebVPN 地址重写不同，某些网关按域名加密路径；本实现不自动猜测加密算法。可为某个出版社建独立 profile，填入该网关真实暴露的出版社 URL 模板。
- API：设置 `url_template`、普通 `headers`、秘密 `headers_env`（HTTP 头名映射到环境变量名）。例如 Elsevier 可按实际授权增加 `X-ELS-Insttoken`。API 端点必须直接返回 PDF；XML/JSON 全文不是 PDF，不自动伪装交付。携带 API 凭据的请求不跟随重定向。

URL 模板变量：`{url}` 为编码的原文章地址；`{raw_url}` 为原地址；`{host}`、`{host_dashed}`、`{path}`、`{query}` 来自原文章地址；`{doi}` 为可放入路径的 DOI。

## 下载、状态与失败

公开来源包括文章页 `citation_pdf_url`/实际主文 PDF 链接以及 OpenAlex 标记的 OA PDF。可通过 `openalex_api_key_env` 指向 API Key 环境变量。元数据暂不可用时仍可按 DOI 尝试出版社；标题搜索返回候选，不猜 DOI。显式接入的外部浏览器在已知 DOI 和文章网址时优先复用当前会话，减少不同 HTTP 客户端的探测。

`publishers` 以域名匹配，可配置 `pdf_selector` 指定实际 PDF 控件。ScienceDirect 使用本篇 PII 绑定 `pdfDownload`、正文 PDF 控件及分发域名，排除参考文献中的其他 PII。只选择一条主文路线，不把参考文献作为失败后的替代论文。

浏览器路径在导航前开启响应/下载事件监听，使用 Chromium 的响应阶段接口读取同一次实际 PDF 响应；内置阅读器尚未接管时取得完整字节，再原样交还浏览器。原生下载保存 GUID 与完成文件也可接续。不再串联页面 fetch、独立 HTTP、网络资源重取和多个参考文献路线。

每篇记录保存在 profile 的 `downloads/<论文哈希>/attempt.json`，保存提交状态、原生 GUID 或捕获文件，不保存带签名的临时 PDF 地址。同一记录只提交一次；后续调用优先本地文件，或只监听仍保留的本篇阅读器。PDF 页的人工验证与文章页认证分开；进度事件写 stderr，最终结果仍是 stdout 的单一 JSON。

PDF 校验包含文件标记、所有页面解析/文本提取、前两页 DOI/标题匹配，以及明确的补充材料标题排除。不会把 HTML、摘要网页或仅抽取到全文文字报告成 PDF 成功。此校验不等于逐页视觉渲染，也不能证明论文没有缺页；扫描件无可提取身份文字时返回 `identity_unverified`。每份文件附 SHA-256、页数和 JSON 回执。缓存复用重新校验内容与哈希；损坏文件重新尝试获取。

同一数据目录通过操作系统锁串行调度检索；并发调用返回 `profile_busy`。批量 DOI 去重、逐篇返回状态，单篇失败不停止整批；重复执行复用有效文件。`delay_seconds` 控制批次内请求间隔，429 的 `Retry-After` 按域名持久化。连续启动单篇命令的间隔也由数据目录内的调度记录约束。上述时间是客户端默认值，不是出版社承诺的额度。

SQLite 保存下载索引和冷却；PDF 原子落盘。取消或崩溃后已经交付的文件可以复用。输入列表是恢复依据，不需要常驻队列服务。

## 验证范围

项目包含本地 HTTP/浏览器模拟的自动认证与下载测试，模拟与真实出版社端到端验收分开报告。科技云、CARSI、各出版社的真实布局、订阅权限、服务器出口和二次认证策略，需要在部署环境完成实测。

下载链路的参考源码与具体取舍见 [download-design.md](download-design.md)。
