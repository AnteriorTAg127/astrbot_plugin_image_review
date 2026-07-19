"""Dashboard Pages 后端 API for astrbot_plugin_image_review.

提供 6 类资源的 REST API（v1.5.0 WebUI，契约见 开发/v1.5.0/分工.md「M4 契约」）：
- 概览统计 (stats/overview)
- 违规记录 (violations，含证据图片 base64 服务)
- 审核日志 (audits)
- 人工白名单 (whitelist)
- 人工黑名单 (blacklist)
- 用户档案 (users)

所有路由通过 ``context.register_web_api`` 挂到 AstrBot Dashboard 上，
鉴权由 AstrBot 全局中间件统一处理，本模块不写鉴权逻辑。
响应约定：成功 ``json_response({...})`` 直出业务 JSON；
错误 ``error_response(msg, status_code=4xx)``；分页统一
``{items, total, page, page_size}``。
"""

from __future__ import annotations

import base64
import os
from io import BytesIO
from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .database import DatabaseManager, RiskLevel

if TYPE_CHECKING:
    from astrbot.api.star import Context

    from .handlers.config_manager import ConfigManager


PLUGIN_NAME = "astrbot_plugin_image_review"

# page_size 上限，防止前端意外打满数据库（DB 层亦有同样限制）
_MAX_PAGE_SIZE = 100

# 合法十六进制字符集（md5_hash 校验用）
_HEX_CHARS = frozenset("0123456789abcdefABCDEF")


def _current_user() -> str:
    """读取当前 Dashboard 登录用户名（写操作留痕日志用）。"""
    return request.username or "unknown"


async def _json_body() -> dict:
    """读取 JSON 请求体，非 dict 一律规整为 {}。"""
    payload = await request.json(default={})
    return payload if isinstance(payload, dict) else {}


def _parse_paging() -> tuple[int, int]:
    """从 query string 提取 page / page_size 并做边界限定。"""
    page = max(1, request.query.get("page", 1, type=int))
    page_size = max(
        1, min(_MAX_PAGE_SIZE, request.query.get("page_size", 20, type=int))
    )
    return page, page_size


def _parse_risk_level(raw: str | None) -> int | None:
    """risk_level query 参数解析：空/None → None，否则 int，非法值 → None。"""
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _is_valid_md5(value: str) -> bool:
    """校验 32 位十六进制 MD5 字符串。"""
    return len(value) == 32 and all(c in _HEX_CHARS for c in value)


def _id_or_400(raw: str) -> tuple[int | None, Any]:
    """路径 id 参数 -> int；失败时返回 (None, 错误响应)。"""
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, error_response("invalid id", status_code=400)


def _parse_ids_body(payload: dict) -> tuple[list[int] | None, Any]:
    """批量删除请求体 {ids: [...]} 的提取与容错转换。"""
    ids_raw = payload.get("ids")
    if not isinstance(ids_raw, list):
        return None, error_response("ids must be a list", status_code=400)
    ids: list[int] = []
    for item in ids_raw:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    if not ids:
        return None, error_response("no valid ids", status_code=400)
    return ids, None


def _parse_group_id_filter(raw: str | None) -> str | None:
    """名单列表 group_id query → group_id_filter。

    ""/"all" → None（全部），"global" → "global"（仅全局），其余 → 具体群号。
    """
    if raw in (None, "", "all"):
        return None
    if raw == "global":
        return "global"
    return raw


def _sanitize_violation(item: dict) -> dict:
    """违规记录安全处理：evidence_path 替换为布尔 has_evidence，不向前端泄露服务器路径。"""
    sanitized = dict(item)
    sanitized["has_evidence"] = bool(sanitized.pop("evidence_path", None))
    return sanitized


class WebApiHandler:
    """image_review Dashboard Page 的 REST API 处理器。"""

    def __init__(
        self,
        db: DatabaseManager,
        config_manager: ConfigManager,
        evidence_dir: str,
        context: Context | None = None,
    ) -> None:
        self._db = db
        self._config = config_manager
        self._evidence_dir = evidence_dir
        self._context: Context | None = context

    # ------------------------------------------------------------------ #
    # 注册
    # ------------------------------------------------------------------ #

    def register(self, context: Context, plugin_name: str = PLUGIN_NAME) -> None:
        """遍历路由表把所有路由挂到 AstrBot Dashboard 上。

        注意：``violations/batch_delete``、``audits/batch_delete`` 必须注册在
        ``<vid>``/``<aid>`` 动态路由之前，否则会被动态段优先匹配。
        """
        prefix = f"/{plugin_name}"
        self._context = context
        routes: list[tuple[str, Any, list[str], str]] = [
            # 概览与群列表
            (f"{prefix}/stats/overview", self.api_stats_overview, ["GET"], "审核概览"),
            (f"{prefix}/groups", self.api_groups_list, ["GET"], "群列表(含群名)"),
            # 违规记录
            (f"{prefix}/violations", self.api_violations_list, ["GET"], "违规列表"),
            (
                f"{prefix}/violations/batch_delete",
                self.api_violations_batch_delete,
                ["POST"],
                "批量删除违规",
            ),
            (
                f"{prefix}/violations/<vid>",
                self.api_violations_get,
                ["GET"],
                "违规详情",
            ),
            (
                f"{prefix}/violations/<vid>/update",
                self.api_violations_update,
                ["POST"],
                "编辑违规",
            ),
            (
                f"{prefix}/violations/<vid>/delete",
                self.api_violations_delete,
                ["POST"],
                "删除违规",
            ),
            (
                f"{prefix}/violations/<vid>/image",
                self.api_violations_image,
                ["GET"],
                "违规证据图片(base64)",
            ),
            # 审核日志
            (f"{prefix}/audits", self.api_audits_list, ["GET"], "审核日志列表"),
            (
                f"{prefix}/audits/batch_delete",
                self.api_audits_batch_delete,
                ["POST"],
                "批量删除审核日志",
            ),
            (f"{prefix}/audits/<aid>", self.api_audits_get, ["GET"], "审核日志详情"),
            (
                f"{prefix}/audits/<aid>/delete",
                self.api_audits_delete,
                ["POST"],
                "删除审核日志",
            ),
            # 人工白名单
            (f"{prefix}/whitelist", self.api_whitelist_list, ["GET"], "白名单列表"),
            (f"{prefix}/whitelist", self.api_whitelist_create, ["POST"], "添加白名单"),
            (
                f"{prefix}/whitelist/<wid>/update",
                self.api_whitelist_update,
                ["POST"],
                "更新白名单备注",
            ),
            (
                f"{prefix}/whitelist/<wid>/delete",
                self.api_whitelist_delete,
                ["POST"],
                "删除白名单",
            ),
            # 人工黑名单
            (f"{prefix}/blacklist", self.api_blacklist_list, ["GET"], "黑名单列表"),
            (f"{prefix}/blacklist", self.api_blacklist_create, ["POST"], "添加黑名单"),
            (
                f"{prefix}/blacklist/<bid>/update",
                self.api_blacklist_update,
                ["POST"],
                "更新黑名单备注",
            ),
            (
                f"{prefix}/blacklist/<bid>/delete",
                self.api_blacklist_delete,
                ["POST"],
                "删除黑名单",
            ),
            # 用户档案
            (f"{prefix}/users", self.api_users_list, ["GET"], "用户档案列表"),
            (f"{prefix}/users", self.api_users_create, ["POST"], "新增用户档案"),
            (f"{prefix}/users/<user_id>", self.api_users_get, ["GET"], "用户档案详情"),
            (
                f"{prefix}/users/<user_id>/update",
                self.api_users_update,
                ["POST"],
                "编辑用户档案",
            ),
            (
                f"{prefix}/users/<user_id>/delete",
                self.api_users_delete,
                ["POST"],
                "删除用户档案",
            ),
            # 账号白名单（v1.5.1）
            (
                f"{prefix}/account_whitelist",
                self.api_account_whitelist_list,
                ["GET"],
                "账号白名单列表",
            ),
            (
                f"{prefix}/account_whitelist",
                self.api_account_whitelist_create,
                ["POST"],
                "添加账号白名单",
            ),
            (
                f"{prefix}/account_whitelist/<wid>/update",
                self.api_account_whitelist_update,
                ["POST"],
                "更新账号白名单备注",
            ),
            (
                f"{prefix}/account_whitelist/<wid>/delete",
                self.api_account_whitelist_delete,
                ["POST"],
                "删除账号白名单",
            ),
            # 成本与定价（v1.5.4）
            (f"{prefix}/providers", self.api_providers_list, ["GET"], "LLM 提供商列表"),
            (f"{prefix}/pricing", self.api_pricing_list, ["GET"], "模型定价列表"),
            (
                f"{prefix}/pricing",
                self.api_pricing_upsert,
                ["POST"],
                "添加/更新模型定价",
            ),
            (
                f"{prefix}/pricing/<model_id>/delete",
                self.api_pricing_delete,
                ["POST"],
                "删除模型定价",
            ),
            (f"{prefix}/cost/overview", self.api_cost_overview, ["GET"], "成本概览"),
        ]
        ok_cnt = 0
        for route, handler, methods, desc in routes:
            try:
                context.register_web_api(route, handler, methods, desc)
                ok_cnt += 1
            except Exception:
                logger.exception(f"[web_api] register failed: {route}")
        logger.info(
            f"[web_api] {ok_cnt}/{len(routes)} routes registered under {prefix}"
        )

    # ------------------------------------------------------------------ #
    # 概览与群列表
    # ------------------------------------------------------------------ #

    async def api_stats_overview(self) -> Any:
        """GET /stats/overview（附加自动名单数量 auto_whitelist_count/auto_blacklist_count）"""
        try:
            data = await self._db.get_overview_stats()
            cache_counts = await self._db.get_cache_counts()
            data["auto_whitelist_count"] = cache_counts.get("whitelist", 0)
            data["auto_blacklist_count"] = cache_counts.get("blacklist", 0)
            # v1.5.4：成本统计数据
            cost_stats = await self._db.get_audit_cost_stats()
            data["audit_total_cost"] = round(cost_stats.get("total_cost", 0), 4)
            data["audit_cost_count"] = cost_stats.get("cost_count", 0)
            return json_response(data)
        except Exception:
            logger.exception("[web_api] api_stats_overview failed")
            return error_response("internal error", status_code=500)

    async def api_groups_list(self) -> Any:
        """GET /groups 返回 [{group_id, group_name}]"""
        try:
            configured = self._config.get_group_display_list()
            result = [
                {
                    "group_id": item.get("group_id", ""),
                    "group_name": item.get("group_name", ""),
                }
                for item in configured
            ]
            return json_response(result)
        except Exception:
            logger.exception("[web_api] api_groups_list failed")
            return error_response("internal error", status_code=500)

    # ------------------------------------------------------------------ #
    # 违规记录
    # ------------------------------------------------------------------ #

    async def api_violations_list(self) -> Any:
        """GET /violations?page=&page_size=&group_id=&user_id=&keyword=&risk_level=
        &sort_by=&sort_dir=&date_from=&date_to="""
        try:
            page, page_size = _parse_paging()
            items, total = await self._db.list_violations(
                page=page,
                page_size=page_size,
                group_id=request.query.get("group_id") or None,
                user_id=request.query.get("user_id") or None,
                keyword=request.query.get("keyword") or None,
                risk_level=_parse_risk_level(request.query.get("risk_level")),
                sort_by=request.query.get("sort_by") or None,
                sort_dir=request.query.get("sort_dir") or None,
                date_from=request.query.get("date_from") or None,
                date_to=request.query.get("date_to") or None,
            )
            await self._enrich_violation_hashes(items)
            return json_response(
                {
                    "items": [_sanitize_violation(item) for item in items],
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                }
            )
        except Exception:
            logger.exception("[web_api] api_violations_list failed")
            return error_response("internal error", status_code=500)

    async def api_violations_get(self, vid: str) -> Any:
        """GET /violations/<vid>（同样做 evidence_path → has_evidence 安全处理）"""
        try:
            vid_int, err = _id_or_400(vid)
            if err is not None:
                return err
            row = await self._db.get_violation(vid_int)
            if row is None:
                return error_response("not found", status_code=404)
            await self._enrich_violation_hashes([row])
            return json_response(_sanitize_violation(row))
        except Exception:
            logger.exception("[web_api] api_violations_get failed")
            return error_response("internal error", status_code=500)

    async def _enrich_violation_hashes(self, items: list[dict]) -> None:
        """为违规记录补全感知哈希（v1.5.2 展示用）。

        记录自带 phash/dhash 时直接用；缺失时（旧记录）按 md5 回退查 image_hashes。
        """
        missing = [
            it.get("md5_hash")
            for it in items
            if it and not it.get("phash") and not it.get("dhash") and it.get("md5_hash")
        ]
        fallback = await self._db.get_hashes_for_md5s(missing) if missing else {}
        for it in items:
            if not it:
                continue
            if it.get("phash") or it.get("dhash"):
                continue
            h = fallback.get(it.get("md5_hash"))
            if h:
                it["phash"] = h.get("phash")
                it["dhash"] = h.get("dhash")

    async def api_violations_update(self, vid: str) -> Any:
        """POST /violations/<vid>/update  body: {user_name?, note?}"""
        try:
            vid_int, err = _id_or_400(vid)
            if err is not None:
                return err
            payload = await _json_body()
            allowed = {"user_name", "note"}
            fields = {k: v for k, v in payload.items() if k in allowed}
            if not fields:
                return error_response("no editable fields provided", status_code=400)
            ok = await self._db.update_violation(vid_int, fields)
            if not ok:
                return error_response("not found or update failed", status_code=404)
            logger.info(
                f"[web_api] update_violation by {_current_user()}: "
                f"id={vid_int} fields={list(fields)}"
            )
            return json_response({})
        except Exception:
            logger.exception("[web_api] api_violations_update failed")
            return error_response("internal error", status_code=500)

    async def api_violations_delete(self, vid: str) -> Any:
        """POST /violations/<vid>/delete"""
        try:
            vid_int, err = _id_or_400(vid)
            if err is not None:
                return err
            ok = await self._db.delete_violation(vid_int)
            if not ok:
                return error_response("not found", status_code=404)
            logger.info(
                f"[web_api] delete_violation by {_current_user()}: id={vid_int}"
            )
            return json_response({})
        except Exception:
            logger.exception("[web_api] api_violations_delete failed")
            return error_response("internal error", status_code=500)

    async def api_violations_batch_delete(self) -> Any:
        """POST /violations/batch_delete  body: {ids: [int]}"""
        try:
            payload = await _json_body()
            ids, err = _parse_ids_body(payload)
            if err is not None:
                return err
            deleted = await self._db.delete_violations_batch(ids)
            logger.info(
                f"[web_api] batch_delete_violations by {_current_user()}: "
                f"req={len(ids)} deleted={deleted}"
            )
            return json_response({"deleted": deleted})
        except Exception:
            logger.exception("[web_api] api_violations_batch_delete failed")
            return error_response("internal error", status_code=500)

    async def api_violations_image(self, vid: str) -> Any:
        """GET /violations/<vid>/image 返回证据图片（Pillow 压缩至最长边 1024px，base64）。

        图片来源**仅为本地证据文件**（QQ 图片 URL 会过期，不作为来源）；
        记录无路径时按文件名模式兜底查找并写回（懒回填）。

        响应：无证据 ``{has_evidence: false}``；有证据
        ``{has_evidence: true, mime: "image/jpeg", base64: <base64>}``。

        注意：字段名用 ``base64`` 而非 ``data``——AstrBot Dashboard 父窗口
        会把响应体的顶层 ``data`` 字段当作包装层解包（``response.data.data``），
        若用 ``data`` 存 base64 串，bridge 会把裸串而非对象交给前端，导致渲染失败。
        """
        try:
            vid_int, err = _id_or_400(vid)
            if err is not None:
                return err
            path = await self._db.get_violation_evidence_path(vid_int)

            # 路径缺失或文件已不存在：按记录字段兜底查找本地证据（懒回填）
            if not path or not os.path.isfile(path):
                path = await self._resolve_evidence_fallback(vid_int)
                if not path:
                    return json_response({"has_evidence": False})

            # 路径安全校验：realpath 必须位于证据目录内，防路径遍历
            real_path = os.path.realpath(path)
            base_dir = os.path.realpath(self._evidence_dir) + os.sep
            if not real_path.startswith(base_dir):
                logger.warning(
                    f"[web_api] evidence path escapes evidence dir: vid={vid_int}"
                )
                return error_response("forbidden", status_code=403)
            if not os.path.isfile(real_path):
                return json_response({"has_evidence": False})

            try:
                from PIL import Image
            except ImportError:
                logger.exception("[web_api] Pillow not installed, cannot serve image")
                return error_response("pillow not installed", status_code=500)

            try:
                with open(real_path, "rb") as f:
                    raw_bytes = f.read()
                im = Image.open(BytesIO(raw_bytes))
                try:
                    im.seek(0)  # 动图取第一帧（静图无 seek，忽略异常）
                except (AttributeError, EOFError):
                    pass
                im = im.copy()
                if im.mode in ("RGBA", "LA", "PA"):
                    # 含透明通道：白底合成后转 RGB
                    alpha = im.getchannel("A")
                    background = Image.new("RGB", im.size, (255, 255, 255))
                    background.paste(im.convert("RGB"), mask=alpha)
                    im = background
                elif im.mode != "RGB":
                    im = im.convert("RGB")
                im.thumbnail((1024, 1024))
                buffer = BytesIO()
                im.save(buffer, format="JPEG", quality=85)
                img_b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
                return json_response(
                    {"has_evidence": True, "mime": "image/jpeg", "base64": img_b64}
                )
            except Exception:
                logger.exception(
                    f"[web_api] evidence image processing failed: vid={vid_int}"
                )
                return error_response("image processing failed", status_code=500)
        except Exception:
            logger.exception("[web_api] api_violations_image failed")
            return error_response("internal error", status_code=500)

    async def _resolve_evidence_fallback(self, vid: int) -> str | None:
        """evidence_path 缺失时按违规记录字段查找本地证据文件，并写回 DB（懒回填）

        QQ 图片 URL 会过期，本地证据文件是违规图片的唯一可靠来源；
        此兜底保证无路径记录的旧数据（或启动回填未命中的记录）
        在本地存在证据文件时仍可正常展示。

        查找规则与启动回填一致：优先 {group}_{user}_*_{md5前8位}.ext 精确命中，
        其次仅 md5 前缀命中。
        """
        record = await self._db.get_violation(vid)
        if not record:
            return None
        md5_hash = record.get("md5_hash") or ""
        if not md5_hash or not os.path.isdir(self._evidence_dir):
            return None
        prefix = md5_hash[:8].lower()
        exact_map, prefix_map = DatabaseManager._index_evidence_files(
            self._evidence_dir
        )
        path = exact_map.get(
            (
                str(record.get("group_id") or ""),
                str(record.get("user_id") or ""),
                prefix,
            )
        ) or prefix_map.get(prefix)
        if path and os.path.isfile(path):
            try:
                await self._db.set_violation_evidence_path(vid, path)
            except Exception:
                logger.exception(
                    f"[web_api] persist fallback evidence path failed: vid={vid}"
                )
            return path
        return None

    # ------------------------------------------------------------------ #
    # 审核日志
    # ------------------------------------------------------------------ #

    async def api_audits_list(self) -> Any:
        """GET /audits?page=&page_size=&group_id=&risk_level=&keyword=
        &sort_by=&sort_dir=&date_from=&date_to=（risk_level=-1 表示全部）"""
        try:
            page, page_size = _parse_paging()
            risk_level = _parse_risk_level(request.query.get("risk_level"))
            if risk_level == -1:
                risk_level = None
            items, total = await self._db.list_audits(
                page=page,
                page_size=page_size,
                group_id=request.query.get("group_id") or None,
                risk_level=risk_level,
                keyword=request.query.get("keyword") or None,
                sort_by=request.query.get("sort_by") or None,
                sort_dir=request.query.get("sort_dir") or None,
                date_from=request.query.get("date_from") or None,
                date_to=request.query.get("date_to") or None,
            )
            return json_response(
                {
                    "items": items,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                }
            )
        except Exception:
            logger.exception("[web_api] api_audits_list failed")
            return error_response("internal error", status_code=500)

    async def api_audits_get(self, aid: str) -> Any:
        """GET /audits/<aid>"""
        try:
            aid_int, err = _id_or_400(aid)
            if err is not None:
                return err
            row = await self._db.get_audit(aid_int)
            if row is None:
                return error_response("not found", status_code=404)
            return json_response(row)
        except Exception:
            logger.exception("[web_api] api_audits_get failed")
            return error_response("internal error", status_code=500)

    async def api_audits_delete(self, aid: str) -> Any:
        """POST /audits/<aid>/delete"""
        try:
            aid_int, err = _id_or_400(aid)
            if err is not None:
                return err
            ok = await self._db.delete_audit(aid_int)
            if not ok:
                return error_response("not found", status_code=404)
            logger.info(f"[web_api] delete_audit by {_current_user()}: id={aid_int}")
            return json_response({})
        except Exception:
            logger.exception("[web_api] api_audits_delete failed")
            return error_response("internal error", status_code=500)

    async def api_audits_batch_delete(self) -> Any:
        """POST /audits/batch_delete  body: {ids: [int]}"""
        try:
            payload = await _json_body()
            ids, err = _parse_ids_body(payload)
            if err is not None:
                return err
            deleted = await self._db.delete_audits_batch(ids)
            logger.info(
                f"[web_api] batch_delete_audits by {_current_user()}: "
                f"req={len(ids)} deleted={deleted}"
            )
            return json_response({"deleted": deleted})
        except Exception:
            logger.exception("[web_api] api_audits_batch_delete failed")
            return error_response("internal error", status_code=500)

    # ------------------------------------------------------------------ #
    # 人工白名单
    # ------------------------------------------------------------------ #

    async def api_whitelist_list(self) -> Any:
        """GET /whitelist?group_id=&sort_by=&sort_dir="""
        try:
            items = await self._db.list_manual_whitelist_detailed(
                group_id_filter=_parse_group_id_filter(request.query.get("group_id")),
                sort_by=request.query.get("sort_by") or None,
                sort_dir=request.query.get("sort_dir") or None,
            )
            return json_response({"items": items, "total": len(items)})
        except Exception:
            logger.exception("[web_api] api_whitelist_list failed")
            return error_response("internal error", status_code=500)

    async def api_whitelist_create(self) -> Any:
        """POST /whitelist  body: {md5_hash, group_id?, reason?}"""
        try:
            payload = await _json_body()
            md5_hash = str(payload.get("md5_hash") or "").strip().lower()
            if not _is_valid_md5(md5_hash):
                return error_response("invalid md5_hash", status_code=400)
            group_id = str(payload.get("group_id") or "")
            reason = str(payload.get("reason") or "")
            added_by = request.username or "webui"
            ok = await self._db.add_manual_whitelist(
                md5_hash=md5_hash,
                added_by=added_by,
                reason=reason,
                group_id=group_id,
            )
            if not ok:
                return error_response("entry already exists", status_code=409)
            logger.info(
                f"[web_api] add_manual_whitelist by {added_by}: "
                f"md5={md5_hash} group_id={group_id or 'global'}"
            )
            return json_response({})
        except Exception:
            logger.exception("[web_api] api_whitelist_create failed")
            return error_response("internal error", status_code=500)

    async def api_whitelist_update(self, wid: str) -> Any:
        """POST /whitelist/<wid>/update  body: {reason}"""
        try:
            wid_int, err = _id_or_400(wid)
            if err is not None:
                return err
            payload = await _json_body()
            if "reason" not in payload:
                return error_response("reason is required", status_code=400)
            reason = str(payload.get("reason") or "")
            ok = await self._db.update_manual_list_reason(
                "manual_whitelist", wid_int, reason
            )
            if not ok:
                return error_response("not found", status_code=404)
            logger.info(
                f"[web_api] update_manual_whitelist by {_current_user()}: id={wid_int}"
            )
            return json_response({})
        except Exception:
            logger.exception("[web_api] api_whitelist_update failed")
            return error_response("internal error", status_code=500)

    async def api_whitelist_delete(self, wid: str) -> Any:
        """POST /whitelist/<wid>/delete"""
        try:
            wid_int, err = _id_or_400(wid)
            if err is not None:
                return err
            ok = await self._db.delete_manual_list_by_id("manual_whitelist", wid_int)
            if not ok:
                return error_response("not found", status_code=404)
            logger.info(
                f"[web_api] delete_manual_whitelist by {_current_user()}: id={wid_int}"
            )
            return json_response({})
        except Exception:
            logger.exception("[web_api] api_whitelist_delete failed")
            return error_response("internal error", status_code=500)

    # ------------------------------------------------------------------ #
    # 人工黑名单
    # ------------------------------------------------------------------ #

    async def api_blacklist_list(self) -> Any:
        """GET /blacklist?group_id=&sort_by=&sort_dir="""
        try:
            items = await self._db.list_manual_blacklist_detailed(
                group_id_filter=_parse_group_id_filter(request.query.get("group_id")),
                sort_by=request.query.get("sort_by") or None,
                sort_dir=request.query.get("sort_dir") or None,
            )
            return json_response({"items": items, "total": len(items)})
        except Exception:
            logger.exception("[web_api] api_blacklist_list failed")
            return error_response("internal error", status_code=500)

    async def api_blacklist_create(self) -> Any:
        """POST /blacklist  body: {md5_hash, risk_level(1/2), risk_reason?, group_id?}"""
        try:
            payload = await _json_body()
            md5_hash = str(payload.get("md5_hash") or "").strip().lower()
            if not _is_valid_md5(md5_hash):
                return error_response("invalid md5_hash", status_code=400)
            try:
                risk_level = int(payload.get("risk_level"))
            except (TypeError, ValueError):
                risk_level = 0
            if risk_level not in (1, 2):
                return error_response("risk_level must be 1 or 2", status_code=400)
            risk_reason = str(payload.get("risk_reason") or "人工添加(WebUI)")
            group_id = str(payload.get("group_id") or "")
            added_by = request.username or "webui"
            ok = await self._db.add_manual_blacklist(
                md5_hash=md5_hash,
                risk_level=RiskLevel(risk_level),
                risk_reason=risk_reason,
                added_by=added_by,
                group_id=group_id,
            )
            if not ok:
                return error_response("entry already exists", status_code=409)
            logger.info(
                f"[web_api] add_manual_blacklist by {added_by}: md5={md5_hash} "
                f"risk_level={risk_level} group_id={group_id or 'global'}"
            )
            return json_response({})
        except Exception:
            logger.exception("[web_api] api_blacklist_create failed")
            return error_response("internal error", status_code=500)

    async def api_blacklist_update(self, bid: str) -> Any:
        """POST /blacklist/<bid>/update  body: {reason?}"""
        try:
            bid_int, err = _id_or_400(bid)
            if err is not None:
                return err
            payload = await _json_body()
            reason = str(payload.get("reason") or "")
            ok = await self._db.update_manual_list_reason(
                "manual_blacklist", bid_int, reason
            )
            if not ok:
                return error_response("not found", status_code=404)
            logger.info(
                f"[web_api] update_manual_blacklist by {_current_user()}: id={bid_int}"
            )
            return json_response({})
        except Exception:
            logger.exception("[web_api] api_blacklist_update failed")
            return error_response("internal error", status_code=500)

    async def api_blacklist_delete(self, bid: str) -> Any:
        """POST /blacklist/<bid>/delete"""
        try:
            bid_int, err = _id_or_400(bid)
            if err is not None:
                return err
            ok = await self._db.delete_manual_list_by_id("manual_blacklist", bid_int)
            if not ok:
                return error_response("not found", status_code=404)
            logger.info(
                f"[web_api] delete_manual_blacklist by {_current_user()}: id={bid_int}"
            )
            return json_response({})
        except Exception:
            logger.exception("[web_api] api_blacklist_delete failed")
            return error_response("internal error", status_code=500)

    # ------------------------------------------------------------------ #
    # 用户档案
    # ------------------------------------------------------------------ #

    async def api_users_list(self) -> Any:
        """GET /users?page=&page_size=&keyword=&status=&groups=&first_seen_from=
        &first_seen_to=&last_seen_from=&last_seen_to=&sort_by=&sort_dir="""
        try:
            page, page_size = _parse_paging()
            status = request.query.get("status")
            if status in ("", "all"):
                status = None
            groups_raw = request.query.get("groups") or None
            groups: list[str] | None = None
            if groups_raw:
                groups = [g.strip() for g in groups_raw.split(",") if g.strip()]
            items, total = await self._db.list_user_profiles(
                page=page,
                page_size=page_size,
                keyword=request.query.get("keyword") or None,
                status=status,
                sort_by=request.query.get("sort_by") or None,
                sort_dir=request.query.get("sort_dir") or None,
                first_seen_from=request.query.get("first_seen_from") or None,
                first_seen_to=request.query.get("first_seen_to") or None,
                last_seen_from=request.query.get("last_seen_from") or None,
                last_seen_to=request.query.get("last_seen_to") or None,
                groups=groups,
            )
            return json_response(
                {
                    "items": items,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                }
            )
        except Exception:
            logger.exception("[web_api] api_users_list failed")
            return error_response("internal error", status_code=500)

    async def api_users_create(self) -> Any:
        """POST /users  body: {user_id, nickname?, note?, status?, group_ids?: [str]}"""
        try:
            payload = await _json_body()
            user_id = str(payload.get("user_id") or "").strip()
            if not user_id:
                return error_response("user_id is required", status_code=400)
            group_ids = payload.get("group_ids") or []
            if not isinstance(group_ids, list):
                return error_response("group_ids must be a list", status_code=400)
            data = {
                "user_id": user_id,
                "nickname": str(payload.get("nickname") or ""),
                "note": str(payload.get("note") or ""),
                "status": str(payload.get("status") or "normal"),
                "group_ids": [str(g) for g in group_ids],
            }
            ok = await self._db.create_user_profile(data)
            if not ok:
                return error_response(
                    "user already exists or insert failed", status_code=409
                )
            logger.info(
                f"[web_api] create_user_profile by {_current_user()}: user_id={user_id}"
            )
            return json_response({})
        except Exception:
            logger.exception("[web_api] api_users_create failed")
            return error_response("internal error", status_code=500)

    async def api_users_get(self, user_id: str) -> Any:
        """GET /users/<user_id>"""
        try:
            user_id = str(user_id or "").strip()
            if not user_id:
                return error_response("invalid user_id", status_code=400)
            row = await self._db.get_user_profile(user_id)
            if row is None:
                return error_response("not found", status_code=404)
            return json_response(row)
        except Exception:
            logger.exception("[web_api] api_users_get failed")
            return error_response("internal error", status_code=500)

    async def api_users_update(self, user_id: str) -> Any:
        """POST /users/<user_id>/update  body: {nickname?, note?, status?, group_ids?}"""
        try:
            user_id = str(user_id or "").strip()
            if not user_id:
                return error_response("invalid user_id", status_code=400)
            payload = await _json_body()
            allowed = {"nickname", "note", "status", "group_ids"}
            fields = {k: v for k, v in payload.items() if k in allowed}
            if not fields:
                return error_response("no editable fields provided", status_code=400)
            if "group_ids" in fields:
                if not isinstance(fields["group_ids"], list):
                    return error_response("group_ids must be a list", status_code=400)
                fields["group_ids"] = [str(g) for g in fields["group_ids"]]
            ok = await self._db.update_user_profile(user_id, fields)
            if not ok:
                return error_response("not found or update failed", status_code=404)
            logger.info(
                f"[web_api] update_user_profile by {_current_user()}: "
                f"user_id={user_id} fields={list(fields)}"
            )
            return json_response({})
        except Exception:
            logger.exception("[web_api] api_users_update failed")
            return error_response("internal error", status_code=500)

    async def api_users_delete(self, user_id: str) -> Any:
        """POST /users/<user_id>/delete"""
        try:
            user_id = str(user_id or "").strip()
            if not user_id:
                return error_response("invalid user_id", status_code=400)
            ok = await self._db.delete_user_profile(user_id)
            if not ok:
                return error_response("not found", status_code=404)
            logger.info(
                f"[web_api] delete_user_profile by {_current_user()}: user_id={user_id}"
            )
            return json_response({})
        except Exception:
            logger.exception("[web_api] api_users_delete failed")
            return error_response("internal error", status_code=500)

    # ------------------------------------------------------------------ #
    # 账号白名单（v1.5.1：QQ 账号维度，支持全局与群级）
    # ------------------------------------------------------------------ #

    async def api_account_whitelist_list(self) -> Any:
        """GET /account_whitelist?group_id=&sort_by=&sort_dir=

        group_id: all/缺省=全部, global=仅全局, 具体群号=该群
        """
        try:
            items = await self._db.list_account_whitelist_detailed(
                group_id_filter=_parse_group_id_filter(request.query.get("group_id")),
                sort_by=request.query.get("sort_by") or None,
                sort_dir=request.query.get("sort_dir") or None,
            )
            return json_response({"items": items, "total": len(items)})
        except Exception:
            logger.exception("[web_api] api_account_whitelist_list failed")
            return error_response("internal error", status_code=500)

    async def api_account_whitelist_create(self) -> Any:
        """POST /account_whitelist  body: {user_id, group_id?, note?}"""
        try:
            payload = await _json_body()
            user_id = str(payload.get("user_id") or "").strip()
            if not user_id:
                return error_response("user_id is required", status_code=400)
            group_id = str(payload.get("group_id") or "")
            note = str(payload.get("note") or "")
            added_by = request.username or "webui"
            ok = await self._db.add_account_whitelist(
                user_id=user_id,
                group_id=group_id,
                added_by=added_by,
                note=note or None,
            )
            if not ok:
                return error_response("entry already exists", status_code=409)
            logger.info(
                f"[web_api] add_account_whitelist by {added_by}: "
                f"user_id={user_id} group_id={group_id or 'global'}"
            )
            return json_response({})
        except Exception:
            logger.exception("[web_api] api_account_whitelist_create failed")
            return error_response("internal error", status_code=500)

    async def api_account_whitelist_update(self, wid: str) -> Any:
        """POST /account_whitelist/<wid>/update  body: {note}"""
        try:
            wid_int, err = _id_or_400(wid)
            if err is not None:
                return err
            payload = await _json_body()
            if "note" not in payload:
                return error_response("note is required", status_code=400)
            note = str(payload.get("note") or "")
            ok = await self._db.update_account_whitelist_note(wid_int, note)
            if not ok:
                return error_response("not found", status_code=404)
            logger.info(
                f"[web_api] update_account_whitelist by {_current_user()}: id={wid_int}"
            )
            return json_response({})
        except Exception:
            logger.exception("[web_api] api_account_whitelist_update failed")
            return error_response("internal error", status_code=500)

    async def api_account_whitelist_delete(self, wid: str) -> Any:
        """POST /account_whitelist/<wid>/delete"""
        try:
            wid_int, err = _id_or_400(wid)
            if err is not None:
                return err
            ok = await self._db.delete_account_whitelist_by_id(wid_int)
            if not ok:
                return error_response("not found", status_code=404)
            logger.info(
                f"[web_api] delete_account_whitelist by {_current_user()}: id={wid_int}"
            )
            return json_response({})
        except Exception:
            logger.exception("[web_api] api_account_whitelist_delete failed")
            return error_response("internal error", status_code=500)

    # ------------------------------------------------------------------ #
    # 成本与定价（v1.5.4）
    # ------------------------------------------------------------------ #

    async def api_providers_list(self) -> Any:
        """GET /providers 返回 [{id, type}]（LLM 提供商清单，供配置页下拉）"""
        try:
            result: list[dict] = []
            if self._context is not None:
                cfg = getattr(
                    getattr(self._context, "provider_manager", None),
                    "providers_config",
                    [],
                )
                for p in cfg:
                    pid = p.get("id")
                    if pid and p.get("enable", True):
                        result.append({"id": str(pid), "type": str(p.get("type", ""))})
            return json_response(result)
        except Exception:
            logger.exception("[web_api] api_providers_list failed")
            return error_response("internal error", status_code=500)

    async def api_pricing_list(self) -> Any:
        """GET /pricing"""
        try:
            items = await self._db.list_model_pricing()
            return json_response({"items": items, "total": len(items)})
        except Exception:
            logger.exception("[web_api] api_pricing_list failed")
            return error_response("internal error", status_code=500)

    async def api_pricing_upsert(self) -> Any:
        """POST /pricing  body: {model_id, currency?, price_per?, input_price?,
        cached_price?, output_price?, label?}  (model_id 必填)"""
        try:
            payload = await _json_body()
            model_id = str(payload.get("model_id", "")).strip()
            if not model_id:
                return error_response("model_id is required", status_code=400)

            def _pos_float(v, default):
                return max(0.0, float(v if v is not None else default))

            def _pos_int(v, default):
                return max(1, int(v if v is not None else default))

            data = {
                "currency": str(payload.get("currency", "CNY")) or "CNY",
                "price_per": _pos_int(payload.get("price_per"), 1000000),
                "input_price": _pos_float(payload.get("input_price"), 0.0),
                "cached_price": _pos_float(payload.get("cached_price"), 0.0),
                "output_price": _pos_float(payload.get("output_price"), 0.0),
                "label": str(payload.get("label", "")),
            }
            ok = await self._db.upsert_model_pricing(model_id, data)
            if not ok:
                return error_response("upsert failed", status_code=500)
            logger.info(
                f"[web_api] upsert_model_pricing by {_current_user()}: model_id={model_id}"
            )
            return json_response({})
        except Exception:
            logger.exception("[web_api] api_pricing_upsert failed")
            return error_response("internal error", status_code=500)

    async def api_pricing_delete(self, model_id: str) -> Any:
        """POST /pricing/<model_id>/delete"""
        try:
            model_id = str(model_id or "").strip()
            if not model_id:
                return error_response("invalid model_id", status_code=400)
            ok = await self._db.delete_model_pricing(model_id)
            if not ok:
                return error_response("not found", status_code=404)
            logger.info(
                f"[web_api] delete_model_pricing by {_current_user()}: model_id={model_id}"
            )
            return json_response({})
        except Exception:
            logger.exception("[web_api] api_pricing_delete failed")
            return error_response("internal error", status_code=500)

    async def api_cost_overview(self) -> Any:
        """GET /cost/overview"""
        try:
            data = await self._db.get_cost_overview()
            return json_response(data)
        except Exception:
            logger.exception("[web_api] api_cost_overview failed")
            return error_response("internal error", status_code=500)
