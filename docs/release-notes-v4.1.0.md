# V4.1.0 发布说明

发布日期：2026-07-28

V4.1.0 增加按会话选择 Hermes 原生消息、无损处理超量表格、认证的运行时完整性监控，以及明确且不隐式提权的 sidecar 服务管理。默认体验保持兼容：未列入 `bindings.native_chats` 的会话继续使用流式卡片；缺少 `integrity` 段的旧配置按 `notify` 加载；`service.manager: auto` 不会进入 system systemd。

## 按会话选择卡片或原生消息

- `bindings.native_chats` 只接受非空、无控制字符的精确 chat id；不支持 wildcard、regex 或 prefix。多 profile 配置只读取 `profiles.<profile_id>.bindings.native_chats`，不会与顶层列表合并。
- 使用 `chats use-native`、`chats use-card` 和 `chats list` 原子修改或查看策略。输出只显示掩码摘要；修改从该会话的下一条新消息开始生效，不改变正在运行的 turn。
- hook 在抑制 Hermes 原生路径前调用带 `hfc-policy-v1` 域分隔签名的 `POST /delivery/policy`；sidecar 在创建 session 或发卡前再次检查。超时、认证失败、配置损坏或未知 profile 都 fail-open 到 Hermes 原生消息。
- 普通回答、工具、approval/clarify、cron、system notice、命令反馈与 picker 使用同一策略；`/hfc help/status/doctor/monitor` 和显式 smoke card 仍是卡片运维面。

## 表格与卡片上限不再静默丢内容

- `card.table_overflow_mode: compact` 是默认值。Markdown-aware scanner 忽略 fenced code 中的假表格，并把第 6 张及后续表格按原顺序转换为可读字段列表；所有行和单元格保留。
- 显式选择 `truncate` 可保留旧版“五张表格后省略”的行为，但 fenced code 不再消耗表格额度，表格后的普通正文也不会被误删。
- 最终 card JSON 使用同一序列化器检查 5 张渲染表格、200 个 tagged element 与保守的 28,000 UTF-8 byte 预算。非终态超限先显示小型等待卡并继续收集；终态仍超限时不发送半截答案，而是一次性回退完整 Hermes 原生答案。已有卡片只更新为不含正文摘录的 handoff 提示；重试保持幂等。

## Hermes 升级后的完整性监控

- 新安装显式写入 `integrity.mode: safe`。没有该段的旧配置按 `notify` 加载，只报告风险，不自动迁移；`off` 关闭控制面，但不关闭普通事件鉴权或手工诊断。
- 旧安装只有在 Git ancestry、owned blobs、backup、manifest、anchors 与可逆 patch 均可验证时，才可运行：

  ```bash
  hermes-feishu-card integrity migrate-safe \
    --config ~/.hermes/config.yaml \
    --hermes-dir ~/.hermes/hermes-agent \
    --yes
  ```

  成功输出 `sidecar.restart_required: true` 与 `gateway.restart_required: false`：先重启 sidecar 让新模式生效，不因模式迁移重启 Gateway。
- 已安装 runtime 通过独立 `hfc-runtime-v1` 签名发送 `runtime.hello` / `runtime.heartbeat`。`/health`、CLI 与 `/hfc` 会区分 ready、starting、degraded 和 restart required，而不是只看进程存活。
- `safe` 只对严格证据执行既有 patcher/recovery transaction。修复后会显示 `gateway.restart_required: true`，但 HFC 绝不自动重启 Gateway；用户在合适窗口手动重启后，新的 `runtime.hello` 才清除该状态。证据不完整、用户编辑、symlink、dirty target、branch rewind、source-stripped root 或认证控制面不可用时保持 fail-closed。

## 明确的 sidecar 服务管理

`service.manager` 支持四个值：

- `auto`：仅探测 `systemd-user`；不可用时选择 `detached`，不访问 system bus，不调用 `sudo`。
- `systemd-user`：明确要求可用的 user manager；不可用就给出修复提示，不静默回退。
- `detached`：在 macOS、Windows、容器或显式选择时使用项目已有的 owned detached process。
- `systemd-system`：仅 Linux 显式 opt-in，使用 transient `systemd-run --system`；不写 `/etc/systemd/system`，不调用 `sudo`，权限不足时直接失败。

Docker Compose 继续以普通 setup、sidecar、Gateway 容器运行，固定使用 `detached`，不在容器内启动 systemd，也不要求 privileged host integration。

## Hermes 兼容边界

Hermes 0.19.0 / `v2026.7.20` 系列仍在 Gateway 运行时导入 `gateway.run.start_gateway`，因此 V4.1.0 保留可检测、可移除、可恢复的 AST-owned `gateway/run.py` hook。升级覆盖由认证 heartbeat 与 strict repair 处理，而不是安装范围更广、难以验证的 import-hook bridge。HFC 不手工编辑已安装 Hermes；所有源码变更仍只经官方 patcher/recovery transaction。

## 升级

```bash
export HFC_VERSION=v4.1.0
curl -fsSL https://raw.githubusercontent.com/baileyh8/hermes-feishu-streaming-card/main/install.sh | bash
```

升级后先运行 `status` / `doctor --explain`。旧配置保持 `integrity.mode=notify`，若要启用自动安全修复，再按上面的 `integrity migrate-safe` 显式迁移并重启 sidecar。任何修复后的 Gateway 重启仍由用户决定。

Release workflow 预期生成 `hermes-feishu-card-v4.1.0-macos.tar.gz`、`hermes-feishu-card-v4.1.0-linux.tar.gz`、`hermes-feishu-card-v4.1.0-windows.zip` 和 `hermes-feishu-card-v4.1.0-checksums.txt`；实际 assets、public tagged install 与真实飞书验收结果仅在完成后确认，不在本说明中提前宣称。

## Credits

- 感谢 @shutdown-awa 在 Issue #157 提出按聊天排除卡片输出的需求。
- 感谢 @Jasonsun77 在 Issue #158 提供 Hermes fast-forward 后 hook 被覆盖的复现与证据。
- 感谢 @Redeemer-w 在 Issue #159 报告五张表格后的内容丢失。
- 感谢 @Cyber-Yichen 的 PR #156 提供 systemd 环境观察。V4.1.0 保留显式 manager 能力，但不采用 `auto` 隐式进入 system service 或提权的部分。
- 感谢 @wholegale39 的 PR #160 提供 Hermes 新入口兼容调查。V4.1.0 采用现有 AST-owned hook、认证运行时监控和 strict repair，而不安装 import-time monkey patch。

本说明不包含真实 chat/message/user 标识符、凭据、transport secret、本机路径或恢复 fingerprint。
