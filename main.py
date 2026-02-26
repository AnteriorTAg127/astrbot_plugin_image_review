"""
图片审核插件主模块
提供图片内容审核、违规处理、管理群通知等功能
"""

import os
from datetime import datetime
from typing import Any, Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp

from .database import DatabaseManager, RiskLevel
from .censor_flow import CensorFlow
from .censor_base import CensorError


@register("image_review", "AstrBot", "图片审核插件，提供图片内容审核、违规处理、管理群通知等功能", "1.0.0")
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
        self._censor_flow: Optional[CensorFlow] = None
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
                logger.debug(f"配置项 {i+1} 不是字典类型，跳过")
                continue
                
            # 跳过非启用的配置（兼容template_list格式）
            if not setting.get("enabled", True):
                logger.debug(f"配置项 {i+1} 未启用，跳过")
                continue
            group_id = str(setting.get("group_id", ""))
            manage_group_id = str(setting.get("manage_group_id", ""))
            if group_id and manage_group_id:
                # 保存每个群的完整配置
                self._group_config[group_id] = {
                    "manage_group_id": manage_group_id,
                    "first_mute_duration": setting.get("first_mute_duration", 600),
                    "max_mute_duration": min(
                        setting.get("max_mute_duration", 2419200),
                        2419200  # 最大28天
                    ),
                    "mute_multiplier": setting.get("mute_multiplier", 2),
                    "auto_recall": setting.get("auto_recall", True),
                    "base_expire_hours": setting.get("base_expire_hours", 2),
                    "max_expire_days": setting.get("max_expire_days", 14),
                }
                logger.info(f"已加载群聊配置: 群{group_id} -> 管理群{manage_group_id}")
            else:
                logger.debug(f"配置项 {i+1} 缺少群ID或管理群ID，跳过")
        logger.debug(f"群聊配置加载完成，共加载 {len(self._group_config)} 个有效配置")

    async def initialize(self):
        """插件初始化"""
        try:
            logger.debug("开始初始化图片审核插件")
            # 初始化审核流程管理器
            self._censor_flow = CensorFlow(self._config, self._db)
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

    def _get_group_config(self, group_id: str) -> Optional[dict]:
        """
        获取群聊的完整配置

        Args:
            group_id: 群ID

        Returns:
            群聊配置字典，未配置则返回None
        """
        return self._group_config.get(group_id)

    def _get_manage_group_id(self, group_id: str) -> Optional[str]:
        """
        获取群聊对应的管理群ID

        Args:
            group_id: 群ID

        Returns:
            管理群ID，未配置则返回None
        """
        config = self._group_config.get(group_id)
        return config["manage_group_id"] if config else None

    def _get_group_id_by_manage_group(self, manage_group_id: str) -> Optional[str]:
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

    def _extract_image_md5(self, event: AstrMessageEvent, image_comp: Comp.Image) -> Optional[str]:
        """
        从消息事件中提取图片的MD5值

        Args:
            event: 消息事件
            image_comp: 图片组件

        Returns:
            图片MD5字符串，如果无法获取则返回None
        """
        try:
            if hasattr(event.message_obj, 'raw'):
                raw_data = event.message_obj.raw
                if raw_data and 'elements' in raw_data:
                    for element in raw_data.get('elements', []):
                        if element.get('elementType') == 2:
                            pic_element = element.get('picElement', {})
                            md5_hex = pic_element.get('md5HexStr')
                            if md5_hex:
                                return md5_hex.lower()
        except Exception:
            pass
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
        message_id: str
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
            logger.debug(f"开始处理违规图片: 用户={user_id}, 群={group_id}, 风险等级={risk_level.name}")
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
            logger.debug(f"计算禁言时长，获取用户违规次数")
            violation_count = await self._db.get_user_violation_count(user_id, group_id)
            logger.debug(f"用户当前违规次数: {violation_count}")
            first_mute = group_config.get("first_mute_duration", 600)
            multiplier = group_config.get("mute_multiplier", 2)
            max_mute = group_config.get("max_mute_duration", 2419200)
            mute_duration = first_mute * (multiplier ** violation_count)
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
                message_id=message_id
            )
            logger.debug("违规记录完成")

            # 违规次数+1（因为刚记录的违规）
            violation_count += 1

            # 5. 发送到管理群
            logger.debug("通知管理群")
            await self._notify_manage_group(
                event, group_id, user_id, user_name, md5_hash,
                image_url, risk_level, risk_reason, mute_duration, violation_count
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
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                if isinstance(event, AiocqhttpMessageEvent):
                    client = event.bot
                    logger.debug(f"调用CQHTTP API撤回消息")
                    await client.api.call_action(
                        'delete_msg',
                        message_id=message_id
                    )
                    logger.debug("消息撤回成功")
            else:
                logger.debug(f"平台 {platform_name} 暂不支持撤回消息")
        except Exception as e:
            logger.error(f"撤回消息失败: {e}")
            logger.debug(f"撤回消息异常详情: {str(e)}")

    async def _download_evidence_image(self, image_url: str, group_id: str, user_id: str) -> str:
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
            from .censor_flow import download_image
            import hashlib
            import aiofiles

            logger.debug(f"开始下载违规证据图片，URL: {image_url}")

            image_data = await download_image(image_url)

            md5_hash = hashlib.md5(image_data).hexdigest()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

            file_ext = ".jpg"
            if image_data[:2] == b'\xff\xd8':
                file_ext = ".jpg"
            elif image_data[:4] == b'\x89PNG':
                file_ext = ".png"
            elif image_data[:3] == b'GIF':
                file_ext = ".gif"

            file_name = f"{group_id}_{user_id}_{timestamp}_{md5_hash[:8]}{file_ext}"
            file_path = os.path.join(self._evidence_dir, file_name)

            async with aiofiles.open(file_path, 'wb') as f:
                await f.write(image_data)

            logger.debug(f"证据图片已保存: {file_path}")
            return file_path

        except Exception as e:
            logger.error(f"下载证据图片失败: {e}")
            logger.debug(f"下载证据图片异常详情: {str(e)}")
            return None

    async def _mute_user(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        duration: int
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
            logger.debug(f"开始禁言用户，平台: {platform_name}, 用户={user_id}, 群={group_id}, 时长={duration}秒")
            if platform_name == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                if isinstance(event, AiocqhttpMessageEvent):
                    client = event.bot
                    logger.debug(f"调用CQHTTP API禁言用户")
                    await client.api.call_action(
                        'set_group_ban',
                        group_id=int(group_id),
                        user_id=int(user_id),
                        duration=duration
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
        violation_count: int
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
            logger.debug(f"开始通知管理群，群={group_id}, 用户={user_id}, 风险等级={risk_level.name}")
            manage_group_id = self._get_manage_group_id(group_id)
            if not manage_group_id:
                logger.debug(f"群 {group_id} 未配置管理群，跳过通知")
                return
            logger.debug(f"管理群ID: {manage_group_id}")

            # 下载并保存证据图片
            evidence_path = await self._download_evidence_image(image_url, group_id, user_id)

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
            evidence_path_str = f"\n证据图片已保存: {evidence_path}" if evidence_path else ""
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
            from astrbot.api.message_components import Node, Plain, Image

            nodes = []

            # 添加违规信息节点
            nodes.append(Node(
                uin=int(user_id),
                name=user_name,
                content=[Plain(violation_info)]
            ))
            logger.debug("违规信息节点添加完成")

            # 添加违规图片节点（优先使用本地保存的证据图片）
            if evidence_path:
                nodes.append(Node(
                    uin=int(user_id),
                    name=user_name,
                    content=[Image.fromFileSystem(evidence_path)]
                ))
                logger.debug("本地证据图片节点添加完成")
            else:
                nodes.append(Node(
                    uin=int(user_id),
                    name=user_name,
                    content=[Image.fromURL(image_url)]
                ))
                logger.debug("URL图片节点添加完成")

            # 发送到管理群
            platform_name = event.get_platform_name()
            logger.debug(f"发送到管理群，平台: {platform_name}")
            if platform_name == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                if isinstance(event, AiocqhttpMessageEvent):
                    client = event.bot

                    # 构建转发消息
                    forward_msgs = []
                    for node in nodes:
                        forward_msgs.append({
                            "type": "node",
                            "data": {
                                "name": node.name,
                                "uin": str(node.uin),
                                "content": self._convert_message_chain(node.content)
                            }
                        })
                    logger.debug(f"转发消息构建完成，共 {len(forward_msgs)} 个节点")

                    await client.api.call_action(
                        'send_group_forward_msg',
                        group_id=int(manage_group_id),
                        messages=forward_msgs
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
                if comp.url:
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
            message_id = str(event.message_obj.message_id) if hasattr(event.message_obj, 'message_id') else ""
            message_str = event.message_str
            logger.debug(f"消息ID: {message_id}, 消息内容: {message_str[:50]}...")

            # 检查是否是图片消息
            message_chain = event.get_messages()
            image_url = None
            image_md5 = None
            message_type = "text"

            for comp in message_chain:
                if isinstance(comp, Comp.Image):
                    image_url = comp.url
                    message_type = "image"
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

            # 进行图片审核
            logger.debug(f"开始审核图片，URL: {image_url}")
            risk_level, risk_reason, md5_hash = await self._censor_flow.submit_image(
                image_url, group_id, precalculated_md5=image_md5
            )
            logger.debug(f"图片审核完成，结果: 风险等级={risk_level.name}, 原因={risk_reason}, MD5={md5_hash}")

            # 处理违规
            if risk_level in (RiskLevel.Review, RiskLevel.Block):
                logger.debug(f"检测到违规图片，开始处理")
                await self._handle_violation(
                    event, group_id, user_id, user_name,
                    md5_hash, image_url, risk_level, risk_reason, message_id
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
            result += f"━━━━━━━━━━━━━━━\n"
            result += f"总违规次数: {violation_count}\n"
            result += f"━━━━━━━━━━━━━━━\n"

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
            image_enabled = self._censor_flow and self._censor_flow.is_image_censor_enabled()
            status_info += f"图片审核: {'✅ 已启用' if image_enabled else '❌ 未启用'}\n"

            # 检查配置
            image_provider = self._config.get("image_censor_provider", "未配置")
            status_info += f"图片审核提供商: {image_provider}\n"

            # 检查群聊配置
            status_info += f"\n已配置的群聊:\n"
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
            info += f"注意: 消息缓存已取消，不再存储\n"
            info += "━━━━━━━━━━━━━━━"

            yield event.plain_result(info)

        except Exception as e:
            logger.error(f"清除缓存异常: {e}")

    @filter.command("删除违规")
    async def delete_violation(self, event: AstrMessageEvent, user_id_str: str = ""):
        """删除指定用户的违规记录（管理群专用）"""
        try:
            manage_group_id = str(event.get_group_id()) if event.get_group_id() else None
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

            deleted_count = await self._db.delete_user_violations(user_id, target_group_id)

            yield event.plain_result(f"✅ 已删除用户 {user_id} 在群 {target_group_id} 的违规记录，共 {deleted_count} 条")

        except Exception as e:
            logger.error(f"删除违规记录异常: {e}")
