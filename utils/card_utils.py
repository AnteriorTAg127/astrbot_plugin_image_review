"""
卡片消息解析工具（纯函数模块）

提供 QQ 内置卡片（json 段）解析所需的 4 个纯函数：
- is_music_video_card      : 按 app 字段识别音乐/视频卡片
- extract_json_card_images : 递归提取 json 卡片内嵌图片 URL（去重）
- extract_jump_url         : 提取 json 卡片跳转链接（jumpUrl/url 优先）
- extract_og_image         : 从跳转页 HTML 提取 og:image

本模块不依赖 AstrBot 框架及插件内任何模块，仅使用标准库，可离线单测。
"""

import re
from urllib.parse import urlsplit

__all__ = [
    "is_music_video_card",
    "extract_json_card_images",
    "extract_jump_url",
    "extract_og_image",
]

# ---------------------------------------------------------------- 常量

# 音乐卡片 app 字段标识（不区分大小写，子串匹配）
_MUSIC_APP_MARKERS = ("music", "qqmusic", "y.qq.com")
# 视频卡片 app 字段标识（不区分大小写，子串匹配）
_VIDEO_APP_MARKERS = ("video", "qvideo", "ovideo", "tencentvideo", "v.qq.com")

# 常见图片扩展名（不区分大小写）——URL 路径以此结尾即视为图片
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

# 明显不是图片的扩展名——反向过滤，即使 key 名暗示图片也跳过
_NON_IMAGE_EXTENSIONS = (
    ".js",
    ".css",
    ".html",
    ".htm",
    ".json",
    ".xml",
    ".txt",
    ".csv",
    ".pdf",
    ".zip",
    ".mp4",
    ".mp3",
    ".webm",
    ".m4a",
    ".flv",
    ".avi",
    ".mov",
    ".m3u8",
    ".ts",
    ".php",
)

# 暗示图片语义的 dict key 子串（key 小写后匹配）
_IMAGE_KEY_MARKERS = ("preview", "image", "img", "thumbnail", "cover", "picture", "pic")

# og:image meta 标签正则：property/content 属性顺序任意、引号单双均可、可夹带其他属性
_OG_IMAGE_RE = re.compile(
    # 顺序一：property 在前，content 在后
    r'<meta\b[^>]*?\sproperty=(["\'])og:image\1[^>]*?\scontent=(["\'])([^"\']*)\2[^>]*>'
    # 顺序二：content 在前，property 在后
    r'|<meta\b[^>]*?\scontent=(["\'])([^"\']*)\4[^>]*?\sproperty=(["\'])og:image\6[^>]*>',
    re.IGNORECASE,
)

# og:image content 值需反转义的常见 HTML 实体（单遍替换，防止 &amp; 被二次反转义）
_HTML_ENTITIES = {
    "&#39;": "'",
    "&quot;": '"',
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
}
_HTML_ENTITY_RE = re.compile("|".join(map(re.escape, _HTML_ENTITIES)))


# ---------------------------------------------------------------- 内部工具


def _unescape_html_entities(text: str) -> str:
    """反转义 HTML 文本中的常见实体（&#39; &quot; &amp; &lt; &gt;）。"""
    return _HTML_ENTITY_RE.sub(lambda m: _HTML_ENTITIES[m.group(0)], text)


def _is_image_url(url: str, key: str) -> bool:
    """
    按 URL 路径扩展名与所在 dict 的 key 语义判断是否为图片 URL。

    判定规则：
    1. 路径（去掉查询/锚点参数）以图片扩展名结尾 → 图片；
    2. 否则路径以明显非图片扩展名结尾 → 反向过滤，判定非图片；
    3. 否则 key 小写后含图片语义（preview/image/img/thumbnail/cover/picture/pic）→ 图片。

    Args:
        url: 候选 URL（已确认以 http(s) 开头）
        key: 该 URL 所在 dict 的 key 名

    Returns:
        是否为图片 URL
    """
    path = urlsplit(url).path.lower()
    if path.endswith(_IMAGE_EXTENSIONS):
        return True
    if path.endswith(_NON_IMAGE_EXTENSIONS):
        return False
    key_lower = key.lower()
    return any(marker in key_lower for marker in _IMAGE_KEY_MARKERS)


def _walk(node: object, results: list[str], seen: set[str]) -> None:
    """
    深度优先遍历 dict/list 混合结构，收集图片 URL。

    dict 中的字符串值按其所在 key 名判断（key 非字符串时视为无 key 语义）；
    list 元素没有所在 dict，仅按扩展名判断。

    Args:
        node: 当前遍历节点
        results: 收集结果（就地追加，保持首次出现顺序）
        seen: 已收集 URL 集合（去重）
    """
    if isinstance(node, dict):
        for k, v in node.items():
            key = k if isinstance(k, str) else ""
            if isinstance(v, str):
                url = v.strip()
                if url.startswith(("http://", "https://")) and _is_image_url(url, key):
                    if url not in seen:
                        seen.add(url)
                        results.append(url)
            else:
                _walk(v, results, seen)
    elif isinstance(node, list):
        for item in node:
            _walk(item, results, seen)
    elif isinstance(node, str):
        # 裸字符串（顶层或 list 元素）：无所在 dict，仅按扩展名判断
        url = node.strip()
        if url.startswith(("http://", "https://")) and _is_image_url(url, ""):
            if url not in seen:
                seen.add(url)
                results.append(url)


def _dfs_find_jump_key(node: object, target_key: str) -> str | None:
    """
    深度优先查找 key（不区分大小写）等于 target_key 的 http(s) 字符串值。

    Args:
        node: 当前遍历节点
        target_key: 目标 key（小写形式，如 "jumpurl"/"url"）

    Returns:
        命中的第一个 http(s) 字符串值；无则返回 None
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and k.lower() == target_key and isinstance(v, str):
                url = v.strip()
                if url.startswith(("http://", "https://")):
                    return url
            found = _dfs_find_jump_key(v, target_key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _dfs_find_jump_key(item, target_key)
            if found is not None:
                return found
    return None


# ---------------------------------------------------------------- 对外接口


def is_music_video_card(data: dict) -> bool:
    """
    判断 json 卡片 data 是否命中音乐/视频卡片标识。

    app 字段为字符串（不区分大小写），包含任一标识即命中：
    - 音乐类：music / qqmusic / y.qq.com
    - 视频类：video / qvideo / ovideo / tencentvideo / v.qq.com

    Args:
        data: json 卡片解析出的 data 字典

    Returns:
        命中返回 True；data 非 dict、app 缺失或非字符串均返回 False
    """
    if not isinstance(data, dict):
        return False
    app = data.get("app")
    if not isinstance(app, str):
        return False
    app_lower = app.lower()
    return any(
        marker in app_lower for marker in (*_MUSIC_APP_MARKERS, *_VIDEO_APP_MARKERS)
    )


def extract_json_card_images(data: dict | list | None) -> list[str]:
    """
    深度递归提取 data 中所有 http(s) 图片 URL，去重并保持首次出现顺序。

    判定为图片 URL（满足其一）：
    - URL 路径（去掉查询/锚点参数）以常见图片扩展名结尾（.jpg/.jpeg/.png/.gif/.webp/.bmp，不区分大小写）
    - URL 所在 dict 的 key 名（小写后）含图片语义（preview/image/img/thumbnail/cover/picture/pic），
      且路径不以明显非图片扩展名（.js/.css/.html 等）结尾

    Args:
        data: json 卡片 data（dict/list 混合嵌套结构；其他类型返回空列表）

    Returns:
        去重后的图片 URL 列表（保持首次出现顺序）
    """
    results: list[str] = []
    seen: set[str] = set()
    _walk(data, results, seen)
    return results


def extract_jump_url(data: dict | list | None) -> str | None:
    """
    从 json 卡片 data 中提取第一个 http(s) 跳转链接。

    优先查找 key 为 "jumpUrl" 的字符串值（深度优先，命中即返回）；
    未找到再查找 key 为 "url" 的字符串值；均未找到返回 None。

    Args:
        data: json 卡片 data（dict/list 混合嵌套结构）

    Returns:
        第一个 http(s) 跳转 URL；无则返回 None
    """
    found = _dfs_find_jump_key(data, "jumpurl")
    if found is not None:
        return found
    return _dfs_find_jump_key(data, "url")


def extract_og_image(html: str) -> str | None:
    """
    从 HTML 源码中提取 og:image 元标签的 content 值。

    仅匹配 <meta property="og:image" content="...">（property 属性可用单引号；
    property 与 content 属性顺序任意）。content 值做常见 HTML 实体反转义
    （&#39; &quot; &amp; &lt; &gt;）。

    Args:
        html: 跳转页 HTML 源码（非字符串返回 None）

    Returns:
        og:image 的 content 值（已反转义）；无匹配或 content 为空时返回 None
    """
    if not isinstance(html, str):
        return None
    match = _OG_IMAGE_RE.search(html)
    if not match:
        return None
    # 顺序一命中取 group(3)，顺序二命中取 group(5)
    value = match.group(3) if match.group(3) is not None else match.group(5)
    value = _unescape_html_entities(value.strip())
    return value if value else None
