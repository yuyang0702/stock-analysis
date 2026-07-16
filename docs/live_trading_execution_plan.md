# 实盘化执行方案

> 主文档：`docs/project_roadmap.md`。本文是实盘化与策略升级的可执行细化方案；如果状态、已实现能力或部署口径与主文档冲突，以主文档为准。

> 文档边界：本文负责阶段路线和真实资金前门槛；当前买入、卖出和风险规则以 `docs/superpowers/specs/2026-07-13-layered-exit-risk-management-design.md` 为基础，五项执行正确性 P0 以 `docs/superpowers/specs/2026-07-14-execution-contract-p0-fixes-design.md` 为最新增量，具体实施以对应计划为准。已归档的早期 JoinQuant、硬止损和实施计划不再定义当前流程。

> 2026-07-15 执行链增量以 `docs/superpowers/specs/2026-07-15-execution-timing-reconciliation-recovery-design.md` 为准：schema 7、开盘边界调度、逐信号时效、退出阶段对账、转换告警和受限自动恢复买入已随 `e2ce5b5` 推送并部署服务器。服务器备份、Linux 324/324 测试、schema 7 `ledger-check`、配置哈希和三个服务状态已核验；用户报告 JoinQuant 网站模板已手动更新，但新模板快照尚待交易日回传。当前为 `implemented（已推送） / deployed / not observed / not validated`。自动恢复仍只适用于 ERROR 对账自己实际造成的停买；CRITICAL 与任何人工控制均要求人工恢复。

> 2026-07-15 成交对账修复当前为 `implemented（已推送） / deployed（服务器） / not observed / not validated`：完整模式只对当前快照交易日做成交存在性比较，不改变 SQLite 历史账本、交易策略、控制严重度或自动恢复门槛。代码提交 `cd83f26` 的 Linux全量326/326、正式账本检查、配置不变、三个服务重启和启动后ERROR日志均已通过；真实交易日行为仍待观察。

> 2026-07-16 止损与网页增量以 `docs/superpowers/specs/2026-07-16-unified-effective-stop-trading-dashboard-design.md` 为准。唯一卖出止损改为成交校验后的冻结 initial、可选 manual 与首段止盈后 trailing 的最大值；网页与策略共用解析器。提交 `8db92bf`、schema 8 和认证交易面板已部署服务器，Linux全量414/414、环境哈希、三个服务、同步和认证门均通过；用户确认 JoinQuant 网站已手工更新 bearer 模板 `2026-07-16.1-unified-effective-stop`。当前为 `implemented（已推送） / deployed（服务器；网站由用户确认） / not observed / not validated`。T+1、停牌、跌停和可卖数量不足仍保留退出意图，停买不阻止合法卖出；部署和非交易时段同步不构成交易日观察。

## 目标

把当前项目从“能扫描、能模拟下单”升级为“可长期稳定运行、具备风控、回测、审计和实盘接入能力的股票量化工具”。

当前主流程仍然是：

```text
本地服务器扫描和生成信号 -> JoinQuant 模拟盘执行 -> 回传账户/持仓/订单 -> 微信通知和本地复盘
```

实盘化前必须先完成：

```text
连续模拟盘观察
完整历史回测
实盘级风控
交易适配层
审计和异常报警
小资金灰度
```

## 总体原则

- 先模拟，后实盘：JoinQuant 模拟盘至少连续稳定运行 2-4 周。
- 先风控，后收益：任何实盘下单前必须通过统一的 `pre_trade_check`。
- 先可解释，后自动化：每一笔买卖必须能追溯信号、原因、价格、风控检查和执行回报。
- 先小资金，后放大：实盘初期只允许极小仓位，连续观察达标后再扩大权限。
- 保留一键停止：任何实盘阶段都必须支持 `KILL_SWITCH=1`。

## 阶段 1：模拟盘稳定性

目标：确认 JoinQuant 模拟盘链路能在真实交易日连续稳定运行。

当前状态：阶段 1 的代码功能已补齐。服务器已具备 JoinQuant 通信、API 访问日志、健康检查、交易时段告警门槛、异常微信报警、失败推送重试、失败原因拆分、持仓一致性检查、网站模板版本自检、稳定性评分和统一 run 脚本定时器。

已实现：

```text
1. 每 5 分钟健康检查定时器
2. 信号文件缺失/异常/超时检查
3. 账户快照缺失/异常/超时检查
4. 今日信号拉取、latest 访问、快照回传和 API 异常统计
5. 买入/卖出失败、拒单、取消、跳过原因细分统计
6. 订单回报和本地 JoinQuant 持仓一致性检查
7. 企业微信异常去重报警
8. 企业微信失败推送重试队列和定时器
9. Markdown 健康日报和稳定性评分
10. health_history.jsonl 连续观察记录
11. 09:30-11:30、13:00-15:00 连续竞价口径，09:29 不再当成可下单盘中
12. 非交易时段信号/快照 stale 只记报告，不反复推微信异常
13. JoinQuant 网站模板版本回传和服务器期望版本对比，防止网站仍运行旧模板
```

仍需做的是线上验收观察，不是代码功能补充：

```text
1. 连续 10 个交易日观察健康报告是否无 critical
2. 确认 JoinQuant 网站委托、服务器快照、本地持仓展示一致
3. 确认微信异常报警和失败重试能在真实网络环境中正常工作
```

验收标准：

```text
连续 10 个交易日无服务中断
信号拉取成功率 >= 99%
账户快照每日稳定回传
订单失败原因能在报告中统计
微信异常报警能及时收到
```

## 阶段 2：完整历史回测

目标：回答“过去 6 个月或 1 年按当前策略逐日运行，收益、回撤和胜率如何”。

当前状态：第一版信号级回测继续保留；独立逐日历史回测框架已在 `origin/main` 中 `implemented（已推送）`，并包含在服务器当前 `52b3653` 中。没有真实 6 个月/1 年严格数据导入和运行，因此仍为 `not observed / not validated`。

本地框架已补充：

```text
1. 独立历史库与 JoinQuant/AkShare CSV 导入映射（不联网下载）
2. A 股历史交易日循环
3. strict 时点特征候选重建与 price_core 代理候选
4. 历史信号生成
5. 历史撮合器
6. 任意显式日期区间报告（6 个月/1 年需真实数据）
7. 参数对比回测
8. 市场环境分组统计
```

实现采用 `strict` 与 `price_core` 双轨。只有 strict 数据完整通过时点、股票池、状态和特征质量门，才可能形成完整历史回测证据；price_core 永久标记 `proxy_only=true`，不能通过 Batch G。真实历史数据导入、至少三段 walk-forward 重复运行和人工复算仍是 observed/validated 前置。

撮合规则至少支持：

```text
手续费
印花税
T+1
涨停不可买
跌停不可卖
停牌不可交易
100 股整数手
单票仓位上限
总仓位上限
T 日收盘决策、T+1 开盘成交
```

验收标准：

```text
能输出净值曲线
能输出逐笔交易明细
能输出收益、最大回撤、胜率、盈亏比
能按策略模式、市场环境、题材热度、分数区间分组统计
能对比至少 2 组参数效果
```

## 阶段 3：实盘级风控

目标：让系统具备“拒绝危险交易”的能力。

必须补充统一下单前检查：

```text
pre_trade_check(signal, account, market, risk_state)
```

硬风控规则：

```text
1. 单票最大仓位
2. 总仓位最大比例
3. 单日最大买入金额
4. 单日最大亏损停止交易
5. 连续亏损暂停交易
6. 黑名单股票
7. ST / *ST / 退市整理过滤
8. 停牌过滤
9. 一字涨停过滤
10. 大盘弱势自动降仓
11. 异常行情保护
12. 下单前二次确认价格和涨跌停状态
13. `KILL_SWITCH=1` 时禁止买入
```

建议状态：

```text
NORMAL：正常交易
CAUTION：降低仓位，只买高分票
RISK_OFF：禁止新开仓，只允许卖出
KILL：停止自动交易
```

验收标准：

```text
每笔买入都记录风控检查结果
任何一项硬风控不通过都拒绝下单并推送原因
KILL_SWITCH 打开后不会产生任何买入订单
弱势市场能自动降仓或停止买入
```

## 阶段 4：交易适配层

目标：避免策略逻辑和具体交易平台绑定死。

建议抽象：

```text
TradingAdapter
  - fetch_account()
  - fetch_positions()
  - place_order()
  - cancel_order()
  - fetch_orders()
  - fetch_trades()
```

适配器路线：

```text
1. JoinQuantAdapter：当前模拟盘适配器
2. PaperAdapter：仅用于测试，不恢复为主流程
3. BrokerAdapter：后续实盘券商适配器
```

可选实盘通道：

```text
QMT / miniQMT
掘金量化
vn.py + 券商接口
JoinQuant 实盘服务（如果确认可用）
```

验收标准：

```text
策略层只生成信号和目标仓位
所有下单统一经过 pre_trade_check
交易平台差异只存在于 adapter 内
模拟盘和实盘能复用同一套信号、风控和审计逻辑
```

## 阶段 5：运维和审计

目标：实盘后能知道每一笔交易为什么发生、是否按规则执行、哪里失败。

必须记录：

```text
1. signal_id
2. 信号生成时间
3. 信号来源和策略模式
4. 入场价、止损、止盈、仓位
5. 下单前风控检查结果
6. 下单请求
7. 交易平台返回
8. 成交回报
9. 持仓变化
10. 每日资产快照
11. 异常报警
```

必须支持：

```text
日报
周报
异常告警
订单失败原因统计
持仓和账户一致性检查
一键停止交易
```

验收标准：

```text
任意一笔订单都能从成交记录追溯到原始信号
每日能生成资产、持仓、订单、异常汇总
微信能推送关键异常
服务重启后不会丢失重要状态
```

## 策略升级方向

策略升级目标：减少弱市亏损，保护盈利，让买卖行为更适合真实市场。

优先补充以下 6 类能力。

### 1. 市场环境过滤

先判断今天适不适合买，再决定是否找票。

建议指标：

```text
沪深300 / 中证500 / 创业板是否在 20 日线之上
上涨家数和下跌家数
涨停家数和跌停家数
两市成交额
指数大跌和跌停扩散
```

策略状态：

```text
强势市场：正常开仓
震荡市场：仓位减半，只买高分票
弱势市场：禁止新开仓，只处理卖出
极端风险：只卖不买或清仓
```

### 2. 买点确认

当前已有“当前价达到入场价才买”，后续继续细化：

```text
放量突破确认
回踩不破确认
开盘冲高不追
高开过多不追
临近涨停不追
分时均价线之上才买
开盘前 5-10 分钟不买
尾盘最后 5 分钟不追新仓
```

### 3. 分层卖出

#### 3.1 2026-07-13 分层退出本地实现补充

- `implemented`：全持仓硬止损不再依赖候选池；持仓周期冻结初始止损与 R；支持 +2R 目标半仓、移动止盈、交易日时间止损；弱市减仓、风险释放禁止新买；卖出始终优先于买入。
- `implemented`：SQLite schema version 6，覆盖持仓周期、正式订单、不可变逐笔成交、账户/持仓检查点、日权益、自动对账、控制审计、退出意图和再买冷却；包含重复/乱序回放、部分成交、压缩快照、自动停买/熔断和人工恢复门槛测试。
- `implemented（已推送基线）`：信号 schema version 1 的兼容扩展 `target_qty`，基线 JoinQuant 模板版本 `2026-07-14.1-ledger-v6`，同证券未完成委托、可卖数量和T+1保护，并回传订单、`get_trades()`逐笔成交、真实成交换手、日内盈亏、账户回撤和连续亏损交易日。
- `implemented（已推送） / deployed（服务器与 JoinQuant 模板） / not observed / not validated`：提交 `52b3653` 的模板版本 `2026-07-14.2-p0-execution-contract` 已同步；包含强制版本化买入执行契约、退出意图续执行、5只/80%双层上限、独立买卖开关，以及已有持仓和未完成买单的行业/主题暴露。服务器专项测试 123/123、Python 编译和 `ledger-check` 通过，但尚无真实交易日观察与验收证据。
- `implemented`：执行回报由 SQLite 首次入账的新 fill 驱动；无 trades 旧快照仅在累计成交增加时报告。相同成交的周期快照不再反复推送，部分成交增量仍逐次报告。
- `implemented`：统一企业微信出口增加实际服务器发送时间；盘后按 D+0/D+1/D+3/D+5/D+10 交易日完整复盘成功推送买点，使用全量行情、最多6只分片并显式保留行情缺失样本。
- `deployed（服务器与 JoinQuant 模板） / not observed / not validated`：服务器代码、数据库 migration 和 JoinQuant 网站模板已同步到 `52b3653` / `2026-07-14.2-p0-execution-contract`；尚不能声称模拟盘已在真实交易日执行并验证新规则。
- 本次部署已按顺序完成：备份服务器 SQLite，拉取代码，运行专项测试、Python 编译与 `ledger-check` 确认 schema version 6，更新 JoinQuant 网站模板，再重启并核验三个核心服务。后续任何新部署和重启仍需用户单独授权。
- `implemented（已推送） / deployed（服务器代码） / not observed / not validated`：独立 `trading_backup.py` 已实现每日在线备份、SHA-256/完整性/schema/核心计数校验、7/4/12 轮转、隔离季度恢复演练、状态报告、失败告警和 Linux timer 模板；本次人工备份成功。timer 是否安装及其连续运行仍需只读核验。

计划要求的可卖/冻结数量、平台委托状态与部分成交对账、退出意图、旧持仓迁移报告、可用现金与组合风险、未完成买单风险占用、按评分排序分配容量、行业25%/题材20%/无分类单票10%限制、连续亏损交易日冻结、ST/停牌/特殊上市阶段/流动性/追价/陈旧行情过滤、JoinQuant下单前当前价和现金复核、卖出异常价保护、市场状态确认滞后及退出原因冷却现已在 `origin/main` 中 `implemented（已推送）`。组合风险、可交易性、状态确认、退出冷却和分层退出均有独立环境开关。详细接口、门槛与观察分栏以分层退出实施计划的Batch A–G为准。
卖出要按场景拆分：

当前实现边界：退出规则、可卖数量、成交状态、旧持仓安全迁移、真实组合风险、可交易性过滤、状态滞后与冷却均已在 `origin/main` 实现，并已同步服务器与 JoinQuant 模板；当前为 `deployed / not observed / not validated`。只有积累真实交易日证据并完成验收后才能继续提升状态。

```text
硬止损：跌破止损价必须卖
移动止盈：盈利后抬高保护位
分批止盈：到第一目标卖一半，第二目标清仓
时间止损：买入后 N 天不涨就卖
破趋势卖出：跌破 5 日/10 日线卖出
市场风险卖出：大盘转弱时主动降仓
```

执行原则：

```text
亏损靠硬止损
盈利靠移动止盈
横盘靠时间止损
系统性风险靠总仓位控制
```

### 4. 动态仓位

仓位不应只看固定 `position_pct`，应根据环境和风险调整。

当前实现：

```text
shadow_score.py 已生成影子增强分 enhanced_score、shadow_adjust_score、original_rank、shadow_rank、shadow_rank_change 和 shadow_reason，影子评分会额外观察消息催化、题材热度、实时板块位置、市场情绪、交易质量和海外风险；影子分不是百分制，允许超过 100。
a_share_strategy.py 的板块行情改为独立低频刷新：a_share_strategy.py --sector-context-only 或 bash run_ubuntu.sh sector-context 会通过 AkShare 东方财富行业/概念板块行情刷新 cache/market/sector_context.json；日常扫描只读取该缓存，不在每轮扫描时主动请求板块接口。刷新失败时优先保留最近成功缓存，没有缓存才按板块中性处理并在依据里提示失败原因。
global_market_context.py 会通过 AkShare 东方财富主源抓取美股、日本、韩国主要指数并写入 cache/market/global_context.json；主源失败时切到 Sina 备用源，备用源也失败时优先复用 24 小时内最近一次成功缓存，仍不可用才按海外风险中性处理。
日常微信扫描汇总、单票提醒和 JoinQuant 下单计划会显示原策略分、影子分、影子调整和原排名到影子排名的变化；影子分标注为仅观察，不参与下单。
JoinQuant 执行保护已补齐：买入信号导出前检查是否至少够买 100 股，不足一手时记录 buy_too_small_for_board_lot 并过滤；订单状态在账户快照回传前统一转成字符串，避免 OrderStatus 无法 JSON 序列化；普通 skipped 订单仍保留在失败原因明细中，但不再计入硬失败阈值；设置 JOINQUANT_ENFORCE_HEALTH_GATE=1 后，健康准入不通过会停止新买单导出，但已有持仓的卖出、止损和止盈仍允许。
JoinQuant 网站模板在交易时间每次 handle_data 后都会回传账户快照，成交后的现金、总资产和持仓通常下一分钟即可更新；服务器 stock-joinquant-sync.timer 每 60 秒同步到本地。周期快照和完整订单事件仍会落盘并同步；企业微信执行回报只由 SQLite 新 fill 或 legacy 累计成交增量触发，零成交、失败、跳过和未变化的历史成交不推送。
strategy_compare_report.py 每天 15:35 生成 output/strategy_compare_report.md，每周五 15:45 推送策略对照微信摘要。
现阶段不改变 final_score 排序、不改变 JoinQuant 下单、不改变 position_pct。
```

建议公式：

```text
最终仓位 = 基础仓位 × 市场系数 × 分数系数 × 波动系数 × 回撤系数
```

调整方向：

```text
策略分数越高，仓位越高
市场越强，仓位越高
波动越大，仓位越低
离止损越远，仓位越低
同题材已有持仓，降低新仓
连续亏损后，自动降仓
```

影子评分验收口径：

```text
连续观察至少 20 个交易日
比较原策略 Top 5 和影子评分 Top 5 的 D+3/D+5 平均收益
比较高影子分和低影子分的止损率、最大回撤和失败订单比例
当前规则型影子评分只作为对照，不等于已训练机器学习模型
未来训练型模型必须记录模型/代码/特征/样本版本并隔离验证集和保留集
只有训练型影子模型连续优于原策略并经用户人工批准，才可在另行授权的任务中进入下单过滤或仓位调整
```

机器学习模型与 Batch G 参数复核分开治理：ML-7 管理版本化训练模型和 `ml_score`，Batch G 管理确定性策略参数。二者都不得自动批准、自动发布、越过硬风控或直接进入真实资金；任何活动信号需要同时可追溯代码版本、模型版本和参数版本。

ML-7 已确认专项设计但仍为 `planned`，见 `docs/superpowers/specs/2026-07-15-trained-shadow-model-design.md`。目标形态是每5分钟保存完整约30只候选批次，以一年 strict 历史数据训练多个小型收益/风险/成交模型，JoinQuant 模拟盘只做现实校准和影子观察。第一阶段 L0 不改变任何交易；之后仍需分别达到至少20/40/60个有效交易日并经人工批准，才可进入 L1排序、L2只减不增的买入过滤和 L3 `0.8–1.1` 仓位微调。卖出、硬止损和绝对风险边界永久由规则控制。

### 5. 股票池风险过滤

实盘前必须过滤高风险标的。

建议过滤：

```text
ST / *ST
退市整理
上市不足 60 天新股
成交额太低
价格太低
一字涨停
连续缩量
频繁炸板
大股东减持
监管问询
重大风险公告
```

### 6. 策略复盘统计

每次交易都要能回答：

```text
为什么买
买点是否合理
买后最大浮盈多少
是否到过止盈
是否触发止损
如果不买会怎样
这个题材/模式胜率如何
```

统计维度：

```text
按策略模式统计
按市场环境统计
按题材热度统计
按分数区间统计
按买点类型统计
按持仓天数统计
```

### 7. 半自动参数复核与发布（Batch G，planned）

本能力尚未实现、部署、观察或验证。现有 `ml_dataset.py`、`strategy_compare_report.py` 和 `backtest_engine.py` 只提供数据与第一版对照基础，不具备自动学习或自调参能力。

目标流程：

```text
冻结参数基线
→ 满足数据就绪门
→ 自动生成最多5个有界候选
→ 按时间切分做walk-forward和保留集评价
→ 输出通过/失败原因
→ 用户按候选ID和SHA-256批准或拒绝
→ 用户另行授权后发布到JoinQuant模拟盘
→ 首日逐笔核对、3日执行安全、20日策略观察
→ 验收或回滚到父版本
```

20 个有效交易日只是开始复核的最低条件，还需至少 30 个闭合持仓周期和每个拟调整参数族至少 30 个相关样本。进入可批准列表必须有通过 strict 质量门的完整历史回测、至少 3 个 walk-forward 窗口并配合 20 个有效模拟盘交易日；缺少可用的完整历史回测证据时，替代门槛为至少 60 个有效模拟盘交易日。样本不足只生成报告。

首版每个候选只允许改变一个参数族，并必须同时检查扣费净收益、Profit Factor、最大回撤、尾部损失、滑点、换手、执行失败、分层退化和去除最高 3 笔收益后的稳定性。硬止损存在性、止损不得下移、卖出安全、T+1/停牌/涨跌停/可卖数量、绝对仓位与开放风险上限、幂等和账本一致性不属于可调范围。

自动任务最多把候选推进到 `approvable`，不得批准、激活、修改配置、部署或重启。批准不等于 `deployed`；服务器与 JoinQuant 同步后才是 `deployed`，有效交易日产生证据后才是 `observed`，达到门槛并人工确认后才是 `validated`。详细设计与实施任务见：

- `docs/superpowers/specs/2026-07-14-semi-automatic-parameter-review-design.md`
- `docs/superpowers/plans/2026-07-14-semi-automatic-parameter-review.md`

## 推荐执行顺序

```text
1. 模拟盘健康检查和异常报警
2. 市场环境过滤
3. 分层卖出：硬止损 + 移动止盈 + 时间止损
4. 动态仓位
5. strict 历史数据导入、walk-forward 运行与人工复算
6. ML-7 五分钟 strict 数据契约、训练型 L0 影子模型和真实模拟盘观察
7. 半自动参数复核只读报告
8. 人工批准与模拟盘版本化发布
9. 实盘级 pre_trade_check
10. 交易适配层
11. 审计日志和日报
12. 小资金实盘灰度
13. 达标后扩大资金和权限
```

## 实盘前门槛

进入真实下单前，至少满足：

```text
JoinQuant 模拟盘连续稳定 2-4 周
完整历史回测覆盖 6 个月或 1 年
最大回撤在可接受范围内
弱市过滤和总仓位控制已启用
KILL_SWITCH 已实现并验证
每笔订单可追溯
异常报警可用
小资金灰度方案明确
```

最重要的两条策略原则：

```text
弱市不买，是减少亏损的第一优先级。
盈利保护，是策略能长期活下来的第一优先级。
```
## 2026-07-09 阶段 1 实现更新

阶段 1 已从“第一版健康检查”升级为“模拟盘稳定性闭环”：

- API 访问审计：`joinquant_signal_server.py` 写入 `cache/joinquant/api_events.jsonl`，记录 `/joinquant/signals`、`/joinquant/latest`、`/joinquant/account_snapshot` 的成功和异常访问。
- 每日稳定性报告：`joinquant_health.py` 输出 `output/joinquant_health_YYYYMMDD.md`，包含拉取次数、回传次数、API 异常、信号/快照年龄、订单失败原因、持仓一致性和稳定性评分。
- 连续观察数据：`joinquant_health.py` 写入 `cache/joinquant/health_history.jsonl`，后续可以用来判断连续交易日是否达标。
- 失败原因归因：报告会按 `buy:reason`、`sell:reason` 聚合失败、拒单、取消和跳过原因，便于区分涨停、停牌、T+1、余额不足或风控限制。
- 持仓一致性：报告会比较 JoinQuant 最新快照和本地同步后的 `cache/portfolio_web/positions.json`，发现数量或代码不一致会标记 critical。
- 模板版本自检：`joinquant_strategy.py` 回传 `strategy_template_version`，`joinquant_health.py` 对比 `JOINQUANT_TEMPLATE_VERSION`，不一致时标记 `template_version_mismatch` 并提示 JoinQuant 网站模板未更新。
- 影子评分第一版：`shadow_score.py` 基于原策略分、消息催化、题材热度、板块位置缓存、市场情绪、交易质量和海外风险生成 `enhanced_score`、`shadow_adjust_score`、`original_rank`、`shadow_rank`、`shadow_rank_change` 和 `shadow_reason`；`enhanced_score = final_score + shadow_adjust_score`，不再按 100 封顶；`a_share_strategy.py --sector-context-only` / `bash run_ubuntu.sh sector-context` 低频刷新 `cache/market/sector_context.json`，扫描只读缓存并补充 `sector_pct_chg`、`sector_rank_pct`、`sector_hot_level` 和 `sector_position_reason`，没有缓存时才按板块中性处理并提示原因；`global_market_context.py` 通过 AkShare 东方财富主源抓取美股、日本、韩国主要指数并写入 `cache/market/global_context.json`，主源失败时切到 Sina 备用源，备用源也失败时优先复用 24 小时内最近一次成功缓存，仍不可用才按中性处理；`a_share_strategy.py` 在日常微信和 JoinQuant 下单计划中展示原分、影子分、调整和排名变化；`strategy_compare_report.py` 每天盘后生成原策略 vs 影子评分对照报告、每周五推送周报摘要；JoinQuant 下单仍使用原 `final_score` 和原仓位。
- 通知兜底：`notifier.py` 会给每次实际发送增加服务器时间，并把发送失败的原始业务正文写入 `cache/notify_failed_queue.jsonl`；`notify_retry.py` 和 `stock-notify-retry.timer` 每 5 分钟重试，重试时显示新的实际发送时间。
- 统一运维入口：`run_ubuntu.sh` 新增 `notify-retry`、`global-context`、`sector-context`、`strategy-compare` 和 `strategy-compare-weekly` 命令；`install` 会统一安装健康检查、持仓同步、微信重试、readiness、ML 复盘、海外上下文、板块行情缓存和策略对照等 timer。

阶段 1 剩余工作不再是代码功能缺口，而是线上观察：至少连续 10 个交易日检查健康报告、执行回报、微信提醒和 JoinQuant 网站委托记录是否一致。阶段 2 框架和部分模拟盘风险能力虽已本地实现，但仍需分别完成部署、真实 strict 数据运行和交易日观察；实盘级风控仍属于后续阶段。

## 2026-07-11 Batch 1 账本部署检查点（历史）

Batch 1 代码已实现并已部署服务器，待部署后的首个有效交易日双写观察；不得把服务器部署、非交易日静态检查或 readiness 结论视为交易日观察验收已经完成。2026-07-12 只读核验确认服务器与本地 SHA 均为 `54eaaf423f690dda84776304c4ec87846aa8cf66`，SQLite schema version 为 `1`，核心服务和现有定时器已运行。项目状态仍以 `docs/project_roadmap.md` 为唯一主文档，五批次专项设计从属于该主文档。

- 完整验证命令：`.venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py" -v`（Linux 服务器使用虚拟环境中的 `python -m unittest discover -s tests -p "test_*.py" -v`）。
- 当时正式数据库：`cache/trading/trading.db`，该历史检查点只接受 schema version `1` 且写入后删除探针事务成功；当前本地代码的 `ledger-check` 目标已是 schema version `6`，不能沿用此旧门槛部署新版本。
- 观察模式：`RISK_MODE=observe`；Batch 1 的仓位、集中度、换手和回撤软阈值只记录告警，不抑制原有有效信号。
- 一个交易日验收：收盘后逐一核对当前 JSON 信号 ID 与 SQLite；确认 JoinQuant 委托行为与 Batch 1 前一致；确认无重复信号；归档 readiness 和 health 报告；health 不得出现 `ledger_unavailable` 或 `ledger_json_signal_mismatch`。全部满足前不得开始 Batch 2 订单状态机计划。
- 阶段门槛：阶段 1 基础稳定性继续使用连续 10 个有效交易日；专项设计的完整账本与策略验证使用 20 个有效交易日。10 日通过不自动代表 20 日专项完成。
