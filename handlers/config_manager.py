"""
配置管理模块
负责加载和管理群聊配置
"""

import re
from datetime import datetime
from typing import Any

from astrbot.api import logger


class ConfigManager:
    """配置管理器 - 负责加载和管理群聊配置"""

    def __init__(self, config: dict[str, Any]):
        """
        初始化配置管理器

        Args:
            config: 插件配置字典
        """
        self._config = config
        self._group_config: dict[str, dict] = {}
        self._load_group_config()

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
                first_mute_duration = self._safe_int(
                    setting.get("first_mute_duration"), 600, min_val=0
                )
                max_mute_duration = self._safe_int(
                    setting.get("max_mute_duration"),
                    2419200,
                    min_val=0,
                    max_val=2419200,
                )
                mute_multiplier = self._safe_float(
                    setting.get("mute_multiplier"), 2, min_val=1
                )
                base_expire_hours = self._safe_int(
                    setting.get("base_expire_hours"), 2, min_val=1
                )
                max_expire_days = self._safe_int(
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

    @staticmethod
    def _safe_int(value, default: int, min_val: int = None, max_val: int = None) -> int:
        """安全地将值转换为整数"""
        try:
            result = int(value) if value is not None else default
            if min_val is not None:
                result = max(min_val, result)
            if max_val is not None:
                result = min(max_val, result)
            return result
        except (ValueError, TypeError):
            logger.warning(f"配置值 '{value}' 无法转换为整数，使用默认值 {default}")
            return default

    @staticmethod
    def _safe_float(
        value, default: float, min_val: float = None, max_val: float = None
    ) -> float:
        """安全地将值转换为浮点数"""
        try:
            result = float(value) if value is not None else default
            if min_val is not None:
                result = max(min_val, result)
            if max_val is not None:
                result = min(max_val, result)
            return result
        except (ValueError, TypeError):
            logger.warning(f"配置值 '{value}' 无法转换为浮点数，使用默认值 {default}")
            return default

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

    @staticmethod
    def _is_in_schedule(schedule: tuple[datetime.time, datetime.time] | None) -> bool:
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

    def get_group_config(self, group_id: str) -> dict | None:
        """
        获取群聊的完整配置

        Args:
            group_id: 群ID

        Returns:
            群聊配置字典，未配置则返回None
        """
        return self._group_config.get(group_id)

    def get_manage_group_id(self, group_id: str) -> str | None:
        """
        获取群聊对应的管理群ID

        Args:
            group_id: 群ID

        Returns:
            管理群ID，未配置则返回None
        """
        config = self._group_config.get(group_id)
        return config["manage_group_id"] if config else None

    def get_group_ids_by_manage_group(self, manage_group_id: str) -> list[str]:
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

    def is_group_enabled(self, group_id: str) -> bool:
        """
        检查群聊是否启用了图片审核

        Args:
            group_id: 群ID

        Returns:
            是否启用
        """
        return group_id in self._group_config

    def is_manage_group(self, group_id: str) -> bool:
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

    def should_enable_censor(
        self, group_id: str, last_admin_time: datetime | None
    ) -> tuple[bool, str]:
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
            last_admin_time: 最后管理员发言时间

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
