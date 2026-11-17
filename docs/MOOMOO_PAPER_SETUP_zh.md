# PRISM moomoo 美股模拟交易接入

范围：美国 moomoo 账户，美股股票和 ETF，Linux，先模拟。
使用官方 REST OpenAPI + OAuth PKCE，无需 OpenD 或 moomoo SDK。
Python 3.10+；依赖沿用项目已有的 `httpx>=0.27.0`。

## 当前接入边界

- `trading/moomoo_paper.py` 提供异步模拟账户、资金、持仓、当日订单、限价下单、撤单接口。
- `tools/moomoo_paper.py` 是 PRISM 的独立接入入口。默认查询；下单/撤单默认预览。
- 只访问 `/api/v1.0/sim-trade/`，只接受账户列表中 `market_id=100`、
  `account_type=1` 的美股个人模拟账户。多个账户必须明确选择，空列表保持未知。
- 订单只支持股票/ETF 代码、整数股、明确限价和常规交易时段；不支持期权、其他市场或实盘。
- 接受订单不代表成交。提交超时或响应异常标记 `outcome_unknown`，不自动重试。
- KIS 的筛选、买卖规则、定时任务和资金配置不变；不写 KIS 持仓表，不发送 Telegram 或交易信号。
- **这是独立模拟接口接入，还未把 PRISM 自动选股/持仓管理接到 moomoo。**
  后续自动化需要 moomoo 独立账本、订单成交核对及完整退出链路；不能仅替换 KIS 工厂函数。

## Linux 安装与登录

使用独立虚拟环境，避免改动正在运行的 PRISM 依赖：

```bash
python3 -m venv ~/.local/share/prism-moomoo/venv
~/.local/share/prism-moomoo/venv/bin/python -m pip install 'httpx>=0.27.0'
~/.local/share/prism-moomoo/venv/bin/python tools/moomoo_paper.py login
```

也可以使用 `uv venv` 和 `uv pip install --python ...` 创建同样的环境。
命令打印 moomoo 官方授权链接，在浏览器中打开并本人登录、验证、确认权限。
初次模拟连接只勾选 **Market Data**，取消 Watchlists、Accounts & Orders、Trade Execution，
不选择实盘账户。链接请求 `quote:read`，但官方授权页可能保留其他默认勾选，需核对最终页面。
2026-11-17 已实测：仅 `quote:read` 的令牌可以读取本用户的美股模拟账户、资金、持仓和当日订单。
这不能证明所有账户或模拟下单均有同样权限；模拟写入仍需单独验证。
不得为了排查模拟写入失败而自动升级至实盘 `trade:write` 权限。

回调只监听 `127.0.0.1:60355`，20 分钟超时，不开放公网端口。
若浏览器与 Linux 不在同一台机器，先在浏览器所在电脑建立 SSH 转发：

```bash
ssh -N -L 60355:127.0.0.1:60355 YOUR_LINUX_SSH_ALIAS
```

再打开授权链接。不要把授权后的回调 URL、密码、验证码或令牌粘贴到聊天或日志。
凭据保存在 `~/.local/state/prism-moomoo/`，文件权限 `0600`，不进入 Git。
OAuth state 和 PKCE 校验后交换令牌，过期时刷新，刷新时跨进程互斥；
授权码兑换结果不明时重新登录，不重放原授权码。

## 只读验证

```bash
~/.local/share/prism-moomoo/venv/bin/python tools/moomoo_paper.py accounts
~/.local/share/prism-moomoo/venv/bin/python tools/moomoo_paper.py status
```

`status` 只显示模拟模式、账户尾号、持仓和订单数量。
`--details` 可在本机查看完整模拟资金、持仓和订单；不要转贴到公共日志。
如无美股模拟账户，先在 moomoo App 创建/启用美股模拟交易，再重新查询。
正式账户已开户不代表已启用模拟账户。

## 订单预览

```bash
~/.local/share/prism-moomoo/venv/bin/python tools/moomoo_paper.py order \
  --account-id PAPER_ACCOUNT_ID --symbol SPY --side BUY --quantity 1 --limit-price 1
```

这是演示参数，不是交易建议。默认只预览，不联网。仅在确实需要提交模拟订单时加 `--submit`。
撤单使用 `cancel --account-id ... --order-id ...`，同样默认预览，`--submit` 才提交。
订单和撤单完成后用 `status --details` 核对实际状态；不将响应成功当作成交。
发生 `outcome_unknown` 时先核对当日订单；跨交易日需在 moomoo App 中查看历史记录，
本入口暂不提供历史订单查询，不因当日列表为空就重新下单。

## 验证与状态

```bash
python -m pytest tests/test_moomoo_paper.py -q
```

测试使用模拟 HTTP 响应，覆盖模拟账户隔离、非法订单、未知提交、不重试、
错误响应、PKCE/state、凭据权限、刷新轮换和命令预览；CI 单独执行此检查。
本地测试通过不等于账户已连通；需用户授权后真实只读查询成功才能确认连接。
定时运行和自动交易尚未启用。

2026-11-17 开发环境验证：Python 3.11.2，34 项离线测试通过；官方 OAuth 注册 HTTP 201，
浏览器登录与令牌保存成功（文件权限 `0600`），仅授予 `quote:read`；真实模拟账户列表、
资金、持仓、当日订单查询通过。没有提交模拟订单，没有开启自动交易或观察到定时运行。
代码位于 `feat/moomoo-paper-api`，PR/CI 状态以 GitHub 为准。

## 实施前检查记录（2026-11-17）

1. 类型与基线：新增券商连接；既有美股接口为 `prism-us/trading/us_stock_trading.py` 的 KIS。
2. 假设与否定条件：无收益假设；模拟账户不可用、缺失响应或权限不符均停止，不推断成功。
3. 兼容性：现有 `AsyncUSTradingContext`/`MultiAccountUSTradingContext` 与账户账本绑定；
   因此新增独立异步入口，不替换筛选、买卖提示词、仓位、订单管理和退出调用链。
4. 比较与反例：离线模拟接口测试覆盖有效美股个人模拟账户、港股/竞赛/未知账户和异常响应；
   策略回测、收益比较、样本划分不适用。
5. 归因与停止：分别报告代码测试、官方注册、用户授权和真实只读查询；没有成交证据就保持未知。
6. 判定：开发环境模拟接口准备；没有 SHADOW/LIVE 晋升或自动交易授权。

官方参考：[授权流程](https://open.moomoo.com/api/overview/getting-started)、
[模拟账户](https://open.moomoo.com/api/sim-trade/account-list)、
[模拟下单](https://open.moomoo.com/api/sim-trade/input-order)、
[当日订单](https://open.moomoo.com/api/sim-trade/order-list)。
