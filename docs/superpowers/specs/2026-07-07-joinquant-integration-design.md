# JoinQuant 模拟盘接入规格文档

> 归档说明：本文是早期设计规格，只保留为历史参考。当前项目规划的唯一主说明是 `docs/project_roadmap.md`。本文中关于“本地模拟盘并行运行”“本地 paper_trading 作为对照账户”“先 dry-run 再打开模拟盘”的描述已经废弃；当前主流程是本地生成信号，JoinQuant 模拟盘执行下单，并由 JoinQuant 回传账户、持仓和订单结果。

## 1. 背景

当前项目已经具备本地 A 股扫描、打分、风控建议、纸面模拟交易、企业微信通知和持仓 Web 回写能力。现有系统主要依赖 AkShare 获取行情和资讯，通过 `a_share_strategy.py` 生成候选股与交易建议，通过 `paper_trading.py` 在本地维护模拟账户。

本次接入 JoinQuant 的目标是把本地纸面模拟升级为“本地策略生成信号，JoinQuant 模拟盘执行交易，本地同步账户状态”的闭环。JoinQuant 不负责重新实现完整选股逻辑，而是作为模拟交易执行与账户状态来源。

## 2. 目标

1. 本地项目继续作为策略研究与信号生成中心。
2. JoinQuant 模拟盘作为交易执行中心。
3. 本地与 JoinQuant 通过稳定的 JSON 契约交换数据。
4. JoinQuant 执行结果回写本地，供现有 Web 持仓、风控和通知继续使用。
5. 第一阶段支持 dry-run，确认信号、下单计划、防重和同步流程稳定后，再打开 JoinQuant 模拟盘真实下单。

## 3. 非目标

1. 不在第一版迁移完整策略逻辑到 JoinQuant 平台。
2. 不接入真实券商实盘交易。
3. 不建设数据库、消息队列、多账户系统或复杂权限后台。
4. 不重构现有扫描、风控、通知和 Web 持仓模块。
5. 不保证 JoinQuant 与本地 AkShare 数据完全一致，只做必要的执行前校验。

## 4. 总体架构

```mermaid
flowchart LR
    A["本地 a_share_strategy.py"] --> B["JoinQuant 信号导出器"]
    B --> C["cache/joinquant/signals.json"]
    C --> D["本地信号服务 Flask API"]
    D --> E["JoinQuant 策略脚本"]
    E --> F["JoinQuant 模拟盘"]
    F --> G["账户快照回写 API"]
    G --> H["cache/joinquant/account_snapshot.json"]
    H --> I["本地账户同步器"]
    I --> J["cache/portfolio_web/positions.json"]
    J --> K["现有持仓 Web / 风控 / 通知"]
```

核心原则：

- 本地系统产出“交易意图”，不直接调用 JoinQuant 下单接口。
- JoinQuant 脚本根据交易意图做最终执行校验和下单。
- JoinQuant 回写账户快照，本地只信任快照作为模拟账户状态来源。
- 文件和接口都保留审计日志，便于排查误下单、漏下单和同步失败。

## 5. 模块设计

### 5.1 `joinquant_exporter.py`

职责：

- 从本地扫描结果 DataFrame 中筛选可交易信号。
- 把本地 6 位股票代码转换为 JoinQuant 代码。
- 导出 `cache/joinquant/signals.json`。
- 保留 `run_id`，用于 JoinQuant 防重复执行。

输入：

- `a_share_strategy.py` 生成的扫描结果 DataFrame。
- 当前配置中的最小分数、最大信号数量、允许买入/卖出开关。

输出：

- `cache/joinquant/signals.json`

### 5.2 `joinquant_signal_server.py`

职责：

- 使用 Flask 暴露只读信号接口。
- 接收 JoinQuant 收盘后的账户快照回写。
- 做 token 校验、请求日志和输入结构校验。

接口：

- `GET /joinquant/signals?token=<token>`：返回最新信号。
- `GET /joinquant/latest?token=<token>`：返回信号摘要和服务状态。
- `POST /joinquant/account_snapshot?token=<token>`：接收账户快照。

安全要求：

- 必须配置 `JOINQUANT_SYNC_TOKEN`。
- token 不允许写入代码仓库。
- 接口不提供任意文件读取能力。
- 回写 payload 必须做结构校验，避免污染本地持仓文件。

### 5.3 `joinquant_strategy.py`

职责：

- 作为模板文件，复制到 JoinQuant 策略平台运行。
- 定时从本地信号服务拉取交易信号。
- 执行交易前校验。
- dry-run 模式下只记录计划，不下单。
- 非 dry-run 模式下使用 JoinQuant API 下单。
- 收盘后回写账户快照到本地。

建议调度：

- `09:20`：拉取并缓存最新信号。
- `09:35`：执行第一轮信号。
- `10:30`：可选执行盘中信号。
- `13:30`：可选执行盘中信号。
- `15:05`：回写账户快照。

### 5.4 `joinquant_sync.py`

职责：

- 读取 `cache/joinquant/account_snapshot.json`。
- 转换为现有 `cache/portfolio_web/positions.json` 结构。
- 保留 JoinQuant 原始字段，便于 Web 展示和后续排错。
- 生成同步事件日志。

### 5.5 `config.py` 配置补充

新增环境变量建议：

```text
JOINQUANT_ENABLE=false
JOINQUANT_SYNC_TOKEN=
JOINQUANT_SIGNAL_FILE=cache/joinquant/signals.json
JOINQUANT_ACCOUNT_FILE=cache/joinquant/account_snapshot.json
JOINQUANT_MAX_SIGNAL_AGE_MIN=20
JOINQUANT_ALLOW_BUY=true
JOINQUANT_ALLOW_SELL=true
JOINQUANT_DRY_RUN=true
JOINQUANT_MIN_SCORE=75
JOINQUANT_MAX_POSITIONS=5
JOINQUANT_MAX_TOTAL_POSITION_PCT=80
JOINQUANT_REQUEST_TIMEOUT=8
```

默认值必须保守：不开启 JoinQuant、不真实下单、不过度扩大仓位。

## 6. 信号 JSON 契约

文件路径：

```text
cache/joinquant/signals.json
```

示例：

```json
{
  "schema_version": 1,
  "trade_date": "2026-07-07",
  "generated_at": "2026-07-07 09:20:00",
  "run_id": "20260707-092000",
  "source": "a_share_strategy",
  "dry_run": true,
  "signals": [
    {
      "id": "20260707-092000-000001-buy",
      "code": "000001",
      "jq_code": "000001.XSHE",
      "name": "平安银行",
      "action": "buy",
      "price": 11.25,
      "entry_price": 11.30,
      "stop_loss": 10.70,
      "take_profit": 12.50,
      "position_pct": 15.0,
      "final_score": 82.5,
      "signal_type": "short",
      "reason": "突破确认，风控通过"
    }
  ]
}
```

字段说明：

- `schema_version`：契约版本，第一版为 `1`。
- `trade_date`：信号所属交易日，格式 `YYYY-MM-DD`。
- `generated_at`：信号生成时间。
- `run_id`：本轮扫描唯一 ID。
- `dry_run`：建议 JoinQuant 是否只打印计划。
- `signals`：交易信号列表。
- `id`：单条信号唯一 ID，用于防重复执行。
- `code`：本地 6 位股票代码。
- `jq_code`：JoinQuant 股票代码。
- `action`：`buy` 或 `sell`。
- `position_pct`：目标仓位百分比，不是买入金额。
- `final_score`：本地最终评分。
- `signal_type`：本地风控分类，例如 `short` 或 `mid`。

## 7. 账户快照 JSON 契约

文件路径：

```text
cache/joinquant/account_snapshot.json
```

示例：

```json
{
  "schema_version": 1,
  "trade_date": "2026-07-07",
  "generated_at": "2026-07-07 15:05:00",
  "source": "joinquant",
  "cash": 52300.25,
  "total_value": 104800.50,
  "positions": [
    {
      "code": "000001",
      "jq_code": "000001.XSHE",
      "name": "平安银行",
      "qty": 3000,
      "avg_cost": 10.95,
      "price": 11.20,
      "market_value": 33600.00,
      "pnl": 750.00
    }
  ],
  "trades": [
    {
      "id": "20260707-093501-000001-buy",
      "datetime": "2026-07-07 09:35:01",
      "code": "000001",
      "jq_code": "000001.XSHE",
      "action": "buy",
      "price": 11.20,
      "qty": 3000,
      "amount": 33600.00,
      "status": "filled"
    }
  ]
}
```

## 8. JoinQuant 执行规则

JoinQuant 策略脚本执行信号前必须校验：

1. `schema_version == 1`。
2. `trade_date` 等于当前交易日。
3. `generated_at` 未超过 `JOINQUANT_MAX_SIGNAL_AGE_MIN`。
4. `final_score >= JOINQUANT_MIN_SCORE`。
5. `position_pct > 0` 且不超过单票仓位上限。
6. 总目标仓位不超过 `JOINQUANT_MAX_TOTAL_POSITION_PCT`。
7. 同一个 `id` 当天只执行一次。
8. 已持仓股票不重复买入，除非后续明确支持加仓。
9. 不在涨停价买入。
10. 本地服务不可访问或信号解析失败时，当天不交易。

建议 JoinQuant 使用：

- 买入：`order_target_percent(jq_code, position_pct / 100)`
- 卖出：`order_target(jq_code, 0)`

第一版不支持复杂加仓、减仓、条件单和撤单管理。

## 9. 本地模拟交易与 JoinQuant dry-run 推送口径

> 本节已废弃：当前不再并行运行本地模拟盘，也不再以 JoinQuant dry-run 作为主流程。当前主模拟盘只使用 JoinQuant 模拟盘真实模拟下单，执行结果以 JoinQuant 回传为准。

第一阶段保留现有 `paper_trading.py` 本地模拟交易能力，并与 JoinQuant dry-run 并行运行。两者都可以推送，但必须在标题、日志和报告中明确区分，避免把本地模拟成交误认为 JoinQuant 下单计划。

三层口径定义：

1. 本地信号：策略认为可以买入或卖出的候选交易意图。
2. 本地 paper_trading 模拟成交：按本地价格、滑点、手续费和仓位规则假设成交。
3. JoinQuant dry-run 下单计划：按 JoinQuant 账户、持仓和执行校验规则判断“如果打开交易会下什么单”。

推送标题建议：

```text
【本地模拟交易】
【JoinQuant Dry-Run】
【交易模拟对照复盘】
```

本地模拟交易推送内容应包含：

- 股票代码、名称、买卖方向、数量、价格。
- 本地模拟账户现金、总资产、持仓数量。
- 触发原因和信号类型。
- 明确标记账户来源为“本地 JSON 模拟账户”。

JoinQuant dry-run 推送内容应包含：

- JoinQuant 代码、名称、买卖方向、目标仓位。
- 执行前校验结果。
- 跳过原因，例如信号过期、已持仓、仓位超限、分数不足、涨停不可买。
- 明确标记状态为“dry-run，未真实下单”。
- 明确标记账户来源为“JoinQuant 模拟盘执行器”。

收盘复盘需要生成对照汇总，至少包括：

- 本地 paper_trading 买入、卖出、跳过数量。
- JoinQuant dry-run 计划买入、计划卖出、跳过数量。
- 两边一致的信号。
- 两边不一致的信号和差异原因。
- 当日最高分、平均分、过期信号数量、仓位限制跳过数量。

建议新增报告文件：

```text
output/joinquant_dry_run_YYYYMMDD.md
output/joinquant_dry_run_YYYYMMDD.csv
output/trading_comparison_YYYYMMDD.md
```

推荐第一阶段配置：

```text
PAPER_TRADE_ENABLE=true
JOINQUANT_ENABLE=true
JOINQUANT_DRY_RUN=true
```

当 JoinQuant 模拟盘真实下单稳定后，本地 `paper_trading.py` 可以降级为对照模拟账户。届时主账户状态以 JoinQuant 回写的 `account_snapshot.json` 和同步后的 `positions.json` 为准。

## 10. 模拟盘放行标准与灰度流程

JoinQuant dry-run 通过的标准不能只看程序是否报错，而应看连续交易日内信号、执行计划、风控、防重、回写和推送是否稳定。第一版建议至少 dry-run 连续运行 5 个交易日，满足放行检查后再打开 JoinQuant 模拟盘真实下单。

### 10.1 放行检查表

打开 JoinQuant 模拟盘真实下单前，应满足以下条件：

1. 信号生成稳定：每天能正常生成 `signals.json`，无空文件、损坏 JSON 或结构不兼容。
2. 信号时效正确：过期信号不会被 JoinQuant 执行，过期原因会被记录。
3. JoinQuant 拉取稳定：连续观察期内拉取成功率建议为 100%，最低不低于 95%。服务不可用时必须不交易。
4. 信号解释完整：每条本地可交易信号在 JoinQuant dry-run 中都有结果，要么计划执行，要么给出明确跳过原因。
5. 防重复有效：同一个 `signal_id` 多次拉取时只能生成一次执行计划，盘中多轮调度不能重复买同一只。
6. 仓位合规：单票仓位、总仓位、最大持仓数量都符合配置。
7. 跳过原因可解释：跳过原因必须落入已知分类，例如已持仓、分数不足、信号过期、仓位超限、涨停不可买、服务不可用。未知原因数量应为 0。
8. 本地模拟与 JoinQuant 差异可解释：两边不要求完全一致，但每个差异都要有明确原因。
9. 收盘快照回写稳定：观察期内每天收盘账户快照都能回写本地，并能同步到 `positions.json`。
10. 推送可读：企业微信推送能清楚说明计划买卖、跳过原因、dry-run 状态和当日汇总，且不会重复刷屏。
11. 人工复盘通过：观察期内没有发现明显不该买的票被高仓位计划买入，也没有发现卖出、止损或风控链路明显缺失。

### 10.2 自动 readiness 报告

建议新增 `joinquant_readiness_report.py`，每天收盘后基于 dry-run 审计日志生成放行检查报告。

建议输出文件：

```text
output/joinquant_readiness_YYYYMMDD.md
output/joinquant_readiness_YYYYMMDD.csv
```

报告至少包含：

- 观察交易日数量。
- 信号生成成功率。
- JoinQuant 拉取成功率。
- 过期信号误执行数量。
- 重复执行数量。
- 仓位违规数量。
- 未知跳过原因数量。
- 收盘快照回写成功率。
- 本地模拟与 JoinQuant dry-run 差异数量和差异原因。
- 最终结论：`暂不建议上模拟盘`、`可小仓位上模拟盘` 或 `可按目标仓位上模拟盘`。

示例结论：

```text
【JoinQuant 模拟盘放行检查】
观察天数：5
结论：可小仓位上模拟盘

信号生成成功率：5/5
JoinQuant 拉取成功率：100%
过期信号误执行：0
重复执行：0
仓位违规：0
未知跳过原因：0
收盘快照回写：5/5
本地模拟与 JoinQuant 差异：3 条，均可解释

建议：打开模拟盘真实下单，但初始总仓位上限控制在 30%。
```

### 10.3 灰度放量流程

dry-run 通过后，不直接恢复默认总仓位。建议按以下节奏灰度：

1. dry-run 连续 5 个交易日，通过 readiness 检查。
2. 打开 JoinQuant 模拟盘真实下单，总仓位上限设为 30%，运行 3 个交易日。
3. 若成交、回写、持仓识别和推送均稳定，总仓位上限提高到 50%，运行 5 个交易日。
4. 若继续稳定，再恢复策略默认总仓位上限，例如 80%。
5. 任一阶段出现重复执行、未知跳过、仓位违规、回写失败或明显误买，回退到 dry-run，修复后重新观察。

## 11. 防重复与审计

本地和 JoinQuant 两侧都需要记录执行状态。

本地建议文件：

```text
cache/joinquant/export_history.jsonl
cache/joinquant/account_snapshot_history.jsonl
```

JoinQuant 侧建议维护：

```python
g.executed_signal_ids = set()
```

JoinQuant 平台如果策略重启，应从持久化 storage 或当日成交记录恢复已执行 ID。第一版可以同时依赖信号 `id` 和当前持仓状态防止重复买入。

## 12. 错误处理

### 本地信号导出失败

- 不覆盖上一份可用信号。
- 写入错误日志。
- 接口返回最近信号时标记 `stale=true`。

### JoinQuant 拉取失败

- 不交易。
- 在 JoinQuant 日志中记录失败原因。
- 下一调度点重试。

### JoinQuant 回写失败

- 不影响当天模拟盘持仓。
- 下一调度点或次日继续尝试。
- 本地保留上一次成功快照，并显示快照时间。

### 本地同步失败

- 不覆盖 `positions.json`。
- 保留原始账户快照供排查。

## 13. 部署方案

### 本地或 Linux 服务器

推荐在现有 Linux 部署基础上新增一个服务：

```bash
python joinquant_signal_server.py --host 0.0.0.0 --port 8010
```

生产建议：

- 使用 systemd 管理。
- 使用 Nginx 反向代理 HTTPS。
- 配置强 token。
- 限制接口路径，只暴露 `/joinquant/*`。

### JoinQuant 平台

需要在 JoinQuant 策略脚本中配置：

```python
SIGNAL_URL = "部署后的 HTTPS 信号接口地址/joinquant/signals"
SNAPSHOT_URL = "部署后的 HTTPS 账户回写接口地址/joinquant/account_snapshot"
SYNC_TOKEN = "从环境或策略参数读取"
DRY_RUN = True
```

如果暂时没有公网服务器，第一阶段可以手动把 `signals.json` 内容复制进 JoinQuant 脚本变量中验证执行逻辑。

## 14. 测试计划

### 单元测试

新增测试：

- `tests/test_joinquant_exporter.py`
- `tests/test_joinquant_sync.py`
- `tests/test_joinquant_signal_server.py`
- `tests/test_joinquant_readiness_report.py`

覆盖场景：

1. 6 位股票代码转换为 JoinQuant 代码。
2. 低分信号不会导出。
3. 缺少价格或仓位的信号不会导出。
4. `signals.json` 结构符合契约。
5. 账户快照可转换为 `positions.json`。
6. 非法 token 被拒绝。
7. 过期信号被标记为 stale。
8. 损坏 JSON 不覆盖现有有效文件。
9. readiness 报告能识别重复执行、仓位违规、未知跳过原因和回写失败。

### 集成测试

1. 本地扫描生成信号文件。
2. Flask 服务返回最新信号。
3. 模拟 JoinQuant POST 账户快照。
4. 本地同步器更新持仓文件。
5. dry-run 审计日志生成 readiness 报告。
6. 现有测试全部通过。

### 试运行验收

第一阶段 dry-run 至少连续运行 5 个交易日：

- JoinQuant 能稳定拉取信号。
- dry-run 下单计划与本地报告一致。
- 同一信号不会重复执行。
- 收盘账户快照能回写本地。
- 本地 Web 持仓能显示 JoinQuant 持仓。
- readiness 报告结论至少达到“可小仓位上模拟盘”。

第二阶段打开 JoinQuant 模拟盘真实下单：

- 单票仓位、总仓位符合配置。
- 成交结果能回写。
- 次日本地扫描能识别已有持仓。
- 出现接口失败时不会误下单。

## 15. 分阶段实施计划

### 阶段 1：信号契约与导出

- 增加 JoinQuant 配置项。
- 新增 `joinquant_exporter.py`。
- 在 `a_share_strategy.py` 扫描结束后可选导出信号。
- 增加导出单元测试。

### 阶段 2：本地服务与账户回写

- 新增 `joinquant_signal_server.py`。
- 新增 `joinquant_sync.py`。
- 支持读取信号和接收账户快照。
- 增加接口和同步测试。

### 阶段 3：JoinQuant 策略模板

- 新增 `joinquant_strategy.py` 模板。
- 支持 dry-run。
- 支持基本买入/卖出。
- 支持收盘账户快照回写。
- 新增 `joinquant_readiness_report.py`，支持 dry-run 放行检查。

### 阶段 4：部署与试运行

- 补充部署文档。
- 在 Linux 环境启动信号服务。
- JoinQuant 侧配置 URL 和 token。
- dry-run 至少试运行 5 个交易日，并生成 readiness 报告。

### 阶段 5：模拟盘真实下单

- readiness 报告通过后，将 `JOINQUANT_DRY_RUN` 调整为 `false`。
- 初始总仓位上限控制在 30%，稳定后按灰度流程提高到 50% 和默认上限。
- 每日复核成交、持仓和本地同步结果。

## 16. 验收标准

第一版完成后应满足：

1. 本地扫描结果可以稳定生成 `cache/joinquant/signals.json`。
2. JoinQuant 策略能通过 HTTP 拉取信号。
3. dry-run 模式不会真实下单，但会打印完整下单计划。
4. 非 dry-run 模式可以在 JoinQuant 模拟盘按目标仓位下单。
5. JoinQuant 收盘账户快照可以回写本地。
6. 本地 `positions.json` 能反映 JoinQuant 持仓。
7. 防重复下单逻辑生效。
8. 接口失败、过期信号、非法 token、损坏 JSON 都不会导致误交易。
9. readiness 报告能输出是否建议打开模拟盘真实下单。
10. 现有测试不被破坏。

## 17. 风险与约束

1. JoinQuant 云端能否访问本地服务取决于公网部署和网络策略。
2. JoinQuant 与 AkShare 的行情字段、复权口径、停牌状态可能存在差异。
3. 模拟盘撮合不等同于真实实盘成交。
4. JoinQuant 策略运行环境对第三方库和网络访问可能有限制，需要在模板中保持依赖最少。
5. token 泄露会导致信号和账户快照接口暴露，必须用环境变量和 HTTPS。

## 18. 推荐结论

采用“HTTP 同步 + JSON 契约 + JoinQuant dry-run 执行器”的方案。

这是对现有项目侵入最小、可验证性最高、后续扩展最清晰的接入路径。未来如果要切换到 QMT、PTrade 或其他执行端，只需要替换执行器和账户同步适配层，本地扫描、风控、通知和 Web 展示可以继续复用。
