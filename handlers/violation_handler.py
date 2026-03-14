"""
违规处理模块
负责处理违规图片的所有逻辑，包括撤回、禁言、记录违规、通知管理群等
"""

import hashlib
import math
import os
from datetime import datetime
from typing import TYPE_CHECKING

import aiofiles

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image, Node, Plain

from ..database import DatabaseManager, RiskLevel
from ..utils.image_utils import ImageUtils
from ..utils.message_utils import MessageUtils

if TYPE_CHECKING:
    from .admin_manager import AdminManager
    from .config_manager import ConfigManager


class ViolationHandler:
    """违规处理器 - 负责处理违规图片的所有逻辑"""

    def __init__(
        self,
        db_manager: DatabaseManager,
        config_manager: "ConfigManager",
        admin_manager: "AdminManager",
        evidence_dir: str,
    ):
        """
        初始化违规处理器

        Args:
            db_manager: 数据库管理器
            config_manager: 配置管理器
            admin_manager: 管理员管理器
            evidence_dir: 证据图片保存目录
        """
        self._db = db_manager
        self._config_manager = config_manager
        self._admin_manager = admin_manager
        self._evidence_dir = evidence_dir

    async def handle_violation(
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
        image_data: bytes | None = None,
    ) -> None:
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
            group_config = self._config_manager.get_group_config(group_id)
            if not group_config:
                return

            # 检查用户是否为管理员或群主
            is_admin = await self._admin_manager.is_user_admin(event, group_id, user_id)
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
                    mute_duration=0,
                    violation_count=0,
                    is_admin=True,
                    auto_recall=group_config.get("auto_recall", True),
                    auto_mute=group_config.get("auto_mute", True),
                    image_data=image_data,
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
                image_data=image_data,
            )

            logger.info(
                f"处理违规图片: 用户={user_id}, 群={group_id}, "
                f"风险等级={risk_level.name}, 禁言={mute_duration}秒"
            )

        except Exception as e:
            logger.error(f"处理违规图片异常: {e}")

    async def _recall_message(self, event: AstrMessageEvent, message_id: str) -> None:
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

    async def _mute_user(
        self, event: AstrMessageEvent, group_id: str, user_id: str, duration: int
    ) -> None:
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
        image_data: bytes | None = None,
    ) -> None:
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
            auto_recall: 是否自动撤回
            auto_mute: 是否自动禁言
            image_data: 已下载的图片数据（可选，避免重复下载）
        """
        try:
            manage_group_id = self._config_manager.get_manage_group_id(group_id)
            if not manage_group_id:
                return

            # 下载并保存证据图片（如果传入了图片数据则直接使用）
            evidence_path = await self._download_evidence_image(
                image_url, group_id, user_id, image_data
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
                                    "content": MessageUtils.convert_message_chain(
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

    async def _download_evidence_image(
        self,
        image_url: str,
        group_id: str,
        user_id: str,
        image_data: bytes | None = None,
    ) -> str | None:
        """
        下载并保存违规证据图片

        Args:
            image_url: 图片URL
            group_id: 群ID
            user_id: 用户ID
            image_data: 已下载的图片数据（可选，如果提供则直接使用）

        Returns:
            保存后的本地文件路径
        """
        try:
            if image_data is None:
                from ..censors import download_image

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
            safe_group_id = ImageUtils.sanitize_filename(group_id)
            safe_user_id = ImageUtils.sanitize_filename(user_id)
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
