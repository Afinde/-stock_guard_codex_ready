# Stock Guard MVP

面向 A 股的“选股研究 + 买卖点提醒 + 仓位建议 + 模拟盘”起步项目。默认关闭实盘下单。

## 核心原则

- 不承诺固定收益；5%/8%仅作为分批止盈观察区，不是每日收益目标。
- 单笔账户风险默认 0.5%，固定止损距离默认 5%。因此单笔理论仓位约为账户的10%，同时受单票15%上限约束。
- 默认 `ENABLE_LIVE_ORDER=false` 且 `MANUAL_CONFIRM_REQUIRED=true`。
- 数据异常、缺失或延迟时输出 `DATA_ERROR`，不得将失败降级为买入信号。

## 快速启动

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

打开：

- API文档：`http://127.0.0.1:8000/docs`
- 健康检查：`GET /health`
- 手动扫描：`POST /api/scan`
- 历史信号：`GET /api/signals`

调度提醒：

```bash
python -m app.scheduler
```

## Codex 推荐开发顺序

1. 在项目根目录启动 Codex，并先让它阅读 `AGENTS.md`，只做仓库审计，不立即改代码。
2. 增加交易日历，不在法定休市日运行。
3. 增加前复权/后复权一致性测试、涨跌停/ST/停牌过滤。
4. 增加事件驱动回测，纳入佣金、印花税、滑点、T+1和成交量约束。
5. 增加模拟持仓、交易日志、最大回撤和策略版本号。
6. 前端可用 Vue3 + ECharts；消息渠道可接企业微信/飞书机器人。
7. 实盘接口必须由持牌券商正式接口提供，并先完成报告、权限、测试与人工确认。

## 验收指标建议

不要用“每天赚5%-8%”。使用：

- 样本外年化收益、最大回撤、收益回撤比、胜率、盈亏比、换手率。
- 滚动回测和多市场阶段稳定性。
- 信号数据可追溯、策略版本可回放、异常自动熔断。
- 模拟盘连续运行至少 8-12 周，无越权下单、无重复提醒、无数据穿越。


## 使用 Codex 继续开发

本版本已经增加：

- `AGENTS.md`：Codex 的仓库级开发规范与金融风控红线；
- `docs/ROADMAP.md`：从数据基础、策略、风险、回测到模拟盘的分阶段路线；
- `docs/CODEX_PROMPTS.md`：可直接复制给 Codex 的任务指令；
- `docs/DEFINITION_OF_DONE.md`：每个任务的完成标准；
- `scripts/verify.sh`：统一验证入口；
- `Makefile`：常用开发命令。

建议首次进入项目后执行：

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
bash scripts/verify.sh
codex
```

首次交给 Codex 的任务不要是“把整个平台全部开发完”，而应使用
`docs/CODEX_PROMPTS.md` 中的“Baseline repository audit”。审计完成后，每次只推进一个里程碑。
