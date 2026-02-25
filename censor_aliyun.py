"""
阿里云内容审核模块
使用阿里云官方Python SDK进行文本和图片审核
"""

import asyncio
import base64
import json
from typing import Any, Set, Tuple

import aiohttp

from .censor_base import CensorBase, CensorError, RiskLevel


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
        self._session = None
        self._semaphore = asyncio.Semaphore(80)

    async def initialize(self):
        """初始化异步资源"""
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self):
        """关闭HTTP会话"""
        if self._session:
            await self._session.close()
            self._session = None

    async def detect_text(self, text: str) -> Tuple[RiskLevel, Set[str]]:
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
        all_risk_words: Set[str] = set()

        for risk_level, words in results:
            if risk_level.value > highest_risk_level.value:
                highest_risk_level = risk_level
            all_risk_words.update(words)

        return highest_risk_level, all_risk_words

    async def _check_single_text(self, content: str) -> Tuple[RiskLevel, Set[str]]:
        """
        审核单段文本

        Args:
            content: 文本内容

        Returns:
            (风险等级, 风险词集合)
        """
        from alibabacloud_green20220302.client import Client
        from alibabacloud_green20220302 import models
        from alibabacloud_tea_openapi.models import Config
        from alibabacloud_tea_util import models as util_models

        try:
            config = Config(
                access_key_id=self._key_id,
                access_key_secret=self._key_secret,
                endpoint=self._endpoint,
                region_id=self._region_id
            )
            client = Client(config)
            runtime = util_models.RuntimeOptions()

            service_params = {"content": content}
            request = models.TextModerationRequest(
                service="chat_detection_pro",
                service_parameters=json.dumps(service_params)
            )

            async with self._semaphore:
                response = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: client.text_moderation_with_options(request, runtime)
                )

                if response.status_code != 200:
                    raise CensorError(f"阿里云文本审核请求失败: HTTP {response.status_code}")

                body = response.body
                if body.code != 200:
                    raise CensorError(f"阿里云文本审核失败: {body.msg}")

                data = body.data
                risk_level = data.risk_level.lower() if data.risk_level else "pass"
                risk_words_set: Set[str] = set()

                if data.result:
                    for r_data in data.result:
                        if hasattr(r_data, 'risk_words') and r_data.risk_words:
                            risk_words_list = [word.strip() for word in r_data.risk_words.split(",")]
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

    async def detect_image(self, image: str) -> Tuple[RiskLevel, Set[str]]:
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

        from alibabacloud_green20220302.client import Client
        from alibabacloud_green20220302 import models
        from alibabacloud_tea_openapi.models import Config
        from alibabacloud_tea_util import models as util_models

        try:
            config = Config(
                access_key_id=self._key_id,
                access_key_secret=self._key_secret,
                endpoint=self._endpoint,
                region_id=self._region_id
            )
            client = Client(config)
            runtime = util_models.RuntimeOptions()

            service_params = {
                "imageUrl": image,
                "infoType": "customImage,textInImage"
            }
            request = models.ImageModerationRequest(
                service="baselineCheck",
                service_parameters=json.dumps(service_params)
            )

            async with self._semaphore:
                response = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: client.image_moderation_with_options(request, runtime)
                )

                if response.status_code != 200:
                    raise CensorError(f"阿里云图片审核请求失败: HTTP {response.status_code}")

                body = response.body
                if body.code != 200:
                    raise CensorError(f"阿里云图片审核失败: {body.msg}")

                data = body.data
                risk_level = data.risk_level.lower() if data.risk_level else "pass"
                reason_words_set: Set[str] = set()

                if data.result:
                    for item in data.result:
                        if hasattr(item, 'label') and item.label:
                            reason_words_set.add(item.label)
                        if hasattr(item, 'sub_label') and item.sub_label:
                            reason_words_set.add(item.sub_label)
                        if hasattr(item, 'description') and item.description:
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
