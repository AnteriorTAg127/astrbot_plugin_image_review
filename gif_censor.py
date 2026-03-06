"""
动图(GIF)增强检测模块
检测图片是否为多帧格式，并对多帧图片进行采样检测
支持两种检测模式：
- separate: 逐帧分开检查（多次调用模型，更精确）
- batch: 多帧合并检查（单次调用模型，更省token）
"""

import asyncio
import base64
import io
from typing import Any

from PIL import Image

from astrbot.api import logger

from .censor_base import CensorBase, CensorError
from .database import RiskLevel


class GIFCensor:
    """动图增强检测器 - 检测多帧图片并采样检测"""

    def __init__(
        self, config: dict[str, Any], context: Any, vl_censor: CensorBase
    ) -> None:
        """
        初始化动图检测器

        Args:
            config: 配置字典
            context: AstrBot 上下文对象
            vl_censor: VLAI 审核器实例（用于单帧检测）
        """
        self._config = config
        self._context = context
        self._vl_censor = vl_censor
        self._provider_id = config.get("provider_id", "")
        self._max_image_size = config.get("max_image_size", 640)
        self._frame_sample_count = config.get("frame_sample_count", 3)
        self._detection_mode = config.get("detection_mode", "separate")
        logger.debug(
            f"GIFCensor 初始化: provider_id={self._provider_id}, "
            f"max_image_size={self._max_image_size}, "
            f"frame_sample_count={self._frame_sample_count}, "
            f"detection_mode={self._detection_mode}"
        )
        self._censor_prompt = config.get(
            "censor_prompt",
            "请分析这张图片是否有显著色情违规内容（性暗示动作，如：露出、叉腿、跪趴，性行为、透明雨衣、性玩具。\n\n"
            "在对应语境下的包含「射精」及其同音词文本的。\n"
            "请严格按照以下格式回复（只回复这一行）：\n"
            "结果: [正常/违规] | 原因: [简要描述]\n\n"
            "如果图片正常，回复: 结果: 正常 | 原因: 无\n"
            "如果图片违规，回复: 结果: 违规 | 原因: [具体违规类型]",
        )
        self._batch_censor_prompt = config.get(
            "batch_censor_prompt",
            "我将发送给你多张图片，这些是同一张动图的不同帧。请分析所有帧中是否有显著色情违规内容"
            "（性暗示动作，如：露出、叉腿、跪趴，性行为、透明雨衣、性玩具。\n\n"
            "在对应语境下的包含「射精」及其同音词文本的）。\n\n"
            "请对每张图片分别判断，并严格按照以下格式回复：\n"
            "帧1: 结果: [正常/违规] | 原因: [简要描述]\n"
            "帧2: 结果: [正常/违规] | 原因: [简要描述]\n"
            "...\n\n"
            "如果所有帧都正常，可以简写为: 所有帧正常\n"
            "如果有任何一帧违规，请分别列出违规帧",
        )

    @staticmethod
    def is_animated_image(image_data: bytes) -> tuple[bool, int]:
        """
        检测图片是否为多帧格式（GIF/动图）

        Args:
            image_data: 图片字节数据

        Returns:
            (是否为动图, 帧数)
        """
        try:
            with Image.open(io.BytesIO(image_data)) as img:
                # 获取帧数
                frame_count = 0
                try:
                    while True:
                        frame_count += 1
                        img.seek(frame_count)
                except EOFError:
                    pass

                # 重置到第一帧
                img.seek(0)

                # 判断是否为动图：帧数大于1，或者是GIF格式
                is_animated = frame_count > 1 or img.format == "GIF"

                logger.debug(
                    f"图片格式: {img.format}, 帧数: {frame_count}, 是否为动图: {is_animated}"
                )
                return is_animated, frame_count
        except Exception as e:
            logger.debug(f"检测图片是否为动图时发生异常: {e}")
            return False, 1

    def _extract_frames(
        self, image_data: bytes, frame_count: int
    ) -> list[tuple[str, str]]:
        """
        从动图中均匀抽取帧并转换为 base64

        Args:
            image_data: 图片字节数据
            frame_count: 总帧数

        Returns:
            帧数据列表，每项为 (base64数据, MIME类型)
        """
        frames = []

        try:
            with Image.open(io.BytesIO(image_data)) as img:
                # 计算采样帧索引（均匀分布）
                if frame_count <= self._frame_sample_count:
                    # 帧数少于等于采样数，全部抽取
                    sample_indices = list(range(frame_count))
                else:
                    # 均匀抽取
                    step = frame_count / self._frame_sample_count
                    sample_indices = [
                        int(i * step) for i in range(self._frame_sample_count)
                    ]

                logger.debug(f"动图总帧数: {frame_count}, 采样帧索引: {sample_indices}")

                for idx in sample_indices:
                    try:
                        img.seek(idx)
                        frame = img.copy()

                        # 处理帧（缩放、格式转换）
                        base64_data, mime_type = self._process_frame(frame)
                        frames.append((base64_data, mime_type))
                        logger.debug(f"成功提取第 {idx} 帧")

                    except Exception as e:
                        logger.debug(f"提取第 {idx} 帧时发生异常: {e}")
                        continue

        except Exception as e:
            logger.error(f"提取动图帧时发生异常: {e}")

        return frames

    def _process_frame(self, frame: Image.Image) -> tuple[str, str]:
        """
        处理单帧图片（缩放、格式转换）

        Args:
            frame: PIL Image 对象

        Returns:
            (base64数据, MIME类型)
        """
        # 尺寸调整
        width, height = frame.size

        if self._max_image_size > 0 and (
            width > self._max_image_size or height > self._max_image_size
        ):
            long_side = max(width, height)
            scale = self._max_image_size / long_side
            new_width = int(width * scale)
            new_height = int(height * scale)
            frame = frame.resize((new_width, new_height), Image.LANCZOS)
            logger.debug(f"帧尺寸过大，已缩放至: {new_width}x{new_height}")

        # 格式转换处理
        if frame.mode in ("RGBA", "LA", "PA"):
            img_format = "PNG"
            background = Image.new("RGB", frame.size, (255, 255, 255))
            if frame.mode == "RGBA":
                background.paste(frame, mask=frame.split()[3])
            else:
                img_rgba = frame.convert("RGBA")
                background.paste(img_rgba, mask=img_rgba.split()[3])
            frame = background
        elif frame.mode == "P":
            img_format = "PNG"
            if "transparency" in frame.info:
                frame = frame.convert("RGBA")
                background = Image.new("RGB", frame.size, (255, 255, 255))
                background.paste(frame, mask=frame.split()[3])
                frame = background
            else:
                frame = frame.convert("RGB")
        elif frame.mode != "RGB":
            frame = frame.convert("RGB")
            img_format = "JPEG"
        else:
            img_format = "JPEG"

        # 转换为 base64
        buffer = io.BytesIO()
        frame.save(buffer, format=img_format)
        base64_data = base64.b64encode(buffer.getvalue()).decode("utf-8")
        mime_type = f"data:image/{img_format.lower()};base64"

        return base64_data, mime_type

    async def detect_animated_image(self, image_data: bytes) -> tuple[RiskLevel, str]:
        """
        检测动图内容（多帧采样检测）

        Args:
            image_data: 图片字节数据

        Returns:
            (风险等级, 风险原因)
        """
        try:
            # 提取帧
            is_animated, frame_count = self.is_animated_image(image_data)

            if not is_animated:
                logger.debug("图片不是动图，使用普通检测方式")
                return RiskLevel.Pass, "非动图"

            logger.info(
                f"检测到动图，总帧数: {frame_count}，检测模式: {self._detection_mode}"
            )

            # 在线程池中执行帧提取（避免阻塞事件循环）
            frames = await asyncio.to_thread(
                self._extract_frames, image_data, frame_count
            )

            if not frames:
                logger.warning("未能提取到任何帧，返回通过")
                return RiskLevel.Pass, "帧提取失败"

            logger.debug(f"成功提取 {len(frames)} 帧")

            # 根据检测模式选择检测方式
            if self._detection_mode == "batch":
                return await self._detect_batch(frames)
            else:
                return await self._detect_separate(frames)

        except Exception as e:
            logger.error(f"动图检测异常: {e}")
            raise CensorError(f"动图检测异常: {e}")

    async def _detect_separate(
        self, frames: list[tuple[str, str]]
    ) -> tuple[RiskLevel, str]:
        """
        逐帧分开检测模式 - 多次调用模型，更精确

        Args:
            frames: 帧数据列表

        Returns:
            (风险等级, 风险原因)
        """
        from astrbot.core.agent.message import (
            ImageURLPart,
            TextPart,
            UserMessageSegment,
        )

        logger.debug(f"使用逐帧分开检测模式，共 {len(frames)} 帧")

        all_violations = []
        provider_id = self._provider_id if self._provider_id else None
        logger.debug(f"动图检测使用提供商: {provider_id if provider_id else '默认'}")

        for i, (base64_data, mime_type) in enumerate(frames):
            try:
                # 构建多模态消息
                image_url = f"{mime_type},{base64_data}"
                user_msg = UserMessageSegment(
                    content=[
                        ImageURLPart(image_url=ImageURLPart.ImageURL(url=image_url)),
                        TextPart(text=self._censor_prompt),
                    ]
                )

                logger.debug(f"开始检测第 {i + 1}/{len(frames)} 帧")

                # 调用 LLM 进行检测，设置30秒超时
                llm_resp = await asyncio.wait_for(
                    self._context.llm_generate(
                        chat_provider_id=provider_id,
                        contexts=[user_msg],
                    ),
                    timeout=30.0,
                )

                # 解析结果
                # 优先使用 completion_text（结果），如果没有则使用 reasoning_content（思维链）
                content = ""
                if llm_resp.completion_text:
                    content = llm_resp.completion_text.strip()
                elif (
                    hasattr(llm_resp, "reasoning_content")
                    and llm_resp.reasoning_content
                ):
                    content = llm_resp.reasoning_content.strip()

                logger.debug(f"第 {i + 1} 帧检测结果: {content}")

                risk_level, risk_reason = self._parse_frame_result(content)

                if risk_level in (RiskLevel.Review, RiskLevel.Block):
                    all_violations.append(
                        {"frame": i + 1, "level": risk_level, "reason": risk_reason}
                    )

            except asyncio.TimeoutError:
                logger.error(f"第 {i + 1} 帧检测超时")
                continue
            except Exception as e:
                logger.error(f"第 {i + 1} 帧检测异常: {e}")
                continue

        return self._aggregate_results(all_violations, len(frames))

    async def _detect_batch(
        self, frames: list[tuple[str, str]]
    ) -> tuple[RiskLevel, str]:
        """
        批量合并检测模式 - 单次调用模型，更省token

        Args:
            frames: 帧数据列表

        Returns:
            (风险等级, 风险原因)
        """
        from astrbot.core.agent.message import (
            ImageURLPart,
            TextPart,
            UserMessageSegment,
        )

        logger.debug(f"使用批量合并检测模式，共 {len(frames)} 帧")

        try:
            # 构建包含所有帧的多模态消息
            content_parts = [TextPart(text=self._batch_censor_prompt)]

            for i, (base64_data, mime_type) in enumerate(frames):
                image_url = f"{mime_type},{base64_data}"
                content_parts.append(
                    ImageURLPart(image_url=ImageURLPart.ImageURL(url=image_url))
                )
                content_parts.append(TextPart(text=f"[帧{i + 1}]"))

            user_msg = UserMessageSegment(content=content_parts)
            provider_id = self._provider_id if self._provider_id else None
            logger.debug(
                f"动图检测使用提供商: {provider_id if provider_id else '默认'}"
            )

            logger.debug("开始批量检测所有帧")

            # 调用 LLM 进行检测，设置60秒超时（因为帧数较多）
            llm_resp = await asyncio.wait_for(
                self._context.llm_generate(
                    chat_provider_id=provider_id,
                    contexts=[user_msg],
                ),
                timeout=60.0,
            )

            # 解析结果
            # 优先使用 completion_text（结果），如果没有则使用 reasoning_content（思维链）
            content = ""
            if llm_resp.completion_text:
                content = llm_resp.completion_text.strip()
                logger.debug("使用 completion_text 作为批量检测结果")
            elif hasattr(llm_resp, "reasoning_content") and llm_resp.reasoning_content:
                content = llm_resp.reasoning_content.strip()
                logger.debug("使用 reasoning_content 作为批量检测结果")

            logger.debug(f"批量检测结果: {content}")

            return self._parse_batch_result(content, len(frames))

        except asyncio.TimeoutError:
            logger.error("批量检测超时")
            # 超时后降级为逐帧检测
            logger.warning("批量检测超时，降级为逐帧检测")
            return await self._detect_separate(frames)
        except Exception as e:
            logger.error(f"批量检测异常: {e}")
            # 异常后降级为逐帧检测
            logger.warning("批量检测异常，降级为逐帧检测")
            return await self._detect_separate(frames)

    def _parse_batch_result(
        self, content: str, total_frames: int
    ) -> tuple[RiskLevel, str]:
        """
        解析批量检测结果

        Args:
            content: LLM 返回的原始内容
            total_frames: 总帧数

        Returns:
            (风险等级, 风险原因)
        """
        content = content.strip()
        all_violations = []

        # 检查是否所有帧都正常（简写格式）
        if "所有帧正常" in content or "全部正常" in content:
            logger.info("批量检测：所有帧正常")
            return RiskLevel.Pass, "动图检测通过"

        # 检查是否是单行统一结果格式（如 "结果: 正常 | 原因: 无"）
        if "结果:" in content and content.count("结果:") == 1:
            try:
                result_part = content.split("结果:")[1].split("|")[0].strip().lower()

                if "原因:" in content:
                    reason_part = content.split("原因:")[1].strip()
                else:
                    reason_part = "未提供原因"

                if "正常" in result_part:
                    logger.info("批量检测：所有帧正常（统一结果格式）")
                    return RiskLevel.Pass, "动图检测通过"
                elif "违规" in result_part:
                    logger.warning(f"批量检测：检测到违规: {reason_part}")
                    return RiskLevel.Block, f"动图违规: {reason_part}"
                elif "review" in result_part or "复审" in result_part:
                    logger.warning(f"批量检测：检测到可疑: {reason_part}")
                    return RiskLevel.Review, f"动图可疑: {reason_part}"
            except Exception as e:
                logger.debug(f"解析统一结果格式失败: {e}")

        # 尝试解析每帧的结果（多行格式）
        for i in range(1, total_frames + 1):
            # 查找帧标记
            frame_patterns = [
                f"帧{i}:",
                f"帧 {i}:",
                f"Frame {i}:",
                f"[{i}]",
            ]

            for pattern in frame_patterns:
                if pattern in content:
                    # 提取该帧的结果
                    try:
                        frame_section = content.split(pattern)[1].split("\n")[0]
                        if "结果:" in frame_section:
                            result_part = (
                                frame_section.split("结果:")[1]
                                .split("|")[0]
                                .strip()
                                .lower()
                            )

                            if "原因:" in frame_section:
                                reason_part = frame_section.split("原因:")[1].strip()
                            else:
                                reason_part = "未提供原因"

                            if "违规" in result_part:
                                all_violations.append(
                                    {
                                        "frame": i,
                                        "level": RiskLevel.Block,
                                        "reason": reason_part,
                                    }
                                )
                            elif "review" in result_part or "复审" in result_part:
                                all_violations.append(
                                    {
                                        "frame": i,
                                        "level": RiskLevel.Review,
                                        "reason": reason_part,
                                    }
                                )
                    except Exception as e:
                        logger.debug(f"解析第 {i} 帧结果失败: {e}")
                    break

        # 如果没有解析到任何帧结果，使用关键词匹配
        if not all_violations:
            content_lower = content.lower()

            # 检查是否有违规关键词
            nsfw_keywords = ["违规", "porn", "nsfw", "sexual", "nude"]
            has_nsfw = any(kw in content_lower for kw in nsfw_keywords)

            # 检查是否有正常关键词
            safe_keywords = ["正常", "safe", "无违规"]
            has_safe = any(kw in content_lower for kw in safe_keywords)

            if has_nsfw and not has_safe:
                # 有违规关键词，无正常关键词
                all_violations.append(
                    {"frame": 1, "level": RiskLevel.Block, "reason": content[:100]}
                )
            elif has_safe and not has_nsfw:
                # 只有正常关键词，返回通过
                logger.info("批量检测：通过关键词判断所有帧正常")
                return RiskLevel.Pass, "动图检测通过"
            elif has_nsfw and has_safe:
                # 同时包含，需要进一步判断
                nsfw_pos = min(
                    content_lower.find(kw)
                    for kw in nsfw_keywords
                    if kw in content_lower
                )
                safe_pos = min(
                    content_lower.find(kw)
                    for kw in safe_keywords
                    if kw in content_lower
                )

                if nsfw_pos < safe_pos:
                    all_violations.append(
                        {"frame": 1, "level": RiskLevel.Block, "reason": content[:100]}
                    )
                else:
                    logger.info("批量检测：通过关键词判断所有帧正常")
                    return RiskLevel.Pass, "动图检测通过"
            else:
                # 无法解析，返回通过（保守策略）
                logger.warning(f"无法解析批量检测结果，返回通过: {content}")
                return RiskLevel.Pass, "批量检测结果解析失败"

        return self._aggregate_results(all_violations, total_frames)

    def _aggregate_results(
        self, all_violations: list[dict], total_frames: int
    ) -> tuple[RiskLevel, str]:
        """
        聚合所有帧的检测结果

        Args:
            all_violations: 违规帧列表
            total_frames: 总帧数

        Returns:
            (风险等级, 风险原因)
        """
        if not all_violations:
            logger.info("动图所有采样帧检测通过")
            return RiskLevel.Pass, "动图检测通过"

        # 统计违规情况
        block_count = sum(1 for v in all_violations if v["level"] == RiskLevel.Block)
        review_count = sum(1 for v in all_violations if v["level"] == RiskLevel.Review)

        # 构建违规原因
        violation_reasons = [
            f"帧{v['frame']}: {v['reason']}" for v in all_violations[:3]
        ]
        reason_str = "; ".join(violation_reasons)
        if len(all_violations) > 3:
            reason_str += f" 等共{len(all_violations)}帧违规"

        if block_count > 0:
            logger.warning(
                f"动图检测到违规帧，Block: {block_count}, Review: {review_count}"
            )
            return (
                RiskLevel.Block,
                f"动图违规({block_count}帧严重违规): {reason_str}",
            )
        else:
            logger.warning(f"动图检测到可疑帧，Review: {review_count}")
            return (
                RiskLevel.Review,
                f"动图可疑({review_count}帧需复审): {reason_str}",
            )

    def _parse_frame_result(self, content: str) -> tuple[RiskLevel, str]:
        """
        解析单帧检测结果

        Args:
            content: LLM 返回的原始内容

        Returns:
            (风险等级, 风险原因)
        """
        content = content.strip()

        # 尝试解析结构化格式: "结果: xxx | 原因: xxx"
        if "结果:" in content:
            try:
                result_part = content.split("结果:")[1].split("|")[0].strip().lower()

                if "原因:" in content:
                    reason_part = content.split("原因:")[1].strip()
                else:
                    reason_part = "未提供原因"

                if "违规" in result_part:
                    return RiskLevel.Block, reason_part
                elif "正常" in result_part:
                    return RiskLevel.Pass, ""
                elif "review" in result_part or "复审" in result_part:
                    return RiskLevel.Review, reason_part
            except Exception as e:
                logger.debug(f"解析结构化结果失败: {e}, 内容: {content}")

        # 降级处理：使用简单的关键词匹配
        content_lower = content.lower()

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
        safe_keywords = ["正常", "safe", "appropriate", "clean", "无违规"]

        has_nsfw = any(kw in content_lower for kw in nsfw_keywords)
        has_safe = any(kw in content_lower for kw in safe_keywords)

        if has_nsfw and not has_safe:
            return RiskLevel.Block, content
        elif has_safe and not has_nsfw:
            return RiskLevel.Pass, ""
        elif has_nsfw and has_safe:
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
            logger.warning(f"无法解析帧检测结果，返回复审: {content}")
            return RiskLevel.Review, f"无法解析: {content[:100]}"
