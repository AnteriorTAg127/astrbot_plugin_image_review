# astrbot-plugin-image-review

图片审核插件 / An image review plugin for AstrBot

[![License](https://img.shields.io/github/license/AnteriorTAg127/astrbot_plugin_image_review)](LICENSE)
[![Version](https://img.shields.io/badge/version-v1.0.4-blue)](metadata.yaml)

> [!NOTE]
> 这是一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 提供图片内容审核功能的插件。
>
> [AstrBot](https://github.com/AstrBotDevs/AstrBot) 是一个支持多个主流即时通讯平台的智能助手，包括 QQ、Telegram、飞书、钉钉、Slack、Discord 等。本插件为 AstrBot 提供图片内容审核能力，帮助群管理员过滤不当图片内容。

## 功能特性

- **双模式图片审核** - 支持阿里云内容安全 API 和 VLAI 视觉语言模型两种审核方式
- **智能缓存机制** - 通过 MD5 缓存已审核图片，重复图片无需再次审核
- **黑白名单系统** - 支持自动黑白名单和人工黑白名单，可灵活管理
- **违规处理** - 自动撤回违规图片并禁言用户
- **管理群通知** - 实时推送违规通知到管理群，支持合并转发消息
- **用户违规统计** - 记录用户违规次数，禁言时长按次数累积
- **证据保存** - 自动下载并保存违规图片证据

## 支持平台

- [x] QQ (aiocqhttp)

## 安装说明

### 方式一：通过 AstrBot 插件市场安装

在 AstrBot 管理面板的插件市场搜索"图片审核"并安装。

### 方式二：手动安装

```bash
# 克隆插件仓库
git clone https://github.com/AnteriorTAg127/astrbot_plugin_image_review.git

# 复制到 AstrBot 插件目录
cp -r astrbot-plugin-image-review <astrbot_plugins_path>/

# 安装依赖
cd <astrbot_plugins_path>/astrbot-plugin-image_review
pip install -r requirements.txt
```

## 配置说明

### 审核提供商选择

插件支持两种图片审核提供商：

1. **Aliyun (阿里云)** - 基于阿里云内容安全服务，审核准确率高，需要阿里云账号
2. **VLAI (视觉语言模型)** - 基于 AstrBot 的 AI 能力，使用视觉语言模型进行审核，无需额外账号

### 阿里云配置

1. 登录 [阿里云内容安全控制台](https://content-safety.console.aliyun.com/)
2. 开通内容安全服务
3. 创建 AccessKey，获取 KeyId 和 KeySecret

### VLAI 配置

VLAI 使用 AstrBot 已配置的 LLM 提供商进行图片审核：

- 支持自定义审核提示词
- 可指定特定的 LLM 提供商（留空使用默认）
- 需要 LLM 支持视觉能力（如 Qwen3-VL 等）

### 群聊配置

在插件配置中添加 `group_settings`：

```json
{
  "image_censor_provider": "Aliyun",
  "enable_image_censor": true,
  "disable_auto_whitelist": false,
  "disable_auto_blacklist": false,
  "aliyun": {
    "key_id": "your_key_id",
    "key_secret": "your_key_secret",
    "image_service": "baselineCheck",
    "image_info_type": "customImage,textInImage"
  },
  "vlai": {
    "provider_id": "",
    "censor_prompt": "请分析这张图片是否有显著色情违规内容..."
  },
  "group_settings": [
    {
      "enabled": true,
      "group_id": "123456789",
      "manage_group_id": "987654321",
      "first_mute_duration": 600,
      "max_mute_duration": 2419200,
      "mute_multiplier": 1.5,
      "auto_recall": true,
      "auto_mute": true,
      "base_expire_hours": 2,
      "max_expire_days": 14
    }
  ]
}
```

### 配置参数说明

#### 基础配置

| 参数 | 类型 | 说明 | 默认值 |
|------|------|------|--------|
| `image_censor_provider` | string | 图片审核提供商 (`Aliyun` 或 `VLAI`) | `Aliyun` |
| `enable_image_censor` | bool | 是否启用图片审核 | `true` |
| `disable_auto_whitelist` | bool | 关闭自动白名单机制 | `false` |
| `disable_auto_blacklist` | bool | 关闭自动黑名单机制 | `false` |

#### 阿里云配置

| 参数 | 类型 | 说明 | 默认值 |
|------|------|------|--------|
| `aliyun.key_id` | string | 阿里云 AccessKey ID | - |
| `aliyun.key_secret` | string | 阿里云 AccessKey Secret | - |
| `aliyun.image_service` | string | 图片审核服务类型 (`baselineCheck` 或 `chat_detection_pro`) | `baselineCheck` |
| `aliyun.image_info_type` | string | 图片审核信息类型 (`customImage`, `textInImage`) | `customImage,textInImage` |

#### VLAI 配置

| 参数 | 类型 | 说明 | 默认值 |
|------|------|------|--------|
| `vlai.provider_id` | string | LLM 提供商 ID（留空使用默认） | `""` |
| `vlai.censor_prompt` | text | 图片审核提示词 | 见默认提示词 |

#### 群聊配置

| 参数 | 类型 | 说明 | 默认值 |
|------|------|------|--------|
| `group_settings[].enabled` | bool | 是否对该群启用审核 | `true` |
| `group_settings[].group_id` | string | 被审核的群号 | - |
| `group_settings[].manage_group_id` | string | 管理群号（接收通知） | - |
| `group_settings[].first_mute_duration` | int | 首次违规禁言时长（秒） | `600` (10分钟) |
| `group_settings[].max_mute_duration` | int | 最大禁言时长（秒） | `2419200` (28天) |
| `group_settings[].mute_multiplier` | number | 禁言时长倍增因子（支持小数如1.5） | `2` |
| `group_settings[].auto_recall` | bool | 是否自动撤回违规图片 | `true` |
| `group_settings[].auto_mute` | bool | 是否自动禁言违规用户 | `true` |
| `group_settings[].base_expire_hours` | int | 缓存基础过期时间（小时） | `2` |
| `group_settings[].max_expire_days` | int | 缓存最大过期时间（天） | `14` |

## 使用说明

### 帮助命令

| 命令 | 说明 | 适用场景 |
|------|------|----------|
| `/审查帮助` | 显示所有可用命令及说明 | 管理群/被审核群 |

### 管理员命令（管理群使用）

| 命令 | 说明 | 示例 |
|------|------|------|
| `/查询违规 [QQ号]` | 查询用户违规记录 | `/查询违规 123456789` |
| `/删除违规 [QQ号]` | 删除指定用户的违规记录 | `/删除违规 123456789` |
| `/审核状态` | 查看插件状态 | `/审核状态` |
| `/清除缓存` | 清除所有自动黑白名单缓存 | `/清除缓存` |
| `/查询名单` | 查询图片在黑白名单中的状态（需引用图片） | `/查询名单` |

### 人工白名单管理

| 命令 | 说明 | 示例 |
|------|------|------|
| `/添加白名单 [原因]` | 添加图片到人工白名单（需引用图片） | `/添加白名单 正常图片` |
| `/移除白名单` | 从人工白名单移除图片（需引用图片） | `/移除白名单` |
| `/清空白名单 确认` | 清空所有人工白名单 | `/清空白名单 确认` |

### 人工黑名单管理

| 命令 | 说明 | 示例 |
|------|------|------|
| `/添加黑名单 [REVIEW/BLOCK] [原因]` | 添加图片到人工黑名单（需引用图片） | `/添加黑名单 BLOCK 色情内容` |
| `/移除黑名单` | 从人工黑名单移除图片（需引用图片） | `/移除黑名单` |
| `/清空黑名单 确认` | 清空所有人工黑名单 | `/清空黑名单 确认` |

### 自动名单管理

| 命令 | 说明 | 示例 |
|------|------|------|
| `/移除自动白名单` | 从自动白名单移除图片（需引用图片） | `/移除自动白名单` |
| `/移除自动黑名单` | 从自动黑名单移除图片（需引用图片） | `/移除自动黑名单` |

### 禁言时长计算

首次违规禁言时长 = `first_mute_duration`

第 N 次违规禁言时长 = `first_mute_duration` × `mute_multiplier`^(N-1)

计算结果会**向上取整到分钟**（例如 15分36秒 → 16分钟）

最大不超过 `max_mute_duration`

## 工作流程

```
收到图片消息
    │
    ▼
提取图片MD5（从消息中获取或下载计算）
    │
    ▼
检查人工白名单 ──命中──▶ 直接放行
    │
    ▼ 未命中
检查人工黑名单 ──命中──▶ 按黑名单结果处理
    │
    ▼ 未命中
检查自动白名单 ──命中──▶ 直接放行
    │
    ▼ 未命中
检查自动黑名单 ──命中──▶ 按黑名单结果处理
    │
    ▼ 未命中
调用审核API (阿里云/VLAI)
    │
    ▼
根据审核结果：
├── 通过 ──▶ 加入自动白名单
└── 复查/拦截 ──▶ 加入自动黑名单 + 禁言 + 通知管理群
```

## 黑白名单说明

### 自动黑白名单

- **自动白名单**：审核通过的图片自动加入，有效期内相同图片无需重复审核
- **自动黑名单**：审核违规的图片自动加入，有效期内直接按违规处理
- 支持配置过期时间，自动清理过期记录
- 可通过配置 `disable_auto_whitelist` 和 `disable_auto_blacklist` 关闭自动机制

### 人工黑白名单

- **人工白名单**：管理员手动添加，优先级最高，永久有效
- **人工黑名单**：管理员手动添加，优先级高于自动名单，永久有效
- 用于人工干预审核结果，修正误判

## 管理员/群主特殊处理

当管理员或群主发送违规图片时，插件会：

1. **不执行处罚** - 不禁言、不撤回图片、不记录违规
2. **发送通知** - 向管理群发送违规通知，标记为「管理员/群主」身份
3. **提示处理措施为"无"** - 通知中明确显示「无（管理员/群主身份，不执行处罚）」

> **注意**：机器人需要是群主才能对管理员执行处罚操作。如果机器人不是群主，即使尝试处罚也会失败，因此插件选择对管理员/群主仅做通知处理。

## 权限说明

| 身份 | 撤回消息 | 禁言用户 | 处理方式 |
|------|----------|----------|----------|
| 普通成员 | ✓ | ✓ | 正常处罚 |
| 管理员 | ✗ | ✗ | 仅通知 |
| 群主 | ✗ | ✗ | 仅通知 |
| 机器人自身 | - | - | 不处理 |

## 数据存储

插件数据存储在 AstrBot 的 `data` 目录下：

- `data/image_review/image_review.db` - SQLite 数据库
  - `whitelist` - 自动白名单
  - `blacklist` - 自动黑名单
  - `manual_whitelist` - 人工白名单
  - `manual_blacklist` - 人工黑名单
  - `violation_records` - 违规记录
  - `user_violation_stats` - 用户违规统计
- `data/image_review/evidence/` - 违规证据图片

## 许可证

本项目基于 [GNU Affero General Public License v3.0 (AGPLv3)](LICENSE) 开源。

```
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published
by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU Affero General Public License for more details.
```

## 相关链接

- [AstrBot 仓库](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot 文档](https://docs.astrbot.app)
- [阿里云内容安全文档](https://help.aliyun.com/document_detail/28417.html)
- [AGPLv3 许可证](https://www.gnu.org/licenses/agpl-3.0.html)
