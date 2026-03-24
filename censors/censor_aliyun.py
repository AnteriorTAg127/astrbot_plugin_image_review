"""
阿里云内容审核模块
使用阿里云HTTP API进行文本和图片审核，避免cryptography版本冲突
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from typing import Any

import aiohttp

from ..database import RiskLevel
from .censor_base import CensorBase, CensorError


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

    async def initialize(self):
        """初始化异步资源"""
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )

    async def close(self):
        """关闭HTTP会话"""
        if self._session:
            await self._session.close()
            self._session = None

    def _sign_request(
        self, method: str, path: str, headers: dict, query: dict | None = None
    ) -> dict:
        """
        生成阿里云 ROA API 签名

        Args:
            method: HTTP方法 (GET, POST, etc.)
            path: 请求路径
            headers: 请求头
            query: 查询参数

        Returns:
            添加签名后的请求头
        """
        sign_headers = {}
        for key, value in headers.items():
            if key.startswith("x-ca-") or key in (
                "content-type",
                "content-md5",
                "date",
            ):
                sign_headers[key.lower()] = value

        sorted_query = sorted(query.items()) if query else []
        query_string = "&".join(f"{k}={v}" for k, v in sorted_query)

        canonicalized_resource = path
        if query_string:
            canonicalized_resource += "?" + query_string

        string_to_sign = (
            f"{method}\n"
            f"{sign_headers.get('content-type', '')}\n"
            f"{sign_headers.get('content-md5', '')}\n"
            f"{sign_headers.get('date', '')}\n"
            f"{canonicalized_resource}"
        )

        signature = hmac.new(
            self._key_secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1,
        ).digest()
        signature = base64.b64encode(signature).decode("utf-8")

        headers["Authorization"] = f"acs {self._key_id}:{signature}"
        return headers

    async def _call_api(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        query: dict | None = None,
    ) -> dict:
        """
        调用阿里云 API

        Args:
            method: HTTP方法
            path: 请求路径
            body: 请求体
            query: 查询参数

        Returns:
            API响应数据
        """
        await self.initialize()

        url = f"https://{self._endpoint}{path}"

        content_md5 = ""
        headers = {
            "Content-Type": "application/json",
            "Date": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()),
            "x-ca-signaturemethod": "HMAC-SHA1",
            "x-ca-version": "2022-03-02",
        }

        if body:
            body_str = json.dumps(body)
            content_md5 = base64.b64encode(
                hashlib.md5(body_str.encode("utf-8")).digest()
            ).decode("utf-8")
            headers["Content-MD5"] = content_md5
            headers["Content-Length"] = str(len(body_str))
        else:
            headers["Content-Length"] = "0"

        headers = self._sign_request(method, path, headers, query)

        async with self._semaphore:
            if method == "POST":
                async with self._session.request(
                    method,
                    url,
                    json=body,
                    headers=headers,
                    params=query,
                ) as response:
                    return await response.json()
            else:
                async with self._session.request(
                    method, url, headers=headers, params=query
                ) as response:
                    return await response.json()

    async def detect_text(self, text: str) -> tuple[RiskLevel, set[str]]:
        """
        对文本进行内容审核

        Args:
            text: 待审核文本

        Returns:
            (风险等级, 风险词集合)
        """
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
            body = {
                "Service": "chat_detection_pro",
                "ServiceParameters": json.dumps({"content": content}),
            }

            response = await self._call_api("POST", "/", body=body)

            if response.get("Code") != 200:
                raise CensorError(f"阿里云文本审核失败: {response.get('Msg')}")

            data = response.get("Data", {})
            risk_level = (data.get("RiskLevel") or "pass").lower()
            risk_words_set: set[str] = set()

            if data.get("Result"):
                for r_data in data["Result"]:
                    if r_data.get("RiskWords"):
                        risk_words_list = [
                            word.strip()
                            for word in r_data["RiskWords"].split(",")
                            if word.strip()
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

    async def detect_image(
        self, image: str, image_data: bytes | None = None
    ) -> tuple[RiskLevel, set[str]]:
        """
        对图片进行内容审核

        Args:
            image: 图片URL或base64字符串
            image_data: 已下载的图片数据（可选）

        Returns:
            (风险等级, 风险描述集合)
        """
        await self.initialize()

        if image.startswith("base64://"):
            return RiskLevel.Review, {"阿里云接口暂不支持base64图片"}

        if not image.startswith("http"):
            raise CensorError("预期外的输入")

        try:
            body = {
                "Service": self._image_service,
                "ServiceParameters": json.dumps(
                    {"imageUrl": image, "infoType": self._image_info_type}
                ),
            }

            response = await self._call_api("POST", "/", body=body)

            if response.get("Code") != 200:
                raise CensorError(f"阿里云图片审核失败: {response.get('Msg')}")

            data = response.get("Data", {})
            risk_level = (data.get("RiskLevel") or "pass").lower()
            reason_words_set: set[str] = set()

            if data.get("Result"):
                for item in data["Result"]:
                    if item.get("Label"):
                        reason_words_set.add(item["Label"])
                    if item.get("SubLabel"):
                        reason_words_set.add(item["SubLabel"])
                    if item.get("Description"):
                        reason_words_set.add(item["Description"])

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
