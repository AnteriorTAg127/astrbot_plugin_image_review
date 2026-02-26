"""
图片审核流程管理模块
整合MD5检测、黑白名单、API审核、违规处理等完整流程
"""

import asyncio
import os
from typing import Any

import aiohttp

from astrbot.api import logger

from .censor_aliyun import AliyunCensor
from .censor_base import CensorBase, CensorError
from .censor_vlai import VLAICensor
from .database import DatabaseManager, RiskLevel

# 单例会话管理
_download_session = None
_download_semaphore = None


async def _ensure_download_session():
    """确保下载会话已初始化"""
    global _download_session, _download_semaphore
    if _download_session is None:
        _download_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
    if _download_semaphore is None:
        _download_semaphore = asyncio.Semaphore(20)  # 限制并发下载数


def _validate_image_content(data: bytes) -> bool:
    """
    验证数据是否为有效的图片格式

    Args:
        data: 图片字节数据

    Returns:
        是否为有效的图片
    """
    if len(data) < 8:
        return False

    # 检查常见图片格式的魔数
    image_signatures = {
        b"\xff\xd8\xff": "JPEG",  # JPEG
        b"\x89PNG\r\n\x1a\n": "PNG",  # PNG
        b"GIF87a": "GIF",  # GIF87a
        b"GIF89a": "GIF",  # GIF89a
        b"RIFF": "WEBP",  # WEBP (RIFF....WEBP)
        b"BM": "BMP",  # BMP
    }

    for signature, fmt in image_signatures.items():
        if data.startswith(signature):
            # 对于 WEBP 需要额外检查
            if fmt == "WEBP" and len(data) >= 12:
                if data[8:12] == b"WEBP":
                    return True
            elif fmt != "WEBP":
                return True
    return False


async def download_image(url: str, max_size_mb: int = 10) -> bytes:
    """
    异步下载图片

    Args:
        url: 图片URL地址
        max_size_mb: 最大允许的图片大小（MB），默认10MB

    Returns:
        图片的原始字节数据

    Raises:
        CensorError: 下载失败或图片过大
    """
    await _ensure_download_session()
    proxy = os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY")
    max_size_bytes = max_size_mb * 1024 * 1024

    async with _download_semaphore:
        async with _download_session.get(url, proxy=proxy) as resp:
            if resp.status != 200:
                raise CensorError(f"图片下载失败，状态码: {resp.status}")

            # 检查Content-Type头
            content_type = resp.headers.get("Content-Type", "").lower()
            allowed_types = ["image/", "application/octet-stream"]
            if not any(ct in content_type for ct in allowed_types) and content_type:
                logger.warning(f"下载的内容类型可能不是图片: {content_type}")

            # 检查Content-Length头
            content_length = resp.headers.get("Content-Length")
            if content_length:
                try:
                    size = int(content_length)
                    if size > max_size_bytes:
                        raise CensorError(
                            f"图片过大: {size / 1024 / 1024:.2f}MB，超过限制 {max_size_mb}MB"
                        )
                except ValueError:
                    pass  # 如果解析失败，继续下载并在读取时检查

            # 流式读取并检查大小
            chunks = []
            total_size = 0
            async for chunk in resp.content.iter_chunked(8192):
                chunks.append(chunk)
                total_size += len(chunk)
                if total_size > max_size_bytes:
                    raise CensorError(f"图片过大，超过限制 {max_size_mb}MB")

            data = b"".join(chunks)

            # 验证下载的内容是否为有效图片
            if not _validate_image_content(data):
                raise CensorError("下载的内容不是有效的图片格式")

            return data


async def close_download_session():
    """关闭下载会话"""
    global _download_session
    if _download_session:
        await _download_session.close()
        _download_session = None


class CensorFlow:
    """内容审核流程管理器"""

    def __init__(
        self, config: dict[str, Any], db_manager: DatabaseManager, context: Any = None
    ):
        """
        初始化审核流程管理器

        Args:
            config: 插件配置
            db_manager: 数据库管理器
            context: AstrBot 上下文对象（VLAI 审核器需要）
        """
        self._config = config
        self._db = db_manager
        self._context = context
        self._image_censor: CensorBase | None = None

        # 缓存配置 - 使用全局默认值，实际值从群配置中获取
        self._base_expire_hours = 2
        self._max_expire_days = 14

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
        elif image_provider == "VLAI":
            vlai_config = self._config.get("vlai", {})
            if self._context:
                logger.debug("开始初始化 VLAI 审核器")
                self._image_censor = VLAICensor(vlai_config, self._context)
                await self._image_censor.initialize()
                logger.debug("VLAI 审核器初始化完成")
            else:
                logger.error("VLAI 审核器需要 AstrBot 上下文对象，但未提供")
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
        precalculated_md5: str | None = None,
        base_expire_hours: int | None = None,
        max_expire_days: int | None = None,
    ) -> tuple[RiskLevel, str, str | None]:
        """
        提交图片进行审核

        Args:
            image_url: 图片URL
            group_id: 群ID
            precalculated_md5: 预计算的图片MD5（可选，如果提供则跳过下载和MD5计算）
            base_expire_hours: 基础缓存过期时间（小时），覆盖全局默认值
            max_expire_days: 最大缓存周期（天），覆盖全局默认值

        Returns:
            (风险等级, 风险原因, 图片MD5)
        """
        import logging

        logger = logging.getLogger(__name__)

        # 使用传入的缓存配置或全局默认值
        expire_hours = (
            base_expire_hours
            if base_expire_hours is not None
            else self._base_expire_hours
        )
        expire_days = (
            max_expire_days if max_expire_days is not None else self._max_expire_days
        )

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

            # 1. 检查人工白名单（最高优先级）
            logger.debug("检查人工白名单")
            if await self._db.check_manual_whitelist(md5_hash):
                logger.debug(f"图片在人工白名单中，MD5: {md5_hash}")
                return RiskLevel.Pass, "人工白名单图片", md5_hash

            # 2. 检查人工黑名单（最高优先级）
            logger.debug("检查人工黑名单")
            manual_blacklist_result = await self._db.check_manual_blacklist(md5_hash)
            if manual_blacklist_result:
                risk_level, risk_reason = manual_blacklist_result
                logger.debug(
                    f"图片在人工黑名单中，风险等级: {risk_level.name}, 原因: {risk_reason}"
                )
                return risk_level, f"人工黑名单图片: {risk_reason}", md5_hash

            # 3. 检查自动白名单（如果未关闭）
            disable_auto_whitelist = self._config.get("disable_auto_whitelist", False)
            if not disable_auto_whitelist:
                logger.debug("检查自动白名单")
                if await self._db.check_whitelist(md5_hash):
                    logger.debug(f"图片在自动白名单中，MD5: {md5_hash}")
                    return RiskLevel.Pass, "白名单图片", md5_hash
            else:
                logger.debug("自动白名单已禁用，跳过检查")

            # 4. 检查自动黑名单（如果未关闭）
            disable_auto_blacklist = self._config.get("disable_auto_blacklist", False)
            if not disable_auto_blacklist:
                logger.debug("检查自动黑名单")
                blacklist_result = await self._db.check_blacklist(md5_hash)
                if blacklist_result:
                    risk_level, risk_reason = blacklist_result
                    logger.debug(
                        f"图片在黑名单中，风险等级: {risk_level.name}, 原因: {risk_reason}"
                    )
                    return risk_level, f"黑名单图片: {risk_reason}", md5_hash
            else:
                logger.debug("自动黑名单已禁用，跳过检查")

            # 5. 调用API审核
            if not self._image_censor:
                logger.debug("未配置审核器，直接通过")
                return RiskLevel.Pass, "未配置审核器", md5_hash

            # 阿里云只支持URL图片
            image_input = image_url
            logger.debug("调用API审核图片")
            risk_level, risk_words = await self._image_censor.detect_image(image_input)
            risk_reason = ", ".join(risk_words) if risk_words else ""
            logger.debug(
                f"API审核完成，风险等级: {risk_level.name}, 原因: {risk_reason}"
            )

            # 6. 根据审核结果更新自动黑白名单（如果未关闭）
            if risk_level == RiskLevel.Pass:
                if not disable_auto_whitelist:
                    logger.debug(f"图片审核通过，添加到自动白名单: {md5_hash}")
                    await self._db.add_to_whitelist(
                        md5_hash,
                        base_expire_hours=expire_hours,
                        max_expire_days=expire_days,
                    )
                    logger.debug("添加到自动白名单完成")
                else:
                    logger.debug("自动白名单已禁用，不添加到白名单")
            elif risk_level in (RiskLevel.Review, RiskLevel.Block):
                if not disable_auto_blacklist:
                    logger.debug(
                        f"图片审核违规，添加到自动黑名单: {md5_hash}, 风险等级: {risk_level.name}"
                    )
                    await self._db.add_to_blacklist(
                        md5_hash,
                        risk_level,
                        risk_reason,
                        base_expire_hours=expire_hours,
                        max_expire_days=expire_days,
                    )
                    logger.debug("添加到自动黑名单完成")
                else:
                    logger.debug("自动黑名单已禁用，不添加到黑名单")

            logger.debug(
                f"图片审核流程完成，最终结果: 风险等级={risk_level.name}, 原因={risk_reason}, MD5={md5_hash}"
            )
            return risk_level, risk_reason, md5_hash

        except Exception as e:
            logger.debug(f"图片审核流程异常: {e}")
            raise CensorError(f"图片审核流程异常: {e}")

    def is_image_censor_enabled(self) -> bool:
        """检查是否启用了图片审核"""
        return (
            self._config.get("enable_image_censor", True)
            and self._image_censor is not None
        )
