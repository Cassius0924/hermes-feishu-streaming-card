# V4.1.3 发布说明

[English](release-notes-v4.1.3.en.md) | [中文](release-notes-v4.1.3.md)

V4.1.3 是 Issue #158 的窄范围升级恢复热修。真实 Hermes upstream 更新会先移除受管 hook；官方 `install` 重新注入 hook 后，当前 integrity plan fingerprint 会变化，但 manual-review fence 仍绑定升级前 plan。V4.1.2 正确拒绝了不一致 binding，却没有受支持的旧 plan 到当前 plan 过渡路径，最终迫使报告者手工修改 fence。

## 修复内容

- `integrity acknowledge-review` 现在可原子迁移同一 Hermes target 的 plan binding，但必须同时满足：当前 recovery/integrity plan 连续两次验证为 installed、sidecar health 连续两次不可达、无 pidfile、显式 `--yes`、旧新 `target_identity` 完全一致、fence snapshot CAS 未变化。
- 不同 target identity、状态漂移、运行中的 sidecar、未知 legacy fence、dirty 或不可验证 plan 继续 fail-closed。该能力不是 force-clear。
- 若已有独立 restart fence 与非空 `pre_repair_runtime_hash`，acknowledgement 只解除 manual review 并更新 plan binding；restart/hash 保留到不同 runtime id 且 generation/package 匹配的新 `runtime.hello` 到达。
- `doctor --explain` 在 `integrity_migration_required` 时给出完整 `integrity migrate-safe` 命令；其他 manual-review 场景会先要求审查 installed evidence，再给出包含 config、Hermes 与 state 路径的完整 `integrity acknowledge-review` 命令。

## 升级与恢复

继续使用官方安装与诊断入口。不要手工编辑 `runtime-integrity-fence.json`，也不要调用内部 Python 函数：

```bash
export HFC_VERSION=v4.1.3
hermes-feishu-card doctor --config CONFIG --hermes-dir HERMES_DIR --explain
hermes-feishu-card setup --config CONFIG --hermes-dir HERMES_DIR --yes
```

若真实 Hermes 更新覆盖 hook，请先按 doctor 提示重新运行官方 `install`。当 install state 已验证为 installed 但仍为 `manual_review_required` 时，停止 sidecar，执行 doctor 给出的 `integrity acknowledge-review --config CONFIG --hermes-dir HERMES_DIR --state-dir STATE_DIR --yes`，再人工启动 sidecar 并重启一次 Hermes Gateway。最后确认 `readiness: ready`、`readiness.reason: runtime_ready` 与 `hook.status: installed`。

## 候选验收范围

- 自动化覆盖同 target plan 过渡成功、默认不放行、不同 target 拒绝、CAS/state/process/current-plan 双重校验以及 restart/hash 保留。
- 诊断覆盖 migration 与 manual review 两类完整命令，不输出真实路径、fingerprint 或状态私密证据。
- 发布前仍需 Issue #158 报告者在 Ubuntu 24.04 / 真实 Hermes upstream update 后使用候选提交按官方流程复测；不得用手工 fence 修改作为通过条件。
- exact merge SHA、公开 tag/install 与 Release assets 只在正式发布阶段完成。
