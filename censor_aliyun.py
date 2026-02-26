"""
阿里云内容审核模块
使用阿里云官方Python SDK进行文本和图片审核
"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import aiohttp
from alibabacloud_green20220302 import models
from alibabacloud_green20220302.client import Client
from alibabacloud_tea_openapi.models import Config
from alibabacloud_tea_util import models as util_models

from .censor_base import CensorBase, CensorError
from .database import RiskLevel


class AliyunCensor(CensorBase):
    """阿里云内容审核"""

    def __init__(self, config: dict[str, Any]) -> None:
        """
        初始化阿里云审核器

        Args:
            config: 配置字典，需包含key_id和key_secret
        """
        super().__init__(config)
        self._key_id = config["key_id"]
        self._key_secret = config["key_secret"]
        self._endpoint = "green-cip.cn-shanghai.aliyuncs.com"
        self._region_id = "cn-shanghai"
        self._image_service = config.get("image_service", "baselineCheck")
        self._image_info_type = config.get("image_info_type", "customImage,textInImage")
        self._session = None
        self._semaphore = asyncio.Semaphore(80)
        self._executor = ThreadPoolExecutor(
            max_workers=20, thread_name_prefix="aliyun_censor"
        )

    async def initialize(self):
        """初始化异步资源"""
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )

    async def close(self):
        """关闭HTTP会话和线程池"""
        if self._session:
            await self._session.close()
            self._session = None
        # 关闭线程池 - 使用 wait=False 避免阻塞事件循环
        if self._executor:
            self._executor.shutdown(wait=False)
            self._executor = None

    async def detect_text(self, text: str) -> tuple[RiskLevel, set[str]]:
        """
        对文本进行内容审核

        Args:
            text: 待审核文本

        Returns:
            (风险等级, 风险词集合)
        """
        await self.initialize()

        if not text:
            return RiskLevel.Pass, set()

        if len(text) <= 600:
            return await self._check_single_text(text)

        chunks = self._split_text(text, max_length=600)
        tasks = [self._check_single_text(chunk) for chunk in chunks]
        results = await asyncio.gather(*tasks)

        highest_risk_level = RiskLevel.Pass
        all_risk_words: set[str] = set()

        for risk_level, words in results:
            if risk_level.value > highest_risk_level.value:
                highest_risk_level = risk_level
            all_risk_words.update(words)

        return highest_risk_level, all_risk_words

    async def _check_single_text(self, content: str) -> tuple[RiskLevel, set[str]]:
        """
        审核单段文本

        Args:
            content: 文本内容

        Returns:
            (风险等级, 风险词集合)
        """
        try:
            config = Config(
                access_key_id=self._key_id,
                access_key_secret=self._key_secret,
                endpoint=self._endpoint,
                region_id=self._region_id,
            )
            client = Client(config)
            runtime = util_models.RuntimeOptions()

            service_params = {"content": content}
            request = models.TextModerationRequest(
                service="chat_detection_pro",
                service_parameters=json.dumps(service_params),
            )

            async with self._semaphore:
                response = await asyncio.get_event_loop().run_in_executor(
                    self._executor,
                    lambda: client.text_moderation_with_options(request, runtime),
                )

                if response.status_code != 200:
                    raise CensorError(
                        f"阿里云文本审核请求失败: HTTP {response.status_code}"
                    )

                body = response.body
                if body.code != 200:
                    raise CensorError(f"阿里云文本审核失败: {body.msg}")

                data = body.data
                risk_level = data.risk_level.lower() if data.risk_level else "pass"
                risk_words_set: set[str] = set()

                if data.result:
                    for r_data in data.result:
                        if hasattr(r_data, "risk_words") and r_data.risk_words:
                            risk_words_list = [
                                word.strip() for word in r_data.risk_words.split(",")
                            ]
                            risk_words_set.update(risk_words_list)

                if risk_level in ("none", "low"):
                    return RiskLevel.Pass, risk_words_set
                elif risk_level == "high":
                    return RiskLevel.Block, risk_words_set
                else:
                    return RiskLevel.Review, risk_words_set

        except CensorError:
            raise
        except Exception as e:
            raise CensorError(f"阿里云文本审核请求失败: {e}")

    async def detect_image(self, image: str) -> tuple[RiskLevel, set[str]]:
        """
        对图片进行内容审核

        Args:
            image: 图片URL或base64字符串

        Returns:
            (风险等级, 风险描述集合)
        """
        await self.initialize()

        if image.startswith("base64://"):
            return RiskLevel.Review, {"阿里云接口暂不支持base64图片"}

        if not image.startswith("http"):
            raise CensorError("预期外的输入")

        try:
            config = Config(
                access_key_id=self._key_id,
                access_key_secret=self._key_secret,
                endpoint=self._endpoint,
                region_id=self._region_id,
            )
            client = Client(config)
            runtime = util_models.RuntimeOptions()

            service_params = {"imageUrl": image, "infoType": self._image_info_type}
            request = models.ImageModerationRequest(
                service=self._image_service,
                service_parameters=json.dumps(service_params),
            )

            async with self._semaphore:
                response = await asyncio.get_event_loop().run_in_executor(
                    self._executor,
                    lambda: client.image_moderation_with_options(request, runtime),
                )

                if response.status_code != 200:
                    raise CensorError(
                        f"阿里云图片审核请求失败: HTTP {response.status_code}"
                    )

                body = response.body
                if body.code != 200:
                    raise CensorError(f"阿里云图片审核失败: {body.msg}")

                data = body.data
                risk_level = data.risk_level.lower() if data.risk_level else "pass"
                reason_words_set: set[str] = set()

                if data.result:
                    for item in data.result:
                        if hasattr(item, "label") and item.label:
                            reason_words_set.add(item.label)
                        if hasattr(item, "sub_label") and item.sub_label:
                            reason_words_set.add(item.sub_label)
                        if hasattr(item, "description") and item.description:
                            reason_words_set.add(item.description)

                if risk_level in ("none", "low"):
                    return RiskLevel.Pass, reason_words_set
                elif risk_level == "high":
                    return RiskLevel.Block, reason_words_set
                else:
                    return RiskLevel.Review, reason_words_set

        except CensorError:
            raise
        except Exception as e:
            raise CensorError(f"阿里云图片审核请求失败: {e}")
