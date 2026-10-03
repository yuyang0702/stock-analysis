# GitHub 量化项目借鉴清单

更新时间：2026-10-04（Asia/Shanghai）

本清单只吸收成熟开源项目的工程方法，不直接复制社区策略参数。所有策略想法都必须回到本项目的 A 股费用、T+1、涨跌停、停牌、容量和 Walk-forward 验证链路中重新验证。

## Qlib：数据层和特征层分离

仓库：[microsoft/qlib](https://github.com/microsoft/qlib)

Qlib 将原始数据、Data Loader、Data Handler、表达式特征、Dataset 和缓存分层，并支持按交易日自动更新数据。对本项目最有价值的做法是：

- 原始 `bars/status/universe` 保持不可变数据集；
- 技术特征由确定性表达式或 `daily_feature_builder` 生成；
- 特征处理参数只在训练窗口拟合，再应用到验证窗口；
- 数据更新和策略运行分开，避免回测直接读取“最新文件”。

本项目已经有 `HistoricalStore`、`dataset_id`、点时特征和 Walk-forward。后续优先补充特征定义版本、特征缓存哈希和训练/验证窗口隔离，不引入 Qlib 作为运行时依赖。

## RQAlpha：可替换模块和回测/交易边界

仓库：[ricequant/rqalpha](https://github.com/ricequant/rqalpha)

RQAlpha 将数据、回测、交易和扩展模块拆开，适合借鉴：

- 数据源通过适配器替换；
- 费用、滑点、订单和撮合规则独立配置；
- 研究回测和交易执行不共享隐式状态；
- 新功能用模块扩展，不把券商 SDK 写进策略函数。

本项目的 `HistoricalMarketDataProvider`、`HistoricalStore` 和未来 `BrokerAdapter` 已采用相同方向。RQAlpha 的仓库许可包含非商业使用限制，不能直接复制代码到本项目生产代码中。

## Backtrader：费用、滑点和订单撮合

仓库：[mementum/backtrader](https://github.com/mementum/backtrader)

Backtrader 将 commission、slippage、订单类型、成交回报和多时间框架作为独立组件。对本项目的直接启发是：

- 每笔成交必须记录费用、滑点和未成交原因；
- 订单状态不能用“信号出现”替代；
- 撮合模型要单独做敏感性分析；
- 交易策略不能假设订单必然成交。

本项目已经实现 A 股佣金、印花税、过户费、滑点、T+1、涨跌停和停牌约束，下一步继续补充未成交重试与实盘回报对照。

## vn.py：事件引擎和券商网关隔离

仓库：[vnpy/vnpy](https://github.com/vnpy/vnpy)

vn.py 将事件引擎、数据网关、交易网关、组合策略、风控、模拟账户和数据库分开，并提供多种 A 股网关。对本项目的借鉴是：

- 策略只生成候选和目标仓位；
- 行情、下单、成交回报分别通过接口注入；
- 风控在订单发送前再次执行；
- 模拟账户和真实账户使用同一订单状态契约，但权限不同。

本项目当前只保留券商历史导出和 `BrokerAdapter` 边界，实时行情与真实下单仍关闭。未来接入券商时，应继续保持策略层不依赖具体券商 SDK。

## 对当前策略的结论

GitHub 项目共同强调的有效方向不是继续增加指标数量，而是：

1. 先把价格、状态、股票池和特征的数据版本固定；
2. 用横截面排名和市场环境过滤降低单一指标失效；
3. 用容量、费用、滑点和未成交记录约束回测收益；
4. 用训练/验证/保留集和 Walk-forward 检查过拟合；
5. 任何候选策略必须与等权基准、低换手基准和空仓/现金基准比较。

当前 JQData 价格核心结果仍为研究证据，不因借鉴开源项目而升级为生产结论。
