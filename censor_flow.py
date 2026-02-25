"""
图片审核流程管理模块
整合MD5检测、黑白名单、API审核、违规处理等完整流程
"""

import asyncio
import os
from typing import Any, Optional, Tuple

import aiohttp

from .censor_base import CensorBase, CensorError
from .censor_aliyun import AliyunCensor
from .database import DatabaseManager, RiskLevel


# 单例会话管理
_download_session = None
_download_semaphore = None

async def _ensure_download_session():
    """确保下载会话已初始化"""
    global _download_session, _download_semaphore
    if _download_session is None:
        _download_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    if _download_semaphore is None:
        _download_semaphore = asyncio.Semaphore(20)  # 限制并发下载数

async def download_image(url: str) -> bytes:
    """
    异步下载图片

    Args:
        url: 图片URL地址

    Returns:
        图片的原始字节数据
    """
    await _ensure_download_session()
    proxy = os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY")
    async with _download_semaphore:
        async with _download_session.get(url, proxy=proxy) as resp:
            if resp.status != 200:
                raise CensorError(f"图片下载失败，状态码: {resp.status}")
            return await resp.read()

async def close_download_session():
    """关闭下载会话"""
    global _download_session
    if _download_session:
        await _download_session.close()
        _download_session = None


class CensorFlow:
    """内容审核流程管理器"""

    def __init__(
        self,
        config: dict[str, Any],
        db_manager: DatabaseManager
    ):
        """
        初始化审核流程管理器

        Args:
            config: 插件配置
            db_manager: 数据库管理器
        """
        self._config = config
        self._db = db_manager
        self._image_censor: Optional[CensorBase] = None

        # 缓存配置
        self._base_expire_hours = config.get("cache_settings", {}).get("base_expire_hours", 2)
        self._max_expire_days = config.get("cache_settings", {}).get("max_expire_days", 14)

    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self.initialize()
        return self

    async def initialize(self):
        """初始化审核流程管理器"""
        import logging
        logger = logging.getLogger(__name__)
        logger.debug("开始初始化审核流程管理器")
        await self._init_censors()
        logger.debug("审核流程管理器初始化完成")

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        await self.close()

    async def _init_censors(self):
        """初始化审核器"""
        import logging
        logger = logging.getLogger(__name__)
        image_provider = self._config.get("image_censor_provider", "Aliyun")
        logger.debug(f"初始化审核器，提供商: {image_provider}")
        if image_provider == "Aliyun":
            aliyun_config = self._config.get("aliyun", {})
            if aliyun_config.get("key_id") and aliyun_config.get("key_secret"):
                logger.debug("阿里云配置完整，开始初始化阿里云审核器")
                self._image_censor = AliyunCensor(aliyun_config)
                await self._image_censor.initialize()
                logger.debug("阿里云审核器初始化完成")
            else:
                logger.debug("阿里云配置不完整，跳过初始化")
        else:
            logger.debug(f"未知的审核提供商: {image_provider}")

    async def close(self):
        """关闭资源"""
        import logging
        logger = logging.getLogger(__name__)
        logger.debug("开始关闭审核流程管理器资源")
        if self._image_censor:
            logger.debug("关闭审核器")
            await self._image_censor.close()
            logger.debug("审核器已关闭")
        # 关闭下载会话
        logger.debug("关闭下载会话")
        await close_download_session()
        logger.debug("下载会话已关闭")

    async def submit_image(
        self,
        image_url: str,
        group_id: str,
        precalculated_md5: Optional[str] = None
    ) -> Tuple[RiskLevel, str, Optional[str]]:
        """
        提交图片进行审核

        Args:
            image_url: 图片URL
            group_id: 群ID
            precalculated_md5: 预计算的图片MD5（可选，如果提供则跳过下载和MD5计算）

        Returns:
            (风险等级, 风险原因, 图片MD5)
        """
        import logging
        logger = logging.getLogger(__name__)
        try:
            logger.debug(f"开始审核图片，URL: {image_url}, 群: {group_id}")
            # 如果提供了预计算的MD5，直接使用
            if precalculated_md5:
                md5_hash = precalculated_md5
                logger.debug(f"使用预计算的MD5: {md5_hash}")
            else:
                # 下载图片
                logger.debug("下载图片")
                image_data = await download_image(image_url)
                logger.debug(f"图片下载完成，大小: {len(image_data)}字节")
                md5_hash = DatabaseManager.calculate_md5(image_data)
                logger.debug(f"计算MD5完成: {md5_hash}")

            # 1. 检查白名单
            logger.debug("检查白名单")
            if await self._db.check_whitelist(md5_hash):
                logger.debug(f"图片在白名单中，MD5: {md5_hash}")
                return RiskLevel.Pass, "白名单图片", md5_hash

            # 2. 检查黑名单
            logger.debug("检查黑名单")
            blacklist_result = await self._db.check_blacklist(md5_hash)
            if blacklist_result:
                risk_level, risk_reason = blacklist_result
                logger.debug(f"图片在黑名单中，风险等级: {risk_level.name}, 原因: {risk_reason}")
                return risk_level, f"黑名单图片: {risk_reason}", md5_hash

            # 3. 调用API审核
            if not self._image_censor:
                logger.debug("未配置审核器，直接通过")
                return RiskLevel.Pass, "未配置审核器", md5_hash

            # 阿里云只支持URL图片
            image_input = image_url
            logger.debug("调用API审核图片")
            risk_level, risk_words = await self._image_censor.detect_image(image_input)
            risk_reason = ", ".join(risk_words) if risk_words else ""
            logger.debug(f"API审核完成，风险等级: {risk_level.name}, 原因: {risk_reason}")

            # 4. 根据审核结果更新黑白名单
            if risk_level == RiskLevel.Pass:
                logger.debug(f"图片审核通过，添加到白名单: {md5_hash}")
                await self._db.add_to_whitelist(
                    md5_hash,
                    base_expire_hours=self._base_expire_hours,
                    max_expire_days=self._max_expire_days
                )
                logger.debug("添加到白名单完成")
            elif risk_level in (RiskLevel.Review, RiskLevel.Block):
                logger.debug(f"图片审核违规，添加到黑名单: {md5_hash}, 风险等级: {risk_level.name}")
                await self._db.add_to_blacklist(
                    md5_hash,
                    risk_level,
                    risk_reason,
                    base_expire_hours=self._base_expire_hours,
                    max_expire_days=self._max_expire_days
                )
                logger.debug("添加到黑名单完成")

            logger.debug(f"图片审核流程完成，最终结果: 风险等级={risk_level.name}, 原因={risk_reason}, MD5={md5_hash}")
            return risk_level, risk_reason, md5_hash

        except Exception as e:
            logger.debug(f"图片审核流程异常: {e}")
            raise CensorError(f"图片审核流程异常: {e}")

    def is_image_censor_enabled(self) -> bool:
        """检查是否启用了图片审核"""
        return self._config.get("enable_image_censor", True) and self._image_censor is not None
