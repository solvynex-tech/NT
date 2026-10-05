# NT

Nautilus 期权研究与日内策略。

独立 Python 工程：四个原生 NautilusTrader `DataActor`，一个破翼蝴蝶 `Strategy`；Dash / Plotly 图表通过 pywebview 显示。数据来自 Thetadata 官方 SDK，交易委托走 Nautilus IBKR 执行客户端。分析任务在线程池执行，GUI 读取隔离状态快照。

停靠式 Dash 交易台提供研究、回放、Paper 和 LIVE 工作区，统一行情与账户来源；期权单元格联动完整合约和图表，候选联动原生组合盈亏与期限 Actor。回测和 EA 参数独立，启动须审阅绑定的账户与参数。默认不连接账户、不下单；详见 [交易台 README](src/qwork/ui/terminal/README.md)。

## 安装与启动

在本目录运行，Python 3.12–3.14：

```powershell
./scripts/setup.ps1
./scripts/start.ps1
# 交易台浏览器模式，8766
.venv/Scripts/python.exe -m qwork terminal --no-gui
# 显式历史模式，美国东部时间
.venv/Scripts/python.exe -m qwork dashboard --symbol SPY --date 2026-10-02 --at 10:00:00 --max-expirations 3
# 浏览器模式
.venv/Scripts/python.exe -m qwork dashboard --no-gui --port 8765
```

交易台默认 `http://127.0.0.1:8766`，原有 Actor 分析界面默认 8765；`--port 0` 可选择空闲端口。密钥使用本目录 `.env` 的 `THETADATA_API_KEY`；不发送到浏览器或写入报告。`data/private/` 包含授权缓存和交易状态，应与 `.env` 一起保密。

## 模块与文档

| 模块 | 输入 → 输出 | 文档 |
| --- | --- | --- |
| 波动率曲面 Actor | 标的 → 按到期日排列的 Call / Put IV | [README](src/qwork/actors/volatility_surface/README.md) |
| 期限结构 Actor | 曲面 → ATM 远期 IV 期限结构 | [README](src/qwork/actors/term_structure/README.md) |
| 破翼筛选 Actor | 标的、到期日、曲面 → 合格组合 | [README](src/qwork/actors/broken_wing/README.md) |
| 组合盈亏 Actor | 一个或多个组合 → 当前、到期与可平仓盈亏 | [README](src/qwork/actors/portfolio_pnl/README.md) |
| 破翼日内 Strategy | 标的、保证金预算 → 不重复组合、逐组合退出 | [README](src/qwork/strategies/broken_wing/README.md) |

每个 Actor、Strategy 都有独立目录。源码统一放在 `src/qwork/`：`core/` 保存配置与状态，`analytics/` 是纯金融计算，`providers/` 接入数据商，`runtime/` 管理原生节点，`backtesting/` 管理历史回放，`ui/` 提供桌面和图表。测试在 `tests/`，启动脚本在 `scripts/`，生成数据只进忽略的 `data/`。详见 [架构](docs/architecture.md)。

交易台面板可拖动停靠、调整尺寸、最大化，各工作区分别保存布局。策略测试器支持倍速、暂停、继续与单步；按钮按状态启用，点击成交可在图表定位，原生历史时钟不倒退。当前 K 线为期权 NBBO 中点聚合，不是股票或成交 OHLC，活动回放不提前显示未来报价，交易工作区隔离历史报价。

## 界面使用

默认交易台顶部选择工作区：研究中点击期权报价查看图表、选择候选查看组合风险；回放中展开独立回测配置再启动；Paper / LIVE 中主动连接目标账户，展开设置 EA 风控，然后审阅并确认启动。状态未知不会显示为零持仓，停止请求不代表券商已平仓。

默认采用期权链与组合风险并排的停靠布局，底部集中账户和执行状态，保留 MT5 布局，视觉参考 ATAS 深色主题。文件菜单提供保存 / 恢复 / 期权交易布局 / MT5 默认布局；M1–H1 切换历史图表周期，Ctrl+M / N / T / R 定位常用面板。组合草稿保存并标明未应用修改；分析结果绑定行情版本，过期实时输入不能应用。桌面支持 CSV 保存及取消反馈，同日到期风险显示精确最大盈亏、盈亏平衡点和无限风险。具体实现与剩余缺口见 [交易台 README](src/qwork/ui/terminal/README.md) 和 [交互评估](docs/ui-review.md)。

以下逐腿编辑与 JSON 导入指 `qwork dashboard` 的原有 Actor 分析界面：

左侧设置标的、历史 / 快照模式、到期范围和刷新频率。周期指期权到期日。现价留空时用同步 Call / Put 平价估计远期价，不请求股票数据；可手动覆盖。

组合区域支持逐腿输入名称、到期日、行权价、方向、数量与实际建仓权利金。同名腿属于同一组合；不同名称分别绘图并汇总。也可导入候选或单个 / 多组合 JSON。导入候选的成本只是报价假设，须复核实际成交价。持仓到期日和行权价优先加入查询。

当前图为固定行权价 IV 的情景曲线；可平仓盈亏按多头 BID、空头 ASK 扣成本和预计双边佣金。缺行情时仍显示到期图，当前估值与合计明确不可用。多到期日的到期合计假设各结算日标的价格相同，不是完整路径回测。

## SPY 日内回测

```powershell
.venv/Scripts/python.exe -m qwork backtest --symbol SPY --margin 10000 --date 2026-10-02 --interval 1m --max-positions 3 --universe-size 6
```

报告保存到 `data/backtests/`，包含 README、配置、选择时曲面、原生订单 / 成交 / 持仓 / 账户报告和事件记录。候选只从入场时已知信息选择；按纽约时间 15:55 平仓，提前收市日按收盘前五分钟平仓。支持 `--interval tick`，需要套餐允许。

默认策略要求计划日内平仓时的模型获利概率严格大于 70%，最大到期盈利 / 最大到期亏损严格大于 1。`--probability-horizon expiration` 可复现到期概率口径。两者都是风险中性固定 IV 模型估计，**不是历史胜率**，未估计完整路径退出规则的胜率。Gamma 只作可关闭的绝对净 Gamma 排序。研究回测以最大亏损乘缓冲系数预留资金；IBKR 执行前另外查询真实 what-if 保证金。

回放和 live 共用开仓超时、撤单确认、冷却重试及平仓限制；默认中点限价等待 120 秒，最多 3 次尝试，不假定中点成交。研究回放额外固定腿滑点为 0，费用按每张每边 0.65 USD，市价退出按有限盘口分批处理。共享腿容量、延迟及组合价差仍是未校准假设，详见 [成交模型](docs/execution-model.md)。实盘使用 NT BAG 订单，实际券商流程仍待验证。

2026-10-02 SPY v3 研究：默认日内模型无开仓；到期概率对照中点挂单 18 笔全部未成交；自然报价对照成交三种组合、49 组，净亏损 812.80 USD；假设组合价差缩小一半则成交三种组合、22 组，仍亏损 276.40 USD。四次研究均日内清仓并通过 12 项审计。改进后的 v4 普通回测和交易台分段回放均复现 -812.80 USD、清仓及审计通过。不同情景的仓位和时点不同，不能直接解释为盈利改善；早期亏损使用不同假设，见 [整体回测检查](docs/backtest-review.md)。

## 生成文件与清理

源码、Actor / 策略说明和测试分别位于 `src/qwork/`、各模块的 `README.md` 和 `tests/`。`__pycache__/` 与 `.ruff_cache/` 可清理；`data/verification/` 保留文档引用的证据和最新验收结果。同配置、同成交及盈亏结果的重复回测仅保留最新完整报告，不同模型的研究报告分别保留。

`.venv/` 是运行环境；`.env`、`data/private/` 保存配置与交易状态，`data/history/` 保存行情缓存，`data/ui/webview/` 保存桌面布局和草稿。日常清理保留这些目录。

## 验证与边界

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -v
.venv/Scripts/python.exe -m ruff check src tests
.venv/Scripts/python.exe -m ruff format --check src tests
```

本机约 8 GB 内存，原生回测与测试请串行运行。遇到 OpenBLAS 线程分配错误可先设置 `$env:OPENBLAS_NUM_THREADS = '1'`；这不能替代足够的内存。

“实时”界面为快照轮询，套餐可能延迟；原生策略按 IBKR 期权 QuoteTick 重算。休市明确显示空数据或错误，不自动切换合成数据。`--source fixture` 只作标注的功能验证。

QuantLib Black 为欧式近似；未建模美式提前行权、离散分红、融资与指派费用。只支持标准乘数 100，纽约 16:00 PM 到期假设；SPX 请使用 SPXW。分钟回测不能验证 tick 内触发顺序。组合回放使用合成 BBO 与原生线性记账代理，不证明真实 BAG 原子成交能力。实盘委托与恢复对账需要独立验证，不能由单日回测认定生产可用。

现已实现 SQLite 策略意图持久化、账户独占锁、原生订单 / 券商逐腿恢复核验、账户净值亏损 / 回撤 / 拒单入口保护和持久停止信号。专用账户的未知订单或仓位会阻止恢复；净值与可用资金缺失或过期禁止开仓。

实盘仍缺 IBKR 组合单和故障恢复实测、实际数量保证金、账户 Greeks 限额、真实成交校准及多日独立评估。亏损基准为本日首次有效净值，窗口只恢复已登记且身份核验通过的后台监看。已有实现与未验证边界见 [实盘验收](docs/live-readiness.md)。
