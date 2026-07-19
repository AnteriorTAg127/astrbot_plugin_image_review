"""
图片处理工具模块
包含图片相关的通用工具函数
"""

import math
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
    def extract_md5_from_filename(file_value: str | None) -> str | None:
        """
        从 OneBot image 段的 file 字段（或任意文件名/路径）提取 QQ MD5

        file 字段形如 "8245CA6AFB584153BAB6638BC05FC9D9.jpg"。
        注意：v4.26.x PreProcessStage 物化后 comp.file 变为
        "media_image_<uuid>.<ext>"，此时无法提取 QQ MD5，返回 None，
        由调用方用下载到的字节计算 MD5 兜底。

        Args:
            file_value: file 字段值（文件名、路径或 URL）

        Returns:
            小写的 MD5 字符串，无法提取则返回 None
        """
        if not file_value:
            return None
        try:
            # 移除可能的URL参数
            name = file_value.split("?")[0]
            # 移除路径，只保留文件名
            name = os.path.basename(name)
            # 移除扩展名，获取MD5
            md5_hex = os.path.splitext(name)[0]
            # 验证MD5格式（32位十六进制字符串）
            if ImageUtils.is_valid_md5(md5_hex):
                return md5_hex.lower()
        except Exception as e:
            logger.debug(f"提取图片MD5时发生异常: {e}")
        return None

    @staticmethod
    def extract_image_md5(
        event: AstrMessageEvent, image_comp: Comp.Image
    ) -> str | None:
        """
        从图片组件的 file 字段提取图片的MD5值

        从图片文件名中提取MD5，文件名格式通常为: 306AED23E3B7AA81B51A3B2A6FAAAF73.jpg
        注意：v4.26.x 物化后 comp.file 为本地临时路径，会返回 None。

        Args:
            event: 消息事件
            image_comp: 图片组件

        Returns:
            图片MD5字符串，如果无法获取则返回None
        """
        return ImageUtils.extract_md5_from_filename(image_comp.file)

    @staticmethod
    def _dct_1d(vector: list[float]) -> list[float]:
        """一维离散余弦变换（Type-II DCT）"""
        n = len(vector)
        result = [0.0] * n
        sqrt_n = math.sqrt(n)
        sqrt_2n = math.sqrt(2.0 / n)
        for u in range(n):
            s = 0.0
            for x in range(n):
                s += vector[x] * math.cos((2 * x + 1) * u * math.pi / (2 * n))
            c = 1.0 / sqrt_n if u == 0 else sqrt_2n
            result[u] = c * s
        return result

    @staticmethod
    def _dct_2d(block: list[list[float]]) -> list[list[float]]:
        """
        使用行列分离法计算二维离散余弦变换（Type-II DCT）

        先对每行做 1D DCT，再对每列做 1D DCT，复杂度 O(2n³)
        """
        n = len(block)
        # 对每行做 1D DCT
        rows = [ImageUtils._dct_1d(row) for row in block]
        # 对每列做 1D DCT
        result = [[0.0] * n for _ in range(n)]
        for col in range(n):
            column = [rows[row][col] for row in range(n)]
            transformed = ImageUtils._dct_1d(column)
            for row in range(n):
                result[row][col] = transformed[row]
        return result

    @staticmethod
    def calculate_phash(image_data: bytes, hash_size: int = 24) -> str | None:
        """
        计算图片的感知哈希值（pHash）

        使用 DCT（离散余弦变换）实现，对图片缩放、旋转、亮度变化等具有较好的鲁棒性。
        缩放到 48x48 后做 DCT，保留左上角 24x24 低频系数，生成 576 位（144 位十六进制）哈希值。
        与 dHash 位数一致，可以使用相同的汉明距离阈值。

        Args:
            image_data: 图片字节数据
            hash_size: 为保持接口兼容保留的参数，实际使用固定 24x24

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

            # 缩放到 48x48 像素（保留更多细节给 DCT）
            dct_size = 48
            img = img.resize((dct_size, dct_size), Image.Resampling.LANCZOS)

            # 使用 img.load() 快速访问像素
            pixels_buffer = img.load()
            # 构建 2D 像素数组
            pixels_2d: list[list[float]] = []
            for y in range(dct_size):
                row: list[float] = []
                for x in range(dct_size):
                    row.append(float(pixels_buffer[x, y]))
                pixels_2d.append(row)

            # 应用 2D DCT 得到系数矩阵
            dct_result = ImageUtils._dct_2d(pixels_2d)

            # 保留左上角 24x24 低频系数（共 576 位）
            keep_size = 24
            low_freq: list[float] = []
            for u in range(keep_size):
                for v in range(keep_size):
                    low_freq.append(dct_result[u][v])

            # 计算 576 个系数的中位数
            sorted_coeffs = sorted(low_freq)
            median = sorted_coeffs[len(sorted_coeffs) // 2]

            # 生成 576 位哈希值：每个系数大于中位数则为 1，否则为 0
            bits = 0
            for coeff in low_freq:
                bits = (bits << 1) | (1 if coeff > median else 0)

            # 格式化为 144 位十六进制字符串（576 位 / 4 = 144 字符）
            hex_length = keep_size * keep_size // 4
            return format(bits, f"0{hex_length}x")

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
