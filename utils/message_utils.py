"""
消息处理工具模块
包含消息相关的通用工具函数
"""

import re

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class MessageUtils:
    """消息处理工具类"""

    @staticmethod
    def convert_message_chain(chain: list) -> list:
        """
        转换消息链为API格式

        Args:
            chain: 消息链

        Returns:
            API格式的消息列表
        """
        result = []
        for comp in chain:
            if isinstance(comp, Comp.Plain):
                result.append({"type": "text", "data": {"text": comp.text}})
            elif isinstance(comp, Comp.Image):
                if comp.file:
                    result.append({"type": "image", "data": {"file": comp.file}})
                elif comp.url:
                    result.append({"type": "image", "data": {"file": comp.url}})
            elif isinstance(comp, Comp.Forward):
                # 处理转发消息
                forward_id = None
                if hasattr(comp, "id") and comp.id:
                    forward_id = comp.id
                elif hasattr(comp, "forward_id") and comp.forward_id:
                    forward_id = comp.forward_id
                if forward_id:
                    result.append({"type": "forward", "data": {"id": forward_id}})
        return result

    @staticmethod
    def extract_reply_info(event: AstrMessageEvent) -> dict | None:
        """
        从回复消息中提取被引用消息的信息

        Args:
            event: 消息事件

        Returns:
            被引用消息的信息字典，如果没有引用则返回None
        """
        try:
            platform_name = event.get_platform_name()

            # aiocqhttp 平台：从 raw_message 中解析 CQ:reply
            if platform_name == "aiocqhttp":
                raw_message_str = ""

                # 尝试从 message_obj.raw_message 获取
                # 注意：raw_message 可能是 Event 对象或字符串
                try:
                    raw_message = event.message_obj.raw_message
                    if isinstance(raw_message, str):
                        raw_message_str = raw_message
                    elif hasattr(raw_message, "raw_message"):
                        # 如果是 Event 对象，尝试获取其 raw_message 属性
                        raw_message_str = raw_message.raw_message
                    elif hasattr(raw_message, "get"):
                        # 如果是 dict-like 对象
                        raw_message_str = raw_message.get("raw_message", "")
                except Exception as e:
                    logger.debug(f"extract_reply_info: 获取 raw_message 失败: {e}")

                # 备选：从 message_str 获取
                if not raw_message_str:
                    raw_message_str = event.message_str or ""

                if raw_message_str:
                    match = re.search(r"\[CQ:reply,id=(\d+)\]", raw_message_str)
                    if match:
                        return {
                            "message_id": match.group(1),
                            "sender_uid": None,
                            "sender_uid_str": None,
                        }

            # QQ官方/其他平台格式 (elementType == 7)
            if hasattr(event.message_obj, "raw"):
                raw_data = event.message_obj.raw
                if raw_data and "elements" in raw_data:
                    for element in raw_data.get("elements", []):
                        if element.get("elementType") == 7:
                            reply_element = element.get("replyElement", {})
                            return {
                                "message_id": reply_element.get("sourceMsgIdInRecords"),
                                "sender_uid": reply_element.get("senderUid"),
                                "sender_uid_str": reply_element.get("senderUidStr"),
                            }

            return None
        except Exception as e:
            logger.debug(f"从回复消息提取信息时发生异常: {e}")
            return None

    @staticmethod
    async def get_message_images(event: AstrMessageEvent, message_id: str) -> list[str]:
        """
        获取指定消息中的所有图片MD5

        Args:
            event: 消息事件
            message_id: 消息ID

        Returns:
            图片MD5列表
        """
        from .image_utils import ImageUtils

        md5_list = []
        try:
            platform_name = event.get_platform_name()

            if platform_name == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                    AiocqhttpMessageEvent,
                )

                if isinstance(event, AiocqhttpMessageEvent):
                    client = event.bot
                    # 使用 get_msg API 获取消息内容
                    result = await client.api.call_action(
                        "get_msg", message_id=int(message_id)
                    )

                    if result and "message" in result:
                        message_list = result["message"]
                        for msg in message_list:
                            if msg.get("type") == "image":
                                data = msg.get("data", {})
                                # 优先从 md5 字段获取
                                md5 = data.get("md5")
                                # 如果没有 md5 字段，从 file 字段提取（文件名通常是 MD5）
                                if not md5:
                                    file_name = data.get("file", "")
                                    # 文件名格式通常是 MD5.jpg 或 MD5.png 等
                                    if file_name:
                                        # 提取文件名中的 MD5 部分（去掉扩展名）
                                        md5 = file_name.split(".")[0]
                                # 验证MD5格式
                                if md5 and ImageUtils.is_valid_md5(md5):
                                    md5_list.append(md5.lower())

        except Exception as e:
            logger.debug(f"获取消息图片失败: {e}")
        return md5_list
