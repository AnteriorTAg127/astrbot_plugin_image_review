"""
图片审核插件主模块
提供图片内容审核、违规处理、管理群通知等功能
"""

import asyncio
import math
import os
import random
import re
from datetime import datetime, timedelta
from typing import Any

import aiofiles

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .censor_base import CensorError
from .censor_flow import CensorFlow
from .database import DatabaseManager, RiskLevel


def _sanitize_filename(filename: str) -> str:
    """
    清理文件名，防止路径遍历攻击

    Args:
        filename: 原始文件名或路径片段

    Returns:
        清理后的安全文件名
    """
    if not filename:
        return "unknown"

    # 移除路径分隔符和特殊字符
    # 替换 Windows 和 Unix 的路径分隔符
    sanitized = filename.replace("\\", "_").replace("/", "_")

    # 移除 .. 防止路径遍历
    sanitized = sanitized.replace("..", "_")

    # 移除其他危险字符
    sanitized = re.sub(r'[<>:"|?*]', "_", sanitized)

    # 限制长度
    if len(sanitized) > 100:
        sanitized = sanitized[:100]

    return sanitized or "unknown"


@register(
    "image_review",
    "AnteriorTAg127",
    "图片审核插件，提供图片内容审核、违规处理、管理群通知等功能",
    "1.3.0",
)
class ImageReviewPlugin(Star):
    """图片审核插件主类"""

    def __init__(self, context: Context, config: dict[str, Any]):
        """
        初始化插件

        Args:
            context: AstrBot上下文
            config: 插件配置
        """
        super().__init__(context)
        self._config = config

        # 数据目录（使用AstrBot规范的插件数据目录）
        self._data_dir = os.path.join(get_astrbot_plugin_data_path(), "image_review")
        os.makedirs(self._data_dir, exist_ok=True)

        # 证据图片保存目录
        self._evidence_dir = os.path.join(self._data_dir, "evidence")
        os.makedirs(self._evidence_dir, exist_ok=True)

        # 初始化数据库
        self._db = DatabaseManager(self._data_dir)

        # 群聊配置映射 {group_id: {manage_group_id, violation_settings, cache_settings}}
        self._group_config: dict[str, dict] = {}
        self._load_group_config()

        # 审核流程管理器（延迟初始化）
        self._censor_flow: CensorFlow | None = None

        # 定时任务引用
        self._cleanup_task: asyncio.Task | None = None

        # 管理员列表缓存 {group_id: {"admins": set(), "expires_at": datetime}}
        self._admin_cache: dict[str, dict] = {}
        self._admin_cache_ttl = 300  # 管理员缓存5分钟

        # 群聊最后管理员发言时间 {group_id: datetime}
        self._last_admin_message_time: dict[str, datetime] = {}

        # 群聊审查状态缓存 {group_id: {"enabled": bool, "reason": str}}
        self._censor_status_cache: dict[str, dict] = {}

        logger.debug("图片审核插件初始化完成")

    def _load_group_config(self):
        """加载群聊配置"""
        group_settings = self._config.get("group_settings", [])

        # 如果是旧格式的dict（兼容旧版本配置）
        if isinstance(group_settings, dict):
            group_settings = [group_settings]

        for i, setting in enumerate(group_settings):
            # 确保是有效配置
            if not isinstance(setting, dict):
                continue

            # 跳过非启用的配置（兼容template_list格式）
            if not setting.get("enabled", True):
                continue
            group_id = str(setting.get("group_id", ""))
            manage_group_id = str(setting.get("manage_group_id", ""))
            if group_id and manage_group_id:
                # 验证并规范化配置值，带异常处理
                def safe_float(value, default, min_val=None, max_val=None):
                    """安全地将值转换为浮点数"""
                    try:
                        result = float(value) if value is not None else default
                        if min_val is not None:
                            result = max(min_val, result)
                        if max_val is not None:
                            result = min(max_val, result)
                        return result
                    except (ValueError, TypeError):
                        logger.warning(
                            f"配置值 '{value}' 无法转换为浮点数，使用默认值 {default}"
                        )
                        return default

                def safe_int(value, default, min_val=None, max_val=None):
                    """安全地将值转换为整数"""
                    try:
                        result = int(value) if value is not None else default
                        if min_val is not None:
                            result = max(min_val, result)
                        if max_val is not None:
                            result = min(max_val, result)
                        return result
                    except (ValueError, TypeError):
                        logger.warning(
                            f"配置值 '{value}' 无法转换为整数，使用默认值 {default}"
                        )
                        return default

                first_mute_duration = safe_int(
                    setting.get("first_mute_duration"), 600, min_val=0
                )
                max_mute_duration = safe_int(
                    setting.get("max_mute_duration"),
                    2419200,
                    min_val=0,
                    max_val=2419200,
                )
                mute_multiplier = safe_float(
                    setting.get("mute_multiplier"), 2, min_val=1
                )
                base_expire_hours = safe_int(
                    setting.get("base_expire_hours"), 2, min_val=1
                )
                max_expire_days = safe_int(
                    setting.get("max_expire_days"), 14, min_val=1, max_val=365
                )

                # 解析自动审查配置
                enable_auto_censor = setting.get("enable_auto_censor", False)
                auto_censor_schedule = setting.get("auto_censor_schedule", "")
                schedule_parsed = self._parse_schedule(auto_censor_schedule)

                # 保存每个群的完整配置
                self._group_config[group_id] = {
                    "manage_group_id": manage_group_id,
                    "first_mute_duration": first_mute_duration,
                    "max_mute_duration": max_mute_duration,
                    "mute_multiplier": mute_multiplier,
                    "auto_recall": setting.get("auto_recall", True),
                    "auto_mute": setting.get("auto_mute", True),
                    "base_expire_hours": base_expire_hours,
                    "max_expire_days": max_expire_days,
                    "enable_auto_censor": enable_auto_censor,
                    "auto_censor_schedule": auto_censor_schedule,
                    "schedule_parsed": schedule_parsed,
                    "auto_censor_no_admin_minutes": setting.get(
                        "auto_censor_no_admin_minutes", 0
                    ),
                }
                logger.info(f"已加载群聊配置: 群{group_id} -> 管理群{manage_group_id}")

    async def initialize(self):
        """插件初始化"""
        try:
            # 初始化审核流程管理器，传入 context 以支持 VLAI 审核器
            self._censor_flow = CensorFlow(self._config, self._db, self.context)
            await self._censor_flow.initialize()

            if self._censor_flow.is_image_censor_enabled():
                logger.info("图片审核插件初始化成功，已启用图片审核")
            else:
                logger.warning("图片审核插件初始化完成，但未启用图片审核（请检查配置）")

            # 启动定时清理任务（每天执行一次）
            self._cleanup_task = asyncio.create_task(self._cleanup_expired_entries())
        except Exception as e:
            logger.error(f"图片审核插件初始化失败: {e}")

    async def _cleanup_expired_entries(self):
        """定时清理过期的黑白名单条目"""
        while True:
            try:
                # 每天执行一次清理
                await asyncio.sleep(24 * 60 * 60)  # 24小时
                await self._db.clean_expired_list_entries()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定时清理过期黑白名单异常: {e}")
                # 发生异常后等待1小时再重试
                await asyncio.sleep(60 * 60)

    async def terminate(self):
        """插件销毁"""
        # 取消定时清理任务
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        if self._censor_flow:
            await self._censor_flow.close()
        logger.info("图片审核插件已卸载")

    def _get_group_config(self, group_id: str) -> dict | None:
        """
        获取群聊的完整配置

        Args:
            group_id: 群ID

        Returns:
            群聊配置字典，未配置则返回None
        """
        return self._group_config.get(group_id)

    def _get_manage_group_id(self, group_id: str) -> str | None:
        """
        获取群聊对应的管理群ID

        Args:
            group_id: 群ID

        Returns:
            管理群ID，未配置则返回None
        """
        config = self._group_config.get(group_id)
        return config["manage_group_id"] if config else None

    def _get_group_ids_by_manage_group(self, manage_group_id: str) -> list[str]:
        """
        根据管理群ID反向查找所有关联的主群ID列表

        Args:
            manage_group_id: 管理群ID

        Returns:
            主群ID列表，未找到则返回空列表
        """
        group_ids = []
        for group_id, config in self._group_config.items():
            if config.get("manage_group_id") == manage_group_id:
                group_ids.append(group_id)
        return group_ids

    @staticmethod
    def _is_valid_md5(md5_hex: str) -> bool:
        """
        验证字符串是否为有效的MD5格式

        Args:
            md5_hex: 待验证的字符串

        Returns:
            是否为有效的32位十六进制MD5字符串
        """
        if not md5_hex or len(md5_hex) != 32:
            return False
        try:
            int(md5_hex, 16)
            return True
        except ValueError:
            return False

    def _extract_image_md5(
        self, event: AstrMessageEvent, image_comp: Comp.Image
    ) -> str | None:
        """
        从图片组件中提取图片的MD5值

        从图片文件名中提取MD5，文件名格式通常为: 306AED23E3B7AA81B51A3B2A6FAAAF73.jpg

        Args:
            event: 消息事件
            image_comp: 图片组件

        Returns:
            图片MD5字符串，如果无法获取则返回None
        """
        try:
            if image_comp.file:
                # 从文件名中提取MD5（去掉扩展名）
                file_name = image_comp.file
                # 移除可能的URL参数
                if "?" in file_name:
                    file_name = file_name.split("?")[0]
                # 移除路径，只保留文件名
                file_name = os.path.basename(file_name)
                # 移除扩展名，获取MD5
                md5_hex = os.path.splitext(file_name)[0]
                # 验证MD5格式（32位十六进制字符串）
                if self._is_valid_md5(md5_hex):
                    return md5_hex.lower()
        except Exception as e:
            logger.debug(f"提取图片MD5时发生异常: {e}")
        return None

    def _is_group_enabled(self, group_id: str) -> bool:
        """
        检查群聊是否启用了图片审核

        Args:
            group_id: 群ID

        Returns:
            是否启用
        """
        return group_id in self._group_config

    def _parse_schedule(
        self, schedule_str: str
    ) -> tuple[datetime.time, datetime.time] | None:
        """
        解析定时开启时间配置

        Args:
            schedule_str: 时间字符串，格式: hh:mm-hh:mm

        Returns:
            (start_time, end_time) 元组，解析失败返回None
        """
        if not schedule_str or not schedule_str.strip():
            return None

        try:
            schedule_str = schedule_str.strip()
            match = re.match(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$", schedule_str)
            if not match:
                logger.warning(
                    f"定时开启配置格式错误: {schedule_str}，应为 hh:mm-hh:mm 格式"
                )
                return None

            start_hour, start_min, end_hour, end_min = map(int, match.groups())

            # 验证时间范围
            if not (0 <= start_hour <= 23 and 0 <= start_min <= 59):
                logger.warning(f"定时开启开始时间无效: {start_hour}:{start_min}")
                return None
            if not (0 <= end_hour <= 23 and 0 <= end_min <= 59):
                logger.warning(f"定时开启结束时间无效: {end_hour}:{end_min}")
                return None

            start_time = datetime.strptime(
                f"{start_hour:02d}:{start_min:02d}", "%H:%M"
            ).time()
            end_time = datetime.strptime(
                f"{end_hour:02d}:{end_min:02d}", "%H:%M"
            ).time()

            return (start_time, end_time)
        except Exception as e:
            logger.warning(f"解析定时开启配置失败: {schedule_str}, 错误: {e}")
            return None

    def _is_in_schedule(
        self, schedule: tuple[datetime.time, datetime.time] | None
    ) -> bool:
        """
        检查当前时间是否在定时开启时间段内

        Args:
            schedule: (start_time, end_time) 元组

        Returns:
            是否在时间段内
        """
        if not schedule:
            return False

        now = datetime.now().time()
        start_time, end_time = schedule

        # 处理跨天的情况 (如 22:00-08:00)
        if start_time <= end_time:
            return start_time <= now <= end_time
        else:
            return now >= start_time or now <= end_time

    async def _get_group_admins(
        self, event: AstrMessageEvent, group_id: str
    ) -> set[str]:
        """
        获取群管理员列表（带缓存）

        Args:
            event: 消息事件
            group_id: 群ID

        Returns:
            管理员和群主QQ号集合
        """
        now = datetime.now()

        # 检查缓存
        if group_id in self._admin_cache:
            cache = self._admin_cache[group_id]
            if now < cache["expires_at"]:
                return cache["admins"]

        admins = set()
        try:
            platform_name = event.get_platform_name()
            if platform_name == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                    AiocqhttpMessageEvent,
                )

                if isinstance(event, AiocqhttpMessageEvent):
                    client = event.bot
                    # 获取群成员列表
                    member_list = await client.api.call_action(
                        "get_group_member_list",
                        group_id=int(group_id),
                    )
                    if member_list:
                        for member in member_list:
                            role = member.get("role", "member")
                            if role in ("owner", "admin"):
                                admins.add(str(member.get("user_id", "")))
        except Exception as e:
            logger.debug(f"获取群管理员列表失败: {e}")

        # 更新缓存
        self._admin_cache[group_id] = {
            "admins": admins,
            "expires_at": now + timedelta(seconds=self._admin_cache_ttl),
        }

        return admins

    async def _is_user_admin_cached(
        self, event: AstrMessageEvent, group_id: str, user_id: str
    ) -> bool:
        """
        检查用户是否为管理员（使用缓存）

        Args:
            event: 消息事件
            group_id: 群ID
            user_id: 用户ID

        Returns:
            是否为管理员或群主
        """
        admins = await self._get_group_admins(event, group_id)
        return user_id in admins

    def _should_enable_censor(self, group_id: str) -> tuple[bool, str]:
        """
        判断是否应该开启审查

        逻辑说明:
        1. 如果未启用智能审查模式 → 始终开启审查（全量审查模式）
        2. 如果启用了智能审查模式:
           - 如果在强制审查时间段内（如夜间）→ 始终开启审查
           - 如果在强制审查时间段外（如白天）:
             * 如果设置了管理在线检测时间:
               · 管理员x分钟内有发言 → 关闭检查（管理在，不打扰）
               · 管理员x分钟未发言 → 开启检查（管理不在，自动补漏）
             * 如果未设置检测时间 → 始终开启检查

        Args:
            group_id: 群ID

        Returns:
            (是否开启, 原因)
        """
        config = self._group_config.get(group_id)
        if not config:
            return (False, "未配置")

        # 检查是否启用了智能审查模式
        if not config.get("enable_auto_censor", False):
            # 未启用智能审查，使用全量审查模式（始终检查）
            return (True, "全量审查模式")

        # 启用了智能审查模式
        # 检查是否在强制审查时间段内（夜间管理睡觉时强制检查）
        schedule = config.get("schedule_parsed")
        if schedule and self._is_in_schedule(schedule):
            return (True, "智能审查-强制时间段")

        # 在强制审查时间段外（白天），检查管理员是否在线
        no_admin_minutes = config.get("auto_censor_no_admin_minutes", 0)
        if no_admin_minutes > 0:
            last_admin_time = self._last_admin_message_time.get(group_id)
            if last_admin_time:
                elapsed = (datetime.now() - last_admin_time).total_seconds() / 60
                if elapsed >= no_admin_minutes:
                    # 管理x分钟未发言，开启检查
                    return (True, f"智能审查-管理不在线({int(elapsed)}分钟)")
                else:
                    # 管理x分钟内有发言，关闭检查
                    return (False, f"智能审查-管理在线({int(elapsed)}分钟前发言)")
            else:
                # 没有记录过管理员发言，视为管理不在线，开启检查
                return (True, "智能审查-管理不在线(无记录)")

        # 未设置管理在线检测，非强制时间段内始终检查
        return (True, "智能审查-非强制时间段")

    def _is_manage_group(self, group_id: str) -> bool:
        """
        检查是否是管理群

        Args:
            group_id: 群ID

        Returns:
            是否是管理群
        """
        for config in self._group_config.values():
            if config["manage_group_id"] == group_id:
                return True
        return False

    def _is_qq_builtin_emoji(self, image_url: str) -> bool:
        """
        检查图片URL是否为QQ官方自带表情包

        QQ官方表情包通常包含以下特征域名：
        - gxh.vip.qq.com
        - p.qpic.cn (QQ表情CDN)
        - imgcache.qq.com

        Args:
            image_url: 图片URL

        Returns:
            是否为QQ官方表情包
        """
        if not image_url:
            return False

        # QQ官方表情包特征域名列表
        qq_emoji_domains = [
            "gxh.vip.qq.com",
            "p.qpic.cn",
            "imgcache.qq.com",
            "qpic.cn",
        ]

        image_url_lower = image_url.lower()
        for domain in qq_emoji_domains:
            if domain in image_url_lower:
                return True

        return False

    async def _extract_forward_images(
        self, event: AstrMessageEvent, forward_id: str
    ) -> list[dict]:
        """
        从转发消息中提取所有图片

        Args:
            event: 消息事件
            forward_id: 转发消息ID

        Returns:
            图片信息列表，每个元素包含 url 和 md5
        """
        images = []
        try:
            platform_name = event.get_platform_name()
            if platform_name == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                    AiocqhttpMessageEvent,
                )

                if isinstance(event, AiocqhttpMessageEvent):
                    client = event.bot
                    # 使用 get_forward_msg API 获取转发消息内容
                    result = await client.api.call_action(
                        "get_forward_msg",
                        id=forward_id,
                    )

                    if result and "messages" in result:
                        messages = result["messages"]
                        for msg in messages:
                            if "message" in msg:
                                msg_content = msg["message"]
                                # 处理消息内容（可能是列表或字符串）
                                if isinstance(msg_content, list):
                                    for item in msg_content:
                                        if (
                                            isinstance(item, dict)
                                            and item.get("type") == "image"
                                        ):
                                            data = item.get("data", {})
                                            image_url = data.get("url", "")
                                            # 从 file 字段提取 MD5
                                            file_name = data.get("file", "")
                                            md5_hex = (
                                                file_name.split(".")[0]
                                                if file_name
                                                else ""
                                            )
                                            if image_url and self._is_valid_md5(
                                                md5_hex
                                            ):
                                                images.append(
                                                    {
                                                        "url": image_url,
                                                        "md5": md5_hex.lower(),
                                                    }
                                                )
                                elif isinstance(msg_content, str):
                                    # 处理 CQ 码格式的消息
                                    # 提取所有图片 CQ 码
                                    cq_image_matches = re.findall(
                                        r"\[CQ:image,([^\]]+)\]",
                                        msg_content,
                                    )
                                    for cq_params in cq_image_matches:
                                        # 从每个图片 CQ 码中提取 url 和 file
                                        url_match = re.search(
                                            r"url=([^,\]]+)", cq_params
                                        )
                                        file_match = re.search(
                                            r"file=([^,\]]+)", cq_params
                                        )
                                        if url_match and file_match:
                                            image_url = url_match.group(1)
                                            file_name = file_match.group(1)
                                            md5_hex = file_name.split(".")[0]
                                            if self._is_valid_md5(md5_hex):
                                                images.append(
                                                    {
                                                        "url": image_url,
                                                        "md5": md5_hex.lower(),
                                                    }
                                                )

        except Exception as e:
            logger.debug(f"提取转发消息图片失败: {e}")

        return images

    def _sample_images(
        self, images: list[dict], threshold: int, sample_rate: float
    ) -> list[dict]:
        """
        对图片进行抽检

        Args:
            images: 原始图片列表
            threshold: 抽检阈值，超过此数量才进行抽检
            sample_rate: 抽检率，0.0-1.0

        Returns:
            抽检后的图片列表
        """
        if threshold == 0 or len(images) <= threshold:
            # 阈值为0或图片数量未超过阈值，全部检查
            return images

        # 超过阈值，进行抽检
        sample_count = max(1, int(len(images) * sample_rate))
        return random.sample(images, min(sample_count, len(images)))

    async def _is_user_admin(
        self, event: AstrMessageEvent, group_id: str, user_id: str
    ) -> bool:
        """
        检查用户是否为管理员或群主

        Args:
            event: 消息事件
            group_id: 群ID
            user_id: 用户ID

        Returns:
            是否为管理员或群主
        """
        try:
            platform_name = event.get_platform_name()
            if platform_name == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                    AiocqhttpMessageEvent,
                )

                if isinstance(event, AiocqhttpMessageEvent):
                    client = event.bot
                    # 获取群成员信息
                    member_info = await client.api.call_action(
                        "get_group_member_info",
                        group_id=int(group_id),
                        user_id=int(user_id),
                        no_cache=True,
                    )
                    if member_info:
                        role = member_info.get("role", "member")
                        # owner=群主, admin=管理员
                        is_admin = role in ("owner", "admin")
                        return is_admin
            return False
        except Exception as e:
            logger.debug(f"检查用户身份失败: {e}")
            return False

    async def _handle_violation(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        user_name: str,
        md5_hash: str,
        image_url: str,
        risk_level: RiskLevel,
        risk_reason: str,
        message_id: str,
    ):
        """
        处理违规图片

        Args:
            event: 消息事件
            group_id: 群ID
            user_id: 用户ID
            user_name: 用户名
            md5_hash: 图片MD5
            image_url: 图片URL
            risk_level: 风险等级
            risk_reason: 风险原因
            message_id: 消息ID
        """
        try:
            # 获取该群的配置
            group_config = self._get_group_config(group_id)
            if not group_config:
                return

            # 检查用户是否为管理员或群主
            is_admin = await self._is_user_admin(event, group_id, user_id)
            if is_admin:
                logger.info(f"用户 {user_id} 是管理员/群主，仅通知管理群，不执行处罚")
                # 对管理员只通知，不记录违规、不禁言、不撤回
                await self._notify_manage_group(
                    event,
                    group_id,
                    user_id,
                    user_name,
                    md5_hash,
                    image_url,
                    risk_level,
                    risk_reason,
                    0,  # 禁言时长为0
                    0,  # 违规次数为0
                    is_admin=True,  # 标记为管理员
                    auto_recall=group_config.get("auto_recall", True),
                    auto_mute=group_config.get("auto_mute", True),
                )
                logger.info(f"管理员违规通知已发送: 用户={user_id}, 群={group_id}")
                return

            # 1. 自动撤回违规图片
            if group_config.get("auto_recall", True):
                await self._recall_message(event, message_id)

            # 2. 计算禁言时长
            violation_count = await self._db.get_user_violation_count(user_id, group_id)
            first_mute = group_config.get("first_mute_duration", 600)
            multiplier = group_config.get("mute_multiplier", 2)
            max_mute = group_config.get("max_mute_duration", 2419200)
            raw_duration = first_mute * (multiplier**violation_count)
            minutes = math.ceil(raw_duration / 60)
            mute_duration = minutes * 60
            mute_duration = min(mute_duration, max_mute)

            # 3. 执行禁言（如果开启自动禁言）
            if group_config.get("auto_mute", True):
                await self._mute_user(event, group_id, user_id, mute_duration)
            else:
                mute_duration = 0

            # 4. 记录违规
            await self._db.record_violation(
                user_id=user_id,
                group_id=group_id,
                md5_hash=md5_hash,
                image_url=image_url,
                risk_level=risk_level,
                risk_reason=risk_reason,
                mute_duration=mute_duration,
                message_id=message_id,
            )

            # 违规次数+1（因为刚记录的违规）
            violation_count += 1

            # 5. 发送到管理群
            await self._notify_manage_group(
                event,
                group_id,
                user_id,
                user_name,
                md5_hash,
                image_url,
                risk_level,
                risk_reason,
                mute_duration,
                violation_count,
                auto_recall=group_config.get("auto_recall", True),
                auto_mute=group_config.get("auto_mute", True),
            )

            logger.info(
                f"处理违规图片: 用户={user_id}, 群={group_id}, "
                f"风险等级={risk_level.name}, 禁言={mute_duration}秒"
            )

        except Exception as e:
            logger.error(f"处理违规图片异常: {e}")

    async def _recall_message(self, event: AstrMessageEvent, message_id: str):
        """
        撤回消息

        Args:
            event: 消息事件
            message_id: 消息ID
        """
        try:
            platform_name = event.get_platform_name()
            if platform_name == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                    AiocqhttpMessageEvent,
                )

                if isinstance(event, AiocqhttpMessageEvent):
                    client = event.bot
                    await client.api.call_action("delete_msg", message_id=message_id)
        except Exception as e:
            logger.error(f"撤回消息失败: {e}")

    async def _download_evidence_image(
        self, image_url: str, group_id: str, user_id: str
    ) -> str:
        """
        下载并保存违规证据图片

        Args:
            image_url: 图片URL
            group_id: 群ID
            user_id: 用户ID

        Returns:
            保存后的本地文件路径
        """
        try:
            import hashlib

            from .censor_flow import download_image

            image_data = await download_image(image_url)

            md5_hash = hashlib.md5(image_data).hexdigest()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

            file_ext = ".jpg"
            if image_data[:2] == b"\xff\xd8":
                file_ext = ".jpg"
            elif image_data[:4] == b"\x89PNG":
                file_ext = ".png"
            elif image_data[:3] == b"GIF":
                file_ext = ".gif"

            # 使用安全的文件名（防止路径遍历攻击）
            safe_group_id = _sanitize_filename(group_id)
            safe_user_id = _sanitize_filename(user_id)
            file_name = (
                f"{safe_group_id}_{safe_user_id}_{timestamp}_{md5_hash[:8]}{file_ext}"
            )
            file_path = os.path.join(self._evidence_dir, file_name)

            async with aiofiles.open(file_path, "wb") as f:
                await f.write(image_data)

            return file_path

        except Exception as e:
            logger.error(f"下载证据图片失败: {e}")
            return None

    async def _mute_user(
        self, event: AstrMessageEvent, group_id: str, user_id: str, duration: int
    ):
        """
        禁言用户

        Args:
            event: 消息事件
            group_id: 群ID
            user_id: 用户ID
            duration: 禁言时长（秒）
        """
        try:
            platform_name = event.get_platform_name()
            if platform_name == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                    AiocqhttpMessageEvent,
                )

                if isinstance(event, AiocqhttpMessageEvent):
                    client = event.bot
                    await client.api.call_action(
                        "set_group_ban",
                        group_id=int(group_id),
                        user_id=int(user_id),
                        duration=duration,
                    )
                    logger.info(f"已禁言用户 {user_id}，时长 {duration} 秒")
        except Exception as e:
            logger.error(f"禁言用户失败: {e}")

    async def _notify_manage_group(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        user_name: str,
        md5_hash: str,
        image_url: str,
        risk_level: RiskLevel,
        risk_reason: str,
        mute_duration: int,
        violation_count: int,
        is_admin: bool = False,
        auto_recall: bool = True,
        auto_mute: bool = True,
    ):
        """
        通知管理群

        Args:
            event: 消息事件
            group_id: 群ID
            user_id: 用户ID
            user_name: 用户名
            md5_hash: 图片MD5
            image_url: 图片URL
            risk_level: 风险等级
            risk_reason: 风险原因
            mute_duration: 禁言时长
            violation_count: 违规次数
            is_admin: 是否为管理员/群主
        """
        try:
            manage_group_id = self._get_manage_group_id(group_id)
            if not manage_group_id:
                return

            # 下载并保存证据图片
            evidence_path = await self._download_evidence_image(
                image_url, group_id, user_id
            )

            # 格式化处理措施
            if is_admin:
                action_str = "无（管理员/群主身份，不执行处罚）"
            else:
                recall_str = "撤回图片" if auto_recall else "未开启撤回"
                if auto_mute and mute_duration > 0:
                    if mute_duration < 60:
                        mute_str = f"{mute_duration}秒"
                    elif mute_duration < 3600:
                        mute_str = f"{mute_duration // 60}分钟"
                    elif mute_duration < 86400:
                        mute_str = f"{mute_duration // 3600}小时"
                    else:
                        mute_str = f"{mute_duration // 86400}天"
                    mute_str = f"禁言{mute_str}"
                elif auto_mute:
                    mute_str = "禁言0秒"
                else:
                    mute_str = "未开启禁言"
                action_str = f"{recall_str}+{mute_str}"

            # 构建违规信息（新格式）
            evidence_path_str = (
                f"\n证据图片已保存: {evidence_path}" if evidence_path else ""
            )
            admin_tag = " [管理员/群主]" if is_admin else ""
            violation_info = (
                f"⚠️ 违规图片检测通知\n"
                f"━━━━━━━━━━━━━━━\n"
                f"1️⃣ 昵称: {user_name}{admin_tag}\n"
                f"2️⃣ QQ号: {user_id}\n"
                f"3️⃣ 违规次数: 第{violation_count}次\n"
                f"4️⃣ 本次违规时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"5️⃣ 处理措施: {action_str}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"风险等级: {risk_level.name}\n"
                f"风险原因: {risk_reason}{evidence_path_str}"
            )

            # 构建合并转发消息
            from astrbot.api.message_components import Image, Node, Plain

            nodes = []

            # 添加违规信息节点
            nodes.append(
                Node(uin=int(user_id), name=user_name, content=[Plain(violation_info)])
            )

            # 添加违规图片节点（使用QQ图片URL，NapCat可直接下载）
            nodes.append(
                Node(
                    uin=int(user_id), name=user_name, content=[Image.fromURL(image_url)]
                )
            )

            # 发送到管理群
            platform_name = event.get_platform_name()
            if platform_name == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                    AiocqhttpMessageEvent,
                )

                if isinstance(event, AiocqhttpMessageEvent):
                    client = event.bot

                    # 构建转发消息
                    forward_msgs = []
                    for node in nodes:
                        forward_msgs.append(
                            {
                                "type": "node",
                                "data": {
                                    "name": node.name,
                                    "uin": str(node.uin),
                                    "content": self._convert_message_chain(
                                        node.content
                                    ),
                                },
                            }
                        )

                    await client.api.call_action(
                        "send_group_forward_msg",
                        group_id=int(manage_group_id),
                        messages=forward_msgs,
                    )

        except Exception as e:
            logger.error(f"通知管理群失败: {e}")

    def _convert_message_chain(self, chain: list) -> list:
        """
        转换消息链为API格式

        Args:
            chain: 消息链

        Returns:
            API格式的消息列表
        """
        result = []
        for comp in chain:
            if isinstance(comp, Comp.Plain):
                result.append({"type": "text", "data": {"text": comp.text}})
            elif isinstance(comp, Comp.Image):
                if comp.file:
                    result.append({"type": "image", "data": {"file": comp.file}})
                elif comp.url:
                    result.append({"type": "image", "data": {"file": comp.url}})
        return result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """
        监听所有消息事件
        """
        try:
            # 获取消息信息
            group_id = str(event.get_group_id()) if event.get_group_id() else None
            user_id = str(event.get_sender_id())
            user_name = event.get_sender_name()

            # 只处理群消息
            if not group_id:
                return

            # 检查是否是机器人自己发送的消息
            bot_user_id = str(event.get_self_id()) if event.get_self_id() else None
            if bot_user_id and user_id == bot_user_id:
                return

            # 缓存消息（用于违规时转发上下文）
            message_id = (
                str(event.message_obj.message_id)
                if hasattr(event.message_obj, "message_id")
                else ""
            )

            # 检查是否启用了图片审核（基础配置检查）
            if not self._is_group_enabled(group_id):
                return

            # 检查是否是管理员发言，更新最后管理员发言时间
            config = self._group_config.get(group_id, {})
            no_admin_minutes = config.get("auto_censor_no_admin_minutes", 0)
            if no_admin_minutes > 0:
                is_admin = await self._is_user_admin_cached(event, group_id, user_id)
                if is_admin:
                    self._last_admin_message_time[group_id] = datetime.now()
                    logger.debug(f"记录管理员发言: 群{group_id}, 用户{user_id}")

            # 检查是否应该开启审查
            should_enable, reason = self._should_enable_censor(group_id)
            if not should_enable:
                return

            # 检查是否是图片消息
            message_chain = event.get_messages()
            images_to_check = []
            forward_images = []

            # 检查是否跳过QQ自带表情包
            skip_qq_emoji = self._config.get("skip_qq_builtin_emoji", True)

            # 检查是否启用转发消息图片检测
            enable_forward_censor = self._config.get(
                "enable_forward_image_censor", False
            )
            forward_threshold = self._config.get("forward_image_sample_threshold", 0)
            forward_sample_rate = self._config.get("forward_image_sample_rate", 0.5)

            for comp in message_chain:
                if isinstance(comp, Comp.Image):
                    image_url = comp.url
                    image_md5 = self._extract_image_md5(event, comp)

                    # 跳过QQ官方表情包（如果开启此选项）
                    if skip_qq_emoji and self._is_qq_builtin_emoji(image_url):
                        continue

                    if image_url:
                        images_to_check.append((image_url, image_md5))
                elif isinstance(comp, Comp.Forward) and enable_forward_censor:
                    # 提取转发消息中的图片
                    forward_id = getattr(comp, "id", None)
                    if forward_id:
                        forward_imgs = await self._extract_forward_images(
                            event, forward_id
                        )
                        if forward_imgs:
                            forward_images.extend(forward_imgs)

            # 处理转发消息图片（如果启用）
            if forward_images:
                original_count = len(forward_images)
                # 应用抽检逻辑
                sampled_images = self._sample_images(
                    forward_images, forward_threshold, forward_sample_rate
                )
                sampled_count = len(sampled_images)

                if sampled_count < original_count:
                    logger.info(
                        f"转发消息图片抽检: 原图{original_count}张，抽检{sampled_count}张"
                    )

                for img_info in sampled_images:
                    image_url = img_info.get("url", "")
                    image_md5 = img_info.get("md5", "")

                    # 跳过QQ官方表情包
                    if skip_qq_emoji and self._is_qq_builtin_emoji(image_url):
                        continue

                    if image_url:
                        images_to_check.append((image_url, image_md5))

            # 检查是否是图片消息且启用了图片审核
            if not images_to_check:
                return
            if not self._censor_flow:
                return
            if not self._censor_flow.is_image_censor_enabled():
                return

            # 获取群配置中的缓存设置
            group_config = self._get_group_config(group_id)
            base_expire_hours = (
                group_config.get("base_expire_hours", 2) if group_config else 2
            )
            max_expire_days = (
                group_config.get("max_expire_days", 14) if group_config else 14
            )

            # 顺序处理所有图片（避免并发过高）
            for image_url, image_md5 in images_to_check:
                try:
                    # 进行图片审核
                    (
                        risk_level,
                        risk_reason,
                        md5_hash,
                    ) = await self._censor_flow.submit_image(
                        image_url,
                        group_id,
                        precalculated_md5=image_md5,
                        base_expire_hours=base_expire_hours,
                        max_expire_days=max_expire_days,
                    )

                    # 处理违规
                    if risk_level in (RiskLevel.Review, RiskLevel.Block):
                        await self._handle_violation(
                            event,
                            group_id,
                            user_id,
                            user_name,
                            md5_hash,
                            image_url,
                            risk_level,
                            risk_reason,
                            message_id,
                        )
                except CensorError as e:
                    logger.error(f"图片审核异常: {e}")
                except Exception as e:
                    logger.error(f"处理图片异常: {e}")

        except CensorError as e:
            logger.error(f"图片审核异常: {e}")
        except Exception as e:
            logger.error(f"消息处理异常: {e}")

    @filter.command("查询违规")
    async def query_violation(self, event: AstrMessageEvent, user_id_str: str = ""):
        """
        查询用户违规记录（管理群专用）
        """
        try:
            # 获取当前群ID
            group_id = str(event.get_group_id()) if event.get_group_id() else None
            if not group_id:
                return

            # 检查是否是管理群
            if not self._is_manage_group(group_id):
                return

            # 如果没有提供用户ID，提示使用方法
            if not user_id_str:
                yield event.plain_result("使用方法: /查询违规 [QQ号]")
                return

            user_id = user_id_str.strip()

            # 查询违规记录
            records = await self._db.get_user_violation_records(user_id, limit=10)

            if not records:
                yield event.plain_result(f"用户 {user_id} 暂无违规记录")
                return

            # 获取违规统计
            violation_count = len(records)

            # 构建回复
            result = f"📊 用户 {user_id} 的违规记录\n"
            result += "━━━━━━━━━━━━━━━\n"
            result += f"总违规次数: {violation_count}\n"
            result += "━━━━━━━━━━━━━━━\n"

            for i, record in enumerate(records[:5], 1):
                violation_time = record.get("violation_time", "")
                risk_level = RiskLevel(record.get("risk_level", 0)).name
                risk_reason = record.get("risk_reason", "")
                group_id_record = record.get("group_id", "")
                mute_duration = record.get("mute_duration", 0)

                # 格式化禁言时长
                if mute_duration < 60:
                    mute_str = f"{mute_duration}秒"
                elif mute_duration < 3600:
                    mute_str = f"{mute_duration // 60}分钟"
                elif mute_duration < 86400:
                    mute_str = f"{mute_duration // 3600}小时"
                else:
                    mute_str = f"{mute_duration // 86400}天"

                result += f"\n{i}. 时间: {violation_time}\n"
                result += f"   群号: {group_id_record}\n"
                result += f"   风险等级: {risk_level}\n"
                result += f"   风险原因: {risk_reason}\n"
                result += f"   处理措施: 禁言{mute_str}\n"

            yield event.plain_result(result)

        except Exception as e:
            logger.error(f"查询违规记录异常: {e}")

    @filter.command("审核状态")
    async def check_status(self, event: AstrMessageEvent):
        """查看审核插件状态（管理群专用）"""
        try:
            # 获取当前群ID
            group_id = str(event.get_group_id()) if event.get_group_id() else None
            if not group_id:
                return

            # 检查是否是管理群
            if not self._is_manage_group(group_id):
                return

            status_info = "📊 图片审核插件状态\n"
            status_info += "━━━━━━━━━━━━━━━\n"

            # 检查图片审核状态（安全地处理 self._censor_flow 为 None 的情况）
            image_enabled = (
                self._censor_flow is not None
                and self._censor_flow.is_image_censor_enabled()
            )
            status_info += (
                f"图片审核: {'✅ 已启用' if image_enabled else '❌ 未启用'}\n"
            )

            # 检查配置
            image_provider = self._config.get("image_censor_provider", "未配置")
            status_info += f"图片审核提供商: {image_provider}\n"

            # 显示 VLAI 配置
            if image_provider == "VLAI":
                vlai_config = self._config.get("vlai", {})
                provider_id = vlai_config.get("provider_id", "")
                status_info += (
                    f"VLAI 提供商ID: {provider_id if provider_id else '默认'}\n"
                )

            # 显示动图增强检测配置
            gif_enabled = self._config.get("enable_gif_enhanced_detection", False)
            status_info += (
                f"动图增强检测: {'✅ 已启用' if gif_enabled else '❌ 未启用'}\n"
            )
            if gif_enabled and image_provider == "VLAI":
                gif_config = self._config.get("gif_enhanced", {})
                gif_provider_id = gif_config.get("provider_id", "")
                frame_count = gif_config.get("frame_sample_count", 3)
                detection_mode = gif_config.get("detection_mode", "separate")
                mode_str = "逐帧分开" if detection_mode == "separate" else "批量合并"
                status_info += (
                    f"  └ 动图检测提供商ID: {gif_provider_id if gif_provider_id else '默认'}\n"
                    f"  └ 采样帧数: {frame_count}\n"
                    f"  └ 检测模式: {mode_str}\n"
                )

            # 显示转发消息图片检测配置
            forward_enabled = self._config.get("enable_forward_image_censor", False)
            status_info += (
                f"转发消息检测: {'✅ 已启用' if forward_enabled else '❌ 未启用'}\n"
            )
            if forward_enabled:
                forward_threshold = self._config.get(
                    "forward_image_sample_threshold", 0
                )
                forward_sample_rate = self._config.get("forward_image_sample_rate", 0.5)
                if forward_threshold == 0:
                    status_info += "  └ 抽检设置: 全部检查\n"
                else:
                    status_info += f"  └ 抽检设置: 超过{forward_threshold}张时抽检{int(forward_sample_rate * 100)}%\n"

            # 获取自动黑白名单数量
            cache_counts = await self._db.get_cache_counts()
            status_info += "\n📋 自动名单统计\n"
            status_info += "━━━━━━━━━━━━━━━\n"
            status_info += f"自动白名单: {cache_counts['whitelist']} 条\n"
            status_info += f"自动黑名单: {cache_counts['blacklist']} 条\n"

            # 检查群聊配置及审查模式
            status_info += "\n📌 群聊审查模式\n"
            status_info += "━━━━━━━━━━━━━━━\n"
            for gid, config in self._group_config.items():
                if config.get("enable_auto_censor", False):
                    # 智能审查模式
                    schedule = config.get("auto_censor_schedule", "")
                    no_admin_min = config.get("auto_censor_no_admin_minutes", 0)
                    status_info += f"群 {gid}:\n"
                    status_info += "  └ 模式: 智能审查\n"
                    if schedule:
                        status_info += f"  └ 强制时段: {schedule}\n"
                    if no_admin_min > 0:
                        status_info += f"  └ 管理检测: {no_admin_min}分钟\n"
                else:
                    # 全量审查模式
                    status_info += f"群 {gid}:\n"
                    status_info += "  └ 模式: 全量审查\n"

                # 显示当前该群是否应该开启审查
                should_enable, reason = self._should_enable_censor(gid)
                status_info += f"  └ 当前状态: {'✅ 检查中' if should_enable else '⏸️ 暂停'} ({reason})\n"

            status_info += "━━━━━━━━━━━━━━━"

            yield event.plain_result(status_info)

        except Exception as e:
            logger.error(f"查看状态异常: {e}")

    @filter.command("清除缓存")
    async def clear_cache(self, event: AstrMessageEvent):
        """清除所有缓存数据（黑白名单）（管理群专用）"""
        try:
            group_id = str(event.get_group_id()) if event.get_group_id() else None
            if not group_id:
                return

            if not self._is_manage_group(group_id):
                return

            result = await self._db.clear_all_cache()

            info = "🗑️ 缓存清除完成\n"
            info += "━━━━━━━━━━━━━━━\n"
            info += f"白名单: {result['whitelist']} 条\n"
            info += f"黑名单: {result['blacklist']} 条\n"
            info += "注意: 消息缓存已取消，不再存储\n"
            info += "━━━━━━━━━━━━━━━"

            yield event.plain_result(info)

        except Exception as e:
            logger.error(f"清除缓存异常: {e}")

    @filter.command("查询名单")
    async def query_list_status(self, event: AstrMessageEvent):
        """查询图片在黑白名单中的状态（管理群专用，需引用图片）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return

            if not self._is_manage_group(manage_group_id):
                return

            # 检查是否引用了消息
            reply_info = self._extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要查询的图片消息")
                return

            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return

            # 获取被引用消息中的图片
            image_md5s = await self._get_message_images(event, message_id)
            if not image_md5s:
                yield event.plain_result("❌ 引用的消息中没有图片")
                return

            result = "📋 图片名单状态查询\n"
            result += "━━━━━━━━━━━━━━━\n"

            for i, md5_hash in enumerate(image_md5s, 1):
                result += f"\n图片 {i} (MD5: {md5_hash[:16]}...)\n"

                # 检查人工白名单
                in_manual_whitelist = await self._db.check_manual_whitelist(md5_hash)
                result += (
                    f"  人工白名单: {'✅ 是' if in_manual_whitelist else '❌ 否'}\n"
                )

                # 检查人工黑名单
                manual_blacklist_result = await self._db.check_manual_blacklist(
                    md5_hash
                )
                if manual_blacklist_result:
                    result += f"  人工黑名单: ✅ 是 (等级: {manual_blacklist_result[0].name})\n"
                else:
                    result += "  人工黑名单: ❌ 否\n"

                # 检查自动白名单
                in_auto_whitelist = await self._db.check_whitelist(md5_hash)
                result += f"  自动白名单: {'✅ 是' if in_auto_whitelist else '❌ 否'}\n"

                # 检查自动黑名单
                auto_blacklist_result = await self._db.check_blacklist(md5_hash)
                if auto_blacklist_result:
                    result += (
                        f"  自动黑名单: ✅ 是 (等级: {auto_blacklist_result[0].name})\n"
                    )
                else:
                    result += "  自动黑名单: ❌ 否\n"

            # 显示配置状态
            disable_auto_whitelist = self._config.get("disable_auto_whitelist", False)
            disable_auto_blacklist = self._config.get("disable_auto_blacklist", False)
            result += "\n━━━━━━━━━━━━━━━\n"
            result += (
                f"自动白名单禁用: {'✅ 是' if disable_auto_whitelist else '❌ 否'}\n"
            )
            result += (
                f"自动黑名单禁用: {'✅ 是' if disable_auto_blacklist else '❌ 否'}\n"
            )
            result += "━━━━━━━━━━━━━━━"

            yield event.plain_result(result)

        except Exception as e:
            logger.error(f"查询名单状态异常: {e}")
            yield event.plain_result(f"❌ 操作失败: {str(e)}")

    @filter.command("删除违规")
    async def delete_violation(self, event: AstrMessageEvent, user_id_str: str = ""):
        """删除指定用户的违规记录（管理群专用）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return

            if not self._is_manage_group(manage_group_id):
                return

            if not user_id_str:
                yield event.plain_result("使用方法: /删除违规 [QQ号]")
                return

            user_id = user_id_str.strip()

            target_group_ids = self._get_group_ids_by_manage_group(manage_group_id)
            if not target_group_ids:
                yield event.plain_result("❌ 未找到对应的被管理群")
                return

            # 删除该管理群对应的所有被管理群中的违规记录
            total_deleted = 0
            deleted_details = []
            for target_group_id in target_group_ids:
                deleted_count = await self._db.delete_user_violations(
                    user_id, target_group_id
                )
                total_deleted += deleted_count
                if deleted_count > 0:
                    deleted_details.append(f"群 {target_group_id}: {deleted_count} 条")

            if total_deleted > 0:
                details_str = "\n".join(deleted_details)
                yield event.plain_result(
                    f"✅ 已删除用户 {user_id} 的违规记录，共 {total_deleted} 条\n{details_str}"
                )
            else:
                yield event.plain_result(f"⚠️ 用户 {user_id} 暂无违规记录")

        except Exception as e:
            logger.error(f"删除违规记录异常: {e}")

    def _extract_reply_image_md5(self, event: AstrMessageEvent) -> str | None:
        """从回复消息中提取图片MD5"""
        try:
            if hasattr(event.message_obj, "raw"):
                raw_data = event.message_obj.raw
                if raw_data and "elements" in raw_data:
                    for element in raw_data.get("elements", []):
                        if element.get("elementType") == 2:
                            pic_element = element.get("picElement", {})
                            md5_hex = pic_element.get("md5HexStr")
                            if md5_hex:
                                return md5_hex.lower()
            return None
        except Exception as e:
            logger.debug(f"从回复消息提取图片MD5时发生异常: {e}")
            return None

    def _extract_reply_info(self, event: AstrMessageEvent) -> dict | None:
        """从回复消息中提取被引用消息的信息"""
        try:
            platform_name = event.get_platform_name()

            # aiocqhttp 平台：从 raw_message 中解析 CQ:reply
            if platform_name == "aiocqhttp":
                raw_message_str = ""

                # 尝试从 message_obj.raw_message 获取
                # 注意：raw_message 可能是 Event 对象或字符串
                try:
                    raw_message = event.message_obj.raw_message
                    if isinstance(raw_message, str):
                        raw_message_str = raw_message
                    elif hasattr(raw_message, "raw_message"):
                        # 如果是 Event 对象，尝试获取其 raw_message 属性
                        raw_message_str = raw_message.raw_message
                    elif hasattr(raw_message, "get"):
                        # 如果是 dict-like 对象
                        raw_message_str = raw_message.get("raw_message", "")
                except Exception as e:
                    logger.debug(f"_extract_reply_info: 获取 raw_message 失败: {e}")

                # 备选：从 message_str 获取
                if not raw_message_str:
                    raw_message_str = event.message_str or ""

                if raw_message_str:
                    match = re.search(r"\[CQ:reply,id=(\d+)\]", raw_message_str)
                    if match:
                        return {
                            "message_id": match.group(1),
                            "sender_uid": None,
                            "sender_uid_str": None,
                        }

            # QQ官方/其他平台格式 (elementType == 7)
            if hasattr(event.message_obj, "raw"):
                raw_data = event.message_obj.raw
                if raw_data and "elements" in raw_data:
                    for element in raw_data.get("elements", []):
                        if element.get("elementType") == 7:
                            reply_element = element.get("replyElement", {})
                            return {
                                "message_id": reply_element.get("sourceMsgIdInRecords"),
                                "sender_uid": reply_element.get("senderUid"),
                                "sender_uid_str": reply_element.get("senderUidStr"),
                            }

            return None
        except Exception as e:
            logger.debug(f"从回复消息提取信息时发生异常: {e}")
            return None

    async def _get_message_images(
        self, event: AstrMessageEvent, message_id: str
    ) -> list[str]:
        """获取指定消息中的所有图片MD5"""
        md5_list = []
        try:
            platform_name = event.get_platform_name()

            if platform_name == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                    AiocqhttpMessageEvent,
                )

                if isinstance(event, AiocqhttpMessageEvent):
                    client = event.bot
                    # 使用 get_msg API 获取消息内容
                    result = await client.api.call_action(
                        "get_msg", message_id=int(message_id)
                    )

                    if result and "message" in result:
                        message_list = result["message"]
                        for msg in message_list:
                            if msg.get("type") == "image":
                                data = msg.get("data", {})
                                # 优先从 md5 字段获取
                                md5 = data.get("md5")
                                # 如果没有 md5 字段，从 file 字段提取（文件名通常是 MD5）
                                if not md5:
                                    file_name = data.get("file", "")
                                    # 文件名格式通常是 MD5.jpg 或 MD5.png 等
                                    if file_name:
                                        # 提取文件名中的 MD5 部分（去掉扩展名）
                                        md5 = file_name.split(".")[0]
                                # 验证MD5格式
                                if md5 and self._is_valid_md5(md5):
                                    md5_list.append(md5.lower())

        except Exception as e:
            logger.debug(f"获取消息图片失败: {e}")
        return md5_list

    # ========== 人工白名单管理指令 ==========

    @filter.command("添加白名单")
    async def add_manual_whitelist_cmd(self, event: AstrMessageEvent, reason: str = ""):
        """添加图片到人工白名单（管理群专用，需引用图片）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return

            if not self._is_manage_group(manage_group_id):
                return

            # 检查是否引用了消息
            reply_info = self._extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要添加到白名单的图片消息")
                return

            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return

            # 获取被引用消息中的图片
            image_md5s = await self._get_message_images(event, message_id)
            if not image_md5s:
                yield event.plain_result("❌ 引用的消息中没有图片")
                return

            user_id = str(event.get_sender_id())
            added_count = 0

            for md5_hash in image_md5s:
                success = await self._db.add_manual_whitelist(
                    md5_hash=md5_hash,
                    added_by=user_id,
                    reason=reason if reason else None,
                )
                if success:
                    added_count += 1

            if added_count > 0:
                yield event.plain_result(
                    f"✅ 成功添加 {added_count} 张图片到人工白名单"
                )
            else:
                yield event.plain_result("⚠️ 图片已在人工白名单中")

        except Exception as e:
            logger.error(f"添加人工白名单异常: {e}")
            yield event.plain_result(f"❌ 操作失败: {str(e)}")

    @filter.command("移除白名单")
    async def remove_manual_whitelist_cmd(self, event: AstrMessageEvent):
        """从人工白名单移除图片（管理群专用，需引用图片）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return

            if not self._is_manage_group(manage_group_id):
                return

            # 检查是否引用了消息
            reply_info = self._extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要从白名单移除的图片消息")
                return

            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return

            # 获取被引用消息中的图片
            image_md5s = await self._get_message_images(event, message_id)
            if not image_md5s:
                yield event.plain_result("❌ 引用的消息中没有图片")
                return

            removed_count = 0
            for md5_hash in image_md5s:
                success = await self._db.remove_manual_whitelist(md5_hash)
                if success:
                    removed_count += 1

            if removed_count > 0:
                yield event.plain_result(
                    f"✅ 成功从人工白名单移除 {removed_count} 张图片"
                )
            else:
                yield event.plain_result("⚠️ 图片不在人工白名单中")

        except Exception as e:
            logger.error(f"移除人工白名单异常: {e}")
            yield event.plain_result(f"❌ 操作失败: {str(e)}")

    @filter.command("清空白名单")
    async def clear_manual_whitelist_cmd(
        self, event: AstrMessageEvent, confirm: str = ""
    ):
        """清空人工白名单（管理群专用，需二次确认）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return

            if not self._is_manage_group(manage_group_id):
                return

            # 二次确认
            if confirm.strip().lower() != "确认":
                yield event.plain_result(
                    "⚠️ 此操作将清空所有人工白名单数据，不可恢复！\n"
                    "如需确认，请发送: /清空白名单 确认"
                )
                return

            count = await self._db.clear_all_manual_whitelist()
            yield event.plain_result(f"✅ 已清空人工白名单，共移除 {count} 条记录")

        except Exception as e:
            logger.error(f"清空人工白名单异常: {e}")
            yield event.plain_result(f"❌ 操作失败: {str(e)}")

    # ========== 人工黑名单管理指令 ==========

    @filter.command("添加黑名单")
    async def add_manual_blacklist_cmd(
        self, event: AstrMessageEvent, risk_level_str: str = "", reason: str = ""
    ):
        """添加图片到人工黑名单（管理群专用，需引用图片）

        使用方法: /添加黑名单 [REVIEW/BLOCK] [原因]
        示例: /添加黑名单 BLOCK 色情内容
        示例: /添加黑名单 REVIEW 需要复审
        """
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return

            if not self._is_manage_group(manage_group_id):
                return

            # 检查是否引用了消息
            reply_info = self._extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要添加到黑名单的图片消息")
                return

            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return

            # 获取被引用消息中的图片
            image_md5s = await self._get_message_images(event, message_id)
            if not image_md5s:
                yield event.plain_result("❌ 引用的消息中没有图片")
                return

            # 解析风险等级
            risk_level = RiskLevel.Block
            if risk_level_str:
                risk_level_str = risk_level_str.strip().upper()
                if risk_level_str == "REVIEW":
                    risk_level = RiskLevel.Review
                elif risk_level_str == "BLOCK":
                    risk_level = RiskLevel.Block
                else:
                    yield event.plain_result(
                        "❌ 风险等级参数错误，可选: REVIEW(建议复审) 或 BLOCK(违规)"
                    )
                    return

            user_id = str(event.get_sender_id())
            added_count = 0

            for md5_hash in image_md5s:
                success = await self._db.add_manual_blacklist(
                    md5_hash=md5_hash,
                    risk_level=risk_level,
                    risk_reason=reason if reason else "人工添加",
                    added_by=user_id,
                    reason=reason if reason else None,
                )
                if success:
                    added_count += 1

            if added_count > 0:
                yield event.plain_result(
                    f"✅ 成功添加 {added_count} 张图片到人工黑名单"
                )
            else:
                yield event.plain_result("⚠️ 图片已在人工黑名单中")

        except Exception as e:
            logger.error(f"添加人工黑名单异常: {e}")
            yield event.plain_result(f"❌ 操作失败: {str(e)}")

    @filter.command("移除黑名单")
    async def remove_manual_blacklist_cmd(self, event: AstrMessageEvent):
        """从人工黑名单移除图片（管理群专用，需引用图片）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return

            if not self._is_manage_group(manage_group_id):
                return

            # 检查是否引用了消息
            reply_info = self._extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要从黑名单移除的图片消息")
                return

            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return

            # 获取被引用消息中的图片
            image_md5s = await self._get_message_images(event, message_id)
            if not image_md5s:
                yield event.plain_result("❌ 引用的消息中没有图片")
                return

            removed_count = 0
            for md5_hash in image_md5s:
                success = await self._db.remove_manual_blacklist(md5_hash)
                if success:
                    removed_count += 1

            if removed_count > 0:
                yield event.plain_result(
                    f"✅ 成功从人工黑名单移除 {removed_count} 张图片"
                )
            else:
                yield event.plain_result("⚠️ 图片不在人工黑名单中")

        except Exception as e:
            logger.error(f"移除人工黑名单异常: {e}")
            yield event.plain_result(f"❌ 操作失败: {str(e)}")

    @filter.command("清空黑名单")
    async def clear_manual_blacklist_cmd(
        self, event: AstrMessageEvent, confirm: str = ""
    ):
        """清空人工黑名单（管理群专用，需二次确认）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return

            if not self._is_manage_group(manage_group_id):
                return

            # 二次确认
            if confirm.strip().lower() != "确认":
                yield event.plain_result(
                    "⚠️ 此操作将清空所有人工黑名单数据，不可恢复！\n"
                    "如需确认，请发送: /清空黑名单 确认"
                )
                return

            count = await self._db.clear_all_manual_blacklist()
            yield event.plain_result(f"✅ 已清空人工黑名单，共移除 {count} 条记录")

        except Exception as e:
            logger.error(f"清空人工黑名单异常: {e}")
            yield event.plain_result(f"❌ 操作失败: {str(e)}")

    # ========== 自动名单管理指令 ==========

    @filter.command("移除自动白名单")
    async def remove_auto_whitelist_cmd(self, event: AstrMessageEvent):
        """从自动白名单移除图片（管理群专用，需引用图片）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return

            if not self._is_manage_group(manage_group_id):
                return

            # 检查是否引用了消息
            reply_info = self._extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要移除的图片消息")
                return

            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return

            # 获取被引用消息中的图片
            image_md5s = await self._get_message_images(event, message_id)
            if not image_md5s:
                yield event.plain_result("❌ 引用的消息中没有图片")
                return

            removed_count = 0
            for md5_hash in image_md5s:
                success = await self._db.remove_auto_whitelist(md5_hash)
                if success:
                    removed_count += 1

            if removed_count > 0:
                yield event.plain_result(
                    f"✅ 成功从自动白名单移除 {removed_count} 张图片"
                )
            else:
                yield event.plain_result("⚠️ 图片不在自动白名单中")

        except Exception as e:
            logger.error(f"移除自动白名单异常: {e}")
            yield event.plain_result(f"❌ 操作失败: {str(e)}")

    @filter.command("移除自动黑名单")
    async def remove_auto_blacklist_cmd(self, event: AstrMessageEvent):
        """从自动黑名单移除图片（管理群专用，需引用图片）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return

            if not self._is_manage_group(manage_group_id):
                return

            # 检查是否引用了消息
            reply_info = self._extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要移除的图片消息")
                return

            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return

            # 获取被引用消息中的图片
            image_md5s = await self._get_message_images(event, message_id)
            if not image_md5s:
                yield event.plain_result("❌ 引用的消息中没有图片")
                return

            removed_count = 0
            for md5_hash in image_md5s:
                success = await self._db.remove_auto_blacklist(md5_hash)
                if success:
                    removed_count += 1

            if removed_count > 0:
                yield event.plain_result(
                    f"✅ 成功从自动黑名单移除 {removed_count} 张图片"
                )
            else:
                yield event.plain_result("⚠️ 图片不在自动黑名单中")

        except Exception as e:
            logger.error(f"移除自动黑名单异常: {e}")
            yield event.plain_result(f"❌ 操作失败: {str(e)}")

    @filter.command("审查帮助")
    async def review_help(self, event: AstrMessageEvent):
        """显示图片审核插件帮助信息"""
        try:
            group_id = str(event.get_group_id()) if event.get_group_id() else None
            if not group_id:
                return

            # 检查是否是管理群或被审核的群
            is_manage = self._is_manage_group(group_id)
            is_enabled = self._is_group_enabled(group_id)

            if not is_manage and not is_enabled:
                return

            help_text = (
                "📖 图片审核插件使用帮助\n"
                "━━━━━━━━━━━━━━━\n"
                "\n"
                "【管理员命令】\n"
                "━━━━━━━━━━━━━━━\n"
                "/查询违规 [QQ号] - 查询用户违规记录\n"
                "/删除违规 [QQ号] - 删除用户违规记录\n"
                "/审核状态 - 查看插件运行状态\n"
                "/清除缓存 - 清除自动黑白名单缓存\n"
                "/查询名单 - 查询图片名单状态(需引用图片)\n"
                "\n"
                "【人工白名单管理】\n"
                "━━━━━━━━━━━━━━━\n"
                "/添加白名单 [原因] - 添加图片到白名单(需引用)\n"
                "  提示: 原因含空格时用引号包裹，如:\n"
                '  /添加白名单 "误拦截，正常图片"\n'
                "/移除白名单 - 从白名单移除图片(需引用)\n"
                "/清空白名单 确认 - 清空所有人工白名单\n"
                "\n"
                "【人工黑名单管理】\n"
                "━━━━━━━━━━━━━━━\n"
                "/添加黑名单 [REVIEW/BLOCK] [原因]\n"
                "  添加图片到黑名单(需引用图片)\n"
                "  提示: 原因含空格时用引号包裹，如:\n"
                '  /添加黑名单 BLOCK "色情违规内容"\n'
                "/移除黑名单 - 从黑名单移除图片(需引用)\n"
                "/清空黑名单 确认 - 清空所有人工黑名单\n"
                "\n"
                "【自动名单管理】\n"
                "━━━━━━━━━━━━━━━\n"
                "/移除自动白名单 - 移除自动白名单(需引用)\n"
                "/移除自动黑名单 - 移除自动黑名单(需引用)\n"
                "\n"
                "【动图检测说明】\n"
                "━━━━━━━━━━━━━━━\n"
                "• 动图增强检测仅在使用 VLAI 提供商时生效\n"
                "• 开启后会对多帧 GIF 图片进行采样检测\n"
                "• 可单独配置动图检测的 VL 模型防止并发问题\n"
                "• 缩放处理会应用于每一采样帧\n"
                "• 检测模式:\n"
                "  - separate: 逐帧分开检查（多次调用，更精确）\n"
                "  - batch: 多帧合并检查（单次调用，更省token）\n"
                "\n"
                "【转发消息检测说明】\n"
                "━━━━━━━━━━━━━━━\n"
                "• 开启后可检测合并转发消息中的图片\n"
                "• 支持抽检功能，图片过多时可按比例抽检\n"
                "• 抽检阈值设为0表示全部检查\n"
                "• 转发消息中的违规图片会触发同样的处理\n"
                "\n"
                "【说明】\n"
                "━━━━━━━━━━━━━━━\n"
                "• 带(需引用)的命令需要引用图片消息\n"
                "• REVIEW=建议复审, BLOCK=违规拦截\n"
                "• 管理员/群主违规仅通知，不执行处罚\n"
                "• 机器人需为群主才能处理管理员\n"
                "• 参数含空格时请用引号包裹\n"
                "━━━━━━━━━━━━━━━"
            )

            yield event.plain_result(help_text)

        except Exception as e:
            logger.error(f"显示帮助异常: {e}")
            yield event.plain_result("❌ 获取帮助信息失败")
