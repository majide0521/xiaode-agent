# 小德竞品研究 Agent

当前版本：v0.7.1（Deterministic Report Rendering）

这是一个面向竞品研究的多阶段 Agent。它的目标不是“尽可能写满一份报告”，而是在证据不足时主动留白，并让每条事实都能追溯到程序真实读取的网页。

## 理想态逻辑

```text
用户输入
  ↓ 域名归一化、长度校验
结构化检索计划
  ↓ 固定查询/来源预算
安全检索与网页读取
  ↓ SSRF、重定向、体积和来源等级检查
结构化 Evidence Ledger
  ↓ 原文逐字匹配、Source ID/URL/Tier 一致性检查
独立 Auditor
  ↓ Python fail-closed 硬门禁
Writer 专用白名单证据包
  ↓ 结构化 ReportDraft
Python 确定性渲染
  ↓ 自动生成章节、标签、引用、CAUTION 限定语和 Sources
最终报告硬校验
独立运行目录 + manifest
```

设计原则：模型负责搜索规划、语义抽取、审核与分析；代码负责权限、预算、数据契约、来源映射和发布门禁。

## v0.7.1 的主要升级

- Writer 改为 Pydantic `ReportDraft` 结构化输出，不再让模型直接拼装 Markdown。
- Python 确定性生成全部章节、事实/判断标签、`[E##]` 引用和 Sources URL 映射。
- FACT 文本强制回落到通过审核的 Evidence Claim，模型不能借合法编号改写或扩大事实。
- 无有效引用的事实/判断会被删除；无直接依据的建议自动降级为待验证假设；空章节自动写研究缺口。
- CAUTION 证据的限定语由程序自动追加，避免 Writer 漏写或升级为确定事实。
- 失败页面现在直接展示具体校验错误，并允许下载校验详情、被拒报告、结构化草稿、证据和审计产物。

## v0.7.0 的基础能力

- 官网输入支持域名或完整 URL，只提取规范主机名，修复路径和查询参数导致 Tier A 失效的问题。
- 所有用户可影响的网页读取只允许公开 HTTP(S) 地址，逐跳验证重定向和 DNS，阻止本机/内网/保留地址，并限制响应体积。
- 每次运行使用 UUID 独立目录；Streamlit 只读取本次 `manifest.json` 指定的产物，不再用“最新文件”猜测结果。
- Planner、Extractor、Auditor 和 Writer 使用 Pydantic 结构化输出；网页正文被明确视为不可信数据。
- Auditor 同时收到 Evidence、程序预检结果和对应原始上下文。
- Python 门禁检查逐字引文、Source ID、URL、Tier、置信度、审计完整性和 CAUTION 限定语；任何不一致默认 REJECTED。
- 审核分别统计 Approved Rate 和 Usable Rate，不再把 CAUTION 算成 100% 通过。
- Writer 只能看到通过门禁的证据包；事实与判断都必须引用 `[E##]`，发布前再做一次代码校验。
- 提供快速/深度预算、运行超时、最小化子进程环境变量、可选网页密码、本地事件日志和证据/审计 UI。

结构化输出与评测思路参考了 [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) 和 [Agent evals](https://developers.openai.com/api/docs/guides/agent-evals)。当前模型通过 SiliconFlow 提供，因此 OpenAI 托管 tracing 保持关闭，运行事件写入每次任务的 `events.jsonl`。

## 本地运行

建议使用 Python 3.11。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export SILICONFLOW_API_KEY="..."
export BOCHA_API_KEY="..."
streamlit run app.py
```

命令行也可直接运行：

```bash
python agent.py \
  --competitor "Perplexity" \
  --domain "https://www.perplexity.ai/" \
  --focus "分析产品、增长、商业化和对搜索业务的威胁" \
  --mode deep
```

每次运行的产物保存在 `runs/<run_id>/`，包括搜索计划、候选来源、原始来源、Evidence Ledger、预检、Audit、Writer 白名单包、报告校验和最终报告。

## 密钥与部署

- 立即轮换任何曾在截图、日志或聊天中出现过的真实 API Key。
- 不要把 `.env` 或 `.streamlit/secrets.toml` 提交到 Git；仓库只提供无真实值的 `.env.example`。
- 公开部署时建议设置 `XIAODE_APP_PASSWORD`，并在 SiliconFlow/博查控制台设置额度告警或消费上限。
- `XIAODE_RUN_TIMEOUT_SECONDS` 默认为 900 秒，允许范围为 60–3600 秒。
- `XIAODE_MIN_RUN_INTERVAL_SECONDS` 默认为 30 秒，用于限制同一网页会话的重复启动频率。

## 离线测试

测试不会调用任何模型或搜索 API：

```bash
python -m unittest discover -s tests -v
```

同一套离线测试也会在 GitHub Actions 的 push 和 pull request 上运行。

上线前还应准备 20–30 个固定研究题作为回归集，长期跟踪来源命中率、逐字引文通过率、审核通过率、未知引用率、报告硬校验通过率、耗时和单次成本。

已有运行可先用内置聚合器形成质量基线：

```bash
python -m xiaode.evals --runs-root runs
```
