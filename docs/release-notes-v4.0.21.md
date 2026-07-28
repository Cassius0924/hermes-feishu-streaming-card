# V4.0.21 发布说明

发布日期：2026-07-28

V4.0.21 是针对 Issue #155 的内容完整性热修，并为 Issue #147 固化图片与 `system.notice` 的组合回归。它不改变卡片 UI 或配置。

## Issue #155：显式 answer/tool 归档边界

- 只有明确的 `answer -> tool` 边界，才允许把该答案片段归入“思考与工具”时间线。
- 对 `tool -> answer -> completed`，较早的工具不能成为归档依据；完成态保留整个用户可见答案，不能仅因本轮曾出现工具就剥离流式前缀。
- 对多个 `answer -> tool` 片段，仍按各自的显式后续工具边界依次归档，终态答案保持可见。

## Issue #147：图片与 notice 组合回归

- 自动化回归覆盖已接管的完成事件：卡片正文只保留一次，匹配的原生媒体文本只抑制一次，native image 仍由 Hermes 原生媒体发送器投递。
- 同一轮中，已 accepted 的排队 `system.notice` 不产生 uncertain-delivery warning；无关的后续原生文本不应被抑制。
- 既有 `MEDIA:` 清理、附件摘要和 `native_delivery=required` 语义保持不变。

## 验证边界

- Task 1 的答案顺序聚焦回归为 `74 passed`；Task 2 的图片/notice 组合回归为 `277 passed`，且未发现需要运行时代码改动的新根因。
- 真实飞书图片 smoke 尚未完成。发布前仍需验证“一张完成卡 + 一条 native image”、无灰色重复答案且无 uncertain-delivery warning；自动化结果不等同于飞书客户端视觉验收。
- 本说明不包含真实 chat/message/user 标识符、凭据、token 或本机路径。

## 升级

```bash
export HFC_VERSION=v4.0.21
curl -fsSL https://raw.githubusercontent.com/baileyh8/hermes-feishu-streaming-card/main/install.sh | bash
```
