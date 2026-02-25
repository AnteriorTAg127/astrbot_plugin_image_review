# astrbot-plugin-image-review

图片审核插件 / An image review plugin for AstrBot

[![License](https://img.shields.io/github/license/AstrBotDevs/astrbot-plugin-image-review)](LICENSE)
[![Version](https://img.shields.io/badge/version-v1.0.0-blue)](metadata.yaml)

> [!NOTE]
> 这是一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 提供图片内容审核功能的插件。
>
> [AstrBot](https://github.com/AstrBotDevs/AstrBot) 是一个支持多个主流即时通讯平台的智能助手，包括 QQ、Telegram、飞书、钉钉、Slack、Discord 等。本插件为 AstrBot 提供图片内容审核能力，帮助群管理员过滤不当图片内容。

## 功能特性

- **图片内容审核** - 基于阿里云内容安全 API 进行图片审核
- **智能缓存机制** - 通过 MD5 缓存已审核图片，重复图片无需再次审核
- **黑白名单** - 自动将审核结果加入黑白名单，支持过期自动清理
- **违规处理** - 自动撤回违规图片并禁言用户
- **管理群通知** - 实时推送违规通知到管理群
- **用户违规统计** - 记录用户违规次数，禁言时长按次数累积

## 支持平台

- [x] QQ (aiocqhttp)
- [ ] Telegram (开发中)
- [ ] 其他平台 (开发中)

## 安装说明

### 方式一：通过 AstrBot 插件市场安装

在 AstrBot 管理面板的插件市场搜索"图片审核"并安装。

### 方式二：手动安装

```bash
# 克隆插件仓库
git clone https://github.com/AstrBotDevs/astrbot-plugin-image-review.git

# 复制到 AstrBot 插件目录
cp -r astrbot-plugin-image-review <astrbot_plugins_path>/

# 安装依赖
cd <astrbot_plugins_path>/astrbot-plugin-image_review
pip install -r requirements.txt
```

## 配置说明

### 阿里云配置

1. 登录 [阿里云内容安全控制台](https://content safety.console.aliyun.com/)
2. 开通内容安全服务
3. 创建 AccessKey，获取 KeyId 和 KeySecret

### 群聊配置

在插件配置中添加 `group_settings`：

```json
{
  "image_censor_provider": "Aliyun",
  "enable_image_censor": true,
  "aliyun": {
    "key_id": "your_key_id",
    "key_secret": "your_key_secret"
  },
  "cache_settings": {
    "base_expire_hours": 2,
    "max_expire_days": 14
  },
  "group_settings": [
    {
      "enabled": true,
      "group_id": "123456789",
      "manage_group_id": "987654321",
      "first_mute_duration": 600,
      "max_mute_duration": 2419200,
      "mute_multiplier": 2,
      "auto_recall": true,
      "base_expire_hours": 2,
      "max_expire_days": 14
    }
  ]
}
```

### 配置参数说明

| 参数 | 类型 | 说明 | 默认值 |
|------|------|------|--------|
| `image_censor_provider` | string | 图片审核提供商 | `Aliyun` |
| `enable_image_censor` | bool | 是否启用图片审核 | `true` |
| `aliyun.key_id` | string | 阿里云 AccessKey ID | - |
| `aliyun.key_secret` | string | 阿里云 AccessKey Secret | - |
| `cache_settings.base_expire_hours` | int | 缓存基础过期时间（小时） | `2` |
| `cache_settings.max_expire_days` | int | 缓存最大过期时间（天） | `14` |
| `group_settings[].group_id` | string | 被审核的群号 | - |
| `group_settings[].manage_group_id` | string | 管理群号（接收通知） | - |
| `group_settings[].first_mute_duration` | int | 首次违规禁言时长（秒） | `600` |
| `group_settings[].max_mute_duration` | int | 最大禁言时长（秒） | `2419200` (28天) |
| `group_settings[].mute_multiplier` | int | 禁言时长倍增因子 | `2` |
| `group_settings[].auto_recall` | bool | 是否自动撤回违规图片 | `true` |

## 使用说明

### 管理员命令（管理群使用）

| 命令 | 说明 | 示例 |
|------|------|------|
| `/查询违规 [QQ号]` | 查询用户违规记录 | `/查询违规 123456789` |
| `/审核状态` | 查看插件状态 | `/审核状态` |

### 禁言时长计算

首次违规禁言时长 = `first_mute_duration`

第 N 次违规禁言时长 = `first_mute_duration` × `mute_multiplier`^(N-1)

最大不超过 `max_mute_duration`

## 工作流程

```
收到图片消息
    │
    ▼
提取图片MD5（从消息中获取或下载计算）
    │
    ▼
检查白名单 ──命中──▶ 直接放行
    │
    ▼ 未命中
检查黑名单 ──命中──▶ 按黑名单结果处理
    │
    ▼ 未命中
调用阿里云API审核
    │
    ▼
根据审核结果：
├── 通过 ──▶ 加入白名单
├── 复查/拦截 ──▶ 加入黑名单 + 禁言 + 通知管理群
```

## 数据存储

插件数据存储在 AstrBot 的 `data` 目录下：

- `data/image_review/image_review.db` - SQLite 数据库
  - `whitelist` - 白名单
  - `blacklist` - 黑名单
  - `violation_records` - 违规记录
  - `user_violation_stats` - 用户违规统计
  - `message_cache` - 消息缓存

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
