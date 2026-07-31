# Hermes Feishu Streaming Card v4.2.0

> 候选版本说明；本实现分支不会自行发布、打 tag 或合并。

## 新功能

- 在飞书私聊发送裸 `/update`，会先检查 Hermes、Git 工作树、HFC 钩子、更新目标、运行任务和维护包，再显示 120 秒有效的确认卡。
- 确认后由 Hermes 安装目录之外的独立维护进程执行官方 `hermes update --yes`，并从私有缓存重新安装**当前同一 HFC 版本**，恢复 hook、sidecar 和 Gateway。
- 同一卡片持续显示等待任务、恢复 hook、更新 Hermes、重装 HFC、启动服务、验证以及最终结果。
- 新增本机恢复入口：
  - `hermes-feishu-card maintenance provision`
  - `hermes-feishu-card maintenance status`
  - `hermes-feishu-card maintenance run`
  - `hermes-feishu-card maintenance resume`

## 安全边界

- 仅拦截飞书私聊中的裸 `/update`。群聊、非飞书、别名和带参数调用仍进入 Hermes 原处理器。
- 确认绑定发起人、私聊、profile、目标与本机证据，并在 120 秒后失效；重复或跨用户点击会被拒绝。
- 只执行官方 updater，不使用 `--force`、`--force-venv`、`--no-backup`，不执行自定义 `reset`、`checkout`、`stash` 或 Git 回滚。
- 保留 untracked 文件；存在非 HFC 的 tracked 改动、未完成 Git 操作、维护包漂移或运行时验证失败时停止。
- 维护目录为私有权限，journal 不记录 Feishu secret、transport secret、原始命令输出或任意命令。

## 验收建议

1. `maintenance status` 显示 `ready`。
2. 在飞书私聊发送 `/update`，检查确认卡中的 Hermes/HFC 版本与目标。
3. 验证取消不会执行更新。
4. 再次确认更新，观察原卡片完成全部阶段。
5. 完成后运行 `doctor --explain`，确认 HFC 版本、`site-packages` 导入来源、hook 与 sidecar/Gateway 状态。

