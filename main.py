"""
图片审核插件主模块
提供图片内容审核、违规处理、管理群通知等功能
"""

import os
import re
from datetime import datetime
from typing import Any

import aiofiles

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

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
    "AstrBot",
    "图片审核插件，提供图片内容审核、违规处理、管理群通知等功能",
    "1.0.0",
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

        # 数据目录（使用AstrBot的data目录）
        self._data_dir = os.path.join("data", "image_review")
        os.makedirs(self._data_dir, exist_ok=True)
        logger.debug(f"图片审核插件数据目录: {self._data_dir}")

        # 证据图片保存目录
        self._evidence_dir = os.path.join(self._data_dir, "evidence")
        os.makedirs(self._evidence_dir, exist_ok=True)
        logger.debug(f"证据图片目录: {self._evidence_dir}")

        # 初始化数据库
        self._db = DatabaseManager(self._data_dir)
        logger.debug("数据库管理器初始化完成")

        # 群聊配置映射 {group_id: {manage_group_id, violation_settings, cache_settings}}
        self._group_config: dict[str, dict] = {}
        self._load_group_config()
        logger.debug(f"已加载 {len(self._group_config)} 个群聊配置")

        # 审核流程管理器（延迟初始化）
        self._censor_flow: CensorFlow | None = None
        logger.debug("图片审核插件初始化完成")

    def _load_group_config(self):
        """加载群聊配置"""
        group_settings = self._config.get("group_settings", [])
        logger.debug(f"开始加载群聊配置，共 {len(group_settings)} 个配置项")

        # 如果是旧格式的dict（兼容旧版本配置）
        if isinstance(group_settings, dict):
            group_settings = [group_settings]
            logger.debug("检测到旧格式配置，已转换为列表格式")

        for i, setting in enumerate(group_settings):
            # 确保是有效配置
            if not isinstance(setting, dict):
                logger.debug(f"配置项 {i + 1} 不是字典类型，跳过")
                continue

            # 跳过非启用的配置（兼容template_list格式）
            if not setting.get("enabled", True):
                logger.debug(f"配置项 {i + 1} 未启用，跳过")
                continue
            group_id = str(setting.get("group_id", ""))
            manage_group_id = str(setting.get("manage_group_id", ""))
            if group_id and manage_group_id:
                # 验证并规范化配置值，带异常处理
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
                mute_multiplier = safe_int(setting.get("mute_multiplier"), 2, min_val=1)
                base_expire_hours = safe_int(
                    setting.get("base_expire_hours"), 2, min_val=1
                )
                max_expire_days = safe_int(
                    setting.get("max_expire_days"), 14, min_val=1, max_val=365
                )

                # 保存每个群的完整配置
                self._group_config[group_id] = {
                    "manage_group_id": manage_group_id,
                    "first_mute_duration": first_mute_duration,
                    "max_mute_duration": max_mute_duration,
                    "mute_multiplier": mute_multiplier,
                    "auto_recall": setting.get("auto_recall", True),
                    "base_expire_hours": base_expire_hours,
                    "max_expire_days": max_expire_days,
                }
                logger.info(f"已加载群聊配置: 群{group_id} -> 管理群{manage_group_id}")
            else:
                logger.debug(f"配置项 {i + 1} 缺少群ID或管理群ID，跳过")
        logger.debug(f"群聊配置加载完成，共加载 {len(self._group_config)} 个有效配置")

    async def initialize(self):
        """插件初始化"""
        try:
            logger.debug("开始初始化图片审核插件")
            # 初始化审核流程管理器，传入 context 以支持 VLAI 审核器
            self._censor_flow = CensorFlow(self._config, self._db, self.context)
            logger.debug("审核流程管理器创建完成，开始初始化")
            await self._censor_flow.initialize()
            logger.debug("审核流程管理器初始化完成")

            if self._censor_flow.is_image_censor_enabled():
                logger.info("图片审核插件初始化成功，已启用图片审核")
            else:
                logger.warning("图片审核插件初始化完成，但未启用图片审核（请检查配置）")
        except Exception as e:
            logger.error(f"图片审核插件初始化失败: {e}")
            logger.debug(f"初始化失败详情: {str(e)}")

    async def terminate(self):
        """插件销毁"""
        logger.debug("开始卸载图片审核插件")
        if self._censor_flow:
            logger.debug("关闭审核流程管理器")
            await self._censor_flow.close()
            logger.debug("审核流程管理器已关闭")
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

    def _get_group_id_by_manage_group(self, manage_group_id: str) -> str | None:
        """
        根据管理群ID反向查找主群ID

        Args:
            manage_group_id: 管理群ID

        Returns:
            主群ID，未找到则返回None
        """
        for group_id, config in self._group_config.items():
            if config.get("manage_group_id") == manage_group_id:
                return group_id
        return None

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
                else:
                    logger.debug(f"提取的MD5格式无效: {md5_hex}")
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
            logger.debug(
                f"开始处理违规图片: 用户={user_id}, 群={group_id}, 风险等级={risk_level.name}"
            )
            # 获取该群的配置
            group_config = self._get_group_config(group_id)
            if not group_config:
                logger.debug(f"群 {group_id} 未配置，跳过处理")
                return

            # 1. 自动撤回违规图片
            if group_config.get("auto_recall", True):
                logger.debug(f"撤回违规消息，消息ID: {message_id}")
                await self._recall_message(event, message_id)
                logger.debug("违规消息撤回完成")

            # 2. 计算禁言时长
            logger.debug("计算禁言时长，获取用户违规次数")
            violation_count = await self._db.get_user_violation_count(user_id, group_id)
            logger.debug(f"用户当前违规次数: {violation_count}")
            first_mute = group_config.get("first_mute_duration", 600)
            multiplier = group_config.get("mute_multiplier", 2)
            max_mute = group_config.get("max_mute_duration", 2419200)
            mute_duration = first_mute * (multiplier**violation_count)
            mute_duration = min(mute_duration, max_mute)
            logger.debug(f"计算禁言时长: {mute_duration}秒")

            # 3. 执行禁言
            logger.debug(f"执行禁言，用户={user_id}, 时长={mute_duration}秒")
            await self._mute_user(event, group_id, user_id, mute_duration)

            # 4. 记录违规
            logger.debug("记录违规到数据库")
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
            logger.debug("违规记录完成")

            # 违规次数+1（因为刚记录的违规）
            violation_count += 1

            # 5. 发送到管理群
            logger.debug("通知管理群")
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
            )
            logger.debug("管理群通知完成")

            logger.info(
                f"处理违规图片: 用户={user_id}, 群={group_id}, "
                f"风险等级={risk_level.name}, 禁言={mute_duration}秒"
            )

        except Exception as e:
            logger.error(f"处理违规图片异常: {e}")
            logger.debug(f"处理违规异常详情: {str(e)}")

    async def _recall_message(self, event: AstrMessageEvent, message_id: str):
        """
        撤回消息

        Args:
            event: 消息事件
            message_id: 消息ID
        """
        try:
            platform_name = event.get_platform_name()
            logger.debug(f"开始撤回消息，平台: {platform_name}, 消息ID: {message_id}")
            if platform_name == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                    AiocqhttpMessageEvent,
                )

                if isinstance(event, AiocqhttpMessageEvent):
                    client = event.bot
                    logger.debug("调用CQHTTP API撤回消息")
                    await client.api.call_action("delete_msg", message_id=message_id)
                    logger.debug("消息撤回成功")
            else:
                logger.debug(f"平台 {platform_name} 暂不支持撤回消息")
        except Exception as e:
            logger.error(f"撤回消息失败: {e}")
            logger.debug(f"撤回消息异常详情: {str(e)}")

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

            logger.debug(f"开始下载违规证据图片，URL: {image_url}")

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

            logger.debug(f"证据图片已保存: {file_path}")
            return file_path

        except Exception as e:
            logger.error(f"下载证据图片失败: {e}")
            logger.debug(f"下载证据图片异常详情: {str(e)}")
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
            logger.debug(
                f"开始禁言用户，平台: {platform_name}, 用户={user_id}, 群={group_id}, 时长={duration}秒"
            )
            if platform_name == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                    AiocqhttpMessageEvent,
                )

                if isinstance(event, AiocqhttpMessageEvent):
                    client = event.bot
                    logger.debug("调用CQHTTP API禁言用户")
                    await client.api.call_action(
                        "set_group_ban",
                        group_id=int(group_id),
                        user_id=int(user_id),
                        duration=duration,
                    )
                    logger.info(f"已禁言用户 {user_id}，时长 {duration} 秒")
                    logger.debug("用户禁言成功")
            else:
                logger.debug(f"平台 {platform_name} 暂不支持禁言操作")
        except Exception as e:
            logger.error(f"禁言用户失败: {e}")
            logger.debug(f"禁言用户异常详情: {str(e)}")

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
        """
        try:
            logger.debug(
                f"开始通知管理群，群={group_id}, 用户={user_id}, 风险等级={risk_level.name}"
            )
            manage_group_id = self._get_manage_group_id(group_id)
            if not manage_group_id:
                logger.debug(f"群 {group_id} 未配置管理群，跳过通知")
                return
            logger.debug(f"管理群ID: {manage_group_id}")

            # 下载并保存证据图片
            evidence_path = await self._download_evidence_image(
                image_url, group_id, user_id
            )

            # 格式化处理措施
            if mute_duration < 60:
                mute_str = f"{mute_duration}秒"
            elif mute_duration < 3600:
                mute_str = f"{mute_duration // 60}分钟"
            elif mute_duration < 86400:
                mute_str = f"{mute_duration // 3600}小时"
            else:
                mute_str = f"{mute_duration // 86400}天"
            logger.debug(f"处理措施: 禁言{mute_str}")

            # 构建违规信息（新格式）
            evidence_path_str = (
                f"\n证据图片已保存: {evidence_path}" if evidence_path else ""
            )
            violation_info = (
                f"⚠️ 违规图片检测通知\n"
                f"━━━━━━━━━━━━━━━\n"
                f"1️⃣ 昵称: {user_name}\n"
                f"2️⃣ QQ号: {user_id}\n"
                f"3️⃣ 违规次数: 第{violation_count}次\n"
                f"4️⃣ 本次违规时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"5️⃣ 处理措施: 撤回图片+禁言{mute_str}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"风险等级: {risk_level.name}\n"
                f"风险原因: {risk_reason}{evidence_path_str}"
            )
            logger.debug(f"违规信息构建完成，长度: {len(violation_info)} 字符")

            # 构建合并转发消息
            from astrbot.api.message_components import Image, Node, Plain

            nodes = []

            # 添加违规信息节点
            nodes.append(
                Node(uin=int(user_id), name=user_name, content=[Plain(violation_info)])
            )
            logger.debug("违规信息节点添加完成")

            # 添加违规图片节点（使用QQ图片URL，NapCat可直接下载）
            nodes.append(
                Node(
                    uin=int(user_id), name=user_name, content=[Image.fromURL(image_url)]
                )
            )
            logger.debug("违规图片节点添加完成")

            # 发送到管理群
            platform_name = event.get_platform_name()
            logger.debug(f"发送到管理群，平台: {platform_name}")
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
                    logger.debug(f"转发消息构建完成，共 {len(forward_msgs)} 个节点")

                    await client.api.call_action(
                        "send_group_forward_msg",
                        group_id=int(manage_group_id),
                        messages=forward_msgs,
                    )
                    logger.debug("管理群通知发送成功")
            else:
                logger.debug(f"平台 {platform_name} 暂不支持管理群通知")

        except Exception as e:
            logger.error(f"通知管理群失败: {e}")
            logger.debug(f"通知管理群异常详情: {str(e)}")

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
            logger.debug(f"收到消息: 群={group_id}, 用户={user_id}, 用户名={user_name}")

            # 只处理群消息
            if not group_id:
                logger.debug("非群消息，跳过处理")
                return

            # 缓存消息（用于违规时转发上下文）
            message_id = (
                str(event.message_obj.message_id)
                if hasattr(event.message_obj, "message_id")
                else ""
            )
            message_str = event.message_str
            logger.debug(f"消息ID: {message_id}, 消息内容: {message_str[:50]}...")

            # 检查是否是图片消息
            message_chain = event.get_messages()
            image_url = None
            image_md5 = None

            for comp in message_chain:
                if isinstance(comp, Comp.Image):
                    image_url = comp.url
                    image_md5 = self._extract_image_md5(event, comp)
                    logger.debug(f"检测到图片消息，URL: {image_url}, MD5: {image_md5}")
                    break

            # 检查是否启用了图片审核
            if not self._is_group_enabled(group_id):
                logger.debug(f"群 {group_id} 未启用图片审核，跳过")
                return

            # 检查是否是图片消息且启用了图片审核
            if not image_url:
                logger.debug("非图片消息，跳过审核")
                return
            if not self._censor_flow:
                logger.debug("审核流程管理器未初始化，跳过审核")
                return
            if not self._censor_flow.is_image_censor_enabled():
                logger.debug("图片审核未启用，跳过审核")
                return

            # 获取群配置中的缓存设置
            group_config = self._get_group_config(group_id)
            base_expire_hours = (
                group_config.get("base_expire_hours", 2) if group_config else 2
            )
            max_expire_days = (
                group_config.get("max_expire_days", 14) if group_config else 14
            )

            # 进行图片审核
            logger.debug(f"开始审核图片，URL: {image_url}")
            risk_level, risk_reason, md5_hash = await self._censor_flow.submit_image(
                image_url,
                group_id,
                precalculated_md5=image_md5,
                base_expire_hours=base_expire_hours,
                max_expire_days=max_expire_days,
            )
            logger.debug(
                f"图片审核完成，结果: 风险等级={risk_level.name}, 原因={risk_reason}, MD5={md5_hash}"
            )

            # 处理违规
            if risk_level in (RiskLevel.Review, RiskLevel.Block):
                logger.debug("检测到违规图片，开始处理")
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
                logger.debug("违规图片处理完成")
            else:
                logger.debug("图片审核通过，无需处理")

        except CensorError as e:
            logger.error(f"图片审核异常: {e}")
            logger.debug(f"审核异常详情: {str(e)}")
        except Exception as e:
            logger.error(f"消息处理异常: {e}")
            logger.debug(f"消息处理异常详情: {str(e)}")

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

            # 检查图片审核状态
            image_enabled = (
                self._censor_flow and self._censor_flow.is_image_censor_enabled()
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

            # 检查群聊配置
            status_info += "\n已配置的群聊:\n"
            for gid, config in self._group_config.items():
                status_info += f"  群 {gid} -> 管理群 {config['manage_group_id']}\n"

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
            logger.debug(f"query_list_status: reply_info={reply_info}")
            if not reply_info:
                yield event.plain_result("❌ 请引用需要查询的图片消息")
                return

            message_id = reply_info.get("message_id")
            logger.debug(f"query_list_status: message_id={message_id}")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return

            # 获取被引用消息中的图片
            logger.debug("query_list_status: 开始调用 _get_message_images")
            image_md5s = await self._get_message_images(event, message_id)
            logger.debug(
                f"query_list_status: _get_message_images 返回 {len(image_md5s)} 张图片"
            )
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

            target_group_id = self._get_group_id_by_manage_group(manage_group_id)
            if not target_group_id:
                yield event.plain_result("❌ 未找到对应的被管理群")
                return

            deleted_count = await self._db.delete_user_violations(
                user_id, target_group_id
            )

            yield event.plain_result(
                f"✅ 已删除用户 {user_id} 在群 {target_group_id} 的违规记录，共 {deleted_count} 条"
            )

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
            logger.debug(f"_extract_reply_info: 平台={platform_name}")

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
                    logger.debug(
                        f"_extract_reply_info: raw_message_str={raw_message_str}"
                    )
                except Exception as e:
                    logger.debug(f"_extract_reply_info: 获取 raw_message 失败: {e}")

                # 备选：从 message_str 获取
                if not raw_message_str:
                    raw_message_str = event.message_str or ""
                    logger.debug(f"_extract_reply_info: message_str={raw_message_str}")

                if raw_message_str:
                    match = re.search(r"\[CQ:reply,id=(\d+)\]", raw_message_str)
                    logger.debug(f"_extract_reply_info: 正则匹配结果={match}")
                    if match:
                        logger.debug(
                            f"_extract_reply_info: 匹配成功, message_id={match.group(1)}"
                        )
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

            logger.debug("_extract_reply_info: 未找到引用信息")
            return None
        except Exception as e:
            logger.debug(f"从回复消息提取信息时发生异常: {e}")
            logger.debug(f"异常详情: {str(e)}")
            return None

    async def _get_message_images(
        self, event: AstrMessageEvent, message_id: str
    ) -> list[str]:
        """获取指定消息中的所有图片MD5"""
        md5_list = []
        logger.debug(f"_get_message_images: 方法被调用, message_id={message_id}")
        try:
            platform_name = event.get_platform_name()
            logger.debug(
                f"_get_message_images: 平台={platform_name}, message_id={message_id}"
            )

            if platform_name == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                    AiocqhttpMessageEvent,
                )

                if isinstance(event, AiocqhttpMessageEvent):
                    client = event.bot
                    # 使用 get_msg API 获取消息内容
                    logger.debug(
                        f"_get_message_images: 调用 get_msg API, message_id={message_id}"
                    )
                    result = await client.api.call_action(
                        "get_msg", message_id=int(message_id)
                    )
                    logger.debug(f"_get_message_images: get_msg 返回结果={result}")

                    if result and "message" in result:
                        message_list = result["message"]
                        logger.debug(
                            f"_get_message_images: 消息包含 {len(message_list)} 个元素"
                        )
                        for msg in message_list:
                            logger.debug(
                                f"_get_message_images: 检查消息元素 type={msg.get('type')}"
                            )
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
                                        logger.debug(
                                            f"_get_message_images: 从文件名提取 md5={md5}"
                                        )
                                logger.debug(
                                    f"_get_message_images: 找到图片, md5={md5}"
                                )
                                # 验证MD5格式
                                if md5 and self._is_valid_md5(md5):
                                    md5_list.append(md5.lower())
                                elif md5:
                                    logger.debug(
                                        f"_get_message_images: MD5格式无效，跳过: {md5}"
                                    )
                    else:
                        logger.debug("_get_message_images: 返回结果中没有 message 字段")
                else:
                    logger.debug("_get_message_images: 事件类型不匹配")
            else:
                logger.debug("_get_message_images: 非 aiocqhttp 平台，跳过")

            logger.debug(f"_get_message_images: 共找到 {len(md5_list)} 张图片")
        except Exception as e:
            logger.debug(f"获取消息图片失败: {e}")
            logger.debug(f"异常详情: {str(e)}")
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
        self, event: AstrMessageEvent, risk_level_str: str = "", *, reason: str = ""
    ):
        """添加图片到人工黑名单（管理群专用，需引用图片）"""
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
