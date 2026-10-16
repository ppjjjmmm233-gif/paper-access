# 下载实现参考与取舍

本项目参考了用户提供的 CARSI Paper Access 本地源码与 [ScanSci PDF](https://github.com/Rimagination/scansci-pdf)，采用独立实现。

## 沿用的思路

- CARSI 的 `extension/publisher-flows.js`：正文 DOI/PII、PDF 入口与分发响应分别绑定，不能拿参考文献、附件或显示阅读器代替完成文件。
- CARSI 的 `scripts/devtools_sciencedirect_serial_fetch.py`：导航之前开启响应监听，从真实响应或浏览器下载取得文件；保留原文章上下文及实际 PDF 控件。
- CARSI 的 `scripts/browser_download.py`、`extension/native-pdf-save.js`：提交一次并记录下载编号，后续接续原记录；未知结果不能再次发起保存。
- ScanSci 的 [`browser_engine.py`](https://github.com/Rimagination/scansci-pdf/blob/main/src/scansci_pdf/browser_engine.py) 与 [`_publisher_strategies_core.py`](https://github.com/Rimagination/scansci-pdf/blob/main/src/scansci_pdf/_publisher_strategies_core.py)：复用浏览器 profile，优先从实际浏览器响应获得原始 PDF，并保留出版社专用适配。

## 本实现的约束

主文候选在请求前按 PII 筛选，而不是下载其他论文后才发现身份不符。普通 Edge 的内置 PDF 阅读器可能使事后读取响应失败，因此在响应阶段取得原始字节，避免为了保存而重新获取同一地址。

ScienceDirect 分发路径中的第一个 PII 可能属于整期，正文身份取后面的主文 PII。响应监听不限定 Document 类型，以覆盖真实 PDF 的响应类型。原生下载期间不在 Playwright 的下载事件中同步等待 `path()`，避免阻塞负责保存的 CDP 事件循环。

响应捕获未取得文件时，可绑定原 Edge 阅读器并提交一次原生保存；若浏览器明确报告网络中断，只能恢复与持久化 GUID 路径一致的原下载一次。临时签名地址会过期，不能认为保留阅读器就能永久重新下载。此次真实测试遇到过过期链接；确认旧下载中断后，单独诊断重开一次官方正文入口，取得完整文件。普通重复调用不会自动清除未知结果的检查点。

## 真实测试

2026-10-16，用户完成 PDF 入口验证后，从同一专用 Edge 会话下载 `10.1016/j.jmps.2026.106826`，23 页，8,487,772 字节，所有页面完成解析并抽取文本，正文 DOI 匹配。随后重复调用返回已验证缓存。本测试包含人工验证，不证明无人值守通过 Cloudflare，亦未在云服务器验证。

同日新增跨期刊 7 篇实测：JMPS 两篇、PRL 两篇、Nature Physics、Nature、Nature Communications 各一篇，均取得 DOI 匹配且所有页面可解析的文件。采用公开全文、公开元数据发现及现有专用浏览器接续，不代表新机构登录链已自动验证。此批次修正了 Nature 同域不同文章阅读器的身份绑定、短暂 403 后原导航成功响应的等待，并加入字体解析依赖。PRL 文件版本以每篇交付记录为准，不能把投稿版本自动标成正式版。

登录身份、文章访问、PDF 入口验证是不同状态。维持同一专用浏览器可减少重复验证，但无法承诺任何网站永远不要求人工验证。服务器返回的 429 与 Retry-After 仍然有效；ScienceDirect 官网和其分发域名共享冷却记录，不通过切换域名绕过它。

ScanSci 支持可选的 Patchright、CloakBrowser 等浏览器后端；其可用性与某个真实站点是否放行需要单独验证。当前已登录会话不因后端切换而被替换，也未宣称这些后端能够取消出版社的限流。
