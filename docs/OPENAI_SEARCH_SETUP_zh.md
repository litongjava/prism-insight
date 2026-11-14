# OpenAI 搜索选项

项目的统一搜索提供方有 `perplexity`（默认）、`tavily` 和 `openai` 三种。
这是报告参考资料的数据源切换（新闻路线图 U1、R5/U4），不改变交易规则。

## 配置

在本机 `.env` 配置：

```dotenv
PRISM_WEB_SEARCH_PROVIDER=openai
OPENAI_API_KEY=<你的 OpenAI API Key>
OPENAI_WEB_SEARCH_MODEL=gpt-5.4
PRISM_MCP_PYTHON=<项目虚拟环境的 Python 绝对路径>
PRISM_REPO_ROOT=<项目根目录的绝对路径>
```

需要可访问 OpenAI Responses API 的密钥、可用额度和支持 `web_search` 的模型。
这里直接请求 `https://api.openai.com/v1/responses`；不自动复用 Codex CLI 的 ChatGPT 登录，
也不使用项目的 OAuth 代理。密钥只保存在本机配置，不能提交到 Git。
目前环境没有配置 API Key，因此没有执行真实付费搜索。

默认请求实时网络搜索，最多 2 次工具调用、3000 个输出 token、55 秒请求超时；失败不自动重试。
这只是单次调用上限，不是整个报告或每天的费用上限。

## 兼容范围

- 注册表继续使用 `perplexity` 这个历史名称，实际执行 OpenAI 提供方；原有代理的工具名称和提示词无需调整。
- `perplexity_ask` 返回搜索回答及真实工具回执中的来源网址。
- `perplexity_search` 返回来源列表供资料预取调用，随后仍由 Firecrawl 获取正文。OpenAI 的模型回答不会冒充网页原文。
- 美股同业比较的直接候选查询也遵循 `openai` 配置，其财务和市值校验仍使用原有 yfinance 路径。
- 切换提供方或 OpenAI 搜索模型会使用独立的资料预取缓存，回执记录所选提供方和模型。
- 未执行搜索、未完成响应、超时、无效密钥等作为错误处理。来源日期未提供时保留未知。
- 域名限制传给 `web_search`。新近程度筛选仅作为提示词偏好，不能保证与 Perplexity 的严格时间筛选完全等价；仍需从原文确认日期。
- OpenAI 搜索没有返回每页正文，因此 `max_tokens_per_page` 在兼容接口中不产生网页摘录。

Firecrawl 的实现、服务器地址和凭证要求没有修改。选择 OpenAI **不会自动部署 Firecrawl**，也不会将现有云服务自动变成免费服务。自部署需要另外准备服务并配置连接。

回退时设置 `PRISM_WEB_SEARCH_PROVIDER=perplexity` 或删除这个配置项即可；原 Perplexity 凭证仍需有效。已有自定义 `REPORT_MCP_CONFIG` / `PRISM_MCP_CONFIG` 也要包含 `search_providers.openai`，否则配置加载会明确报错。

静态 `deploy/codex-prism-config.toml` 及隔离 Codex profile 有独立的 MCP 定义，不经报告注册表；它们不会被这个开关自动重写。本次切换适用于项目报告注册表及上述直接同业查询。

## 验证

离线测试使用模拟 OpenAI 回执，覆盖来源去重、搜索必须实际执行、未知日期、失败处理、提供方切换、Firecrawl 保持不变、搜索→抓取→资料包及真实 stdio MCP 工具发现。

```bash
python -m pytest tests/test_openai_search_mcp_server.py tests/test_tavily_mcp_server.py cores/llm/tests/test_config.py tests/test_report_research_prefetch.py tests/test_us_peer_comparison.py tests/test_firecrawl_mcp_config.py -q
```

真实搜索、正式报告、部署和首次定时运行仍需分别验证。未配置密钥时不能将离线测试通过表述为服务已可用。

依据：[OpenAI 官方 Web Search 文档](https://developers.openai.com/api/docs/guides/tools-web-search)。Codex 的搜索与打开网页也是托管检索能力；本次未复制其私有接口或实现新的正文爬虫。
