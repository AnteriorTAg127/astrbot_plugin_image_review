"""
图片处理工具模块
包含图片相关的通用工具函数
"""

import os
import re
from io import BytesIO

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

# 尝试导入 PIL，如果不可用则提供降级方案
try:
    from PIL import Image

    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    logger.warning("PIL/Pillow 未安装，相似图片匹配功能将不可用")


class ImageUtils:
    """图片处理工具类"""

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """
        清理文件名，防止路径遍历攻击

        Args:
            filename: 原始文件名或路径片段

        Returns:
            清理后的安全文件名
        """
        if not filename:
            return "unknown"

        # 移除路径分隔符和特殊字符
        # 替换 Windows 和 Unix 的路径分隔符
        sanitized = filename.replace("\\", "_").replace("/", "_")

        # 移除 .. 防止路径遍历
        sanitized = sanitized.replace("..", "_")

        # 移除其他危险字符
        sanitized = re.sub(r'[<>:"|?*]', "_", sanitized)

        # 限制长度
        if len(sanitized) > 100:
            sanitized = sanitized[:100]

        return sanitized or "unknown"

    @staticmethod
    def is_valid_md5(md5_hex: str) -> bool:
        """
        验证字符串是否为有效的MD5格式

        Args:
            md5_hex: 待验证的字符串

        Returns:
            是否为有效的32位十六进制MD5字符串
        """
        if not md5_hex or len(md5_hex) != 32:
            return False
        try:
            int(md5_hex, 16)
            return True
        except ValueError:
            return False

    @staticmethod
    def is_qq_builtin_emoji(image_url: str) -> bool:
        """
        检查图片URL是否为QQ官方自带表情包

        QQ官方表情包通常包含以下特征域名：
        - gxh.vip.qq.com
        - p.qpic.cn (QQ表情CDN)
        - imgcache.qq.com

        Args:
            image_url: 图片URL

        Returns:
            是否为QQ官方表情包
        """
        if not image_url:
            return False

        # QQ官方表情包特征域名列表
        qq_emoji_domains = [
            "gxh.vip.qq.com",
            "p.qpic.cn",
            "imgcache.qq.com",
            "qpic.cn",
        ]

        image_url_lower = image_url.lower()
        for domain in qq_emoji_domains:
            if domain in image_url_lower:
                return True

        return False

    @staticmethod
    def extract_image_md5(
        event: AstrMessageEvent, image_comp: Comp.Image
    ) -> str | None:
        """
        从图片组件中提取图片的MD5值

        从图片文件名中提取MD5，文件名格式通常为: 306AED23E3B7AA81B51A3B2A6FAAAF73.jpg

        Args:
            event: 消息事件
            image_comp: 图片组件

        Returns:
            图片MD5字符串，如果无法获取则返回None
        """
        try:
            if image_comp.file:
                # 从文件名中提取MD5（去掉扩展名）
                file_name = image_comp.file
                # 移除可能的URL参数
                if "?" in file_name:
                    file_name = file_name.split("?")[0]
                # 移除路径，只保留文件名
                file_name = os.path.basename(file_name)
                # 移除扩展名，获取MD5
                md5_hex = os.path.splitext(file_name)[0]
                # 验证MD5格式（32位十六进制字符串）
                if ImageUtils.is_valid_md5(md5_hex):
                    return md5_hex.lower()
        except Exception as e:
            logger.debug(f"提取图片MD5时发生异常: {e}")
        return None

    @staticmethod
    def calculate_phash(image_data: bytes, hash_size: int = 24) -> str | None:
        """
        计算图片的感知哈希值（pHash）

        感知哈希对图片缩放、旋转、亮度变化等具有较好的鲁棒性

        Args:
            image_data: 图片字节数据
            hash_size: 哈希大小，默认24（生成576位哈希）

        Returns:
            十六进制哈希字符串，如果计算失败则返回None
        """
        if not HAS_PIL:
            return None

        try:
            # 加载图片
            img = Image.open(BytesIO(image_data))

            # 转换为灰度图
            if img.mode != "L":
                img = img.convert("L")

            # 缩放图片到 (hash_size + 1) x hash_size
            # 使用ANTIALIAS滤波器
            img = img.resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)

            # 获取像素值
            pixels = list(img.getdata())

            # 计算差异值（水平方向相邻像素的差值）
            diff = []
            for row in range(hash_size):
                for col in range(hash_size):
                    left_pixel = pixels[row * (hash_size + 1) + col]
                    right_pixel = pixels[row * (hash_size + 1) + col + 1]
                    diff.append(left_pixel > right_pixel)

            # 将差异值转换为十六进制字符串
            decimal_value = 0
            for bit in diff:
                decimal_value = (decimal_value << 1) | int(bit)

            # 格式化为十六进制字符串
            hex_length = hash_size * hash_size // 4
            return format(decimal_value, f"0{hex_length}x")

        except Exception as e:
            logger.debug(f"计算pHash时发生异常: {e}")
            return None

    @staticmethod
    def calculate_dhash(image_data: bytes, hash_size: int = 24) -> str | None:
        """
        计算图片的差异哈希值（dHash）

        差异哈希对图片平移、缩放等变化敏感，计算速度快

        Args:
            image_data: 图片字节数据
            hash_size: 哈希大小，默认24（生成576位哈希）

        Returns:
            十六进制哈希字符串，如果计算失败则返回None
        """
        if not HAS_PIL:
            return None

        try:
            # 加载图片
            img = Image.open(BytesIO(image_data))

            # 转换为灰度图
            if img.mode != "L":
                img = img.convert("L")

            # 缩放图片到 (hash_size + 1) x hash_size
            img = img.resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)

            # 获取像素值
            pixels = list(img.getdata())

            # 计算差异值（水平方向相邻像素的差值）
            diff = []
            for row in range(hash_size):
                for col in range(hash_size):
                    left_pixel = pixels[row * (hash_size + 1) + col]
                    right_pixel = pixels[row * (hash_size + 1) + col + 1]
                    diff.append(left_pixel > right_pixel)

            # 将差异值转换为十六进制字符串
            decimal_value = 0
            for bit in diff:
                decimal_value = (decimal_value << 1) | int(bit)

            # 格式化为十六进制字符串
            hex_length = hash_size * hash_size // 4
            return format(decimal_value, f"0{hex_length}x")

        except Exception as e:
            logger.debug(f"计算dHash时发生异常: {e}")
            return None

    @staticmethod
    def calculate_image_hashes(
        image_data: bytes, hash_size: int = 24
    ) -> tuple[str | None, str | None]:
        """
        同时计算图片的pHash和dHash

        Args:
            image_data: 图片字节数据
            hash_size: 哈希大小，默认24

        Returns:
            (phash, dhash) 元组，如果计算失败则对应值为None
        """
        phash = ImageUtils.calculate_phash(image_data, hash_size)
        dhash = ImageUtils.calculate_dhash(image_data, hash_size)
        return phash, dhash
