"""
管理员管理模块
负责管理员检测、缓存和智能审查模式判断
"""

from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class AdminManager:
    """管理员管理器 - 负责管理员检测和缓存"""

    def __init__(self):
        """初始化管理员管理器"""
        # 管理员列表缓存 {group_id: {"admins": set(), "expires_at": datetime}}
        self._admin_cache: dict[str, dict] = {}
        self._admin_cache_ttl = 300  # 管理员缓存5分钟

        # 群聊最后管理员发言时间 {group_id: datetime}
        self._last_admin_message_time: dict[str, datetime] = {}

    async def get_group_admins(
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

    async def is_user_admin_cached(
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
        admins = await self.get_group_admins(event, group_id)
        return user_id in admins

    async def is_user_admin(
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

    def record_admin_message(self, group_id: str):
        """
        记录管理员发言时间

        Args:
            group_id: 群ID
        """
        self._last_admin_message_time[group_id] = datetime.now()
        logger.debug(f"记录管理员发言: 群{group_id}")

    def get_last_admin_time(self, group_id: str) -> datetime | None:
        """
        获取最后管理员发言时间

        Args:
            group_id: 群ID

        Returns:
            最后管理员发言时间，如果没有记录则返回None
        """
        return self._last_admin_message_time.get(group_id)

    def clear_cache(self, group_id: str | None = None):
        """
        清除管理员缓存

        Args:
            group_id: 群ID，如果为None则清除所有缓存
        """
        if group_id:
            self._admin_cache.pop(group_id, None)
        else:
            self._admin_cache.clear()
