"""
VLAI (Vision Language AI) 内容审核模块
使用 AstrBot 标准 AI 接口进行图片违规内容审核
基于 Qwen3-VL 模型的图片审核实现
"""

import asyncio
import base64
import io
from typing import Any

from PIL import Image

from astrbot.api import logger
from astrbot.core.agent.message import ImageURLPart, TextPart, UserMessageSegment

from .censor_base import CensorBase, CensorError
from .database import RiskLevel


class VLAICensor(CensorBase):
    """VLAI 内容审核器 - 使用 AstrBot 标准 AI 接口"""

    # 默认审核提示词 - 通用图片内容审核
    DEFAULT_CENSOR_PROMPT = (
        "你是一个内容审核助手。请分析用户提供的图片内容，判断是否包含以下违规类型：\n\n"
        "1. 色情低俗：包含性行为、性暗示、裸露、色情互动、色情文本、性玩具等\n"
        "2. 暴力血腥：包含杀人、打架、自残、血腥画面、恐怖袭击、虐待等\n"
        "3. 违禁违法：包含毒品、赌博、枪支、诈骗、钓鱼信息、非法交易等\n"
        "4. 仇恨言论：包含种族歧视、地域黑、宗教仇恨、性别对立、针对群体的攻击等\n"
        "5. 政治敏感：包含分裂国家、反动言论、领导人负面讨论、历史虚无主义等\n"
        "6. 引人不适：包含恐怖、恶心、令人生理不适的内容（如尸块、密集恐惧、畸形等）\n"
        "7. 攻击辱骂：包含人身攻击、辱骂他人、网络暴力、反特定群体等\n\n"
        "注意：请区分正常玩梗与恶意违规，避免误判。请识别图片中的场景、文字和元素，综合判断。\n"
        "若内容不涉及上述任何违规，则视为正常。\n\n"
        "请严格按照以下格式回复（只回复一行）：\n"
        "结果: [正常/违规] | 原因: [简要描述]\n\n"
        "如果正常，回复: 结果: 正常 | 原因: 无\n"
        "如果违规，回复: 结果: 违规 | 原因: [具体违规类型]（不超过20字）"
    )

    # 动图审核提示词 - 严格模式（侧重色情低俗检测）
    DEFAULT_GIF_STRICT_PROMPT = (
        "你是一个内容审核助手。请分析用户提供的动图内容，逐帧观察画面变化，判断是否包含以下违规类型：\n\n"
        "1. 色情低俗：包含性行为、性暗示动作（如：露出、叉腿、跪趴）、裸露、色情互动、色情文本、性玩具等\n"
        "2. 暴力血腥：包含杀人、打架、自残、血腥画面、恐怖袭击、虐待等\n"
        "3. 违禁违法：包含毒品、赌博、枪支、诈骗、钓鱼信息、非法交易等\n"
        "4. 仇恨言论：包含种族歧视、地域黑、宗教仇恨、性别对立、针对群体的攻击等\n"
        "5. 政治敏感：包含分裂国家、反动言论、领导人负面讨论、历史虚无主义等\n"
        "6. 引人不适：包含恐怖、恶心、令人生理不适的内容（如尸块、密集恐惧、畸形等）\n"
        "7. 攻击辱骂：包含人身攻击、辱骂他人、网络暴力、反特定群体等\n\n"
        "注意：动图可能包含快速变化的画面，请仔细观察关键帧。特别警惕性暗示动作和暴露内容。\n"
        "请区分正常玩梗与恶意违规，避免误判。若内容不涉及上述任何违规，则视为正常。\n\n"
        "请严格按照以下格式回复（只回复一行）：\n"
        "结果: [正常/违规] | 原因: [简要描述]\n\n"
        "如果正常，回复: 结果: 正常 | 原因: 无\n"
        "如果违规，回复: 结果: 违规 | 原因: [具体违规类型]（不超过20字）"
    )

    # 动图审核提示词 - 宽松模式（侧重明显违规检测）
    DEFAULT_GIF_LOOSE_PROMPT = (
        "你是一个内容审核助手。请分析用户提供的动图内容，观察整体画面，判断是否包含以下明显违规类型：\n\n"
        "1. 色情低俗：包含明显性行为、严重裸露、露骨色情互动、色情文本、性玩具展示等\n"
        "2. 暴力血腥：包含明显杀人、严重自残、大量血腥画面、恐怖袭击、残忍虐待等\n"
        "3. 违禁违法：包含明显毒品、赌博、枪支展示、诈骗信息、非法交易等\n"
        "4. 仇恨言论：包含明显种族歧视、宗教仇恨、针对群体的恶意攻击等\n"
        "5. 政治敏感：包含明显分裂国家、反动言论、恶意诋毁领导人等\n"
        "6. 引人不适：包含明显恐怖、极度恶心、严重生理不适的内容\n"
        "7. 攻击辱骂：包含明显人身攻击、恶意辱骂、网络暴力等\n\n"
        "注意：此模式为宽松检测，仅拦截明显违规内容。轻微的暗示、正常的表情动作、普通玩梗应视为正常。\n"
        "请识别动图中的场景、文字和元素，综合判断。若内容不涉及上述明显违规，则视为正常。\n\n"
        "请严格按照以下格式回复（只回复一行）：\n"
        "结果: [正常/违规] | 原因: [简要描述]\n\n"
        "如果正常，回复: 结果: 正常 | 原因: 无\n"
        "如果违规，回复: 结果: 违规 | 原因: [具体违规类型]（不超过20字）"
    )

    def __init__(self, config: dict[str, Any], context: Any) -> None:
        """
        初始化 VLAI 审核器

        Args:
            config: 配置字典
            context: AstrBot 上下文对象
        """
        super().__init__(config)
        self._context = context
        self._provider_id = config.get("provider_id", "")
        self._backup_provider_id = config.get("backup_provider_id", "")
        self._max_image_size = config.get("max_image_size", 640)
        self._censor_prompt = config.get("censor_prompt", self.DEFAULT_CENSOR_PROMPT)

    async def initialize(self):
        """初始化审核器"""
        logger.debug("VLAI 审核器初始化完成")

    async def close(self):
        """关闭资源"""
        logger.debug("VLAI 审核器资源已关闭")

    def _resize_image_if_needed(self, image_data: bytes) -> tuple[str, str]:
        """
        图片预处理 - 尺寸调整与格式转换

        Args:
            image_data: 图片字节数据

        Returns:
            (base64图片数据, MIME类型前缀)
        """
        logger.debug(f"开始处理图片，原始数据大小: {len(image_data)} bytes")

        with Image.open(io.BytesIO(image_data)) as img:
            original_width, original_height = img.size
            original_mode = img.mode
            logger.debug(
                f"图片原始信息: 尺寸={original_width}x{original_height}, 模式={original_mode}"
            )

            width, height = original_width, original_height

            # 尺寸调整
            if self._max_image_size > 0 and (
                width > self._max_image_size or height > self._max_image_size
            ):
                long_side = max(width, height)
                scale = self._max_image_size / long_side
                new_width = int(width * scale)
                new_height = int(height * scale)
                img = img.resize((new_width, new_height), Image.LANCZOS)
                width, height = new_width, new_height
                logger.debug(f"图片尺寸过大，已缩放至: {width}x{height}")
            elif width < 256 and height < 256:
                new_width = width * 2
                new_height = height * 2
                img = img.resize((new_width, new_height), Image.LANCZOS)
                width, height = new_width, new_height
                logger.debug(f"图片尺寸过小，已放大至: {width}x{height}")
            else:
                logger.debug(f"图片尺寸无需调整: {width}x{height}")

            # 格式转换处理
            if img.mode in ("RGBA", "LA", "PA"):
                img_format = "PNG"
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "RGBA":
                    background.paste(img, mask=img.split()[3])
                else:
                    img_rgba = img.convert("RGBA")
                    background.paste(img_rgba, mask=img_rgba.split()[3])
                img = background
                logger.debug(f"透明通道图片已转换为 RGB 模式，格式: {img_format}")
            elif img.mode == "P":
                img_format = "PNG"
                if "transparency" in img.info:
                    img = img.convert("RGBA")
                    background = Image.new("RGB", img.size, (255, 255, 255))
                    background.paste(img, mask=img.split()[3])
                    img = background
                    logger.debug(
                        f"Palette模式(透明)图片已转换为 RGB 模式，格式: {img_format}"
                    )
                else:
                    img = img.convert("RGB")
                    logger.debug(
                        f"Palette模式图片已转换为 RGB 模式，格式: {img_format}"
                    )
            elif img.mode != "RGB":
                img = img.convert("RGB")
                img_format = "JPEG"
                logger.debug(
                    f"非RGB模式({original_mode})图片已转换为 RGB 模式，格式: {img_format}"
                )
            else:
                img_format = "JPEG"
                logger.debug(f"图片模式为 RGB，格式: {img_format}")

            # 转换为 base64
            buffer = io.BytesIO()
            img.save(buffer, format=img_format)
            base64_data = base64.b64encode(buffer.getvalue()).decode("utf-8")
            mime_type = f"data:image/{img_format.lower()};base64"

            logger.debug(
                f"图片处理完成: 最终尺寸={width}x{height}, 格式={img_format}, base64长度={len(base64_data)}"
            )

            return base64_data, mime_type

    async def detect_text(self, text: str) -> tuple[RiskLevel, set[str]]:
        """
        检测文本内容

        Args:
            text: 待检测文本

        Returns:
            (风险等级, 风险词集合)
        """
        # VLAI 主要用于图片审核，文本审核返回通过
        return RiskLevel.Pass, set()

    async def detect_image(self, image: str) -> tuple[RiskLevel, set[str]]:
        """
        检测图片内容

        Args:
            image: 图片URL或base64字符串

        Returns:
            (风险等级, 风险描述集合)
        """
        logger.debug(
            f"开始 VLAI 图片审核，输入类型: {'URL' if image.startswith('http') else 'base64' if image.startswith('base64://') else '未知'}"
        )

        try:
            # 获取图片数据
            if image.startswith("http"):
                logger.debug(f"从 URL 下载图片: {image[:80]}...")
                # 下载图片
                from .censor_flow import download_image

                image_data = await download_image(image)
                logger.debug(f"图片下载完成，大小: {len(image_data)} bytes")
                # 使用线程池执行同步的图片处理操作，避免阻塞事件循环
                base64_data, mime_type = await asyncio.to_thread(
                    self._resize_image_if_needed, image_data
                )
            elif image.startswith("base64://"):
                logger.debug("处理 base64 图片")
                # 处理 base64 图片
                base64_str = image[9:]  # 去掉 "base64://" 前缀
                image_data = base64.b64decode(base64_str)
                logger.debug(f"base64 解码完成，大小: {len(image_data)} bytes")
                # 使用线程池执行同步的图片处理操作，避免阻塞事件循环
                base64_data, mime_type = await asyncio.to_thread(
                    self._resize_image_if_needed, image_data
                )
            else:
                raise CensorError(f"不支持的图片格式: {image[:50]}...")

            # 构建多模态消息
            image_url = f"{mime_type},{base64_data}"
            user_msg = UserMessageSegment(
                content=[
                    ImageURLPart(image_url=ImageURLPart.ImageURL(url=image_url)),
                    TextPart(text=self._censor_prompt),
                ]
            )

            # 先尝试使用主提供商
            provider_id = self._provider_id if self._provider_id else None
            logger.debug(
                f"开始调用 LLM 进行图片审核，主提供商: {provider_id if provider_id else '默认'}"
            )

            try:
                llm_resp = await asyncio.wait_for(
                    self._context.llm_generate(
                        chat_provider_id=provider_id,
                        contexts=[user_msg],
                    ),
                    timeout=30.0,
                )
                logger.debug("主提供商 LLM 调用完成")
            except Exception as primary_error:
                # 主提供商失败，尝试备用提供商
                if self._backup_provider_id:
                    logger.warning(
                        f"主提供商调用失败: {primary_error}，尝试使用备用提供商: {self._backup_provider_id}"
                    )
                    try:
                        llm_resp = await asyncio.wait_for(
                            self._context.llm_generate(
                                chat_provider_id=self._backup_provider_id,
                                contexts=[user_msg],
                            ),
                            timeout=30.0,
                        )
                        logger.debug("备用提供商 LLM 调用完成")
                    except Exception as backup_error:
                        logger.error(f"备用提供商也调用失败: {backup_error}")
                        raise CensorError(
                            f"主提供商和备用提供商均调用失败: 主错误={primary_error}, 备用错误={backup_error}"
                        )
                else:
                    # 没有配置备用提供商，直接抛出原错误
                    raise

            # 解析返回结果
            # 优先使用 completion_text（结果），如果没有则使用 reasoning_content（思维链）
            content = ""
            if llm_resp.completion_text:
                content = llm_resp.completion_text.strip()
                logger.debug("使用 completion_text 作为审核结果")
            elif hasattr(llm_resp, "reasoning_content") and llm_resp.reasoning_content:
                content = llm_resp.reasoning_content.strip()
                logger.debug("使用 reasoning_content 作为审核结果")

            logger.debug(f"VLAI 审核原始结果: {content}")

            # 解析结构化输出
            risk_level, risk_reason = self._parse_censor_result(content)
            logger.debug(f"解析结果: 风险等级={risk_level.name}, 原因={risk_reason}")

            if risk_level == RiskLevel.Block:
                return RiskLevel.Block, {risk_reason}
            else:
                return RiskLevel.Pass, set()

        except asyncio.TimeoutError:
            logger.error("VLAI 图片审核超时")
            raise CensorError("VLAI 图片审核超时，请稍后重试")
        except CensorError:
            raise
        except Exception as e:
            logger.error(f"VLAI 图片审核失败: {e}")
            raise CensorError(f"VLAI 图片审核失败: {e}")

    def _parse_censor_result(self, content: str) -> tuple[RiskLevel, str]:
        """
        解析审核结果

        Args:
            content: LLM 返回的原始内容

        Returns:
            (风险等级, 风险原因)
        """
        content = content.strip()

        # 尝试解析结构化格式: "结果: xxx | 原因: xxx"
        if "结果:" in content:
            try:
                # 提取结果部分
                result_part = content.split("结果:")[1].split("|")[0].strip().lower()

                # 提取原因部分
                if "原因:" in content:
                    reason_part = content.split("原因:")[1].strip()
                else:
                    reason_part = "未提供原因"

                # 判断结果
                if "违规" in result_part:
                    return RiskLevel.Block, reason_part
                elif "正常" in result_part:
                    return RiskLevel.Pass, ""
                elif "review" in result_part or "复审" in result_part:
                    return RiskLevel.Review, reason_part
            except Exception as e:
                logger.debug(f"解析结构化结果失败: {e}, 内容: {content}")

        # 降级处理：使用简单的关键词匹配（向后兼容）
        content_lower = content.lower()

        # 明确的违规关键词
        nsfw_keywords = [
            "违规",
            "porn",
            "nsfw",
            "adult",
            "sexual",
            "nude",
            "暴力",
            "血腥",
            "恐怖",
        ]
        # 明确的正常关键词
        safe_keywords = ["正常", "safe", "appropriate", "clean", "无违规"]

        has_nsfw = any(kw in content_lower for kw in nsfw_keywords)
        has_safe = any(kw in content_lower for kw in safe_keywords)

        # 逻辑判断
        if has_nsfw and not has_safe:
            return RiskLevel.Block, content
        elif has_safe and not has_nsfw:
            return RiskLevel.Pass, ""
        elif has_nsfw and has_safe:
            # 同时包含两者，根据上下文判断
            # 如果"正常"在"违规"之前出现，可能表示"看起来正常但..."
            nsfw_pos = min(
                content_lower.find(kw) for kw in nsfw_keywords if kw in content_lower
            )
            safe_pos = min(
                content_lower.find(kw) for kw in safe_keywords if kw in content_lower
            )

            if nsfw_pos < safe_pos:
                return RiskLevel.Block, content
            else:
                return RiskLevel.Pass, ""
        else:
            # 无法判断，保守起见返回 Review
            logger.warning(f"无法解析审核结果，返回复审: {content}")
            return RiskLevel.Review, f"无法解析的结果: {content[:100]}"
