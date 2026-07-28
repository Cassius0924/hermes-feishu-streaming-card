# 测试说明

[中文](testing.md) | [English](testing.en.md)

## 单元测试

```bash
python3 -m pytest tests/unit -q
```

单元测试覆盖配置加载、事件模型、文本清理、卡片渲染、会话状态、安装器检测、manifest 和 patcher 行为。

## 集成测试

```bash
python3 -m pytest tests/integration -q
```

集成测试覆盖 CLI、doctor、sidecar server，以及基于 fixture Hermes 目录的安装、恢复和卸载流程。

官方 Hermes `v2026.4.23` Git tag 源码已用于人工安装/恢复 smoke；该上游标签没有顶层 `VERSION` 文件，因此安装器会在 `VERSION` 缺失时回退读取 Git tag。真实 Hermes Gateway 进程已配合真实 Feishu 测试应用完成 E2E 验证。

## Hermes hook runtime tests

```bash
python3 -m pytest tests/unit/test_hook_runtime.py tests/integration/test_hook_runtime_integration.py -q
```

这些测试会验证安装后的 Hermes hook 能把 `SidecarEvent` 发送到 mock sidecar，并在发送失败时保持 fail-open。它们只使用 fixture 和 mock sidecar，不访问真实飞书。

## V4.0.21 内容完整性回归

```bash
python3 -m pytest \
  tests/unit/test_prepare_completed_answer_issue96.py \
  tests/unit/test_prepare_completed_answer_issue155.py \
  tests/unit/test_hook_runtime.py -q
```

`test_prepare_completed_answer_issue155.py` 锁定 Issue #155：只有显式 `answer -> tool` 边界可以归档答案；`tool -> answer -> completed` 的完成态必须保留完整用户可见答案。`test_v4021_hook_runtime_keeps_image_delivery_and_accepted_notice_in_same_turn` 锁定 Issue #147：匹配媒体文本只抑制一次、原生图片仍投递、accepted notice 不出现 uncertain-delivery warning。它们均为自动化回归，不替代真实飞书图片 smoke；该 smoke 在 V4.0.21 文档时尚未完成。

## Sidecar process tests

```bash
python3 -m pytest tests/integration/test_cli_process.py -q
```

该测试会启动真实本机 sidecar 进程，检查 `/health`、`status`、事件接收和 `stop` 清理。测试使用临时 pidfile 目录和 no-op Feishu client，不访问真实飞书。

`/health` 和 `status` 指标由 `tests/integration/test_server.py` 与 `tests/integration/test_cli_process.py` 覆盖，包括 `events_received`、`events_applied`、`events_rejected`、`feishu_send_successes`、`feishu_update_failures` 和 `feishu_update_retries`。更新卡片会验证一次有限重试；创建卡片失败会返回 JSON 错误并清理本地 session，避免盲目重试造成重复卡片。

V3.8.7 增加新版 Hermes 兼容回归：如果首个普通消息事件直接是 `answer.delta`、`thinking.delta`、`tool.updated` 或 `message.completed`，sidecar 应创建初始卡片而不是把事件计入 `events_ignored`。

V3.8.8 增加 Hermes 原生系统提示卡片化回归：`system.notice` 事件应能进入 session timeline 或创建独立提示卡片；Feishu adapter 的 `send` / `edit_message` 拦截在 sidecar 可用时抑制灰色原生文本，在不可用或无法识别时保持 fail-open fallback。

V3.8.9 增加飞书/Lark 话题回复回归：当首张卡片使用原始话题消息 `message_id` 创建，而后续 `tool.updated`、`answer.delta` 或 `system.notice` 使用不同流式 `message_id` 时，sidecar 应通过 `reply_to_message_id` 更新同一张卡片，不新增重复卡片，也不让系统提示回退成外部灰色消息；即使已识别系统提示的卡片投递超时，也应抑制原生灰色文本兜底。

V3.8.10 增加群聊诊断和工具详情回归：`bindings.group_rules` 只作为安全诊断输入，不能泄漏真实 chat/user id；群内 `/hfc status` 应提示 chat binding、fallback/default 路由和 slash command 行为边界；`tool.updated` 应尽量把参数摘要、耗时和失败原因带入紧凑 timeline。

V3.8.11 增加 `/hfc` 原生 unknown 抑制回归：`/commands` 接受 `/hfc status` 后必须先返回 `handled: true`，真实 Feishu/Lark 卡片发送放到后台；patcher 的早期 `/hfc` 拦截必须位于 Hermes 原生 slash fallback 前，避免卡片和灰色 `Unknown command /hfc` 双发。

V3.8.12 增加 issue #82 附件摘要重复 reply 回归：普通 `attachments` 摘要应在卡片完成后抑制原生最终回复；`MEDIA:/tmp/...`、本地文件路径、`files`、`media_files` 和 image/audio/video locals 仍应保留 Hermes 原生文件/媒体投递路径。

## Feishu HTTP client tests

```bash
python3 -m pytest tests/unit/test_feishu_client.py tests/integration/test_feishu_client_http.py -q
```

这些测试使用 mock Feishu server 验证 tenant token、发送 interactive card、更新卡片消息和错误处理，不访问真实飞书，也不需要真实 App Secret。

手动真实飞书 smoke：

```bash
FEISHU_APP_ID=cli_xxx FEISHU_APP_SECRET=xxx \
python3 -m hermes_feishu_card.cli smoke-feishu-card --config config.yaml.example --chat-id oc_xxx
```

该命令会真实发送并更新一张测试卡片。只有在本机环境提供凭据和目标 `chat_id` 时才运行；不要把 App Secret、tenant token 或真实 chat_id 写入仓库。

## 文档测试

```bash
python3 -m pytest tests/unit/test_docs.py -q
```

文档测试只做低脆弱度守卫：确认 README 保留 sidecar-only、`v2026.4.23` 旧版本支持范围和 Hermes `0.13.0+` / `0.14.0` / `0.15.x` / `0.17.x` / `0.18.x` / `v2026.5.16+` / `v2026.6.19+` / `v2026.7.1+` 兼容说明，确认主线文档仍明确 legacy/dual 代码不是 active runtime，并确保事件协议持续声明卡片状态。它不替代人工文档 review。

## E2E visual preview

```bash
python3 tools/generate_e2e_preview.py --output-dir docs/assets
python3 -m pytest tests/unit/test_e2e_preview.py -q
```

生成器会写入 `docs/assets/e2e-card-preview.svg` 和 `docs/assets/e2e-card-preview.json`，用于本地核对 `思考中`、`已完成`、工具调用计数、`</think>` 标签过滤和最终答案覆盖行为。该预览不访问真实飞书，不读取 App Secret。

## Fixture 安装恢复测试

`tests/fixtures/hermes_v2026_4_23/` 是安装器安全测试使用的 Hermes fixture。相关测试会复制 fixture 到临时目录，验证：

- `install` 写入 hook、备份和 manifest。
- `restore` 能恢复原始 `run.py`。
- `uninstall` 能移除本插件拥有的安装状态。
- 用户改动过 `run.py`、备份或 manifest 时拒绝覆盖。

## Doctor

本地检查命令：

```bash
python3 -m hermes_feishu_card.cli doctor --config config.yaml.example --skip-hermes
python3 -m hermes_feishu_card.cli doctor --config config.yaml.example --hermes-dir ~/.hermes/hermes-agent
python3 -m hermes_feishu_card.cli doctor --config config.yaml.example --hermes-dir ~/.hermes/hermes-agent --json
python3 -m hermes_feishu_card.cli doctor --config config.yaml.example --hermes-dir ~/.hermes/hermes-agent --explain
```

当前 CLI 的 `doctor` 需要显式传入 `--config`。`--skip-hermes` 适合仓库内 dry-run；真实安装前应使用 `--hermes-dir` 做只读 Hermes 检测。输出包含 `version_source`、`version`、`minimum_supported_version`、`run_py_exists`、`hook_strategy`、`compatibility`、anchors、`reason` 和 `runtime_import`，不写入 Hermes 文件、备份或 manifest。`--json` 用于 issue/自动化，`--explain` 用于人工排障并会提示是否可运行 `repair --hermes-dir ... --yes`。

自动化矩阵显式覆盖 Hermes `v2026.4.23`、`v2026.5.7`、`v2026.5.16`、`v2026.5.29`、`v2026.6.19+`、`v2026.7.1`、`v2026.7.7.2`、`0.13.0`、`v0.13.0`、`0.14.0`、`v0.14.0`、`0.15.1`、`v0.15.1`、`0.17.x`、`0.18.0`、`v0.18.0`、`0.18.2`、`v0.18.2` 和描述型 `Hermes Agent v0.18.2 (...)` 的 hook strategy。Hermes `0.13.0+`、`0.14.0`、`0.15.x`、`0.17.x`、`0.18.x` / `v2026.5.16+` / `v2026.6.19+` / `v2026.7.1+` 应显示 `gateway_run_013_plus`，旧版本 Hermes `v2026.4.23` 到 `v2026.4.x` 应显示 `legacy_gateway_run`。缺少 `VERSION` 和 `.git` 元数据但存在可验证 `gateway/run.py` anchor 时，应显示 `version_source: gateway anchors`；`VERSION` 存在但不可解析且 anchors 可验证时，应显示 `version_source: VERSION + gateway anchors`。

## 真实飞书联调

真实飞书/Lark 联调只能通过环境变量或本机配置提供凭据，例如 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`。不要把 App Secret 写入仓库、测试 fixture、日志样例或文档。

联调完成后建议轮换测试应用凭据，并检查本地日志中没有持久化 secret。

本轮真实联调覆盖：

- 短/中等回答的卡片创建、流式更新和完成状态。
- 工具调用计数显示。
- sidecar 接受完成事件后抑制 Hermes 原生灰色文本。
- footer 元数据展示和异常 token 过滤。
- 同一张飞书卡片连续更新到 16k 中文字符。
- 真实 Hermes 目录 `restore -> install` 循环，最终保持已安装状态。

全量自动化回归命令：

```bash
python3 -m pytest -q -p no:cacheprovider
```

结果以本地或 CI 当次输出为准，不在文档中写死 passed 数量。
