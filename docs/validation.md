# 验证记录与限制

## 已验证

- 本次 Edge 布局 / 断线验收通过：1680×1050、1366×800 的研究、回放、Paper、LIVE 共八种布局主要控件可见、无页面横向溢出；折叠面板挂载正常。阻断本浏览器的 Dash 请求后出现超时与冻结警告，恢复请求后重新在线；损坏保存布局回退到推荐布局。全过程未连接券商或启动交易，JavaScript / HTTP 500 错误为零。记录 `data/verification/terminal-stability-browser.json`。
- 渲染优化阶段短时界面采样 62.81 秒、60 次 `/api/snapshot` 请求：中位响应 37.195 ms，P95 50.59 ms，服务 RSS 首尾 224.27 / 224.31 MB，最小可用物理内存 1965.27 MB。此结果只代表本机空闲界面场景，不是 QuoteTick、订单延迟、峰值容量或交易整日稳定性证明。

- 2026-10-05 最新完整运行 108 项 unittest 全部通过（11.518 秒），日志为 `data/verification/tests-options-final.log`。覆盖输出去重、隐藏图表、回放 K 线缓存的未来数据隔离、组合 ASK / BID 成本及张数约束、单组图使用 Actor 结果、原生期权元数据保留账户数量 / 价格。Ruff 检查及 87 文件格式检查通过；依赖未改变，此前 `pip check` 通过。进程恢复使用 Mock，不代表实际券商恢复通过。详见 [稳定性记录](terminal-stability.md)。
- 本机 Edge 渲染对照：12 秒空闲回调体总量由 1,874,814 字节降至 21,983 字节（约 98.8%）；修复前图表与容器尺寸相差约 254×190 像素，修复后两种窗口尺寸均对齐。重复切换面板、拖动分隔线及窗口缩放通过，未变化表格零重复传递、浏览器错误为零。长任务样本由 4 次降至 1 次，仅为短时测量，不能推断整日性能；面板切换的 Playwright 操作用时包含自动等待，不当作输入延迟。记录 `data/verification/terminal-rendering-before.json`、`terminal-rendering-after.json`。
- 完成期权子功能后的渲染复核再次通过：12 秒空闲回调 22,444 字节，未变化表格零重复传递，两种分辨率的实际已加载 K 线尺寸对齐，JavaScript 错误为零。记录 `data/verification/terminal-rendering-final.json`。
- MT5 布局与 ATAS 配色浏览器验收已通过：两个分辨率、真实期权 Watch 选完整合约、M1–H1 聚合、菜单 / 导航器 / 视图快捷键、右侧价格轴；未连接券商或启动交易。记录 `data/verification/terminal-mt5-browser.json`。
- 使用真实 SPY 历史期权缓存 390 条观察、不带 profiler 的 100 次计时，在保持同一回放时钟的场景下：快照中位数由 37.284 ms 降至 1.449 ms，P95 由 46.007 ms 降至 1.648 ms。Profiler 只用于热点定位，其计时单独保存，未作为上述指标；结果不代表动态时钟、策略执行或远程取数加速同等倍数。记录 `data/verification/terminal-profile-before.json`、`terminal-profile-after.json`。
- 期权子功能实际浏览器验收通过：链中切换到期日、导出当前链 CSV、组合选腿保持链可见、编辑张数、多组原生 Actor 估值及单组 / 合计图切换；报价 ASK / BID 成本方向由现有模型校验，未创建委托。记录 `data/verification/terminal-options-browser.json`。
- 新交互浏览器验收通过：期权 PUT 单元格直接联动完整合约与真实历史 K 线、原生 PortfolioPnLActor 与 TermStructureActor 输出组合风险及期限结构、回测参数与 EA 独立、空闲回放禁用无效操作、LIVE 红色环境提示且隔离历史链与图表、未核验账户禁止启动。JavaScript / HTTP 错误均为零，记录为 `data/verification/terminal-workflow-browser.json`，截图为 `terminal-workflow-research.png`、`terminal-workflow-live.png`。
- 最新期权功能与提速后的完整浏览器原生回放通过：375 个时间戳分组，SPY 2026-10-02 到期概率 / 自然报价研究对照仍亏损 812.80 USD，日内清仓、审计通过；暂停、单步、调速、真实停靠与按工作区保存 / 恢复布局、报告载入和 CSV 下载通过。日志 `data/verification/replay-options-final.log`；没有连接 IBKR 或启动 EA，显示优化没有改变该研究对照的交易结果。
- 启动审阅测试使用明确的账户回报 Mock：90 秒过期、参数变化、一次性令牌、账户数据重新核验、持久 STOP 阻断。真实 IBKR 字段与下单仍未验证；测试不能作为券商实盘验收。
- 停靠交易台 Edge 浏览器完整检查通过：加载真实期权 K 线，暂停保持时钟，单步 09:45 → 09:46，最大速度完成 375 个时间戳分组、日内清仓和审计，保存 / 恢复真实停靠布局、载入报告、下载 CSV；JavaScript 与 HTTP 错误均为零。记录和截图为 `data/verification/terminal-browser.json`、`terminal-replay.png`、`terminal-docked.png`。
- 本机 8 GB 且没有额外分页空间。优化后浏览器传输快照编号，默认二维 IV 图，已完成上述回放；采样最低剩余提交空间约 427 MB。大量候选、tick 历史、更多窗口和三维渲染会增加资源需求；本次结果不代表任意规模均可运行。
- 最后复跑时桌面窗口与测试同时运行，系统剩余提交空间约 200 MB，库导入发生 MemoryError；关闭本次桌面窗口后串行运行通过，再恢复窗口。该机器应避免同时运行桌面窗口、完整测试和回测；本次未更改系统分页设置。最终桌面截图为 `data/verification/terminal-final.png`，账户数值及按实际到期日显示的 IV 图已通过浏览器检查。
- unittest 覆盖 IV、报价质量、期限结构、精确到期收益、费用、多组合、旧参数隔离、回放审计、限价等待撤单、部分成交、有限盘口、共享腿及报价舍入。
- 新增恢复与风控测试：SQLite 提交 / 回滚、账户锁、原生已成交数量恢复、未知订单 / 仓位、迟到成交、撤单确认超时、净值缺字段 / 过期、亏损 / 回撤 / 拒单锁定；不连接券商，不代表实盘故障演练通过。
- 交易台测试覆盖来源隔离、禁止自动连接与实盘未确认启动、档案路径、未来报价不能修改当前 K 线、代理偏移只作显示转换、历史现金曲线准确标记。原生分段回放与普通回放成交数量 / 价格 / 盈亏一致。
- 真实 Nautilus LiveNode 上四个 Actor 的消息总线联动、重配置和关闭通过。
- 策略回归覆盖 70% 获利、20% 回撤、初始负浮盈不误触发、跨分钟报价拒绝、无可用模型时日内强平、Gamma 可选与硬阈值，以及原生记账代理现金和佣金对账。
- Chromium 浏览器验证真实历史数据，724 个 IV 点；曲面、期限、当前和到期图均生成，浏览器无 JavaScript 错误。截图在 `data/verification/dashboard.png`。
- Windows pywebview 的 EdgeChromium / WebView2 窗口真实加载曲面与期限图；记录在 `data/verification/desktop.json`。
- `pip check` 没有依赖冲突；使用统一 `python -m qwork` 启动。

## 数据与执行范围

2026-10-05 期权交互改进：119 项 unittest 通过；独立案例覆盖同日到期精确最大盈亏、无限尾部风险、手续费和盈亏平衡点。原生分析结果版本与来源时间隔离、过期实时输入拒绝、失败 / 暂停采集拒绝均通过测试；这些为隔离输入验证，不代表真实行情中断演练。

`terminal_review_smoke.py` 验证实际历史链和 PortfolioPnLActor、多次 CSV 导出、草稿数量编辑后刷新恢复、清空保存、未应用编辑不改变已提交分析、Paper 默认 EA 界面；1680×1050、1366×800、1120×800 的期权链面板纵向溢出均为 0，风险图均在面板内可见。真实 WebView2 下载保存和取消反馈通过；保存对话框目标 / 结果由测试自动化代选。证据位于 `data/verification/options-desk-browser.json`、`desktop-options-export.json`、`desktop-options-cancel.json`。未连接 IBKR 或提交委托。

SPY 回测使用 2026-10-02 的真实 Thetadata 分钟 NBBO。当前套餐拒绝 tick 历史接口，记录在 `data/verification/tick_history_permission.json`。1m 回放不证明分钟内触发顺序或 tick 执行效果。

最新 10-02 v3 四个成交情景通过 12 项审计且全部日内清仓：默认日内模型无委托；到期概率中点挂单 18 笔未成交；到期概率自然报价成交 49 组、亏损 812.80 USD；假设半价差挂单成交 22 组、亏损 276.40 USD。情景未校准，成交数量和时点不同。详见 [成交模型与结果](execution-model.md)。此前 1,875 USD 对照保留为旧模型历史记录；跨分钟报价、旧报价强平和 `natural-v2` 共享腿重复使用报告仅作诊断。本机约 8 GB 内存，原生测试与回测建议串行运行。

Nautilus 不为 spread 本身创建持仓；回测通过乘数 100 的原生线性记账代理跟踪合成组合 BBO，使现金和佣金可对账。未证明 IBKR BAG 原子成交、成交滑点或生产风控能力。

未启动自动实盘交易。新策略 IBKR 下单、what-if 保证金、断线恢复、不同经纪商回报与美式指派需要进一步实际环境验证。中断程序不等于账户已平仓；只有成交回报和持仓状态能证明平仓。

live 已实现持久策略意图、原生订单 / 逐腿核验和账户入口风控；限价超时、撤单确认与重试为共用流程，有限合成盘口只用于回放。实际 IBKR 账户字段、BAG 订单和恢复仍未验证。详细缺口见 [实盘接入](live-readiness.md)。

Dash 4 对原有分析界面 DataTable 发出弃用警告，当前不是测试失败；新交易台使用 AG Grid。浏览器完整交易台验证命令为 `.venv/Scripts/python.exe tests/terminal_smoke.py`，须先启动本地服务及准备对应 SPY 缓存。该脚本不会连接账户或点击启动 EA。
