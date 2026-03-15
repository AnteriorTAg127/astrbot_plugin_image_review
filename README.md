# astrbot-plugin-image-review

图片审核插件 / An image review plugin for AstrBot

[!\[License\](https://img.shields.io/github/license/AnteriorTAg127/astrbot\_plugin\_image\_review null)](LICENSE)
[!\[Version\](https://img.shields.io/badge/version-v1.3.6-blue null)](metadata.yaml)

> \[!IMPORTANT]
> **本代码由 AI 生成，不保证代码质量，如有问题请多提 issues。**

> \[!NOTE]
> 这是一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 提供图片内容审核功能的插件。
>
> [AstrBot](https://github.com/AstrBotDevs/AstrBot) 是一个支持多个主流即时通讯平台的智能助手，包括 QQ、Telegram、飞书、钉钉、Slack、Discord 等。本插件为 AstrBot 提供图片内容审核能力，帮助群管理员过滤不当图片内容。

## 更新日志

### v1.3.7

- **新增相似图片匹配功能** - 支持基于感知哈希（pHash/dHash）和汉明距离的相似图片检测
  - 在 MD5 精确匹配失败后，可进行相似图片匹配
  - 支持自定义哈希算法（phash/dhash）和汉明距离阈值
  - ⚠️ **警告**：此功能可能导致误判，例如两张图片整体相似但某一小部分包含违规内容时，正常图片可能被误判为违规
  - 建议仅在必要时开启，并结合人工审核使用

### v1.3.6

- **修复指令重复注册问题** - 将命令处理逻辑内联到 main.py，解决指令重复注册的 bug
- **代码重构优化** - 移除 command\_handlers.py，简化代码结构

### v1.3.5

- **新增管理命令权限验证** - 可配置是否要求管理员/群主身份才能执行敏感命令，增强安全性
- **优化图片下载流程** - 减少重复下载，优化资源使用
- **修复动图检测耦合问题** - 动图增强检测现在仅在 VLAI 提供商下生效，避免逻辑混乱

### v1.3.0

- **新增转发消息图片检测** - 支持检测合并转发消息中的图片内容
- **新增抽检功能** - 转发消息图片过多时可按比例抽检，避免资源浪费
- **配置增强** - 新增转发消息检测相关配置项

### v1.2.0

- **新增智能审查模式** - 支持定时审查和管理在线检测，夜间强制检查，白天智能补漏
- **新增管理员列表缓存** - 缓存管理员身份，避免频繁查询
- **审核状态增强** - 显示审查模式、自动黑白名单数量等详细信息

### v1.1.4

- **新增动图增强检测** - 支持对 GIF/动图进行多帧检测，可配置逐帧分开检查或批量合并检查模式
- **新增 QQ 自带表情包跳过** - 可配置跳过 QQ 官方表情包检测，避免误审
- **新增动图检测专用配置** - 支持单独配置动图检测的 LLM 提供商、采样帧数、检测模式等

## 功能特性

- **双模式图片审核** - 支持阿里云内容安全 API 和 VLAI 视觉语言模型两种审核方式
- **相似图片匹配** - 基于感知哈希（pHash/dHash）和汉明距离的相似图片检测，可识别经过简单处理的违规图片
- **动图增强检测** - 对多帧图片(GIF/动图)进行增强检测，抽取多帧进行审核
- **转发消息检测** - 支持检测合并转发消息中的图片内容，可配置抽检策略
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

1. **Aliyun (阿里云)** - 基于阿里云内容安全服务，常规图片审核误判率低，需要阿里云账号
2. **VLAI (视觉语言模型)** - 基于 AstrBot 的 AI 能力，使用视觉语言模型进行审核，无需额外账号，不建议使用推理模型，会显著增加延迟。

**一般还是建议使用VLAI检测，尤其是冷门圈子的违规图像使用aliyun很难检测出来。但是误判率稍高，需要自己调整提示词。**

### 阿里云配置

1. 登录 [阿里云内容安全控制台](https://yundun.console.aliyun.com/)
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
  "image_censor_provider": "VLAI",
  "enable_image_censor": true,
  "enable_gif_enhanced_detection": true,
  "skip_qq_builtin_emoji": true,
  "enable_forward_image_censor": true,
  "forward_image_sample_threshold": 10,
  "forward_image_sample_rate": 0.5,
  "disable_auto_whitelist": false,
  "disable_auto_blacklist": false,
  "enable_similarity_match": false,
  "similarity_hash_algorithm": "phash",
  "similarity_hamming_threshold": 80,
  "aliyun": {
    "key_id": "your_key_id",
    "key_secret": "your_key_secret",
    "image_service": "baselineCheck",
    "image_info_type": "customImage,textInImage"
  },
  "vlai": {
    "provider_id": "",
    "backup_provider_id": "",
    "max_image_size": 640,
    "censor_prompt": "请分析这张图片是否有显著色情违规内容..."
  },
  "gif_enhanced": {
    "provider_id": "",
    "backup_provider_id": "",
    "max_image_size": 640,
    "frame_sample_count": 3,
    "detection_mode": "separate",
    "censor_prompt": "请分析这张图片是否有显著色情违规内容...",
    "batch_censor_prompt": "我将发送给你多张图片，这些是同一张动图的不同帧..."
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
      "max_expire_days": 14,
      "enable_auto_censor": true,
      "auto_censor_schedule": "23:00-09:00",
      "auto_censor_no_admin_minutes": 30
    }
  ]
}
```

### 配置参数说明

#### 基础配置

| 参数                                | 类型     | 说明                          | 默认值      |
| --------------------------------- | ------ | --------------------------- | -------- |
| `image_censor_provider`           | string | 图片审核提供商 (`Aliyun` 或 `VLAI`) | `Aliyun` |
| `enable_image_censor`             | bool   | 是否启用图片审核                    | `true`   |
| `enable_gif_enhanced_detection`   | bool   | 是否启用动图增强检测（仅 VLAI 提供商生效）    | `false`  |
| `skip_qq_builtin_emoji`           | bool   | 是否跳过QQ自带表情包                 | `true`   |
| `enable_forward_image_censor`     | bool   | 是否启用转发消息图片检测                | `false`  |
| `forward_image_sample_threshold`  | int    | 转发消息图片抽检阈值，0表示全部检查          | `0`      |
| `forward_image_sample_rate`       | number | 转发消息图片抽检率，0.0-1.0           | `0.5`    |
| `enable_admin_permission_check`   | bool   | 是否开启管理命令权限验证                | `false`  |
| `vlai.provider_id`                | string | 图片审核 LLM 提供商                | `""`     |
| `vlai.backup_provider_id`         | string | 图片审核备用 LLM 提供商              | `""`     |
| `gif_enhanced.provider_id`        | string | 动图检测 LLM 提供商                | `""`     |
| `gif_enhanced.backup_provider_id` | string | 动图检测备用 LLM 提供商              | `""`     |
| `disable_auto_whitelist`          | bool   | 关闭自动白名单机制                   | `false`  |
| `disable_auto_blacklist`          | bool   | 关闭自动黑名单机制                   | `false`  |
| `enable_similarity_match`         | bool   | 是否启用相似图片匹配                 | `false`  |
| `similarity_hash_algorithm`       | string | 相似图片哈希算法 (`phash`/`dhash`)   | `phash`  |
| `similarity_hamming_threshold`    | int    | 相似度汉明距离阈值 (建议60-100)      | `80`     |

#### 阿里云配置

| 参数                       | 类型     | 说明                                                | 默认值                       |
| ------------------------ | ------ | ------------------------------------------------- | ------------------------- |
| `aliyun.key_id`          | string | 阿里云 AccessKey ID                                  | -                         |
| `aliyun.key_secret`      | string | 阿里云 AccessKey Secret                              | -                         |
| `aliyun.image_service`   | string | 图片审核服务类型 (`baselineCheck` 或 `chat_detection_pro`) | `baselineCheck`           |
| `aliyun.image_info_type` | string | 图片审核信息类型 (`customImage`, `textInImage`)           | `customImage,textInImage` |

#### VLAI 配置

| 参数                        | 类型     | 说明                       | 默认值    |
| ------------------------- | ------ | ------------------------ | ------ |
| `vlai.provider_id`        | string | LLM 提供商 ID（留空使用默认）       | `""`   |
| `vlai.backup_provider_id` | string | 备用 LLM 提供商 ID（主提供商失败时使用） | `""`   |
| `vlai.max_image_size`     | int    | 图片缩放最大边长(像素)，0表示不缩放      | `640`  |
| `vlai.censor_prompt`      | text   | 图片审核提示词                  | 见默认提示词 |

> **备用提供商说明**: 当主 LLM 提供商调用失败（如超时、错误等）时，会自动切换到备用提供商进行审核。如果未配置备用提供商，则直接返回错误。

#### 动图增强检测配置

| 参数                                 | 类型     | 说明                            | 默认值        |
| ---------------------------------- | ------ | ----------------------------- | ---------- |
| `gif_enhanced.provider_id`         | string | 动图检测专用 LLM 提供商 ID             | `""`       |
| `gif_enhanced.backup_provider_id`  | string | 动图检测备用 LLM 提供商 ID             | `""`       |
| `gif_enhanced.max_image_size`      | int    | 动图帧缩放最大边长(像素)                 | `640`      |
| `gif_enhanced.frame_sample_count`  | int    | 采样帧数，建议3-5帧                   | `3`        |
| `gif_enhanced.detection_mode`      | string | 检测模式 (`separate`逐帧/`batch`批量) | `separate` |
| `gif_enhanced.censor_prompt`       | text   | 逐帧检查模式提示词                     | 见默认提示词     |
| `gif_enhanced.batch_censor_prompt` | text   | 批量检查模式提示词                     | 见默认提示词     |

> **备用提供商说明**: 动图检测同样支持备用提供商机制，当主提供商失败时会自动切换到备用提供商。

### 相似图片匹配

插件支持基于感知哈希（Perceptual Hash）的相似图片匹配功能，可在 MD5 精确匹配失败后检测视觉上相似的图片。

#### ⚠️ 重要警告

**此功能可能导致误判，请谨慎使用：**

- **差分误判问题**：如果两张图片整体相似，但其中一张在某一小部分包含违规内容（如添加了色情贴纸），另一张正常的图片可能被误判为违规
- **阈值设置影响**：阈值过低可能导致漏检，阈值过高可能导致误判
- **建议**：仅在必要时开启，并结合人工审核使用

#### 工作原理

```
┌─────────────────────────────────────────────────────────────┐
│                    相似图片匹配流程                          │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────────┐
                    │   MD5精确匹配失败    │
                    └─────────────────────┘
                              │
                              ▼
                    ┌─────────────────────┐
                    │  计算图片感知哈希    │
                    │  (phash 或 dhash)   │
                    └─────────────────────┘
                              │
                              ▼
                    ┌─────────────────────┐
                    │  查询数据库中的哈希  │
                    │  计算汉明距离        │
                    └─────────────────────┘
                              │
                              ▼
                    ┌─────────────────────┐
                    │  汉明距离 ≤ 阈值？   │
                    └─────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
       ┌─────────────┐                 ┌─────────────┐
       │     是      │                 │     否      │
       │  判定为相似  │                 │  继续API审核 │
       └─────────────┘                 └─────────────┘
```

#### 哈希算法选择

| 算法 | 特点 | 适用场景 |
|------|------|----------|
| `phash` | 对缩放、旋转、亮度变化更鲁棒 | 检测经过简单处理的相似图片（推荐） |
| `dhash` | 计算速度快，对平移敏感 | 快速检测，对处理后的图片敏感度较低 |

#### 汉明距离阈值建议

基于 24x24=576 位哈希的推荐阈值：

| 阈值 | 效果 | 建议 |
|------|------|------|
| 40-60 | 非常严格，只有几乎相同的图片才匹配 | 误报率低，但可能漏检 |
| 80 | 平衡，通常认为是相似图片的范围 | **推荐起始值** |
| 100+ | 宽松，可能匹配差异较大的图片 | 误报率高，谨慎使用 |

> **注意**：哈希尺寸从 8x8（64位）调整为 24x24（576位），阈值也相应调整。原 8x8 的阈值 10 约等于 24x24 的阈值 90。

#### 相似图片匹配配置示例

**场景：检测经过简单处理的违规图片**

```json
{
  "enable_similarity_match": true,
  "similarity_hash_algorithm": "phash",
  "similarity_hamming_threshold": 80
}
```

**效果说明：**
- 开启相似图片匹配
- 使用 phash 算法（对缩放/旋转更鲁棒）
- 使用 24x24=576 位哈希，更高的精度
- 汉明距离 ≤ 80 的图片被认为是相似的

#### 注意事项

1. **性能影响**：开启后会增加图片哈希计算和数据库查询开销
2. **存储增加**：每张审核过的图片会额外存储 phash/dhash 值
3. **误判风险**：请根据实际效果调整阈值，如频繁误判请降低阈值或关闭功能
4. **人工干预**：发现误判时，及时使用 `/添加白名单` 命令将误判图片加入人工白名单

#### 群聊配置

| 参数                                              | 类型     | 说明                    | 默认值             |
| ----------------------------------------------- | ------ | --------------------- | --------------- |
| `group_settings[].enabled`                      | bool   | 是否对该群启用审核             | `true`          |
| `group_settings[].group_id`                     | string | 被审核的群号                | -               |
| `group_settings[].manage_group_id`              | string | 管理群号（接收通知）            | -               |
| `group_settings[].first_mute_duration`          | int    | 首次违规禁言时长（秒）           | `600` (10分钟)    |
| `group_settings[].max_mute_duration`            | int    | 最大禁言时长（秒）             | `2419200` (28天) |
| `group_settings[].mute_multiplier`              | number | 禁言时长倍增因子（支持小数如1.5）    | `2`             |
| `group_settings[].auto_recall`                  | bool   | 是否自动撤回违规图片            | `true`          |
| `group_settings[].auto_mute`                    | bool   | 是否自动禁言违规用户            | `true`          |
| `group_settings[].base_expire_hours`            | int    | 缓存基础过期时间（小时）          | `2`             |
| `group_settings[].max_expire_days`              | int    | 缓存最大过期时间（天）           | `14`            |
| `group_settings[].enable_auto_censor`           | bool   | 启用智能审查模式              | `false`         |
| `group_settings[].auto_censor_schedule`         | string | 强制审查时间段 (hh:mm-hh:mm) | `""`            |
| `group_settings[].auto_censor_no_admin_minutes` | int    | 管理在线检测时间（分钟）          | `0`             |

## 智能审查模式

插件支持两种审查模式：

### 全量审查模式（默认）

不启用 `enable_auto_censor` 时，对所有已配置群聊的图片进行全量审查。

### 智能审查模式

启用 `enable_auto_censor: true` 后，插件会根据时间段和管理员在线状态智能决定是否审查：

```
┌─────────────────────────────────────────────────────────────┐
│              智能审查模式开关 (enable_auto_censor)            │
└─────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
       ┌─────────────┐                 ┌─────────────┐
       │   关闭       │                 │    开启     │
       │  (默认)      │                 │  (智能模式)  │
       └─────────────┘                 └─────────────┘
              │                               │
              ▼                               ▼
        ┌───────────┐                 ┌─────────────────────┐
        │ 全量审查   │                 │   强制审查时间段     │
        │ 始终检查   │                 │ (auto_censor_schedule)│
        │           │                 │   例: 23:00-09:00   │
        └───────────┘                 │   （夜间管理睡觉）   │
                                      └─────────────────────┘
                                                    │
                                    ┌───────────────┴───────────────┐
                                    ▼                               ▼
                            ┌─────────────┐                 ┌─────────────────────┐
                            │  在时间段内  │                 │    不在时间段内      │
                            │  （夜间）   │                 │    （白天管理可能在）  │
                            └─────────────┘                 └─────────────────────┘
                                    │                               │
                                    ▼                               ▼
                            ┌─────────────┐                 ┌─────────────────────┐
                            │   始终检查   │                 │  管理在线检测        │
                            │  （值守）   │                 │(auto_censor_no_admin │
                            └─────────────┘                 │    _minutes)         │
                                                            │    例: 30分钟        │
                                                            └─────────────────────┘
                                                                          │
                                                          ┌───────────────┴───────────────┐
                                                          ▼                               ▼
                                                  ┌─────────────┐                 ┌─────────────┐
                                                  │ 管理x分钟内  │                 │ 管理x分钟未  │
                                                  │   有发言    │                 │   发言      │
                                                  │  （管理在）  │                 │  （管理不在） │
                                                  └─────────────┘                 └─────────────┘
                                                          │                               │
                                                          ▼                               ▼
                                                  ┌─────────────┐                 ┌─────────────┐
                                                  │   关闭检查   │                 │   开启检查   │
                                                  │  （不打扰）  │                 │  （自动补漏） │
                                                  └─────────────┘                 └─────────────┘
```

### 智能审查配置示例

**场景：夜间强制检查，白天智能检测**

```json
{
  "group_settings": [{
    "enabled": true,
    "group_id": "123456789",
    "manage_group_id": "987654321",
    "enable_auto_censor": true,
    "auto_censor_schedule": "23:00-09:00",
    "auto_censor_no_admin_minutes": 30
  }]
}
```

**效果说明：**

- **23:00-09:00（夜间）**：始终检查（管理睡觉，自动值守）
- **09:00-23:00（白天）**：
  - 管理30分钟内有发言 → 关闭检查（管理在，不打扰）
  - 管理30分钟未发言 → 开启检查（管理不在，自动补漏）

> **注意**：跨天时间格式也支持，如 `22:00-08:00` 表示晚上22点到次日8点

### 转发消息图片检测

插件支持检测合并转发消息（如群聊的合并转发消息）中的图片内容。

#### 抽检机制

当转发消息中包含大量图片时，可以启用抽检功能以节省资源：

```
┌─────────────────────────────────────────────────────────────┐
│              转发消息图片检测开关 (enable_forward_image_censor)│
└─────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
       ┌─────────────┐                 ┌─────────────┐
       │    关闭      │                 │    开启     │
       │  不检测转发  │                 │  检测转发   │
       └─────────────┘                 └─────────────┘
                                                │
                                                ▼
                              ┌─────────────────────────────────┐
                              │   转发消息图片数量 vs 阈值       │
                              │   (forward_image_sample_threshold)│
                              └─────────────────────────────────┘
                                                │
                    ┌───────────────────────────┴───────────────────────────┐
                    ▼                                                       ▼
            ┌───────────────┐                                       ┌───────────────┐
            │  数量 ≤ 阈值   │                                       │  数量 > 阈值   │
            │  (或阈值为0)   │                                       │               │
            └───────────────┘                                       └───────────────┘
                    │                                                       │
                    ▼                                                       ▼
            ┌───────────────┐                                       ┌───────────────┐
            │   全部检查     │                                       │   按比例抽检   │
            │               │                                       │  (sample_rate) │
            └───────────────┘                                       └───────────────┘
```

#### 转发消息检测配置示例

**场景1：全部检查（严格模式）**

```json
{
  "enable_forward_image_censor": true,
  "forward_image_sample_threshold": 0,
  "forward_image_sample_rate": 0.5
}
```

**效果**：转发消息中的所有图片都会被检测。

**场景2：抽检模式（节省资源）**

```json
{
  "enable_forward_image_censor": true,
  "forward_image_sample_threshold": 10,
  "forward_image_sample_rate": 0.3
}
```

**效果**：

- 转发消息中图片 ≤ 10 张：全部检查
- 转发消息中图片 > 10 张：抽检 30% 的图片

## 使用说明

### 帮助命令

| 命令      | 说明          | 适用场景     |
| ------- | ----------- | -------- |
| `/审查帮助` | 显示所有可用命令及说明 | 管理群/被审核群 |

### 管理员命令（管理群使用）

| 命令            | 说明                   | 示例                |
| ------------- | -------------------- | ----------------- |
| `/查询违规 [QQ号]` | 查询用户违规记录             | `/查询违规 123456789` |
| `/删除违规 [QQ号]` | 删除指定用户的违规记录          | `/删除违规 123456789` |
| `/审核状态`       | 查看插件状态               | `/审核状态`           |
| `/清除缓存`       | 清除所有自动黑白名单缓存         | `/清除缓存`           |
| `/查询名单`       | 查询图片在黑白名单中的状态（需引用图片） | `/查询名单`           |

### 人工白名单管理

| 命令            | 说明                | 示例            |
| ------------- | ----------------- | ------------- |
| `/添加白名单 [原因]` | 添加图片到人工白名单（需引用图片） | `/添加白名单 正常图片` |
| `/移除白名单`      | 从人工白名单移除图片（需引用图片） | `/移除白名单`      |
| `/清空白名单 确认`   | 清空所有人工白名单         | `/清空白名单 确认`   |

### 人工黑名单管理

| 命令                           | 说明                | 示例                  |
| ---------------------------- | ----------------- | ------------------- |
| `/添加黑名单 [REVIEW/BLOCK] [原因]` | 添加图片到人工黑名单（需引用图片） | `/添加黑名单 BLOCK 色情内容` |
| `/移除黑名单`                     | 从人工黑名单移除图片（需引用图片） | `/移除黑名单`            |
| `/清空黑名单 确认`                  | 清空所有人工黑名单         | `/清空黑名单 确认`         |

### 自动名单管理

| 命令         | 说明                | 示例         |
| ---------- | ----------------- | ---------- |
| `/移除自动白名单` | 从自动白名单移除图片（需引用图片） | `/移除自动白名单` |
| `/移除自动黑名单` | 从自动黑名单移除图片（需引用图片） | `/移除自动黑名单` |

### 禁言时长计算

首次违规禁言时长 = `first_mute_duration`

第 N 次违规禁言时长 = `first_mute_duration` × `mute_multiplier`^(N-1)

计算结果会**向上取整到分钟**（例如 15分36秒 → 16分钟）

最大不超过 `max_mute_duration`

## 工作流程

### 普通图片消息

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
相似图片匹配（如启用）──命中──▶ 按相似结果处理
    │
    ▼ 未命中
调用审核API (阿里云/VLAI)
    │
    ▼
根据审核结果：
├── 通过 ──▶ 加入自动白名单 + 保存哈希（如启用相似匹配）
└── 复查/拦截 ──▶ 加入自动黑名单 + 保存哈希 + 禁言 + 通知管理群
```

### 转发消息图片

```
收到转发消息
    │
    ▼
提取转发消息中的所有图片
    │
    ▼
图片数量超过阈值？
    │
    ├── 否 ──▶ 全部检查
    │
    └── 是 ──▶ 按比例抽检
                    │
                    ▼
            对选中的图片进行审核
                    │
                    ▼
            发现违规图片？
                │
                ├── 否 ──▶ 正常处理
                │
                └── 是 ──▶ 按违规处理（禁言 + 通知管理群）
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

| 身份    | 撤回消息 | 禁言用户 | 处理方式 |
| ----- | ---- | ---- | ---- |
| 普通成员  | ✓    | ✓    | 正常处罚 |
| 管理员   | ✗    | ✗    | 仅通知  |
| 群主    | ✗    | ✗    | 仅通知  |
| 机器人自身 | -    | -    | 不处理  |

## 数据存储

插件数据存储在 AstrBot 的 `data` 目录下：

- `data/image_review/image_review.db` - SQLite 数据库
  - `whitelist` - 自动白名单
  - `blacklist` - 自动黑名单
  - `manual_whitelist` - 人工白名单
  - `manual_blacklist` - 人工黑名单
  - `image_hashes` - 图片哈希值（用于相似图片匹配）
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

