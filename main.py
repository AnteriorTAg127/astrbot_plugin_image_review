"""
图片审核插件主模块
提供图片内容审核、违规处理、管理群通知等功能
"""

import asyncio
import os
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .censors import CensorError, CensorFlow
from .database import DatabaseManager, RiskLevel
from .handlers import AdminManager, CommandHandlers, ConfigManager, ViolationHandler
from .utils import ImageUtils


@register(
    "image_review",
    "AnteriorTAg127",
    "图片审核插件，提供图片内容审核、违规处理、管理群通知等功能",
    "1.3.5",
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

        # 初始化各个管理器
        self._config_manager = ConfigManager(self._config)
        self._admin_manager = AdminManager()
        self._violation_handler = ViolationHandler(
            self._db,
            self._config_manager,
            self._admin_manager,
            self._evidence_dir,
        )

        # 审核流程管理器（延迟初始化）
        self._censor_flow: CensorFlow | None = None

        # 定时任务引用
        self._cleanup_task: asyncio.Task | None = None

        logger.debug("图片审核插件初始化完成")

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

            # 缓存消息ID（用于违规时处理）
            message_id = (
                str(event.message_obj.message_id)
                if hasattr(event.message_obj, "message_id")
                else ""
            )

            # 检查是否启用了图片审核（基础配置检查）
            if not self._config_manager.is_group_enabled(group_id):
                return

            # 检查是否是管理员发言，更新最后管理员发言时间
            config = self._config_manager.get_group_config(group_id)
            no_admin_minutes = (
                config.get("auto_censor_no_admin_minutes", 0) if config else 0
            )
            if no_admin_minutes > 0:
                is_admin = await self._admin_manager.is_user_admin_cached(
                    event, group_id, user_id
                )
                if is_admin:
                    self._admin_manager.record_admin_message(group_id)

            # 检查是否应该开启审查
            last_admin_time = self._admin_manager.get_last_admin_time(group_id)
            should_enable, reason = self._config_manager.should_enable_censor(
                group_id, last_admin_time
            )
            if not should_enable:
                return

            # 获取群配置
            group_config = self._config_manager.get_group_config(group_id)

            # 检查是否是图片消息
            message_chain = event.get_messages()
            images_to_check = []

            # 检查是否跳过QQ自带表情包
            skip_qq_emoji = self._config.get("skip_qq_builtin_emoji", True)

            for comp in message_chain:
                if isinstance(comp, Comp.Image):
                    image_url = comp.url
                    image_md5 = ImageUtils.extract_image_md5(event, comp)

                    # 跳过QQ官方表情包（如果开启此选项）
                    if skip_qq_emoji and ImageUtils.is_qq_builtin_emoji(image_url):
                        continue

                    if image_url:
                        images_to_check.append((image_url, image_md5))

                elif isinstance(comp, Comp.Forward):
                    # 检查是否启用了转发消息图片检测
                    enable_forward_censor = self._config.get(
                        "enable_forward_image_censor", False
                    )
                    if not enable_forward_censor:
                        continue

                    # 处理转发消息中的图片
                    forward_images = await self._extract_forward_images(event, comp)
                    if forward_images:
                        # 应用抽检逻辑
                        sampled_images = self._sample_images(
                            forward_images, group_id, group_config
                        )
                        images_to_check.extend(sampled_images)

            # 检查是否是图片消息且启用了图片审核
            if not images_to_check:
                return
            if not self._censor_flow:
                return
            if not self._censor_flow.is_image_censor_enabled():
                return
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
                        image_data,
                    ) = await self._censor_flow.submit_image(
                        image_url,
                        group_id,
                        precalculated_md5=image_md5,
                        base_expire_hours=base_expire_hours,
                        max_expire_days=max_expire_days,
                    )

                    # 处理违规
                    if risk_level in (RiskLevel.Review, RiskLevel.Block):
                        await self._violation_handler.handle_violation(
                            event,
                            group_id,
                            user_id,
                            user_name,
                            md5_hash,
                            image_url,
                            risk_level,
                            risk_reason,
                            message_id,
                            image_data,
                        )
                except CensorError as e:
                    logger.error(f"图片审核异常: {e}")
                except Exception as e:
                    logger.error(f"处理图片异常: {e}")

        except CensorError as e:
            logger.error(f"图片审核异常: {e}")
        except Exception as e:
            logger.error(f"消息处理异常: {e}")

    async def _extract_forward_images(
        self, event: AstrMessageEvent, forward_comp: Comp.Forward
    ) -> list[tuple[str, str | None]]:
        """
        从转发消息中提取所有图片URL

        Args:
            event: 消息事件
            forward_comp: 转发消息组件

        Returns:
            图片URL和MD5列表
        """
        images = []
        try:
            # 获取转发消息ID
            forward_id = None
            if hasattr(forward_comp, "id") and forward_comp.id:
                forward_id = forward_comp.id
            elif hasattr(forward_comp, "forward_id") and forward_comp.forward_id:
                forward_id = forward_comp.forward_id

            if not forward_id:
                logger.debug("转发消息ID为空，无法获取内容")
                return images

            # 通过API获取转发消息内容
            platform_name = event.get_platform_name()
            if platform_name == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                    AiocqhttpMessageEvent,
                )

                if isinstance(event, AiocqhttpMessageEvent):
                    client = event.bot
                    try:
                        # 调用get_forward_msg API获取转发消息内容
                        forward_data = await client.api.call_action(
                            "get_forward_msg", message_id=forward_id
                        )

                        if forward_data and "messages" in forward_data:
                            for msg in forward_data["messages"]:
                                if "message" in msg:
                                    # 解析消息内容
                                    for msg_item in msg["message"]:
                                        if (
                                            isinstance(msg_item, dict)
                                            and msg_item.get("type") == "image"
                                        ):
                                            image_data = msg_item.get("data", {})
                                            image_url = image_data.get("url", "")
                                            # 尝试获取MD5，如果没有则尝试从file字段提取
                                            image_md5 = image_data.get("md5", "")
                                            if not image_md5:
                                                # 从file字段提取，如 "ABCDEFG.jpg" → "ABCDEFG"
                                                file_field = image_data.get("file", "")
                                                if file_field:
                                                    # 移除扩展名
                                                    image_md5 = (
                                                        file_field.split(".")[0]
                                                        if "." in file_field
                                                        else file_field
                                                    )
                                            if image_url:
                                                images.append((image_url, image_md5))
                    except Exception as e:
                        logger.debug(f"获取转发消息内容失败: {e}")

        except Exception as e:
            logger.debug(f"提取转发消息图片异常: {e}")
        return images

    def _sample_images(
        self,
        images: list[tuple[str, str | None]],
        group_id: str,
        group_config: dict | None,
    ) -> list[tuple[str, str | None]]:
        """
        对转发消息中的图片进行抽检

        Args:
            images: 所有图片列表
            group_id: 群ID
            group_config: 群配置

        Returns:
            抽检后的图片列表
        """
        if not images:
            return []

        # 获取抽检配置（使用旧版配置项名称）
        if group_config:
            sample_threshold = group_config.get("forward_image_sample_threshold", 0)
            sample_rate = group_config.get("forward_image_sample_rate", 0.5)
        else:
            sample_threshold = self._config.get("forward_image_sample_threshold", 0)
            sample_rate = self._config.get("forward_image_sample_rate", 0.5)

        # 如果图片数量小于阈值，全量检测
        if len(images) <= sample_threshold:
            logger.debug(
                f"转发消息图片数 {len(images)} <= 阈值 {sample_threshold}，全量检测"
            )
            return images

        # 超过阈值，按比例抽检
        import random

        sample_count = max(1, int(len(images) * sample_rate))
        sampled = random.sample(images, min(sample_count, len(images)))
        logger.info(
            f"群 {group_id} 转发消息抽检: 共 {len(images)} 张图片，抽检 {len(sampled)} 张"
        )
        return sampled

    # ========== 命令处理（委托给 CommandHandlers）==========

    @filter.command("查询违规")
    async def query_violation(self, event: AstrMessageEvent, user_id_str: str = ""):
        """查询用户违规记录（管理群专用）"""
        handlers = CommandHandlers(
            self._db,
            self._config_manager,
            self._admin_manager,
            self._violation_handler,
            self._config,
        )
        async for result in handlers.query_violation(event, user_id_str):
            yield result

    @filter.command("审核状态")
    async def check_status(self, event: AstrMessageEvent):
        """查看审核插件状态（管理群专用）"""
        handlers = CommandHandlers(
            self._db,
            self._config_manager,
            self._admin_manager,
            self._violation_handler,
            self._config,
        )
        async for result in handlers.check_status(event):
            yield result

    @filter.command("清除缓存")
    async def clear_cache(self, event: AstrMessageEvent):
        """清除所有缓存数据（黑白名单）（管理群专用）"""
        handlers = CommandHandlers(
            self._db,
            self._config_manager,
            self._admin_manager,
            self._violation_handler,
            self._config,
        )
        async for result in handlers.clear_cache(event):
            yield result

    @filter.command("查询名单")
    async def query_list_status(self, event: AstrMessageEvent):
        """查询图片在黑白名单中的状态（管理群专用，需引用图片）"""
        handlers = CommandHandlers(
            self._db,
            self._config_manager,
            self._admin_manager,
            self._violation_handler,
            self._config,
        )
        async for result in handlers.query_list_status(event):
            yield result

    @filter.command("删除违规")
    async def delete_violation(self, event: AstrMessageEvent, user_id_str: str = ""):
        """删除指定用户的违规记录（管理群专用）"""
        handlers = CommandHandlers(
            self._db,
            self._config_manager,
            self._admin_manager,
            self._violation_handler,
            self._config,
        )
        async for result in handlers.delete_violation(event, user_id_str):
            yield result

    @filter.command("添加白名单")
    async def add_manual_whitelist_cmd(self, event: AstrMessageEvent, reason: str = ""):
        """添加图片到人工白名单（管理群专用，需引用图片）"""
        handlers = CommandHandlers(
            self._db,
            self._config_manager,
            self._admin_manager,
            self._violation_handler,
            self._config,
        )
        async for result in handlers.add_manual_whitelist_cmd(event, reason):
            yield result

    @filter.command("移除白名单")
    async def remove_manual_whitelist_cmd(self, event: AstrMessageEvent):
        """从人工白名单移除图片（管理群专用，需引用图片）"""
        handlers = CommandHandlers(
            self._db,
            self._config_manager,
            self._admin_manager,
            self._violation_handler,
            self._config,
        )
        async for result in handlers.remove_manual_whitelist_cmd(event):
            yield result

    @filter.command("清空白名单")
    async def clear_manual_whitelist_cmd(
        self, event: AstrMessageEvent, confirm: str = ""
    ):
        """清空人工白名单（管理群专用，需二次确认）"""
        handlers = CommandHandlers(
            self._db,
            self._config_manager,
            self._admin_manager,
            self._violation_handler,
            self._config,
        )
        async for result in handlers.clear_manual_whitelist_cmd(event, confirm):
            yield result

    @filter.command("添加黑名单")
    async def add_manual_blacklist_cmd(
        self, event: AstrMessageEvent, risk_level_str: str = "", reason: str = ""
    ):
        """添加图片到人工黑名单（管理群专用，需引用图片）"""
        handlers = CommandHandlers(
            self._db,
            self._config_manager,
            self._admin_manager,
            self._violation_handler,
            self._config,
        )
        async for result in handlers.add_manual_blacklist_cmd(
            event, risk_level_str, reason
        ):
            yield result

    @filter.command("移除黑名单")
    async def remove_manual_blacklist_cmd(self, event: AstrMessageEvent):
        """从人工黑名单移除图片（管理群专用，需引用图片）"""
        handlers = CommandHandlers(
            self._db,
            self._config_manager,
            self._admin_manager,
            self._violation_handler,
            self._config,
        )
        async for result in handlers.remove_manual_blacklist_cmd(event):
            yield result

    @filter.command("清空黑名单")
    async def clear_manual_blacklist_cmd(
        self, event: AstrMessageEvent, confirm: str = ""
    ):
        """清空人工黑名单（管理群专用，需二次确认）"""
        handlers = CommandHandlers(
            self._db,
            self._config_manager,
            self._admin_manager,
            self._violation_handler,
            self._config,
        )
        async for result in handlers.clear_manual_blacklist_cmd(event, confirm):
            yield result

    @filter.command("移除自动白名单")
    async def remove_auto_whitelist_cmd(self, event: AstrMessageEvent):
        """从自动白名单移除图片（管理群专用，需引用图片）"""
        handlers = CommandHandlers(
            self._db,
            self._config_manager,
            self._admin_manager,
            self._violation_handler,
            self._config,
        )
        async for result in handlers.remove_auto_whitelist_cmd(event):
            yield result

    @filter.command("移除自动黑名单")
    async def remove_auto_blacklist_cmd(self, event: AstrMessageEvent):
        """从自动黑名单移除图片（管理群专用，需引用图片）"""
        handlers = CommandHandlers(
            self._db,
            self._config_manager,
            self._admin_manager,
            self._violation_handler,
            self._config,
        )
        async for result in handlers.remove_auto_blacklist_cmd(event):
            yield result

    @filter.command("审查帮助")
    async def review_help(self, event: AstrMessageEvent):
        """显示图片审核插件帮助信息"""
        handlers = CommandHandlers(
            self._db,
            self._config_manager,
            self._admin_manager,
            self._violation_handler,
            self._config,
        )
        async for result in handlers.review_help(event):
            yield result
