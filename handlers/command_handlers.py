"""
命令处理模块
负责处理所有管理员命令
"""

from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter

from ..database import DatabaseManager, RiskLevel
from ..utils.message_utils import MessageUtils

if TYPE_CHECKING:
    from .admin_manager import AdminManager
    from .config_manager import ConfigManager
    from .violation_handler import ViolationHandler


class CommandHandlers:
    """命令处理器 - 负责处理所有管理员命令"""

    def __init__(
        self,
        db_manager: DatabaseManager,
        config_manager: "ConfigManager",
        admin_manager: "AdminManager",
        violation_handler: "ViolationHandler",
        config: dict[str, Any],
    ):
        """
        初始化命令处理器

        Args:
            db_manager: 数据库管理器
            config_manager: 配置管理器
            admin_manager: 管理员管理器
            violation_handler: 违规处理器
            config: 插件配置
        """
        self._db = db_manager
        self._config_manager = config_manager
        self._admin_manager = admin_manager
        self._violation_handler = violation_handler
        self._config = config

    async def _check_admin_permission(self, event: AstrMessageEvent, group_id: str) -> bool:
        """
        检查用户是否为管理员/群主

        Args:
            event: 消息事件
            group_id: 群ID

        Returns:
            是否具有管理员权限
        """
        enable_check = self._config.get("enable_admin_permission_check", False)
        if not enable_check:
            return True

        user_id = str(event.get_sender_id())
        return await self._admin_manager.is_user_admin(event, group_id, user_id)

    @filter.command("查询违规")
    async def query_violation(self, event: AstrMessageEvent, user_id_str: str = ""):
        """查询用户违规记录（管理群专用）"""
        try:
            group_id = str(event.get_group_id()) if event.get_group_id() else None
            if not group_id:
                return

            if not self._config_manager.is_manage_group(group_id):
                return

            if not await self._check_admin_permission(event, group_id):
                yield event.plain_result("❌ 您没有执行此命令的权限，需要管理员或群主身份")
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
            group_id = str(event.get_group_id()) if event.get_group_id() else None
            if not group_id:
                return

            if not self._config_manager.is_manage_group(group_id):
                return

            if not await self._check_admin_permission(event, group_id):
                yield event.plain_result("❌ 您没有执行此命令的权限，需要管理员或群主身份")
                return

            status_info = "📊 图片审核插件状态\n"
            status_info += "━━━━━━━━━━━━━━━\n"

            # 检查图片审核状态（安全地处理 self._censor_flow 为 None 的情况）
            # 这里需要从主插件获取 censor_flow 状态
            # 暂时跳过这部分，由主插件处理
            status_info += "图片审核: 请查看主插件状态\n"

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

            # 获取自动黑白名单数量
            cache_counts = await self._db.get_cache_counts()
            status_info += "\n📋 自动名单统计\n"
            status_info += "━━━━━━━━━━━━━━━\n"
            status_info += f"自动白名单: {cache_counts['whitelist']} 条\n"
            status_info += f"自动黑名单: {cache_counts['blacklist']} 条\n"

            # 检查群聊配置及审查模式
            status_info += "\n📌 群聊审查模式\n"
            status_info += "━━━━━━━━━━━━━━━\n"
            # 这里需要从主插件获取 group_config，暂时简化处理
            status_info += "请查看主插件获取详细群聊配置\n"

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

            if not self._config_manager.is_manage_group(group_id):
                return

            if not await self._check_admin_permission(event, group_id):
                yield event.plain_result("❌ 您没有执行此命令的权限，需要管理员或群主身份")
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

            if not self._config_manager.is_manage_group(manage_group_id):
                return

            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result("❌ 您没有执行此命令的权限，需要管理员或群主身份")
                return

            # 检查是否引用了消息
            reply_info = MessageUtils.extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要查询的图片消息")
                return

            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return

            # 获取被引用消息中的图片
            image_md5s = await MessageUtils.get_message_images(event, message_id)
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

            if not self._config_manager.is_manage_group(manage_group_id):
                return

            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result("❌ 您没有执行此命令的权限，需要管理员或群主身份")
                return

            if not user_id_str:
                yield event.plain_result("使用方法: /删除违规 [QQ号]")
                return

            user_id = user_id_str.strip()

            target_group_ids = self._config_manager.get_group_ids_by_manage_group(
                manage_group_id
            )
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

    @filter.command("添加白名单")
    async def add_manual_whitelist_cmd(self, event: AstrMessageEvent, reason: str = ""):
        """添加图片到人工白名单（管理群专用，需引用图片）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return

            if not self._config_manager.is_manage_group(manage_group_id):
                return

            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result("❌ 您没有执行此命令的权限，需要管理员或群主身份")
                return

            # 检查是否引用了消息
            reply_info = MessageUtils.extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要添加到白名单的图片消息")
                return

            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return

            # 获取被引用消息中的图片
            image_md5s = await MessageUtils.get_message_images(event, message_id)
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

            if not self._config_manager.is_manage_group(manage_group_id):
                return

            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result("❌ 您没有执行此命令的权限，需要管理员或群主身份")
                return

            # 检查是否引用了消息
            reply_info = MessageUtils.extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要从白名单移除的图片消息")
                return

            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return

            # 获取被引用消息中的图片
            image_md5s = await MessageUtils.get_message_images(event, message_id)
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

            if not self._config_manager.is_manage_group(manage_group_id):
                return

            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result("❌ 您没有执行此命令的权限，需要管理员或群主身份")
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

            if not self._config_manager.is_manage_group(manage_group_id):
                return

            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result("❌ 您没有执行此命令的权限，需要管理员或群主身份")
                return

            # 检查是否引用了消息
            reply_info = MessageUtils.extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要添加到黑名单的图片消息")
                return

            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return

            # 获取被引用消息中的图片
            image_md5s = await MessageUtils.get_message_images(event, message_id)
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

            if not self._config_manager.is_manage_group(manage_group_id):
                return

            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result("❌ 您没有执行此命令的权限，需要管理员或群主身份")
                return

            # 检查是否引用了消息
            reply_info = MessageUtils.extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要从黑名单移除的图片消息")
                return

            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return

            # 获取被引用消息中的图片
            image_md5s = await MessageUtils.get_message_images(event, message_id)
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

            if not self._config_manager.is_manage_group(manage_group_id):
                return

            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result("❌ 您没有执行此命令的权限，需要管理员或群主身份")
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

    @filter.command("移除自动白名单")
    async def remove_auto_whitelist_cmd(self, event: AstrMessageEvent):
        """从自动白名单移除图片（管理群专用，需引用图片）"""
        try:
            manage_group_id = (
                str(event.get_group_id()) if event.get_group_id() else None
            )
            if not manage_group_id:
                return

            if not self._config_manager.is_manage_group(manage_group_id):
                return

            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result("❌ 您没有执行此命令的权限，需要管理员或群主身份")
                return

            # 检查是否引用了消息
            reply_info = MessageUtils.extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要移除的图片消息")
                return

            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return

            # 获取被引用消息中的图片
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

            if not self._config_manager.is_manage_group(manage_group_id):
                return

            if not await self._check_admin_permission(event, manage_group_id):
                yield event.plain_result("❌ 您没有执行此命令的权限，需要管理员或群主身份")
                return

            # 检查是否引用了消息
            reply_info = MessageUtils.extract_reply_info(event)
            if not reply_info:
                yield event.plain_result("❌ 请引用需要移除的图片消息")
                return

            message_id = reply_info.get("message_id")
            if not message_id:
                yield event.plain_result("❌ 无法获取引用消息ID")
                return

            # 获取被引用消息中的图片
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

    @filter.command("审查帮助")
    async def review_help(self, event: AstrMessageEvent):
        """显示图片审核插件帮助信息"""
        try:
            group_id = str(event.get_group_id()) if event.get_group_id() else None
            if not group_id:
                return

            # 检查是否是管理群或被审核的群
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
