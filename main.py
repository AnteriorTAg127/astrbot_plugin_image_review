"""
图片审核插件主模块
提供图片内容审核、违规处理、管理群通知等功能
"""

import asyncio
import json
import os
from typing import Any

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .censors import CensorError, CensorFlow, download_image
from .database import DatabaseManager, RiskLevel
from .handlers import AdminManager, ConfigManager, ViolationHandler
from .utils import ImageUtils, MessageUtils
from .utils.card_utils import (
    extract_json_card_images,
    extract_jump_url,
    extract_og_image,
    is_music_video_card,
)


@register(
    "image_review",
    "AnteriorTAg127",
    "图片审核插件，提供图片内容审核、违规处理、管理群通知等功能",
    "1.6.3",
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

        # Dashboard Web API 处理器（v1.5.0，延迟初始化）
        self._web_api = None

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

            # v1.5.0: 尽力回填旧违规记录的证据图片路径（幂等，供 WebUI 展示）
            try:
                await self._db.backfill_evidence_paths(self._evidence_dir)
            except Exception as e:
                logger.error(f"证据图片路径回填异常: {e}")

            # v1.5.0: 注册 Dashboard Web API（WebUI 管理面板后端）
            try:
                from .web_api import WebApiHandler

                self._web_api = WebApiHandler(
                    self._db, self._config_manager, self._evidence_dir, self.context
                )
                self._web_api.register(self.context)
            except Exception:
                logger.exception("注册 Dashboard Web API 失败，WebUI 将不可用")

            # 启动定时清理任务（每天执行一次）
            self._cleanup_task = asyncio.create_task(self._cleanup_expired_entries())
        except Exception as e:
            logger.error(f"图片审核插件初始化失败: {e}")

    async def _cleanup_expired_entries(self):
        """定时清理过期的黑白名单条目与过期审核日志"""
        while True:
            try:
                # 每天执行一次清理
                await asyncio.sleep(24 * 60 * 60)  # 24小时
                await self._db.clean_expired_list_entries()
                # v1.6.0：保留天数可在 WebUI 自定义（存 DB，每轮清理读取，改完下轮生效）
                retention = await self._db.get_retention_settings()
                audit_keep = retention["audit_log_retention_days"]
                cost_keep = retention["cost_log_retention_days"]
                deleted = await self._db.cleanup_audit_log(keep_days=audit_keep)
                if deleted:
                    logger.info(
                        f"审核日志清理完成，删除 {deleted} 条过期记录（保留 {audit_keep} 天）"
                    )
                cost_deleted = await self._db.cleanup_cost_log(keep_days=cost_keep)
                if cost_deleted:
                    logger.info(
                        f"成本日志清理完成，删除 {cost_deleted} 条过期记录（保留 {cost_keep} 天）"
                    )
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

            # 账号白名单检查（全局或本群条目命中即跳过审核，v1.5.1）
            if self._config.get("enable_account_whitelist", True):
                if await self._db.check_account_whitelist(user_id, group_id):
                    logger.debug(f"用户 {user_id} 在账号白名单中，跳过审核")
                    return

            # 获取群配置
            group_config = self._config_manager.get_group_config(group_id)

            # 缓存配置（v1.6.3 起提前计算：卡片质量流程与审核流水线共用）
            base_expire_hours = (
                group_config.get("base_expire_hours", 2) if group_config else 2
            )
            max_expire_days = (
                group_config.get("max_expire_days", 14) if group_config else 14
            )

            # 检查是否是图片消息
            message_chain = event.get_messages()
            images_to_check = []
            # v1.6.3：图片来源类型映射（image/forward/card），供审核日志 scene 列
            scene_map: dict[str, str] = {}

            # 检查是否跳过QQ自带表情包
            skip_qq_emoji = self._config.get("skip_qq_builtin_emoji", True)

            # 优先从协议端原始消息提取图片（避免 v4.26.x PreProcessStage 物化后
            # comp.url 变成本地路径，导致下载/Aliyun 审核/表情跳过全部失效）
            original_images = self._extract_original_images(event)
            if original_images is not None:
                # 主路径：用协议端原始公网 URL 走正常流水线
                for image_url, image_md5 in original_images:
                    if skip_qq_emoji and ImageUtils.is_qq_builtin_emoji(image_url):
                        continue
                    images_to_check.append((image_url, image_md5))
            else:
                # 回退路径：raw_message 不可用（非 aiocqhttp 等），用消息链中的 comp.url
                for comp in message_chain:
                    if isinstance(comp, Comp.Image):
                        image_url = comp.url
                        image_md5 = ImageUtils.extract_image_md5(event, comp)
                        if skip_qq_emoji and ImageUtils.is_qq_builtin_emoji(image_url):
                            continue
                        if image_url:
                            images_to_check.append((image_url, image_md5))

            # 转发消息图片（不受物化影响，走 get_forward_msg API 获取原始 URL）
            if self._config.get("enable_forward_image_censor", False):
                for comp in message_chain:
                    if isinstance(comp, Comp.Forward):
                        # 处理转发消息中的图片
                        forward_images = await self._extract_forward_images(event, comp)
                        if forward_images:
                            # 应用抽检逻辑
                            sampled_images = self._sample_images(
                                forward_images, group_id
                            )
                            images_to_check.extend(sampled_images)
                            # v1.6.3：转发图片审核日志标注 scene='forward'
                            for forward_url, _ in sampled_images:
                                scene_map[forward_url] = "forward"

            # 卡片消息图片审核（v1.6.3：share/json 段内嵌图片，独立开关控制）
            if self._config.get("enable_card_image_censor", False):
                try:
                    card_images = await self._collect_card_images(event)
                except Exception as e:
                    logger.debug(f"收集卡片消息图片异常: {e}")
                    card_images = []
                if card_images:
                    # 复用转发抽检配置（阈值/比例，不新增配置项）
                    sampled_cards = self._sample_images(card_images, group_id)
                    # 降级（需人工审核）项存在时才懒查询管理员身份，避免无谓网络查询
                    degrade_admin_loaded = False
                    degrade_is_admin = False
                    for card_item in sampled_cards:
                        try:
                            processed = await self._process_card_image(
                                card_item,
                                group_id,
                                base_expire_hours,
                                max_expire_days,
                            )
                        except Exception as e:
                            logger.debug(f"卡片图片质量处理异常: {e}")
                            continue
                        if processed is None:
                            continue  # 下载失败等整体问题，跳过该卡片图
                        final_url, card_md5, degrade_reason = processed
                        if degrade_reason:
                            # 降级：仅审核日志 + 管理群人工审核通知，绝不进入违规链
                            if not degrade_admin_loaded:
                                degrade_admin_loaded = True
                                try:
                                    degrade_is_admin = (
                                        await self._admin_manager.is_user_admin(
                                            event, group_id, user_id
                                        )
                                    )
                                except Exception:
                                    degrade_is_admin = False
                            try:
                                await self._db.record_audit(
                                    group_id,
                                    user_id,
                                    user_name,
                                    md5_hash=None,
                                    risk_level=RiskLevel.Review,
                                    risk_reason="卡片图片分辨率过低，已标记需人工审核",
                                    source=self._config.get(
                                        "image_censor_provider", "Aliyun"
                                    ),
                                    scene="card",
                                    is_admin=degrade_is_admin,
                                )
                            except Exception as e:
                                logger.error(f"记录卡片图片降级审核日志异常: {e}")
                            try:
                                await self._violation_handler.notify_manual_review(
                                    event,
                                    group_id,
                                    user_id,
                                    user_name,
                                    card_item["url"],
                                    "卡片图片分辨率过低，无法自动审核",
                                )
                            except Exception as e:
                                logger.error(f"发送卡片图片人工审核通知异常: {e}")
                            continue
                        # 合格：与普通图片一致进入审核流水线（md5 预计算跳过首次下载）
                        images_to_check.append((final_url, card_md5))
                        scene_map[final_url] = "card"

            # 检查是否是图片消息且启用了图片审核
            if not images_to_check:
                return
            if not self._censor_flow:
                return
            if not self._censor_flow.is_image_censor_enabled():
                return

            # 预加载全部模型定价表（v1.5.4 成本记账用，一次 DB 查所有避免循环内 N 次查询）
            pricing_map = (
                {p["model_id"]: p for p in await self._db.list_model_pricing()}
                if self._config.get("image_censor_provider") in ("VLAI", None)
                else {}
            )

            # v1.6.1：同一消息事件内的违规先收集，循环结束后统一批量通报，
            # 修复合并转发消息内多张违规图片被逐张通报（管理群刷屏）的问题
            pending_violations: list[dict] = []

            # v1.6.1：管理员身份查询一次即可（同一事件内同群同用户结果不变），
            # 供循环内审核日志标注使用，避免每张图片一次网络查询
            try:
                audit_is_admin = await self._admin_manager.is_user_admin(
                    event, group_id, user_id
                )
            except Exception:
                audit_is_admin = False

            # 顺序处理所有图片（避免并发过高）
            for image_url, image_md5 in images_to_check:
                usages: list = []

                def usage_sink(pid, u):
                    usages.append((pid, u))

                if not pricing_map:
                    usage_sink = None
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
                        usage_sink=usage_sink,
                    )

                    # 记录审核日志（无论结果如何，供 WebUI 统计与浏览，v1.5.0）
                    # 一并记录管理员身份，使审核日志与违规记录的管理员标注口径一致
                    audit_id = None
                    try:
                        audit_id = await self._db.record_audit(
                            group_id,
                            user_id,
                            user_name,
                            md5_hash,
                            risk_level,
                            risk_reason,
                            source=self._config.get("image_censor_provider", "Aliyun"),
                            scene=scene_map.get(image_url, "image"),
                            is_admin=audit_is_admin,
                        )
                    except Exception as e:
                        logger.error(f"记录审核日志异常: {e}")

                    # 处理违规：先收集，循环结束后统一批量通报（v1.6.1）
                    if risk_level in (RiskLevel.Review, RiskLevel.Block):
                        # 计算感知哈希（仅违规时，供 WebUI 展示十六进制；失败不阻断）
                        phash: str | None = None
                        dhash: str | None = None
                        if image_data:
                            try:
                                phash, dhash = await asyncio.to_thread(
                                    ImageUtils.calculate_image_hashes, image_data
                                )
                            except Exception as e:
                                logger.debug(f"计算感知哈希失败: {e}")
                        pending_violations.append(
                            {
                                "md5_hash": md5_hash,
                                "image_url": image_url,
                                "risk_level": risk_level,
                                "risk_reason": risk_reason,
                                "image_data": image_data,
                                "phash": phash,
                                "dhash": dhash,
                            }
                        )
                except CensorError as e:
                    logger.error(f"图片审核异常: {e}")
                except Exception as e:
                    logger.error(f"处理图片异常: {e}")

                # v1.6.0：成本记账（遍历本次审核中所有 LLM 调用）。无论该模型是否已配置
                # 单价都记录用量，使「所有被调用过的模型」都出现在成本概览；未配置单价者
                # 以 0 价记账（仅累计 token / 调用次数），待用户在「成本配置」补价后再计成本。
                audit_total_cost = 0.0
                for upid, usage in usages:
                    if not usage:
                        continue
                    pricing = pricing_map.get(upid)
                    if pricing:
                        currency = pricing["currency"]
                        price_per = pricing["price_per"]
                        in_price = pricing["input_price"]
                        cached_price = pricing["cached_price"]
                        out_price = pricing["output_price"]
                    else:
                        currency, price_per = "CNY", 1000000
                        in_price = cached_price = out_price = 0.0
                    try:
                        await self._db.record_cost(
                            upid,
                            currency,
                            price_per,
                            in_price,
                            cached_price,
                            out_price,
                            usage.input_other,
                            usage.input_cached,
                            usage.output,
                        )
                        if pricing:
                            audit_total_cost += (
                                usage.input_other * in_price
                                + usage.input_cached * cached_price
                                + usage.output * out_price
                            ) / price_per
                    except Exception as e:
                        logger.debug(f"记录 LLM 成本失败: {e}")
                # 回写到当次审核日志（供审核日志列展示单次成本 + 概览均价）
                if audit_id and audit_total_cost > 0:
                    try:
                        await self._db.set_audit_cost(audit_id, audit_total_cost)
                    except Exception as e:
                        logger.debug(f"回写审核成本失败: {e}")

            # v1.6.1：统一处理本事件内收集到的所有违规
            # （处罚/记录逐张执行，通报只发一条，修复合并转发多图多次通报）
            if pending_violations:
                await self._violation_handler.handle_violations_batch(
                    event,
                    group_id,
                    user_id,
                    user_name,
                    message_id,
                    pending_violations,
                )

        except CensorError as e:
            logger.error(f"图片审核异常: {e}")
        except Exception as e:
            logger.error(f"消息处理异常: {e}")

    def _extract_original_images(
        self, event: AstrMessageEvent
    ) -> list[tuple[str, str | None]] | None:
        """
        从协议端原始消息提取图片 (url, md5)，按消息顺序对齐

        v4.26.x PreProcessStage 会把 Image 组件的 url/file/path 覆写为本地临时路径，
        但不会修改 message_obj.raw_message。本方法从 raw_message 中恢复原始公网 URL
        与 QQ MD5 文件名，使下游下载、Aliyun 审核、表情跳过等按物化前逻辑工作。

        目前仅适配 aiocqhttp 平台（Event.message 为 OneBot 段列表）。

        Args:
            event: 消息事件

        Returns:
            list[(url, md5)]：成功提取时返回（可能为空列表，顺序与消息中图片顺序一致）。
            None：该平台不支持或 raw_message 不可用，调用方需回退到 comp.url。
        """
        try:
            if event.get_platform_name() != "aiocqhttp":
                return None
            raw = getattr(event.message_obj, "raw_message", None)
            segments = raw.get("message") if isinstance(raw, dict) else None
            if not isinstance(segments, list):
                return None

            result: list[tuple[str, str | None]] = []
            for seg in segments:
                if not (isinstance(seg, dict) and seg.get("type") == "image"):
                    continue
                data = seg.get("data", {}) or {}
                url = data.get("url", "")
                if not url.startswith("http"):
                    continue  # 无公网 URL 的段交给回退分支处理
                md5 = ImageUtils.extract_md5_from_filename(data.get("file", ""))
                result.append((url, md5))
            return result
        except Exception as e:
            logger.debug(f"从 raw_message 提取原始图片失败，将回退到 comp.url: {e}")
            return None

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

    async def _collect_card_images(self, event: AstrMessageEvent) -> list[dict]:
        """
        收集卡片消息内嵌图片（v1.6.3，share/json 段）

        返回 list[dict]，每项 {"url": 图片URL, "jump_url": 跳转链接|None}：
        - Comp.Share: image 非空 → (comp.image, comp.url)
        - Comp.Json: data 为 dict，先 is_music_video_card 过滤（音乐/视频卡片跳过），
          再 extract_json_card_images 递归提取图片、extract_jump_url 取跳转链接
        - Comp.Music / Comp.Video：跳过
        - 消息链中无 Comp.Json 但 raw_message 有 json 段（OneBot json 段含 HTML
          实体如 &#44; 时 adapter 构造 Json 组件失败、组件被忽略）→ 从 raw_message 回退
        - 无图卡片返回空列表（不记录、不统计）

        Args:
            event: 消息事件

        Returns:
            卡片图片列表（已按 URL 去重）；异常时返回空列表
        """
        cards: list[dict] = []
        try:
            chain = event.get_messages()
            has_json_comp = False
            for comp in chain:
                if isinstance(comp, Comp.Share):
                    image = getattr(comp, "image", "") or ""
                    if image.startswith(("http://", "https://")):
                        cards.append(
                            {
                                "url": image,
                                "jump_url": getattr(comp, "url", "") or None,
                            }
                        )
                elif isinstance(comp, Comp.Json):
                    has_json_comp = True
                    data = getattr(comp, "data", None)
                    if not isinstance(data, dict):
                        continue
                    if is_music_video_card(data):
                        continue
                    jump_url = extract_jump_url(data)
                    for url in extract_json_card_images(data):
                        cards.append({"url": url, "jump_url": jump_url})
                # Comp.Music / Comp.Video 及其他组件：跳过

            # 解析失败回退：消息链无 Json 组件时从 raw_message 提取 json 段
            if not has_json_comp:
                cards.extend(self._extract_json_cards_from_raw_message(event))

            # 去重（share 与 json 段可能携带相同 URL）
            deduped: list[dict] = []
            seen_urls: set[str] = set()
            for item in cards:
                if item["url"] in seen_urls:
                    continue
                seen_urls.add(item["url"])
                deduped.append(item)
            return deduped
        except Exception as e:
            logger.debug(f"收集卡片消息图片异常: {e}")
            return []

    def _extract_json_cards_from_raw_message(
        self, event: AstrMessageEvent
    ) -> list[dict]:
        """
        从 raw_message 提取 json 段并解析为卡片图片（v1.6.3 解析失败回退）

        OneBot json 段在 adapter 构造 Comp.Json 时 json.loads 失败（如 JSON
        字符串含 &#44; 等 HTML 实体）后组件会被忽略，消息链中没有 Json 组件，
        此时只能从 raw_message 段列表读取原始 JSON 字符串，反转义后解析
        （参照 astrbot/core/utils/quoted_message/chain_parser.py 先例）。

        Args:
            event: 消息事件

        Returns:
            卡片图片列表（每项 {"url", "jump_url"}）；raw_message 不可用或
            解析失败时返回空列表
        """
        cards: list[dict] = []
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            segments = raw.get("message") if isinstance(raw, dict) else None
            if not isinstance(segments, list):
                return cards
            for seg in segments:
                if not (isinstance(seg, dict) and seg.get("type") == "json"):
                    continue
                seg_data = seg.get("data", {}) or {}
                raw_json = seg_data.get("data", "")
                if not isinstance(raw_json, str) or not raw_json.strip():
                    continue
                try:
                    raw_json = raw_json.replace("&#44;", ",")
                    parsed = json.loads(raw_json)
                except Exception as e:
                    logger.debug(f"json 卡片段解析失败，跳过: {e}")
                    continue
                if not isinstance(parsed, dict):
                    continue
                if is_music_video_card(parsed):
                    continue
                jump_url = extract_jump_url(parsed)
                for url in extract_json_card_images(parsed):
                    cards.append({"url": url, "jump_url": jump_url})
        except Exception as e:
            logger.debug(f"从 raw_message 提取 json 卡片异常: {e}")
        return cards

    async def _process_card_image(
        self,
        card_item: dict,
        group_id: str,
        base_expire_hours: int,
        max_expire_days: int,
    ) -> tuple[str, str | None, str | None] | None:
        """
        卡片图片质量处理（v1.6.3，进入审核流水线前）

        1. 下载图片 → ImageUtils.get_image_size 读取尺寸（PIL 不可用/解析失败视为合格）
        2. 最短边 ≥ card_image_min_side → 直接使用原 URL（md5 一并计算返回）
        3. 尺寸不足且开启 enable_card_image_fetch_original → 从卡片跳转页抓取
           og:image 原图并验证分辨率，合格则用原图 URL（md5 重算）
        4. 其余情况（抓取失败/开关关闭）→ 降级为需人工审核

        Args:
            card_item: 卡片图片项，含 url 与 jump_url
            group_id: 群ID（保留参数）
            base_expire_hours: 基础缓存过期小时（保留参数）
            max_expire_days: 最大缓存天数（保留参数）

        Returns:
            合格: (最终图片URL, md5, None)；md5 供 submit_image 的 precalculated_md5
            降级: ("", None, 降级原因)，调用方只记录日志并通知人工审核
            None: 下载失败等整体问题，跳过该卡片图
        """
        image_url = str(card_item.get("url", "") or "")
        if not image_url:
            return None
        try:
            image_data = await download_image(image_url)
        except CensorError as e:
            logger.debug(f"卡片图片下载失败，跳过审核: {image_url} - {e}")
            return None
        except Exception as e:
            logger.debug(f"卡片图片下载异常，跳过审核: {image_url} - {e}")
            return None

        min_side = int(self._config.get("card_image_min_side", 200) or 200)
        size = ImageUtils.get_image_size(image_data)
        if size is None:
            # PIL 不可用或解析失败：跳过分辨率检查，视为合格
            return (image_url, DatabaseManager.calculate_md5(image_data), None)

        if min(size) >= min_side:
            return (image_url, DatabaseManager.calculate_md5(image_data), None)

        # 分辨率不足
        if not self._config.get("enable_card_image_fetch_original", True):
            return ("", None, "resolution_too_low_fetch_disabled")

        # 尝试从卡片跳转页抓取 og:image 原图并验证
        og_image_url = await self._fetch_card_original_image(card_item)
        if og_image_url:
            try:
                og_data = await download_image(og_image_url)
            except CensorError as e:
                logger.debug(f"卡片原图下载失败: {og_image_url} - {e}")
                og_data = None
            except Exception as e:
                logger.debug(f"卡片原图下载异常: {og_image_url} - {e}")
                og_data = None
            if og_data is not None:
                og_size = ImageUtils.get_image_size(og_data)
                if og_size is not None and min(og_size) >= min_side:
                    return (og_image_url, DatabaseManager.calculate_md5(og_data), None)

        logger.debug(f"卡片图片分辨率不足且无法获取合格原图，降级: {image_url}")
        return ("", None, "resolution_too_low")

    async def _fetch_card_original_image(self, card_item: dict) -> str | None:
        """
        从卡片跳转页 HTML 抓取 og:image 原图 URL（v1.6.3）

        使用 aiohttp（5 秒超时），仅处理 http(s) 跳转链接；任何异常或缺失都
        返回 None，由调用方降级处理，不影响消息主流程。

        Args:
            card_item: 卡片图片项（含 jump_url）

        Returns:
            og:image URL；跳转链接缺失/请求失败/无 og:image 时返回 None
        """
        jump_url = str(card_item.get("jump_url", "") or "")
        if not jump_url.startswith(("http://", "https://")):
            return None
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(jump_url, timeout=timeout) as resp:
                    if resp.status != 200:
                        return None
                    html = await resp.text()
            return extract_og_image(html)
        except Exception as e:
            logger.debug(f"抓取卡片跳转页 og:image 失败: {jump_url} - {e}")
            return None

    def _sample_images(
        self,
        images: list[Any],
        group_id: str,
    ) -> list[Any]:
        """
        对转发消息/卡片消息中的图片进行抽检（不检查元素内部结构）

        Args:
            images: 所有图片列表（转发消息为 (url, md5) 元组，卡片为 {"url","jump_url"} dict）
            group_id: 群ID

        Returns:
            抽检后的图片列表
        """
        if not images:
            return []

        # 获取抽检配置（从全局配置读取）
        sample_threshold = self._config.get("forward_image_sample_threshold", 0)
        sample_rate = self._config.get("forward_image_sample_rate", 0.5)

        # 如果图片数量小于阈值，全量检测
        if len(images) <= sample_threshold:
            logger.debug(f"图片数 {len(images)} <= 阈值 {sample_threshold}，全量检测")
            return images

        # 超过阈值，按比例抽检
        import random

        sample_count = max(1, int(len(images) * sample_rate))
        sampled = random.sample(images, min(sample_count, len(images)))
        logger.info(
            f"群 {group_id} 图片抽检: 共 {len(images)} 张图片，抽检 {len(sampled)} 张"
        )
        return sampled

    # ========== 命令处理 ==========

    async def _check_admin_permission(
        self, event: AstrMessageEvent, group_id: str
    ) -> bool:
        """检查用户是否为管理员/群主"""
        enable_check = self._config.get("enable_admin_permission_check", False)
        if not enable_check:
            return True
        user_id = str(event.get_sender_id())
        return await self._admin_manager.is_user_admin(event, group_id, user_id)

    def _resolve_target_groups(
        self, manage_group_id: str, group_arg: str | None
    ) -> tuple[list[str] | None, str]:
        """解析管理群指令的操作目标群列表（v1.5.0 双群管理机制）

        规则（与 astrbot_plugin_content_audit 一致）:
        - 仅绑定 1 个被管理群: 命令直接作用于该群（无需群号）
        - 绑定多个: 必须指定 <群号|all>，未指定则拒绝并列出绑定群
        - 指定群号时校验其必须绑定到当前管理群

        Args:
            manage_group_id: 当前管理群 ID
            group_arg: 用户指定的群号参数，None 表示未提供

        Returns:
            (目标群列表, 错误信息)；错误信息非空时表示拒绝操作
        """
        managed = self._config_manager.get_group_ids_by_manage_group(manage_group_id)
        if not managed:
            return None, "当前管理群没有关联的被管理群"
        if len(managed) == 1:
            return managed, ""
        if not group_arg:
            return None, (
                "本管理群绑定了多个群，请指定群号或 all。绑定群：" + "、".join(managed)
            )
        if group_arg == "all":
            return managed, ""
        if group_arg not in managed:
            return None, f"群 {group_arg} 未绑定到本管理群"
        return [group_arg], ""

    def _parse_scope_arg(
        self, manage_group_id: str, reason: str, group_arg: str
    ) -> tuple[str, str]:
        """解析名单指令末尾的 [群号|all] 范围参数（v1.5.0，向后兼容）

        末位参数为 all 或本管理群已绑定群号时识别为范围；
        否则视为原因文本的一部分，范围取全局（旧用法不受影响）。

        Args:
            manage_group_id: 当前管理群 ID
            reason: 已解析的原因参数
            group_arg: 末位参数（可能为空）

        Returns:
            (最终原因, 群范围)；群范围 '' 表示全局
        """
        ga = (group_arg or "").strip()
        reason = (reason or "").strip()
        if not ga:
            return reason, ""
        if ga == "all":
            return reason, ""
        managed = self._config_manager.get_group_ids_by_manage_group(manage_group_id)
        if ga in managed:
            return reason, ga
        # 非有效范围参数 → 并入原因（兼容旧版 /添加白名单 <多词原因> 用法）
        return f"{reason} {ga}".strip(), ""

    def _format_list_scopes(self, entries: list[dict]) -> str:
        """将名单条目列表格式化为范围描述，如「全局、群123、群456」"""
        scopes = []
        for entry in entries:
            gid = entry.get("group_id", "")
            scopes.append("全局" if not gid else f"群{gid}")
        return "、".join(scopes)

    @filter.command("查询违规")
    async def query_violation(
        self, event: AstrMessageEvent, user_id_str: str = "", group_arg: str = ""
    ):
        """查询用户违规记录（管理群专用；多绑定群需指定 群号|all）"""
        try:
            group_id = str(event.get_group_id()) if event.get_group_id() else None
            if not group_id:
                return
            self._config_manager.maybe_reload()
            if not self._config_manager.is_manage_group(group_id):
                return
            if not await self._check_admin_permission(event, group_id):
                yield event.plain_result(
                    "❌ 您没有执行此命令的权限，需要管理员或群主身份"
                )
                return
            if not user_id_str:
                yield event.plain_result("使用方法: /查询违规 [QQ号] [群号|all]")
                return
            user_id = user_id_str.strip()
            targets, err = self._resolve_target_groups(
                group_id, group_arg.strip() or None
            )
            if err:
                yield event.plain_result(f"⚠️ {err}")
                return
            # 查询目标群范围内的违规记录并合并
            all_records: list[dict] = []
            for target in targets:
                records = await self._db.get_user_violation_records(
                    user_id, group_id=target, limit=10
                )
                all_records.extend(records)
            if not all_records:
                yield event.plain_result(f"用户 {user_id} 在目标群范围内暂无违规记录")
                return
            all_records.sort(
                key=lambda r: str(r.get("violation_time", "")), reverse=True
            )
            result = f"📊 用户 {user_id} 的违规记录\n"
            result += "━━━━━━━━━━━━━━━\n"
            result += f"总违规次数: {len(all_records)}\n"
            result += "━━━━━━━━━━━━━━━\n"
            for i, record in enumerate(all_records[:5], 1):
                violation_time = record.get("violation_time", "")
                risk_level = RiskLevel(record.get("risk_level", 0)).name
                risk_reason = record.get("risk_reason", "")
                group_id_record = record.get("group_id", "")
                mute_duration = record.get("mute_duration", 0)
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
    async def check_status(self, event: AstrMessageEvent, group_arg: str = ""):
        """查看审核插件状态（管理群专用；多绑定群需指定 群号|all）"""
        try:
            group_id = str(event.get_group_id()) if event.get_group_id() else None
            if not group_id:
                return
            self._config_manager.maybe_reload()
            if not self._config_manager.is_manage_group(group_id):
                return
            if not await self._check_admin_permission(event, group_id):
                yield event.plain_result(
                    "❌ 您没有执行此命令的权限，需要管理员或群主身份"
                )
                return
            status_info = "📊 图片审核插件状态\n"
            status_info += "━━━━━━━━━━━━━━━\n"
            status_info += f"图片审核: {'✅ 已启用' if self._censor_flow and self._censor_flow.is_image_censor_enabled() else '❌ 未启用'}\n"
            image_provider = self._config.get("image_censor_provider", "未配置")
            status_info += f"图片审核提供商: {image_provider}\n"
            if image_provider == "VLAI":
                vlai_config = self._config.get("vlai", {})
                provider_id = vlai_config.get("provider_id", "")
                status_info += (
                    f"VLAI 提供商ID: {provider_id if provider_id else '默认'}\n"
                )
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
                status_info += f"  └ 动图检测提供商ID: {gif_provider_id if gif_provider_id else '默认'}\n"
                status_info += f"  └ 采样帧数: {frame_count}\n"
                status_info += f"  └ 检测模式: {mode_str}\n"
            cache_counts = await self._db.get_cache_counts()
            status_info += "\n📋 自动名单统计\n"
            status_info += "━━━━━━━━━━━━━━━\n"
            status_info += f"自动白名单: {cache_counts['whitelist']} 条\n"
            status_info += f"自动黑名单: {cache_counts['blacklist']} 条\n"

            # v1.5.0: 按目标群输出违规统计（双群管理机制）
            targets, err = self._resolve_target_groups(
                group_id, group_arg.strip() or None
            )
            if err:
                status_info += f"\n⚠️ {err}\n"
            else:
                status_info += "\n📈 被管理群统计\n"
                status_info += "━━━━━━━━━━━━━━━\n"
                for target in targets:
                    stats = await self._db.get_group_violation_stats(target)
                    cfg = self._config_manager.get_group_config(target)
                    name = (cfg or {}).get("group_name") or ""
                    label = f"{name}({target})" if name else target
                    status_info += (
                        f"群 {label}: 今日审核 {stats['today_audits']} | "
                        f"今日违规 {stats['today_violations']} | "
                        f"累计违规 {stats['total_violations']}\n"
                    )
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
            self._config_manager.maybe_reload()
            if not self._config_manager.is_manage_group(group_id):
                return
            if not await self._check_admin_permission(event, group_id):
                yield event.plain_result(
                    "❌ 您没有执行此命令的权限，需要管理员或群主身份"
                )
                return
            result = await self._db.clear_all_cache()
            info = "🗑️ 缓存清除完成\n"
            info += "━━━━━━━━━━━━━━━\n"
            info += f"白名单: {result['whitelist']} 条\n"
            info += f"黑名单: {result['blacklist']} 条\n"
            info += "━━━━━━━━━━━━━━━"
            yield event.plain_result(info)
        except Exception as e:
            logger.error(f"清除缓存异常: {e}")

    @filter.command("查询名单")
    async def query_list_status(self, event: AstrMessageEvent):
        """查询图片在黑白名单中的状态（管理群专用，需引用图片；人工名单按范围展示）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return
            self._config_manager.maybe_reload()
            if not self._config_manager.is_manage_group(manage_group_id):
                return
            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result(
                    "❌ 您没有执行此命令的权限，需要管理员或群主身份"
                )
                return
            reply_info = MessageUtils.extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要查询的图片消息")
                return
            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return
            image_md5s = await MessageUtils.get_message_images(event, message_id)
            if not image_md5s:
                yield event.plain_result("❌ 引用的消息中没有图片")
                return
            result = "📋 图片名单状态查询\n"
            result += "━━━━━━━━━━━━━━━\n"
            for i, md5_hash in enumerate(image_md5s, 1):
                result += f"\n图片 {i} (MD5: {md5_hash[:16]}...)\n"
                # 人工白名单（按范围展示，v1.5.0）
                wl_entries = await self._db.get_manual_whitelist_entries(md5_hash)
                if wl_entries:
                    result += (
                        f"  人工白名单: ✅ {self._format_list_scopes(wl_entries)}\n"
                    )
                else:
                    result += "  人工白名单: ❌ 否\n"
                # 人工黑名单（按范围展示，v1.5.0）
                bl_entries = await self._db.get_manual_blacklist_entries(md5_hash)
                if bl_entries:
                    levels = "、".join(
                        RiskLevel(e.get("risk_level", 2)).name for e in bl_entries
                    )
                    result += (
                        f"  人工黑名单: ✅ {self._format_list_scopes(bl_entries)} "
                        f"(等级: {levels})\n"
                    )
                else:
                    result += "  人工黑名单: ❌ 否\n"
                in_auto_whitelist = await self._db.check_whitelist(
                    md5_hash, extend_on_hit=False
                )
                result += f"  自动白名单: {'✅ 是' if in_auto_whitelist else '❌ 否'}\n"
                auto_blacklist_result = await self._db.check_blacklist(
                    md5_hash, extend_on_hit=False
                )
                if auto_blacklist_result:
                    result += (
                        f"  自动黑名单: ✅ 是 (等级: {auto_blacklist_result[0].name})\n"
                    )
                else:
                    result += "  自动黑名单: ❌ 否\n"
            result += "━━━━━━━━━━━━━━━"
            yield event.plain_result(result)
        except Exception as e:
            logger.error(f"查询名单状态异常: {e}")
            yield event.plain_result(f"❌ 操作失败: {str(e)}")

    @filter.command("删除违规")
    async def delete_violation(
        self,
        event: AstrMessageEvent,
        user_id_str: str = "",
        arg2: str = "",
        arg3: str = "",
    ):
        """删除指定用户的违规记录（管理群专用；多绑定群需指定 群号|all；需确认）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return
            self._config_manager.maybe_reload()
            if not self._config_manager.is_manage_group(manage_group_id):
                return
            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result(
                    "❌ 您没有执行此命令的权限，需要管理员或群主身份"
                )
                return
            if not user_id_str:
                yield event.plain_result("使用方法: /删除违规 [QQ号] [群号|all] 确认")
                return
            user_id = user_id_str.strip()
            # 解析 确认 与 群号|all 参数（顺序不敏感）
            extras = [a.strip() for a in (arg2, arg3) if a.strip()]
            has_confirm = any(a == "确认" for a in extras)
            group_tokens = [a for a in extras if a != "确认"]
            group_arg = group_tokens[0] if group_tokens else None
            if not has_confirm:
                yield event.plain_result(
                    f"⚠️ 确认清除用户 {user_id} 的违规记录？请追加「确认」参数:\n"
                    f"/删除违规 {user_id} [群号|all] 确认"
                )
                return
            targets, err = self._resolve_target_groups(manage_group_id, group_arg)
            if err:
                yield event.plain_result(f"⚠️ {err}")
                return
            total_deleted = 0
            deleted_details = []
            for target_group_id in targets:
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

    @filter.command("添加白名单")
    async def add_manual_whitelist_cmd(
        self, event: AstrMessageEvent, reason: str = "", group_arg: str = ""
    ):
        """添加图片到人工白名单（管理群专用，需引用图片；末位 [群号|all] 指定范围，缺省全局）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return
            self._config_manager.maybe_reload()
            if not self._config_manager.is_manage_group(manage_group_id):
                return
            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result(
                    "❌ 您没有执行此命令的权限，需要管理员或群主身份"
                )
                return
            reply_info = MessageUtils.extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要添加到白名单的图片消息")
                return
            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return
            image_md5s = await MessageUtils.get_message_images(event, message_id)
            if not image_md5s:
                yield event.plain_result("❌ 引用的消息中没有图片")
                return
            user_id = str(event.get_sender_id())
            reason, scope = self._parse_scope_arg(manage_group_id, reason, group_arg)
            added_count = 0
            for md5_hash in image_md5s:
                success = await self._db.add_manual_whitelist(
                    md5_hash=md5_hash,
                    added_by=user_id,
                    reason=reason if reason else None,
                    group_id=scope,
                )
                if success:
                    added_count += 1
            scope_str = "全局" if not scope else f"群 {scope} 的"
            if added_count > 0:
                yield event.plain_result(
                    f"✅ 成功添加 {added_count} 张图片到{scope_str}人工白名单"
                )
            else:
                yield event.plain_result("⚠️ 图片已在该范围的人工白名单中")
        except Exception as e:
            logger.error(f"添加人工白名单异常: {e}")
            yield event.plain_result(f"❌ 操作失败: {str(e)}")

    @filter.command("移除白名单")
    async def remove_manual_whitelist_cmd(
        self, event: AstrMessageEvent, group_arg: str = ""
    ):
        """从人工白名单移除图片（管理群专用，需引用图片；[群号|all] 指定范围，缺省全部范围）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return
            self._config_manager.maybe_reload()
            if not self._config_manager.is_manage_group(manage_group_id):
                return
            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result(
                    "❌ 您没有执行此命令的权限，需要管理员或群主身份"
                )
                return
            reply_info = MessageUtils.extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要从白名单移除的图片消息")
                return
            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return
            image_md5s = await MessageUtils.get_message_images(event, message_id)
            if not image_md5s:
                yield event.plain_result("❌ 引用的消息中没有图片")
                return
            # 解析范围：all/缺省=全部范围；具体群号需绑定到本管理群
            ga = group_arg.strip()
            if ga in ("", "all"):
                scope: str | None = None
            else:
                managed = self._config_manager.get_group_ids_by_manage_group(
                    manage_group_id
                )
                if ga not in managed:
                    yield event.plain_result(f"⚠️ 群 {ga} 未绑定到本管理群")
                    return
                scope = ga
            removed_count = 0
            for md5_hash in image_md5s:
                success = await self._db.remove_manual_whitelist(md5_hash, scope)
                if success:
                    removed_count += 1
            scope_str = "全部范围" if scope is None else f"群 {scope} 范围"
            if removed_count > 0:
                yield event.plain_result(
                    f"✅ 成功从{scope_str}人工白名单移除 {removed_count} 张图片"
                )
            else:
                yield event.plain_result("⚠️ 图片不在该范围的人工白名单中")
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
            self._config_manager.maybe_reload()
            if not self._config_manager.is_manage_group(manage_group_id):
                return
            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result(
                    "❌ 您没有执行此命令的权限，需要管理员或群主身份"
                )
                return
            if confirm.strip().lower() != "确认":
                yield event.plain_result(
                    "⚠️ 此操作将清空所有人工白名单数据，不可恢复！\n如需确认，请发送: /清空白名单 确认"
                )
                return
            count = await self._db.clear_all_manual_whitelist()
            yield event.plain_result(f"✅ 已清空人工白名单，共移除 {count} 条记录")
        except Exception as e:
            logger.error(f"清空人工白名单异常: {e}")
            yield event.plain_result(f"❌ 操作失败: {str(e)}")

    @filter.command("添加黑名单")
    async def add_manual_blacklist_cmd(
        self,
        event: AstrMessageEvent,
        risk_level_str: str = "",
        reason: str = "",
        group_arg: str = "",
    ):
        """添加图片到人工黑名单（管理群专用，需引用图片；末位 [群号|all] 指定范围，缺省全局）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return
            self._config_manager.maybe_reload()
            if not self._config_manager.is_manage_group(manage_group_id):
                return
            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result(
                    "❌ 您没有执行此命令的权限，需要管理员或群主身份"
                )
                return
            reply_info = MessageUtils.extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要添加到黑名单的图片消息")
                return
            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return
            image_md5s = await MessageUtils.get_message_images(event, message_id)
            if not image_md5s:
                yield event.plain_result("❌ 引用的消息中没有图片")
                return
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
            reason, scope = self._parse_scope_arg(manage_group_id, reason, group_arg)
            added_count = 0
            for md5_hash in image_md5s:
                success = await self._db.add_manual_blacklist(
                    md5_hash=md5_hash,
                    risk_level=risk_level,
                    risk_reason=reason if reason else "人工添加",
                    added_by=user_id,
                    reason=reason if reason else None,
                    group_id=scope,
                )
                if success:
                    added_count += 1
            scope_str = "全局" if not scope else f"群 {scope} 的"
            if added_count > 0:
                yield event.plain_result(
                    f"✅ 成功添加 {added_count} 张图片到{scope_str}人工黑名单"
                )
            else:
                yield event.plain_result("⚠️ 图片已在该范围的人工黑名单中")
        except Exception as e:
            logger.error(f"添加人工黑名单异常: {e}")
            yield event.plain_result(f"❌ 操作失败: {str(e)}")

    @filter.command("移除黑名单")
    async def remove_manual_blacklist_cmd(
        self, event: AstrMessageEvent, group_arg: str = ""
    ):
        """从人工黑名单移除图片（管理群专用，需引用图片；[群号|all] 指定范围，缺省全部范围）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return
            self._config_manager.maybe_reload()
            if not self._config_manager.is_manage_group(manage_group_id):
                return
            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result(
                    "❌ 您没有执行此命令的权限，需要管理员或群主身份"
                )
                return
            reply_info = MessageUtils.extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要从黑名单移除的图片消息")
                return
            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return
            image_md5s = await MessageUtils.get_message_images(event, message_id)
            if not image_md5s:
                yield event.plain_result("❌ 引用的消息中没有图片")
                return
            # 解析范围：all/缺省=全部范围；具体群号需绑定到本管理群
            ga = group_arg.strip()
            if ga in ("", "all"):
                scope: str | None = None
            else:
                managed = self._config_manager.get_group_ids_by_manage_group(
                    manage_group_id
                )
                if ga not in managed:
                    yield event.plain_result(f"⚠️ 群 {ga} 未绑定到本管理群")
                    return
                scope = ga
            removed_count = 0
            for md5_hash in image_md5s:
                success = await self._db.remove_manual_blacklist(md5_hash, scope)
                if success:
                    removed_count += 1
            scope_str = "全部范围" if scope is None else f"群 {scope} 范围"
            if removed_count > 0:
                yield event.plain_result(
                    f"✅ 成功从{scope_str}人工黑名单移除 {removed_count} 张图片"
                )
            else:
                yield event.plain_result("⚠️ 图片不在该范围的人工黑名单中")
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
            self._config_manager.maybe_reload()
            if not self._config_manager.is_manage_group(manage_group_id):
                return
            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result(
                    "❌ 您没有执行此命令的权限，需要管理员或群主身份"
                )
                return
            if confirm.strip().lower() != "确认":
                yield event.plain_result(
                    "⚠️ 此操作将清空所有人工黑名单数据，不可恢复！\n如需确认，请发送: /清空黑名单 确认"
                )
                return
            count = await self._db.clear_all_manual_blacklist()
            yield event.plain_result(f"✅ 已清空人工黑名单，共移除 {count} 条记录")
        except Exception as e:
            logger.error(f"清空人工黑名单异常: {e}")
            yield event.plain_result(f"❌ 操作失败: {str(e)}")

    @filter.command("移除自动白名单")
    async def remove_auto_whitelist_cmd(self, event: AstrMessageEvent):
        """从自动白名单移除图片（管理群专用，需引用图片）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return
            self._config_manager.maybe_reload()
            if not self._config_manager.is_manage_group(manage_group_id):
                return
            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result(
                    "❌ 您没有执行此命令的权限，需要管理员或群主身份"
                )
                return
            reply_info = MessageUtils.extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要移除的图片消息")
                return
            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return
            image_md5s = await MessageUtils.get_message_images(event, message_id)
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
            self._config_manager.maybe_reload()
            if not self._config_manager.is_manage_group(manage_group_id):
                return
            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result(
                    "❌ 您没有执行此命令的权限，需要管理员或群主身份"
                )
                return
            reply_info = MessageUtils.extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要移除的图片消息")
                return
            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return
            image_md5s = await MessageUtils.get_message_images(event, message_id)
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

    @filter.command("添加账号白名单")
    async def add_account_whitelist_cmd(
        self, event: AstrMessageEvent, qq_str: str = "", group_arg: str = ""
    ):
        """添加 QQ 账号到白名单（管理群专用；末位 [群号|all] 指定范围，缺省全局）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return
            self._config_manager.maybe_reload()
            if not self._config_manager.is_manage_group(manage_group_id):
                return
            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result(
                    "❌ 您没有执行此命令的权限，需要管理员或群主身份"
                )
                return
            qq = qq_str.strip()
            if not qq:
                yield event.plain_result("使用方法: /添加账号白名单 <QQ号> [群号|all]")
                return
            # 范围解析：all/缺省=全局；具体群号须绑定到本管理群
            ga = group_arg.strip()
            if ga in ("", "all"):
                scope = ""
            else:
                managed = self._config_manager.get_group_ids_by_manage_group(
                    manage_group_id
                )
                if ga not in managed:
                    yield event.plain_result(f"⚠️ 群 {ga} 未绑定到本管理群")
                    return
                scope = ga
            added_by = str(event.get_sender_id())
            success = await self._db.add_account_whitelist(
                qq, group_id=scope, added_by=added_by
            )
            if success:
                scope_str = "全局" if not scope else f"群 {scope} 的"
                yield event.plain_result(f"✅ 已将用户 {qq} 加入{scope_str}账号白名单")
            else:
                yield event.plain_result("⚠️ 该用户已在该范围的账号白名单中")
        except Exception as e:
            logger.error(f"添加账号白名单异常: {e}")
            yield event.plain_result(f"❌ 操作失败: {str(e)}")

    @filter.command("移除账号白名单")
    async def remove_account_whitelist_cmd(
        self, event: AstrMessageEvent, qq_str: str = "", group_arg: str = ""
    ):
        """从账号白名单移除 QQ 账号（管理群专用；[群号|all] 指定范围，缺省全部范围）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return
            self._config_manager.maybe_reload()
            if not self._config_manager.is_manage_group(manage_group_id):
                return
            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result(
                    "❌ 您没有执行此命令的权限，需要管理员或群主身份"
                )
                return
            qq = qq_str.strip()
            if not qq:
                yield event.plain_result("使用方法: /移除账号白名单 <QQ号> [群号|all]")
                return
            # 范围解析：all/缺省=全部范围；具体群号须绑定到本管理群
            ga = group_arg.strip()
            if ga in ("", "all"):
                scope: str | None = None
            else:
                managed = self._config_manager.get_group_ids_by_manage_group(
                    manage_group_id
                )
                if ga not in managed:
                    yield event.plain_result(f"⚠️ 群 {ga} 未绑定到本管理群")
                    return
                scope = ga
            success = await self._db.remove_account_whitelist(qq, scope)
            if success:
                scope_str = "全部范围" if scope is None else f"群 {scope} 范围"
                yield event.plain_result(
                    f"✅ 已将用户 {qq} 从{scope_str}账号白名单移除"
                )
            else:
                yield event.plain_result("⚠️ 该用户不在该范围的账号白名单中")
        except Exception as e:
            logger.error(f"移除账号白名单异常: {e}")
            yield event.plain_result(f"❌ 操作失败: {str(e)}")

    @filter.command("账号白名单列表")
    async def account_whitelist_list_cmd(
        self, event: AstrMessageEvent, group_arg: str = ""
    ):
        """列出账号白名单（管理群专用；[群号|all]：all/缺省=全局，群号=该群）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return
            self._config_manager.maybe_reload()
            if not self._config_manager.is_manage_group(manage_group_id):
                return
            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result(
                    "❌ 您没有执行此命令的权限，需要管理员或群主身份"
                )
                return
            ga = group_arg.strip()
            if ga in ("", "all"):
                scope = ""
                label = "全局"
            else:
                managed = self._config_manager.get_group_ids_by_manage_group(
                    manage_group_id
                )
                if ga not in managed:
                    yield event.plain_result(f"⚠️ 群 {ga} 未绑定到本管理群")
                    return
                scope = ga
                label = f"群 {ga}"
            users = await self._db.get_account_whitelist_by_group(scope)
            if not users:
                yield event.plain_result(f"📋 账号白名单（{label}）\n暂无白名单用户")
                return
            lines = [f"📋 账号白名单（{label}）"]
            for i, uid in enumerate(users[:50], 1):
                lines.append(f"{i}. {uid}")
            if len(users) > 50:
                lines.append(f"... 共 {len(users)} 条，仅显示前 50 条")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.error(f"账号白名单列表异常: {e}")
            yield event.plain_result(f"❌ 操作失败: {str(e)}")

    @filter.command("审查帮助")
    async def review_help(self, event: AstrMessageEvent):
        """显示图片审核插件帮助信息"""
        try:
            group_id = str(event.get_group_id()) if event.get_group_id() else None
            if not group_id:
                return
            is_manage = self._config_manager.is_manage_group(group_id)
            is_enabled = self._config_manager.is_group_enabled(group_id)
            if not is_manage and not is_enabled:
                return
            help_text = (
                "📖 图片审核插件使用帮助\n"
                "━━━━━━━━━━━━━━━\n"
                "\n"
                "【管理员命令】\n"
                "━━━━━━━━━━━━━━━\n"
                "/查询违规 [QQ号] [群号|all] - 查询用户违规记录\n"
                "/删除违规 [QQ号] [群号|all] 确认 - 删除用户违规记录\n"
                "/审核状态 [群号|all] - 查看插件运行状态与群统计\n"
                "/清除缓存 - 清除自动黑白名单缓存\n"
                "/查询名单 - 查询图片名单状态(需引用图片)\n"
                "\n"
                "【双群管理说明】\n"
                "━━━━━━━━━━━━━━━\n"
                "• 管理群仅绑定1个被管理群时，命令直接作用该群(无需群号)\n"
                "• 绑定多个时需带 群号 或 all，群号须绑定到本管理群\n"
                "• 名单命令末位 [群号|all] 指定范围，缺省为全局\n"
                "\n"
                "【人工白名单管理】\n"
                "━━━━━━━━━━━━━━━\n"
                "/添加白名单 [原因] [群号|all] - 添加图片到白名单(需引用)\n"
                "  提示: 原因含空格时用引号包裹，如:\n"
                '  /添加白名单 "误拦截，正常图片" 123456\n'
                "/移除白名单 [群号|all] - 从白名单移除图片(需引用)\n"
                "/清空白名单 确认 - 清空所有人工白名单\n"
                "\n"
                "【人工黑名单管理】\n"
                "━━━━━━━━━━━━━━━\n"
                "/添加黑名单 [REVIEW/BLOCK] [原因] [群号|all]\n"
                "  添加图片到黑名单(需引用图片)\n"
                "  提示: 原因含空格时用引号包裹，如:\n"
                '  /添加黑名单 BLOCK "色情违规内容" 123456\n'
                "/移除黑名单 [群号|all] - 从黑名单移除图片(需引用)\n"
                "/清空黑名单 确认 - 清空所有人工黑名单\n"
                "\n"
                "【自动名单管理】\n"
                "━━━━━━━━━━━━━━━\n"
                "/移除自动白名单 - 移除自动白名单(需引用)\n"
                "/移除自动黑名单 - 移除自动黑名单(需引用)\n"
                "\n"
                "【账号白名单】\n"
                "━━━━━━━━━━━━━━━\n"
                "白名单中的 QQ 账号跳过图片审核\n"
                "/添加账号白名单 <QQ号> [群号|all] - 添加账号\n"
                "  缺省/all=全局生效，群号=仅该群生效\n"
                "/移除账号白名单 <QQ号> [群号|all] - 移除账号\n"
                "  缺省/all=移除全部范围\n"
                "/账号白名单列表 [群号|all] - 查看列表\n"
                "\n"
                "【WebUI 管理面板】\n"
                "━━━━━━━━━━━━━━━\n"
                "• AstrBot Dashboard → 插件详情 → 图片审核管理\n"
                "• 可查看违规记录/违规图片/LLM分析/违规档案/审核统计\n"
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
