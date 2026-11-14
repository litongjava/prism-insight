# moomoo 美国账户接入

## 实施前审查（2026-11-14）

1. 类型与基线：券商执行适配，基线 `f344c923`。用户确认账户主体为 Moomoo Financial Inc.（美国）。不改变选股、买卖判断、止损、仓位或韩股规则。
2. 验收目标：美股交易入口可选择 moomoo，未配置时保持 KIS；moomoo 路径不依赖 KIS 凭证。收益改善不是本次假设。账户错配、提交被当作成交、超预算或重复下单均判失败。
3. 路径：US tracking 的账户选择 → `ExecutionService.us` → moomoo 适配器；Oneil 执行层的账户发现、持仓与订单核对也需要分流。筛选和报告仍使用原有数据来源；KIS 的可选补数能力不自动等价替换。
4. 验证：使用官方 SDK 接口形状的离线响应，通过真实执行服务验证模拟/实盘隔离、拒绝错误账户、数量预算、持仓不足、部分成交、撤单及超时后禁止重复提交。另跑现有执行服务与 Oneil 回归。未进行券商实际订单测试。
5. 故障区分：查询失败保留 UNKNOWN；订单接收与成交分开；不将 KIS 历史账户、订单号迁移到 moomoo。回退前先处理 moomoo 的挂单和持仓，不能将旧账户记录直接归到另一券商。
6. 当前结论：允许实现和离线验证。默认模拟环境；未授权切换实盘。实际账户连接、模拟订单、部署和首轮运行分别验证。`tasks/lessons.md` 在本克隆中不存在；已查研究经验，不据此开展策略实验。

## 状态

已实现可选执行适配器、账户隔离、订单核对和只读连接检查。尚未连接用户账户，未进行实际模拟下单、实盘切换或部署。离线验证不能替代 OpenD 的账户、行情权限及模拟交易验证。

## 配置步骤

1. 在项目使用的 Python 环境安装原有依赖和可选 SDK：

   ```bash
   python -m pip install -r requirements.txt -r requirements-moomoo.txt
   ```

2. 安装并登录官方 moomoo OpenD，使用美国 Moomoo Financial Inc. 账户。OpenD 监听本机 `127.0.0.1:11111`；远程机器通过 SSH 隧道接入本机端口。适配器拒绝直接连接非本机地址。

3. 将 `trading/config/moomoo.env.example` 中的配置合并到本机 `.env`，保留已有项目配置。填写 OpenAPI 的**美股股票模拟账户 `acc_id`**，以及你自行确定的每次买入金额上限。`acc_id` 来自官方 `get_acc_list()`，不是自行猜测的 App 展示账号；请在自己的终端查看，不要将账号列表、登录密码或交易密码发到聊天中。

   ```dotenv
   PRISM_US_BROKER=moomoo
   MOOMOO_TRADING_MODE=demo
   MOOMOO_ALLOW_LIVE=false
   MOOMOO_ACCOUNT_ID=<美股股票模拟账户的acc_id>
   MOOMOO_BUY_AMOUNT_USD=<自行确定的买入金额>
   ```

4. 运行只读检查：

   ```bash
   python tools/check_moomoo_connection.py --ticker AAPL
   ```

   检查账户主体/环境、美股权限、持仓、挂单和行情，输出脱敏状态及数量，不解锁交易、不下单、不发送 Telegram。`OK` 只证明这些查询成功，不证明行情实时权限或交易权限完整；`UNVERIFIED` 表示未确认，需要核对本机配置、OpenD 登录和权限。行情时间会原样输出，缺失仍为未知。

5. 模拟订单与首次定时运行必须另外验证。不要直接启用生产定时任务。实际交易仍需另行明确授权；代码不会自动解锁实盘。只改券商名称不会迁移原 KIS 的历史、挂单或持仓。

## 数据与支持范围

- `PRISM_US_BROKER` 省略时仍为 `kis`。选择 `moomoo` 后，美股 `ExecutionService.us`、tracking 账户、Oneil 持仓/订单核对、成交核对使用 moomoo；韩股保持现有实现。
- 本次支持一个明确指定的美国账户、整股多头、DAY/RTH 限价买卖、正常交易时段市价卖出、整笔剩余数量撤单及限价修改。分数股、做空、期权和多 moomoo 账户不在支持范围。
- MOO（开盘集合竞价）请求明确拒绝，不替换成其他订单。KIS 的本地延时订单队列在 moomoo 模式下不处理；盘外限价订单交给 OpenD/券商确认，是否接收以券商结果为准。
- 买入仍通过原有预算与行情校验；提交回执不是成交。查询失败、超时或无法匹配准确订单号均保留未知，禁止自动重试同一意图。成交费用目前为未知。
- 筛选和报告的 Yahoo Finance、SEC 等数据来源没有更换。无 KIS 凭证时，运行时行情需要显式设置 `PRISM_US_MARKET_DATA_SOURCES=yfinance`；KIS 可选补数关闭为 `US_SCREENING_KIS_SHORTLIST=false` 和 `US_SNAPSHOT_KIS_DAILY_FALLBACK=false`。缺失的 KIS 独有字段不会伪造。
- 独立的 `prism_inverse` KIS 适配器、直接调用 KIS 的示例和 KIS 专用资产推送不属于这次切换范围，不应作为 moomoo 任务启动。Stance 的 KIS 资产快照上报在 moomoo 入口未启用。
- 回退到 KIS 前，先在 moomoo 核实并处理已有挂单及持仓；账户键不同，不能把一家的订单号或历史改为另一家。

## 离线验证

以下两个命令必须在不同 pytest 进程运行，以免韩股与美股包名互相覆盖。测试使用假 SDK 响应，不连接券商、不发送消息。

2026-11-14 本地验证：190 项相关测试通过（分进程执行，修正测试导入及参数后补跑失败项）；包含已安装 `moomoo-api==10.12.7208` 的接口签名校验。连接检查 `--help` 和 `git diff --check` 通过。尚无真实 OpenD 连接、模拟成交或部署验证。

```bash
python -m pytest tests/test_moomoo_trading.py tests/test_execution_service.py tests/test_oneil_broker.py tests/test_fill_reconciliation.py -q
python -m pytest prism-us/tests/test_moomoo_broker_selection.py prism-us/tests/test_fill_chaser_us.py prism-us/tests/test_us_buy_runtime_safety.py -q
```

## 官方接口依据

- [获取账户列表](https://openapi.moomoo.com/moomoo-api-doc/en/trade/get-acc-list.html)
- [下单与订单参数](https://openapi.moomoo.com/moomoo-api-doc/en/trade/place-order.html)
- [查询订单](https://openapi.moomoo.com/moomoo-api-doc/en/trade/get-order-list.html)
- [查询历史订单](https://openapi.moomoo.com/moomoo-api-doc/en/trade/get-history-order-list.html)
