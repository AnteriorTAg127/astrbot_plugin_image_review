# 更新日志

## v1.5.0

- **迁移双群管理机制**（对齐 astrbot_plugin_content_audit）
  - 多群管理命令优化：管理群仅绑定 1 个被管理群时，`/查询违规`、`/删除违规`、`/审核状态` 等命令直接作用于该群（无需群号）；绑定多个时需带 `[群号|all]`，且群号必须绑定到当前管理群，否则拒绝并列出绑定群清单
  - 群级人工名单：人工黑/白名单条目支持「全局 / 指定群」范围（数据库迁移为 `UNIQUE(md5_hash, group_id)`，旧数据作为全局条目保留）；审核时全局条目或本群条目命中即生效，群级条目优先
  - 名单命令向后兼容：`/添加白名单 [原因] [群号|all]`、`/移除白名单 [群号|all]`、`/添加黑名单 [REVIEW|BLOCK] [原因] [群号|all]`、`/移除黑名单 [群号|all]`，末位参数为 `all` 或已绑定群号时识别为范围，缺省全局
  - `/删除违规` 增加强制「确认」参数，防止误删
  - `/查询名单` 按范围展示人工名单状态（全局 / 各绑定群）
  - 配置热重载：`ConfigManager.maybe_reload()` 检测 group_settings 变更后自动重建群配置缓存，无需重启插件
  - 群别名：group_settings 新增 `group_name` 配置项，用于 WebUI 与状态统计展示
- **新增 Dashboard WebUI 管理面板**（插件详情页 → 图片审核管理）
  - 概览：今日/累计审核与违规统计、审核状态数量（通过/复审/违规分布）、近 7 天趋势图、Top10 违规用户、各群分布
  - 违规记录：违规图片查看（服务端 Pillow 压缩 + base64 安全输出）、LLM 分析全文、筛选/排序/分页、编辑备注、单条与批量删除
  - 审核日志：全部图片审核记录（含通过）浏览、筛选、删除，保留 30 天自动清理
  - 白名单 / 黑名单：群级筛选（全局/指定群）、添加/改备注/删除
  - 违规档案：跨群用户档案（昵称/状态/备注/首末次出现/违规计数），自动建档、可管理
- **数据库演进**（幂等迁移，旧数据无损）
  - `violation_records` 新增 `user_name` / `evidence_path` / `note` 列；插件启动时按证据文件名前缀尽力回填旧记录的证据路径
  - 新表 `audit_log`（审核日志）、`user_profiles`（用户违规档案），违规记录删除时自动重算群级统计与档案计数
- **证据图片保存时机调整** - 无论是否配置管理群都保存证据图片（原仅在有管理群时保存），确保 WebUI 始终可展示

## v1.4.1

- **修复 v4.26.x 图片审核崩溃** - 解决 AstrBot v4.26.x 起 `PreProcessStage` 把 `Image.url` 覆写为本地临时路径导致的 `图片审核流程异常: /AstrBot/data/temp/media_image_xxx.gif` 崩溃
  - 根因：`PreProcessStage`（`astrbot/core/pipeline/preprocess_stage/stage.py`）下载图片到 `data/temp/` 并覆写 `comp.url/file/path`，但 `raw_message` 保留原始协议端内容
  - 主方案：aiocqhttp 下优先从 `event.message_obj.raw_message` 恢复原始公网 URL 与 QQ MD5，走原有正常流水线（下载、Aliyun、VLAI、GIF 检测、表情跳过均恢复物化前行为）
  - 兜底：`download_image` 改用 AstrBot `MediaResolver`，兼容本地路径 / `file://` / base64 / HTTP；VLAI `detect_image` 增加"已有字节直接用"分支；Aliyun 无公网 URL 时降级跳过 + 警告
- **修复 QQ 表情跳过回归** - 物化后 `comp.url` 为本地路径导致 `is_qq_builtin_emoji` 失效，现通过原始 URL 恢复识别
- **新增工具方法** - `ImageUtils.extract_md5_from_filename`，从 OneBot image 段 `file` 字段提取 QQ MD5
- **移除冗余资源** - 移除自维护的 aiohttp 下载会话（`_download_session`/`_download_semaphore`/`close_download_session`），统一由 `MediaResolver` 管理

## v1.3.8

- **重构阿里云审核实现** - 将阿里云 SDK 调用改为直接 HTTP API 调用，解决与 AstrBot cryptography 版本冲突问题
- **移除 SDK 依赖** - 不再依赖 alibabacloud_green20220302、alibabacloud_tea_util、alibabacloud_tea_openapi 等 SDK
- **新增 HTTP API 签名** - 手动实现阿里云 ROA API 签名（HMAC-SHA1）

## v1.3.7

- **新增相似图片匹配功能** - 支持基于感知哈希（pHash/dHash）和汉明距离的相似图片检测
  - 在 MD5 精确匹配失败后，可进行相似图片匹配
  - 支持自定义哈希算法（phash/dhash）和汉明距离阈值
  - ⚠️ **警告**：此功能可能导致误判，例如两张图片整体相似但某一小部分包含违规内容时，正常图片可能被误判为违规
  - 建议仅在必要时开启，并结合人工审核使用

## v1.3.6

- **修复指令重复注册问题** - 将命令处理逻辑内联到 main.py，解决指令重复注册的 bug
- **代码重构优化** - 移除 command_handlers.py，简化代码结构

## v1.3.5

- **新增管理命令权限验证** - 可配置是否要求管理员/群主身份才能执行敏感命令，增强安全性
- **优化图片下载流程** - 减少重复下载，优化资源使用
- **修复动图检测耦合问题** - 动图增强检测现在仅在 VLAI 提供商下生效，避免逻辑混乱

## v1.3.0

- **新增转发消息图片检测** - 支持检测合并转发消息中的图片内容
- **新增抽检功能** - 转发消息图片过多时可按比例抽检，避免资源浪费
- **配置增强** - 新增转发消息检测相关配置项

## v1.2.0

- **新增智能审查模式** - 支持定时审查和管理在线检测，夜间强制检查，白天智能补漏
- **新增管理员列表缓存** - 缓存管理员身份，避免频繁查询
- **审核状态增强** - 显示审查模式、自动黑白名单数量等详细信息

## v1.1.4

- **新增动图增强检测** - 支持对 GIF/动图进行多帧检测，可配置逐帧分开检查或批量合并检查模式
- **新增 QQ 自带表情包跳过** - 可配置跳过 QQ 官方表情包检测，避免误审
- **新增动图检测专用配置** - 支持单独配置动图检测的 LLM 提供商、采样帧数、检测模式等
