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
        phash: str | None = None,
        dhash: str | None = None,
    ) -> None:
        """
        处理单个违规图片（处罚+记录+单张通报）

        签名与行为保持 v1.6.0 兼容：处罚/记录逻辑见 _process_violation，
        单张通报逻辑见 _notify_manage_group。同一消息事件内的多个违规请使用
        handle_violations_batch（v1.6.1 起合并为一次通报）。

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
            image_data: 已下载的图片数据（可选）
            phash: 感知哈希（可选）
            dhash: 差异哈希（可选）
        """
        result = await self._process_violation(
            event,
            group_id,
            user_id,
            user_name,
            md5_hash,
            image_url,
            risk_level,
            risk_reason,
            message_id,
            image_data=image_data,
            phash=phash,
            dhash=dhash,
        )
        if result is None:
            return
        group_config = self._config_manager.get_group_config(group_id)
        await self._notify_manage_group(
            event,
            group_id,
            user_id,
            user_name,
            md5_hash,
            image_url,
            risk_level,
            risk_reason,
            mute_duration=result["mute_duration"],
            violation_count=result["violation_count"],
            is_admin=result["is_admin"],
            auto_recall=group_config.get("auto_recall", True),
            auto_mute=group_config.get("auto_mute", True),
            evidence_path=result["evidence_path"],
        )

    async def _process_violation(
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
        phash: str | None = None,
        dhash: str | None = None,
        recall_message: bool = True,
    ) -> dict | None:
        """
        单张违规的处罚与记录（不含通报），v1.6.1 抽出供单张/批量共用

        处理顺序：保存证据 → 管理员留痕分支 → 撤回（可跳过）→ 禁言计算与执行 →
        写入违规记录。

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
            image_data: 已下载的图片数据（可选）
            phash: 感知哈希（可选）
            dhash: 差异哈希（可选）
            recall_message: 是否执行撤回（批量处理同一条消息时仅首次撤回）

        Returns:
            dict(is_admin, mute_duration, violation_count, evidence_path, recalled)；
            群配置缺失等无法处理时返回 None
        """
        try:
            # 获取该群的配置
            group_config = self._config_manager.get_group_config(group_id)
            if not group_config:
                return None

            # 0. 保存证据图片（无论是否有管理群都保存，供 WebUI 展示，v1.5.0）
            evidence_path = await self._download_evidence_image(
                image_url, group_id, user_id, image_data
            )

            # 检查用户是否为管理员或群主
            is_admin = await self._admin_manager.is_user_admin(event, group_id, user_id)
            if is_admin:
                logger.info(f"用户 {user_id} 是管理员/群主，记录违规留痕但不执行处罚")
                # 管理员/群主：不撤回、不禁言，但仍写入违规记录留痕（标注 is_admin），
                # 使 WebUI 完整反映每一次违规判定（修复历史遗漏：旧逻辑此处直接 return，
                # 导致管理员违规只进了审核日志、未进违规记录表）。
                await self._db.record_violation(
                    user_id=user_id,
                    group_id=group_id,
                    md5_hash=md5_hash,
                    image_url=image_url,
                    risk_level=risk_level,
                    risk_reason=risk_reason,
                    mute_duration=0,
                    message_id=message_id,
                    user_name=user_name,
                    evidence_path=evidence_path,
                    phash=phash,
                    dhash=dhash,
                    is_admin=True,
                    update_stats=False,
                )
                logger.info(f"管理员违规已记录留痕: 用户={user_id}, 群={group_id}")
                return {
                    "is_admin": True,
                    "mute_duration": 0,
                    "violation_count": 0,
                    "evidence_path": evidence_path,
                    "recalled": False,
                }

            # 1. 自动撤回违规图片（批量处理同一条消息时仅首次执行）
            recalled = False
            if recall_message and group_config.get("auto_recall", True):
                await self._recall_message(event, message_id)
                recalled = True

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

            # 4. 记录违规（含用户名与证据路径，v1.5.0）
            await self._db.record_violation(
                user_id=user_id,
                group_id=group_id,
                md5_hash=md5_hash,
                image_url=image_url,
                risk_level=risk_level,
                risk_reason=risk_reason,
                mute_duration=mute_duration,
                message_id=message_id,
                user_name=user_name,
                evidence_path=evidence_path,
                phash=phash,
                dhash=dhash,
            )

            # 违规次数+1（因为刚记录的违规）
            violation_count += 1

            logger.info(
                f"处理违规图片: 用户={user_id}, 群={group_id}, "
                f"风险等级={risk_level.name}, 禁言={mute_duration}秒"
            )

            return {
                "is_admin": False,
                "mute_duration": mute_duration,
                "violation_count": violation_count,
                "evidence_path": evidence_path,
                "recalled": recalled,
            }

        except Exception as e:
            logger.error(f"处理违规图片异常: {e}")
            return None

    async def handle_violations_batch(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        user_name: str,
        message_id: str,
        items: list[dict],
    ) -> None:
        """
        批量处理同一消息事件内的多个违规图片（v1.6.1）

        用于修复：合并转发消息内多张违规图片被逐张通报、管理群刷屏的问题。
        处罚与记录仍逐张执行（撤回去重：同一条消息只撤回一次）；
        通报只发一条——单张走与 handle_violation 一致的旧格式，
        多张走 _notify_manage_group_batch 的汇总格式。

        Args:
            event: 消息事件
            group_id: 群ID
            user_id: 用户ID
            user_name: 用户名
            message_id: 消息ID
            items: 违规图片列表，每项为 dict，含:
                md5_hash(str)、image_url(str)、risk_level(RiskLevel)、
                risk_reason(str)、image_data(bytes|None)、phash(str|None)、dhash(str|None)
        """
        if not items:
            return
        try:
            group_config = self._config_manager.get_group_config(group_id)
            if not group_config:
                return
            auto_recall = group_config.get("auto_recall", True)
            auto_mute = group_config.get("auto_mute", True)

            results: list[tuple[dict, dict]] = []
            recall_done = False
            for item in items:
                result = await self._process_violation(
                    event,
                    group_id,
                    user_id,
                    user_name,
                    item["md5_hash"],
                    item["image_url"],
                    item["risk_level"],
                    item["risk_reason"],
                    message_id,
                    image_data=item.get("image_data"),
                    phash=item.get("phash"),
                    dhash=item.get("dhash"),
                    recall_message=auto_recall and not recall_done,
                )
                if result is None:
                    continue
                if result["recalled"]:
                    recall_done = True
                results.append((item, result))

            if not results:
                return

            # 单张：与 v1.6.0 完全一致的通报格式
            if len(results) == 1:
                item, result = results[0]
                await self._notify_manage_group(
                    event,
                    group_id,
                    user_id,
                    user_name,
                    item["md5_hash"],
                    item["image_url"],
                    item["risk_level"],
                    item["risk_reason"],
                    mute_duration=result["mute_duration"],
                    violation_count=result["violation_count"],
                    is_admin=result["is_admin"],
                    auto_recall=auto_recall,
                    auto_mute=auto_mute,
                    evidence_path=result["evidence_path"],
                )
                return

            # 多张：一次汇总通报（禁言时长/违规次数取最后一张的处理结果，
            # 与现状逐张处理时通报内容一致——禁言时长随违规次数递增而变长）
            _, last_result = results[-1]
            await self._notify_manage_group_batch(
                event,
                group_id,
                user_id,
                user_name,
                [
                    (
                        item["md5_hash"],
                        item["image_url"],
                        item["risk_level"],
                        item["risk_reason"],
                        result["evidence_path"],
                    )
                    for item, result in results
                ],
                mute_duration=last_result["mute_duration"],
                violation_count=last_result["violation_count"],
                is_admin=last_result["is_admin"],
                auto_recall=auto_recall,
                auto_mute=auto_mute,
            )

        except Exception as e:
            logger.error(f"批量处理违规图片异常: {e}")

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
        evidence_path: str | None = None,
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
            evidence_path: 已保存的证据图片路径（v1.5.0 起由 handle_violation 统一保存）
        """
        try:
            manage_group_id = self._config_manager.get_manage_group_id(group_id)
            if not manage_group_id:
                return

            action_str = self._build_action_str(
                is_admin, auto_recall, auto_mute, mute_duration
            )

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
            nodes = [
                Node(uin=int(user_id), name=user_name, content=[Plain(violation_info)]),
                # 添加违规图片节点（使用QQ图片URL，NapCat可直接下载）
                Node(
                    uin=int(user_id), name=user_name, content=[Image.fromURL(image_url)]
                ),
            ]

            await self._send_forward_nodes(event, manage_group_id, nodes)

        except Exception as e:
            logger.error(f"通知管理群失败: {e}")

    def _build_action_str(
        self, is_admin: bool, auto_recall: bool, auto_mute: bool, mute_duration: int
    ) -> str:
        """
        格式化处理措施文案（单张/批量通报共用，v1.6.1 抽出）

        Args:
            is_admin: 是否为管理员/群主
            auto_recall: 是否自动撤回
            auto_mute: 是否自动禁言
            mute_duration: 禁言时长（秒）
        """
        if is_admin:
            return "无（管理员/群主身份，不执行处罚）"
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
        return f"{recall_str}+{mute_str}"

    async def _send_forward_nodes(
        self, event: AstrMessageEvent, manage_group_id: str, nodes: list[Node]
    ) -> None:
        """
        以合并转发消息发送节点列表到管理群（仅 aiocqhttp，v1.6.1 抽出）

        Args:
            event: 消息事件
            manage_group_id: 管理群ID
            nodes: 合并转发节点列表
        """
        try:
            platform_name = event.get_platform_name()
            if platform_name != "aiocqhttp":
                return
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )

            if not isinstance(event, AiocqhttpMessageEvent):
                return
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
                            "content": MessageUtils.convert_message_chain(node.content),
                        },
                    }
                )

            await client.api.call_action(
                "send_group_forward_msg",
                group_id=int(manage_group_id),
                messages=forward_msgs,
            )
        except Exception as e:
            logger.error(f"发送合并转发到管理群失败: {e}")

    async def notify_manual_review(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        user_name: str,
        image_url: str,
        reason: str,
    ) -> None:
        """
        向管理群发送「需人工审核」通知（v1.6.3，卡片图片降级专用）

        发送一条普通文本消息（非合并转发），仅提示管理员人工确认图片，
        不含任何处罚措施（不撤回、不禁言）。仅支持 aiocqhttp；
        manage_group_id 未配置、非 aiocqhttp 或发送失败时静默跳过，不抛出异常。

        Args:
            event: 消息事件
            group_id: 被管理群ID
            user_id: 发送者ID
            user_name: 发送者昵称
            image_url: 卡片图片原始 URL
            reason: 降级原因说明
        """
        try:
            manage_group_id = self._config_manager.get_manage_group_id(group_id)
            if not manage_group_id:
                return
            platform_name = event.get_platform_name()
            if platform_name != "aiocqhttp":
                logger.debug("非 aiocqhttp 平台，跳过人工审核通知")
                return
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )

            if not isinstance(event, AiocqhttpMessageEvent):
                return
            client = event.bot
            text = (
                "⚠️ 图片审核需人工确认\n"
                "━━━━━━━━━━━━━━━\n"
                f"昵称: {user_name}\n"
                f"QQ号: {user_id}\n"
                f"群号: {group_id}\n"
                f"原因: {reason}\n"
                f"图片URL: {image_url}"
            )
            await client.api.call_action(
                "send_group_msg",
                group_id=int(manage_group_id),
                message=[{"type": "text", "data": {"text": text}}],
            )
        except Exception as e:
            logger.error(f"发送人工审核通知失败: {e}")

    async def _notify_manage_group_batch(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        user_name: str,
        images: list[tuple[str, str, RiskLevel, str, str | None]],
        mute_duration: int,
        violation_count: int,
        is_admin: bool = False,
        auto_recall: bool = True,
        auto_mute: bool = True,
    ) -> None:
        """
        通知管理群（批量违规，v1.6.1）

        同一消息事件内多张违规图片合并为一条通报：
        汇总信息节点 + 每张违规图片一个节点（图片 + 序号/风险等级/风险原因）。

        Args:
            event: 消息事件
            group_id: 群ID
            user_id: 用户ID
            user_name: 用户名
            images: 违规图片列表，每项 (md5_hash, image_url, risk_level, risk_reason, evidence_path)
            mute_duration: 禁言时长（取最后一张图片的计算结果）
            violation_count: 违规次数（取最后一张图片处理后的累计值）
            is_admin: 是否为管理员/群主
            auto_recall: 是否自动撤回
            auto_mute: 是否自动禁言
        """
        try:
            manage_group_id = self._config_manager.get_manage_group_id(group_id)
            if not manage_group_id:
                return

            total = len(images)
            action_str = self._build_action_str(
                is_admin, auto_recall, auto_mute, mute_duration
            )
            evidence_count = sum(1 for img in images if img[4])
            evidence_path_str = (
                f"\n证据图片已保存: {evidence_count} 张" if evidence_count else ""
            )
            admin_tag = " [管理员/群主]" if is_admin else ""
            summary = (
                f"⚠️ 违规图片检测通知（共 {total} 张）\n"
                f"━━━━━━━━━━━━━━━\n"
                f"1️⃣ 昵称: {user_name}{admin_tag}\n"
                f"2️⃣ QQ号: {user_id}\n"
                f"3️⃣ 违规次数: 第{violation_count}次\n"
                f"4️⃣ 本次违规时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"5️⃣ 处理措施: {action_str}\n"
                f"━━━━━━━━━━━━━━━{evidence_path_str}"
            )

            # 构建合并转发消息：汇总节点 + 每张违规图片一个节点
            nodes = [Node(uin=int(user_id), name=user_name, content=[Plain(summary)])]
            for i, (_, image_url, risk_level, risk_reason, _) in enumerate(
                images, start=1
            ):
                nodes.append(
                    Node(
                        uin=int(user_id),
                        name=user_name,
                        content=[
                            Image.fromURL(image_url),
                            Plain(
                                f"\n[{i}/{total}] 风险等级: {risk_level.name}\n"
                                f"风险原因: {risk_reason}"
                            ),
                        ],
                    )
                )

            await self._send_forward_nodes(event, manage_group_id, nodes)

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
