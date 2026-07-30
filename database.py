"""
数据库管理模块
用于管理黑白名单、违规记录等数据的持久化存储
"""

import hashlib
import os
from datetime import date, datetime, timedelta, timezone
from enum import Enum

import aiosqlite

# ========== 时区策略（v1.6.x） ==========
# 库内一律存 naive UTC；展示给前端时统一 +8 小时还原成本地。
# 写入用 _utc_iso()；按「本地自然日」过滤/分组见各 SQL 的 date(<col>, _LOCAL_DAY)。
_LOCAL_DAY = "+8 hours"  # 存量数据产生于 UTC+8 环境；展示与本地日换算偏移


def _utc_now() -> datetime:
    """当前 UTC 时间（naive，便于 SQLite 字符串存储与比较）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc_iso() -> str:
    """当前 UTC 时间的 naive 字符串（秒级、空格分隔，与 SQLite CURRENT_TIMESTAMP 同格式）。

    统一格式可保证同列内字符串排序正确，且与存量迁移输出一致。
    """
    return _utc_now().strftime("%Y-%m-%d %H:%M:%S")


# ========== WebUI 查询安全辅助（v1.5.0） ==========

# 排序字段白名单 —— 防 SQL 注入，sort_by 必须在此映射中才生效
_VIOLATION_SORT_FIELDS: dict[str, str] = {
    "id": "id",
    "violation_time": "violation_time",
    "group_id": "group_id",
    "user_id": "user_id",
    "risk_level": "risk_level",
}
_AUDIT_SORT_FIELDS: dict[str, str] = {
    "id": "id",
    "created_at": "created_at",
    "group_id": "group_id",
    "user_id": "user_id",
    "risk_level": "risk_level",
}
_LIST_SORT_FIELDS: dict[str, str] = {
    "id": "id",
    "md5_hash": "md5_hash",
    "created_at": "created_at",
    "group_id": "group_id",
}
_USER_PROFILE_SORT_FIELDS: dict[str, str] = {
    "user_id": "user_id",
    "nickname": "nickname",
    "status": "status",
    "violation_count": "violation_count",
    "first_seen_at": "first_seen_at",
    "last_seen_at": "last_seen_at",
}


def _build_order_clause(
    sort_by: str | None,
    sort_dir: str | None,
    allowlist: dict[str, str],
    default_clause: str,
    tiebreaker: str,
) -> str:
    """构造安全的 ORDER BY 子句。

    sort_by 必须在 allowlist 中，否则使用 default_clause。
    无论显式排序还是默认排序，均追加固定次级键（tiebreaker ASC）保证分页稳定。
    """
    direction = "ASC" if (sort_dir or "").lower() == "asc" else "DESC"
    col = allowlist.get(sort_by or "")
    if col is None:
        return f"{default_clause}, {tiebreaker} ASC"
    if col == tiebreaker:
        return f"ORDER BY {col} {direction}"
    return f"ORDER BY {col} {direction}, {tiebreaker} ASC"


def _escape_like(keyword: str) -> str:
    """转义 LIKE 模式中的 ``\\``、``%``、``_``，配合 ``ESCAPE '\\'`` 使用。"""
    return keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class RiskLevel(Enum):
    """风险等级枚举"""

    Pass = 0
    Review = 1
    Block = 2


class DatabaseManager:
    """数据库管理器"""

    HASH_VERSION = 2

    def __init__(self, data_dir: str):
        """
        初始化数据库管理器

        Args:
            data_dir: 数据存储目录
        """
        self._data_dir = data_dir
        self._db_path = os.path.join(data_dir, "image_review.db")
        # 延迟初始化数据库，在首次使用时调用
        self._initialized = False

    async def _init_db(self):
        """初始化数据库表结构"""
        import logging

        logger = logging.getLogger(__name__)
        if self._initialized:
            logger.debug("数据库已初始化，跳过")
            return

        logger.debug(f"开始初始化数据库，路径: {self._db_path}")
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        logger.debug("数据库目录创建完成")

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()

            # 白名单表
            logger.debug("创建白名单表")
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS whitelist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    md5_hash TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP,
                    hit_count INTEGER DEFAULT 0
                )
            """)

            # 黑名单表
            logger.debug("创建黑名单表")
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS blacklist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    md5_hash TEXT UNIQUE NOT NULL,
                    risk_level INTEGER NOT NULL,
                    risk_reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP,
                    hit_count INTEGER DEFAULT 0
                )
            """)

            # 人工白名单表
            logger.debug("创建人工白名单表")
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS manual_whitelist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    md5_hash TEXT UNIQUE NOT NULL,
                    added_by TEXT,
                    reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 人工黑名单表
            logger.debug("创建人工黑名单表")
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS manual_blacklist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    md5_hash TEXT UNIQUE NOT NULL,
                    risk_level INTEGER NOT NULL,
                    risk_reason TEXT,
                    added_by TEXT,
                    reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 违规记录表
            logger.debug("创建违规记录表")
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS violation_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    md5_hash TEXT NOT NULL,
                    image_url TEXT,
                    risk_level INTEGER NOT NULL,
                    risk_reason TEXT,
                    violation_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    mute_duration INTEGER,
                    message_id TEXT,
                    is_admin INTEGER NOT NULL DEFAULT 0
                )
            """)

            # 用户违规统计表
            logger.debug("创建用户违规统计表")
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_violation_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    violation_count INTEGER DEFAULT 0,
                    last_violation_time TIMESTAMP,
                    total_mute_duration INTEGER DEFAULT 0,
                    UNIQUE(user_id, group_id)
                )
            """)

            # 图片哈希表（用于相似图片匹配）
            logger.debug("创建图片哈希表")
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS image_hashes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    md5_hash TEXT UNIQUE NOT NULL,
                    phash TEXT,
                    dhash TEXT,
                    risk_level INTEGER,
                    risk_reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP,
                    hit_count INTEGER DEFAULT 0
                )
            """)

            # 迁移：检查是否需要添加 hash_version 列
            # v1.3.7 的 phash 实际是 dHash，v1.3.8 修正为真实 pHash（DCT），需要清理旧数据
            try:
                await cursor.execute(
                    "ALTER TABLE image_hashes ADD COLUMN hash_version INTEGER DEFAULT 2"
                )  # 默认值 2 对应 self.HASH_VERSION，此处需要 SQL 字面量
                # 旧版 phash 数据（v1.3.7）不可信，清理
                await cursor.execute("DELETE FROM image_hashes")
                logger.debug(
                    "检测到旧版 image_hashes 表，已添加 hash_version 列并清理旧数据"
                )
            except aiosqlite.OperationalError:
                # 列已存在，无需迁移
                pass

            # 审核日志表（v1.5.0 新增：记录每张图片的审核结果，供 WebUI 统计与浏览）
            logger.debug("创建审核日志表")
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT,
                    user_id TEXT,
                    user_name TEXT,
                    md5_hash TEXT,
                    risk_level INTEGER,
                    risk_reason TEXT,
                    source TEXT,
                    created_at TEXT,
                    cost REAL DEFAULT 0,
                    is_admin INTEGER NOT NULL DEFAULT 0
                )
            """)

            # 用户违规档案表（v1.5.0 新增：跨群用户档案，含状态/备注/首末次出现）
            logger.debug("创建用户违规档案表")
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id TEXT PRIMARY KEY,
                    nickname TEXT DEFAULT '',
                    group_ids TEXT DEFAULT '[]',
                    note TEXT DEFAULT '',
                    status TEXT DEFAULT 'normal',
                    violation_count INTEGER DEFAULT 0,
                    first_seen_at TEXT,
                    last_seen_at TEXT,
                    updated_at TEXT
                )
            """)

            # 账号白名单表（v1.5.1 新增：白名单 QQ 账号跳过审核，支持全局与群级）
            logger.debug("创建账号白名单表")
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS account_whitelist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    group_id TEXT NOT NULL DEFAULT '',
                    added_by TEXT,
                    note TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, group_id)
                )
            """)

            # 模型定价表（v1.5.4：按 provider_id 配置输入/缓存/输出单价）
            logger.debug("创建模型定价表")
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS model_pricing (
                    model_id TEXT PRIMARY KEY,
                    currency TEXT NOT NULL DEFAULT 'CNY',
                    price_per INTEGER NOT NULL DEFAULT 1000000,
                    input_price REAL NOT NULL DEFAULT 0,
                    cached_price REAL NOT NULL DEFAULT 0,
                    output_price REAL NOT NULL DEFAULT 0,
                    label TEXT DEFAULT '',
                    updated_at TEXT
                )
            """)

            # LLM 成本日志表（v1.5.4：每次 LLM 调用一条，供趋势图与审计）
            logger.debug("创建 LLM 成本日志表")
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS llm_cost_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_id TEXT NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'CNY',
                    price_per INTEGER NOT NULL DEFAULT 1000000,
                    input_other INTEGER NOT NULL DEFAULT 0,
                    input_cached INTEGER NOT NULL DEFAULT 0,
                    output INTEGER NOT NULL DEFAULT 0,
                    cost REAL NOT NULL DEFAULT 0,
                    created_at TEXT
                )
            """)

            # 模型成本累计表（v1.5.4：按 model_id 汇总，永不清理，概览页用）
            logger.debug("创建模型成本累计表")
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS model_cost_total (
                    model_id TEXT PRIMARY KEY,
                    currency TEXT NOT NULL DEFAULT 'CNY',
                    total_input_other INTEGER NOT NULL DEFAULT 0,
                    total_input_cached INTEGER NOT NULL DEFAULT 0,
                    total_output INTEGER NOT NULL DEFAULT 0,
                    total_cost REAL NOT NULL DEFAULT 0,
                    call_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT
                )
            """)

            # 插件运行设置表（v1.6.0：保留策略等可在 WebUI 自定义，存 DB 不依赖配置文件）
            logger.debug("创建插件运行设置表")
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS plugin_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT
                )
            """)

            # 创建索引
            logger.debug("创建索引")
            await cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_whitelist_md5 ON whitelist(md5_hash)
            """)
            await cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_blacklist_md5 ON blacklist(md5_hash)
            """)
            await cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_violation_user ON violation_records(user_id)
            """)
            await cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_violation_group ON violation_records(group_id)
            """)
            await cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_image_hashes_md5 ON image_hashes(md5_hash)
            """)
            await cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_image_hashes_phash ON image_hashes(phash)
            """)
            await cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_image_hashes_dhash ON image_hashes(dhash)
            """)
            await cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_audit_log_group ON audit_log(group_id)
            """)
            await cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at)
            """)
            await cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_profiles_status ON user_profiles(status)
            """)
            await cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_profiles_vcount
                ON user_profiles(violation_count)
            """)

            await conn.commit()
            logger.debug("数据库表结构初始化完成")
        self._initialized = True

        # v1.5.0 幂等迁移（独立连接）：名单表群级化 + 违规记录新列
        await self._migrate_v150()

        # v1.6.x 幂等迁移：历史本地时间列转 UTC（统一时区，配合展示层 +8）
        await self._migrate_timezone_utc()

    async def _migrate_v150(self):
        """v1.5.0 数据库迁移（幂等）

        1. violation_records 增加 user_name / evidence_path / note 列
        2. manual_whitelist / manual_blacklist 增加 group_id 列，
           唯一约束由 md5_hash 改为 (md5_hash, group_id)（重建表迁移，
           参照 astrbot_plugin_content_audit 的 _migrate_whitelist_per_group）
        """
        import logging

        logger = logging.getLogger(__name__)

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()

            # 1. violation_records 新列（ALTER 不支持复合操作，逐项尝试）
            for column, type_def in (
                ("user_name", "TEXT DEFAULT ''"),
                ("evidence_path", "TEXT"),
                ("note", "TEXT DEFAULT ''"),
                ("phash", "TEXT"),
                ("dhash", "TEXT"),
                ("is_admin", "INTEGER NOT NULL DEFAULT 0"),
            ):  # fmt: skip
                try:
                    await cursor.execute(
                        f"ALTER TABLE violation_records ADD COLUMN {column} {type_def}"
                    )
                    logger.debug(f"violation_records 迁移: 新增列 {column}")
                except aiosqlite.OperationalError:
                    pass  # 列已存在

            # 2. 人工名单表群级化重建
            await self._rebuild_manual_list_with_group(cursor, "manual_whitelist")
            await self._rebuild_manual_list_with_group(cursor, "manual_blacklist")

            await conn.commit()

        # audit_log 列迁移：cost(v1.5.4) + is_admin(v1.6.x)（独立连接，幂等）
        await self._migrate_audit_log_columns()

    async def _migrate_audit_log_columns(self):
        """audit_log 列迁移（独立连接，PRAGMA 检测后 ALTER，幂等）。

        涵盖 cost(v1.5.4) 与 is_admin(v1.6.x) 两列；老库缺哪列补哪列。
        """
        import logging

        logger = logging.getLogger(__name__)
        try:
            async with aiosqlite.connect(self._db_path) as conn:
                cursor = await conn.cursor()
                await cursor.execute("PRAGMA table_info(audit_log)")
                cols = {row[1] for row in await cursor.fetchall()}
                added: list[str] = []
                if "cost" not in cols:
                    await cursor.execute(
                        "ALTER TABLE audit_log ADD COLUMN cost REAL DEFAULT 0"
                    )
                    added.append("cost")
                if "is_admin" not in cols:
                    await cursor.execute(
                        "ALTER TABLE audit_log ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
                    )
                    added.append("is_admin")
                if added:
                    await conn.commit()
                    logger.info(f"audit_log 迁移: 成功新增列 {added}")
        except Exception as e:
            logger.error(f"audit_log 列迁移失败: {e}")

    def _backup_db_file(self, tag: str) -> bool:
        """破坏性迁移前的文件级数据库备份（含 -wal/-shm），同 tag 幂等。

        备份到 ``<插件数据目录>/backups/<db 文件名>.before-<tag>``（始终位于
        插件自身数据目录下，不依赖 db 文件路径反推）。已存在则视为已备份，
        直接成功；须在无任何 open 连接时调用以保证复制一致性。
        备份失败返回 False（调用方应据此中止迁移，确保「无备份不迁移」）。
        """
        import logging
        import shutil

        logger = logging.getLogger(__name__)
        if not self._db_path or not os.path.exists(self._db_path):
            return True  # 首次初始化尚无 db 文件，无需备份
        backup_dir = os.path.join(self._data_dir, "backups")
        try:
            os.makedirs(backup_dir, exist_ok=True)
        except OSError as e:
            logger.error(f"创建备份目录失败: {e}")
            return False
        marker = os.path.join(
            backup_dir, f"{os.path.basename(self._db_path)}.before-{tag}"
        )
        if os.path.exists(marker):
            return True  # 该迁移已备份过，幂等
        try:
            for suffix in ("", "-wal", "-shm"):
                src = self._db_path + suffix
                if os.path.exists(src):
                    shutil.copy2(src, marker + suffix)
            logger.info(f"升级前数据库已自动备份: {marker}")
            return True
        except OSError as e:
            logger.error(f"数据库自动备份失败: {e}")
            return False

    async def _migrate_timezone_utc(self):
        """启动时自动把历史「本地时间」列转为 UTC（−8h），使全库统一存 UTC。

        仅迁移过去由 ``datetime.now()`` 写入的列；``CURRENT_TIMESTAMP`` 默认值
        写入的列（violation_time、各名单表 created_at、user_violation_stats 的
        last_violation_time）本就是 UTC，跳过。

        流程：检查标志 →（首次）自动文件备份 → 单事务迁移 → 写标志。
        备份失败则中止迁移（不写标志，下次启动重试），保证「无备份不迁移」，
        使终端用户无需任何手动干预即可安全升级。假设存量数据产生于 UTC+8 环境
        （与展示层 _LOCAL_DAY 一致）。表/列名来自固定白名单。
        """
        import logging

        logger = logging.getLogger(__name__)
        flag = "tz_utc_migrated_v1"
        local_cols = [
            ("audit_log", "created_at"),
            ("llm_cost_log", "created_at"),
            ("user_profiles", "first_seen_at"),
            ("user_profiles", "last_seen_at"),
            ("user_profiles", "updated_at"),
            ("model_pricing", "updated_at"),
            ("model_cost_total", "updated_at"),
            ("plugin_settings", "updated_at"),
            ("whitelist", "expires_at"),
            ("blacklist", "expires_at"),
            ("image_hashes", "expires_at"),
        ]
        try:
            # 1) 检查是否已迁移（短连接，随即关闭，确保备份时无 open 连接）
            async with aiosqlite.connect(self._db_path) as conn:
                cursor = await conn.cursor()
                await cursor.execute(
                    "SELECT 1 FROM plugin_settings WHERE key = ?", (flag,)
                )
                if await cursor.fetchone():
                    return  # 已迁移，幂等跳过
            # 2) 首次迁移前自动备份；失败则中止，下次启动重试
            if not self._backup_db_file(flag):
                logger.error("时区迁移中止：升级前自动备份失败，请检查磁盘/权限后重启")
                return
            # 3) 执行迁移并写标志（单事务原子）
            async with aiosqlite.connect(self._db_path) as conn:
                cursor = await conn.cursor()
                for table, col in local_cols:
                    # 变量承载 f-string，避免 execute 直接拼接触发 SQL 注入告警；
                    # table/col 均来自上方固定白名单，无注入风险。
                    sql = (
                        f"UPDATE {table} SET {col} = datetime({col}, '-8 hours') "
                        f"WHERE {col} IS NOT NULL AND {col} != ''"
                    )
                    try:
                        await cursor.execute(sql)
                    except aiosqlite.OperationalError:
                        continue  # 表/列不存在（旧库未启用该功能）跳过
                await cursor.execute(
                    "INSERT INTO plugin_settings (key, value, updated_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                    "value=excluded.value, updated_at=excluded.updated_at",
                    (flag, "1", _utc_iso()),
                )
                await conn.commit()
                logger.info("时区迁移完成：历史本地时间列已 −8h 转为 UTC")
        except Exception as e:
            logger.error(f"时区迁移失败: {e}")

    async def _rebuild_manual_list_with_group(self, cursor, table: str):
        """为人工名单表增加 group_id 并重建唯一约束（幂等）。

        旧表: md5_hash UNIQUE；新表: UNIQUE(md5_hash, group_id)，
        group_id='' 表示全局条目。旧数据一律迁移为全局条目。
        """
        import logging

        logger = logging.getLogger(__name__)

        await cursor.execute(f"PRAGMA table_info({table})")
        cols = {row[1] for row in await cursor.fetchall()}
        if "group_id" in cols:
            return  # 已迁移

        if table == "manual_whitelist":
            create_sql = f"""
                CREATE TABLE {table}_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    md5_hash TEXT NOT NULL,
                    group_id TEXT NOT NULL DEFAULT '',
                    added_by TEXT,
                    reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(md5_hash, group_id)
                )
            """
            insert_sql = f"""
                INSERT INTO {table}_new (md5_hash, group_id, added_by, reason, created_at)
                SELECT md5_hash, '', added_by, reason, created_at FROM {table}
            """
        else:  # manual_blacklist
            create_sql = f"""
                CREATE TABLE {table}_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    md5_hash TEXT NOT NULL,
                    group_id TEXT NOT NULL DEFAULT '',
                    risk_level INTEGER NOT NULL,
                    risk_reason TEXT,
                    added_by TEXT,
                    reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(md5_hash, group_id)
                )
            """
            insert_sql = f"""
                INSERT INTO {table}_new
                    (md5_hash, group_id, risk_level, risk_reason, added_by, reason, created_at)
                SELECT md5_hash, '', risk_level, risk_reason, added_by, reason, created_at
                FROM {table}
            """

        await cursor.execute(create_sql)
        await cursor.execute(insert_sql)
        await cursor.execute(f"DROP TABLE {table}")
        await cursor.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
        logger.debug(f"{table} 迁移: 增加 group_id 列（群级名单），旧数据作为全局条目")

    @staticmethod
    def calculate_md5(data: bytes) -> str:
        """
        计算数据的MD5值

        Args:
            data: 原始数据

        Returns:
            MD5哈希字符串
        """
        return hashlib.md5(data).hexdigest()

    @staticmethod
    def _calculate_expire_hours(
        hit_count: int,
        base_expire_hours: int = 2,
        max_expire_days: int = 14,
    ) -> int:
        """
        根据命中次数计算过期时间（指数增长，上限为最大天数）

        每次命中增加50%过期时间，最多考虑10次命中

        Args:
            hit_count: 当前命中次数
            base_expire_hours: 基础过期时间（小时）
            max_expire_days: 最大过期时间（天）

        Returns:
            过期时间（小时）
        """
        return min(
            int(base_expire_hours * (1.5 ** min(hit_count, 10))),
            max_expire_days * 24,
        )

    async def check_whitelist(
        self,
        md5_hash: str,
        base_expire_hours: int = 2,
        max_expire_days: int = 14,
        extend_on_hit: bool = True,
    ) -> bool:
        """
        检查MD5是否在白名单中

        Args:
            md5_hash: MD5哈希值
            base_expire_hours: 基础过期时间（小时），命中时用于延长
            max_expire_days: 最大过期时间（天），命中时用于延长
            extend_on_hit: 命中时是否延长过期时间

        Returns:
            是否在白名单中
        """
        import logging

        logger = logging.getLogger(__name__)
        logger.debug(f"检查白名单，MD5: {md5_hash}")
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT id, expires_at, hit_count FROM whitelist WHERE md5_hash = ?",
                (md5_hash,),
            )
            result = await cursor.fetchone()

            if result is None:
                logger.debug(f"白名单中未找到，MD5: {md5_hash}")
                return False

            record_id, expires_at, hit_count = result
            logger.debug(
                f"白名单中找到记录，ID: {record_id}, 过期时间: {expires_at}, 命中次数: {hit_count}"
            )

            # 检查是否过期
            if expires_at and _utc_now() > datetime.fromisoformat(expires_at):
                # 过期删除
                logger.debug(f"白名单记录已过期，删除记录，ID: {record_id}")
                await cursor.execute("DELETE FROM whitelist WHERE id = ?", (record_id,))
                await conn.commit()
                return False

            # 更新命中次数并延长过期时间
            new_hit_count = hit_count + 1
            if extend_on_hit:
                expire_hours = self._calculate_expire_hours(
                    new_hit_count, base_expire_hours, max_expire_days
                )
                new_expires_at = _utc_now() + timedelta(hours=expire_hours)
                logger.debug(
                    f"更新白名单命中次数，ID: {record_id}, 旧次数: {hit_count}, "
                    f"新次数: {new_hit_count}, 延长过期时间至: {new_expires_at}"
                )
                await cursor.execute(
                    "UPDATE whitelist SET hit_count = ?, expires_at = ? WHERE id = ?",
                    (new_hit_count, new_expires_at.isoformat(), record_id),
                )
            else:
                await cursor.execute(
                    "UPDATE whitelist SET hit_count = ? WHERE id = ?",
                    (new_hit_count, record_id),
                )
            await conn.commit()
            logger.debug(f"白名单检查通过，MD5: {md5_hash}")
            return True

    async def check_blacklist(
        self,
        md5_hash: str,
        base_expire_hours: int = 2,
        max_expire_days: int = 14,
        extend_on_hit: bool = True,
    ) -> tuple[RiskLevel, str] | None:
        """
        检查MD5是否在黑名单中

        Args:
            md5_hash: MD5哈希值
            base_expire_hours: 基础过期时间（小时），命中时用于延长
            max_expire_days: 最大过期时间（天），命中时用于延长
            extend_on_hit: 命中时是否延长过期时间

        Returns:
            如果存在返回(risk_level, risk_reason)，否则返回None
        """
        import logging

        logger = logging.getLogger(__name__)
        logger.debug(f"检查黑名单，MD5: {md5_hash}")
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                """SELECT id, risk_level, risk_reason, expires_at, hit_count
                   FROM blacklist WHERE md5_hash = ?""",
                (md5_hash,),
            )
            result = await cursor.fetchone()

            if result is None:
                logger.debug(f"黑名单中未找到，MD5: {md5_hash}")
                return None

            record_id, risk_level, risk_reason, expires_at, hit_count = result
            logger.debug(
                f"黑名单中找到记录，ID: {record_id}, 风险等级: {risk_level}, 原因: {risk_reason}, 过期时间: {expires_at}, 命中次数: {hit_count}"
            )

            # 检查是否过期
            if expires_at and _utc_now() > datetime.fromisoformat(expires_at):
                # 过期删除
                logger.debug(f"黑名单记录已过期，删除记录，ID: {record_id}")
                await cursor.execute("DELETE FROM blacklist WHERE id = ?", (record_id,))
                await conn.commit()
                return None

            # 更新命中次数并延长过期时间
            new_hit_count = hit_count + 1
            if extend_on_hit:
                expire_hours = self._calculate_expire_hours(
                    new_hit_count, base_expire_hours, max_expire_days
                )
                new_expires_at = _utc_now() + timedelta(hours=expire_hours)
                logger.debug(
                    f"更新黑名单命中次数，ID: {record_id}, 旧次数: {hit_count}, "
                    f"新次数: {new_hit_count}, 延长过期时间至: {new_expires_at}"
                )
                await cursor.execute(
                    "UPDATE blacklist SET hit_count = ?, expires_at = ? WHERE id = ?",
                    (new_hit_count, new_expires_at.isoformat(), record_id),
                )
            else:
                await cursor.execute(
                    "UPDATE blacklist SET hit_count = ? WHERE id = ?",
                    (new_hit_count, record_id),
                )
            await conn.commit()

            risk_level_enum = RiskLevel(risk_level)
            logger.debug(
                f"黑名单检查命中，风险等级: {risk_level_enum.name}, 原因: {risk_reason or ''}"
            )
            return risk_level_enum, risk_reason or ""

    async def add_to_whitelist(
        self, md5_hash: str, base_expire_hours: int = 2, max_expire_days: int = 14
    ):
        """
        添加到白名单

        Args:
            md5_hash: MD5哈希值
            base_expire_hours: 基础过期时间（小时）
            max_expire_days: 最大过期时间（天）
        """
        import logging

        logger = logging.getLogger(__name__)
        logger.debug(f"添加到白名单，MD5: {md5_hash}")
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()

            # 检查是否已存在
            await cursor.execute(
                "SELECT hit_count FROM whitelist WHERE md5_hash = ?", (md5_hash,)
            )
            result = await cursor.fetchone()

            if result:
                # 已存在，延长过期时间
                hit_count = result[0]
                logger.debug(f"白名单中已存在，命中次数: {hit_count}")
                # 每次命中增加50%过期时间，避免指数增长
                expire_hours = self._calculate_expire_hours(
                    hit_count, base_expire_hours, max_expire_days
                )
                logger.debug(f"延长过期时间: {expire_hours}小时")
            else:
                expire_hours = base_expire_hours
                logger.debug(f"白名单中不存在，设置基础过期时间: {expire_hours}小时")

            expires_at = _utc_now() + timedelta(hours=expire_hours)
            logger.debug(f"过期时间: {expires_at}")

            await cursor.execute(
                """INSERT OR REPLACE INTO whitelist (md5_hash, expires_at, hit_count)
                   VALUES (?, ?, COALESCE((SELECT hit_count FROM whitelist WHERE md5_hash = ?), 0))""",
                (md5_hash, expires_at.isoformat(), md5_hash),
            )
            await conn.commit()
            logger.debug(f"添加到白名单完成，MD5: {md5_hash}")

    async def add_to_blacklist(
        self,
        md5_hash: str,
        risk_level: RiskLevel,
        risk_reason: str,
        base_expire_hours: int = 2,
        max_expire_days: int = 14,
    ):
        """
        添加到黑名单

        Args:
            md5_hash: MD5哈希值
            risk_level: 风险等级
            risk_reason: 风险原因
            base_expire_hours: 基础过期时间（小时）
            max_expire_days: 最大过期时间（天）
        """
        import logging

        logger = logging.getLogger(__name__)
        logger.debug(
            f"添加到黑名单，MD5: {md5_hash}, 风险等级: {risk_level.name}, 原因: {risk_reason}"
        )
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()

            # 检查是否已存在
            await cursor.execute(
                "SELECT hit_count FROM blacklist WHERE md5_hash = ?", (md5_hash,)
            )
            result = await cursor.fetchone()

            if result:
                # 已存在，延长过期时间
                hit_count = result[0]
                logger.debug(f"黑名单中已存在，命中次数: {hit_count}")
                # 每次命中增加50%过期时间，避免指数增长
                expire_hours = self._calculate_expire_hours(
                    hit_count, base_expire_hours, max_expire_days
                )
                logger.debug(f"延长过期时间: {expire_hours}小时")
            else:
                expire_hours = base_expire_hours
                logger.debug(f"黑名单中不存在，设置基础过期时间: {expire_hours}小时")

            expires_at = _utc_now() + timedelta(hours=expire_hours)
            logger.debug(f"过期时间: {expires_at}")

            await cursor.execute(
                """INSERT OR REPLACE INTO blacklist
                   (md5_hash, risk_level, risk_reason, expires_at, hit_count)
                   VALUES (?, ?, ?, ?, COALESCE((SELECT hit_count FROM blacklist WHERE md5_hash = ?), 0))""",
                (
                    md5_hash,
                    risk_level.value,
                    risk_reason,
                    expires_at.isoformat(),
                    md5_hash,
                ),
            )
            await conn.commit()
            logger.debug(f"添加到黑名单完成，MD5: {md5_hash}")

    async def record_violation(
        self,
        user_id: str,
        group_id: str,
        md5_hash: str,
        image_url: str | None,
        risk_level: RiskLevel,
        risk_reason: str,
        mute_duration: int | None = None,
        message_id: str | None = None,
        user_name: str = "",
        evidence_path: str | None = None,
        phash: str | None = None,
        dhash: str | None = None,
        is_admin: bool = False,
        update_stats: bool = True,
    ):
        """
        记录违规信息

        Args:
            user_id: 用户ID
            group_id: 群ID
            md5_hash: 图片MD5
            image_url: 图片URL
            risk_level: 风险等级
            risk_reason: 风险原因
            mute_duration: 禁言时长（秒）
            message_id: 消息ID
            user_name: 用户名（v1.5.0，WebUI 展示用）
            evidence_path: 证据图片本地路径（v1.5.0，WebUI 展示用）
            phash: 感知哈希 pHash 十六进制（v1.5.2，WebUI 展示用，可选）
            dhash: 感知哈希 dHash 十六进制（v1.5.2，WebUI 展示用，可选）
            is_admin: 发送者是否为管理员/群主（管理员违规仅通报不处罚，但仍写入
                      违规记录留痕，WebUI 以徽章标注）
            update_stats: 是否同步累加用户违规统计与档案计数。管理员留痕时传 False，
                          使「记录留痕」之外的最终行为与历史完全一致（不计次、不禁言）。
        """
        import logging

        logger = logging.getLogger(__name__)
        logger.debug(
            f"记录违规信息，用户: {user_id}, 群: {group_id}, 风险等级: {risk_level.name}"
        )
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()

            # 插入违规记录
            logger.debug("插入违规记录到数据库")
            await cursor.execute(
                """INSERT INTO violation_records
                   (user_id, group_id, md5_hash, image_url, risk_level, risk_reason,
                    mute_duration, message_id, user_name, evidence_path, phash, dhash, is_admin)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    group_id,
                    md5_hash,
                    image_url,
                    risk_level.value,
                    risk_reason,
                    mute_duration,
                    message_id,
                    user_name,
                    evidence_path,
                    phash,
                    dhash,
                    1 if is_admin else 0,
                ),
            )
            logger.debug("违规记录插入完成")

            # 更新用户违规统计（管理员留痕时 update_stats=False，跳过以保持行为不变）
            if update_stats:
                logger.debug("更新用户违规统计")
                await cursor.execute(
                    """INSERT INTO user_violation_stats (user_id, group_id, violation_count, last_violation_time, total_mute_duration)
                       VALUES (?, ?, 1, CURRENT_TIMESTAMP, ?)
                       ON CONFLICT(user_id, group_id) DO UPDATE SET
                       violation_count = violation_count + 1,
                       last_violation_time = CURRENT_TIMESTAMP,
                       total_mute_duration = total_mute_duration + ?""",
                    (user_id, group_id, mute_duration or 0, mute_duration or 0),
                )
                logger.debug("用户违规统计更新完成")

            await conn.commit()
            logger.debug("违规信息记录完成")

        # 更新用户违规档案（v1.5.0）：upsert + 全局违规计数 +1
        # 管理员留痕时 update_stats=False，跳过以免档案违规计数被累加（行为不变）
        if update_stats:
            try:
                await self.upsert_user_profile(user_id, user_name, group_id)
                await self.inc_user_violation_count(user_id)
            except Exception as e:
                logger.error(f"更新用户违规档案失败: {e}")

    async def get_user_violation_count(self, user_id: str, group_id: str) -> int:
        """
        获取用户在指定群的违规次数

        Args:
            user_id: 用户ID
            group_id: 群ID

        Returns:
            违规次数
        """
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT violation_count FROM user_violation_stats WHERE user_id = ? AND group_id = ?",
                (user_id, group_id),
            )
            result = await cursor.fetchone()
            return result[0] if result else 0

    async def get_user_violation_records(
        self, user_id: str, group_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        """
        获取用户违规记录

        Args:
            user_id: 用户ID
            group_id: 群ID（可选）
            limit: 返回记录数量限制

        Returns:
            违规记录列表
        """
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()

            if group_id:
                await cursor.execute(
                    """SELECT * FROM violation_records
                       WHERE user_id = ? AND group_id = ?
                       ORDER BY violation_time DESC LIMIT ?""",
                    (user_id, group_id, limit),
                )
            else:
                await cursor.execute(
                    """SELECT * FROM violation_records
                       WHERE user_id = ?
                       ORDER BY violation_time DESC LIMIT ?""",
                    (user_id, limit),
                )

            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_user_violations(
        self, user_id: str, group_id: str | None = None
    ) -> int:
        """
        删除用户违规记录

        Args:
            user_id: 用户ID
            group_id: 群ID（可选，不指定则删除所有群的记录）

        Returns:
            删除的记录数量
        """
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()

            if group_id:
                await cursor.execute(
                    "DELETE FROM violation_records WHERE user_id = ? AND group_id = ?",
                    (user_id, group_id),
                )
                await cursor.execute(
                    "DELETE FROM user_violation_stats WHERE user_id = ? AND group_id = ?",
                    (user_id, group_id),
                )
            else:
                await cursor.execute(
                    "DELETE FROM violation_records WHERE user_id = ?", (user_id,)
                )
                await cursor.execute(
                    "DELETE FROM user_violation_stats WHERE user_id = ?", (user_id,)
                )

            await conn.commit()
            return cursor.rowcount

    async def clean_expired_list_entries(self):
        """清理过期的黑白名单条目和图片哈希记录"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            now = _utc_iso()
            await cursor.execute("DELETE FROM whitelist WHERE expires_at < ?", (now,))
            await cursor.execute("DELETE FROM blacklist WHERE expires_at < ?", (now,))
            await cursor.execute(
                "DELETE FROM image_hashes WHERE expires_at < ?", (now,)
            )
            await conn.commit()

    async def clear_all_cache(self) -> dict:
        """清除所有缓存数据（黑白名单）"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()

            await cursor.execute("SELECT COUNT(*) FROM whitelist")
            whitelist_count = (await cursor.fetchone())[0]
            await cursor.execute("SELECT COUNT(*) FROM blacklist")
            blacklist_count = (await cursor.fetchone())[0]

            await cursor.execute("DELETE FROM whitelist")
            await cursor.execute("DELETE FROM blacklist")
            await conn.commit()

            return {"whitelist": whitelist_count, "blacklist": blacklist_count}

    # ========== 图片哈希管理（用于相似图片匹配） ==========

    async def add_image_hash(
        self,
        md5_hash: str,
        phash: str | None,
        dhash: str | None,
        risk_level: RiskLevel | None = None,
        risk_reason: str | None = None,
        base_expire_hours: int = 2,
        max_expire_days: int = 14,
    ):
        """
        添加图片哈希记录

        Args:
            md5_hash: MD5哈希值
            phash: 感知哈希值
            dhash: 差异哈希值
            risk_level: 风险等级（白名单为None）
            risk_reason: 风险原因
            base_expire_hours: 基础过期时间（小时）
            max_expire_days: 最大过期时间（天）
        """
        import logging

        logger = logging.getLogger(__name__)
        logger.debug(
            f"添加图片哈希记录，MD5: {md5_hash}, phash: {phash}, dhash: {dhash}"
        )
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()

            # 检查是否已存在
            await cursor.execute(
                "SELECT hit_count FROM image_hashes WHERE md5_hash = ?", (md5_hash,)
            )
            result = await cursor.fetchone()

            if result:
                # 已存在，延长过期时间
                hit_count = result[0]
                expire_hours = self._calculate_expire_hours(
                    hit_count, base_expire_hours, max_expire_days
                )
            else:
                expire_hours = base_expire_hours

            expires_at = _utc_now() + timedelta(hours=expire_hours)
            risk_level_value = risk_level.value if risk_level else None

            await cursor.execute(
                """INSERT OR REPLACE INTO image_hashes
                   (md5_hash, phash, dhash, risk_level, risk_reason, expires_at, hit_count, hash_version)
                   VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT hit_count FROM image_hashes WHERE md5_hash = ?), 0), ?)""",
                (
                    md5_hash,
                    phash,
                    dhash,
                    risk_level_value,
                    risk_reason,
                    expires_at.isoformat(),
                    md5_hash,
                    self.HASH_VERSION,
                ),
            )
            await conn.commit()
            logger.debug(f"图片哈希记录添加完成，MD5: {md5_hash}")

    async def find_similar_images(
        self,
        target_hash: str,
        hash_type: str,
        threshold: int,
    ) -> list[tuple[str, int, RiskLevel | None, str | None]]:
        """
        查找相似图片

        Args:
            target_hash: 目标图片哈希值
            hash_type: 哈希类型 ('phash' 或 'dhash')
            threshold: 汉明距离阈值

        Returns:
            相似图片列表，每项为 (md5_hash, hamming_distance, risk_level, risk_reason)
        """
        import logging

        logger = logging.getLogger(__name__)
        logger.debug(
            f"查找相似图片，目标哈希: {target_hash}, 类型: {hash_type}, 阈值: {threshold}"
        )
        await self._init_db()

        if not target_hash:
            return []

        async with aiosqlite.connect(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()

            # 获取所有未过期的哈希记录
            now = _utc_iso()
            hash_column = "phash" if hash_type == "phash" else "dhash"

            await cursor.execute(
                f"""SELECT md5_hash, {hash_column} as hash_value, risk_level, risk_reason, expires_at
                    FROM image_hashes
                    WHERE {hash_column} IS NOT NULL AND hash_version = ? AND expires_at > ?""",
                (self.HASH_VERSION, now),
            )
            rows = await cursor.fetchall()

            similar_images = []
            for row in rows:
                stored_hash = row["hash_value"]
                if not stored_hash:
                    continue

                # 计算汉明距离
                distance = self._hamming_distance(target_hash, stored_hash)
                if distance <= threshold:
                    risk_level = (
                        RiskLevel(row["risk_level"])
                        if row["risk_level"] is not None
                        else None
                    )
                    similar_images.append(
                        (
                            row["md5_hash"],
                            distance,
                            risk_level,
                            row["risk_reason"],
                        )
                    )

            # 按汉明距离排序
            similar_images.sort(key=lambda x: x[1])
            logger.debug(f"找到 {len(similar_images)} 张相似图片")
            return similar_images

    @staticmethod
    def _hamming_distance(hash1: str, hash2: str) -> int:
        """
        计算两个哈希值的汉明距离

        Args:
            hash1: 第一个哈希值（十六进制字符串）
            hash2: 第二个哈希值（十六进制字符串）

        Returns:
            汉明距离
        """
        if len(hash1) != len(hash2):
            # 长度不同时，取较短的长度进行比较
            min_len = min(len(hash1), len(hash2))
            hash1 = hash1[:min_len]
            hash2 = hash2[:min_len]

        try:
            # 将十六进制字符串转换为整数
            int1 = int(hash1, 16)
            int2 = int(hash2, 16)
            # 异或后计算1的位数
            xor = int1 ^ int2
            return bin(xor).count("1")
        except ValueError:
            return float("inf")

    async def update_hash_hit_count(self, md5_hash: str):
        """更新哈希记录命中次数"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "UPDATE image_hashes SET hit_count = hit_count + 1 WHERE md5_hash = ?",
                (md5_hash,),
            )
            await conn.commit()

    # ========== 人工白名单管理（v1.5.0 群级化） ==========

    async def check_manual_whitelist(self, md5_hash: str, group_id: str = "") -> bool:
        """检查MD5是否在人工白名单中（全局条目或指定群条目命中即放行）"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                """SELECT 1 FROM manual_whitelist
                   WHERE md5_hash = ? AND (group_id = '' OR group_id = ?) LIMIT 1""",
                (md5_hash, group_id),
            )
            result = await cursor.fetchone()
            return result is not None

    async def add_manual_whitelist(
        self,
        md5_hash: str,
        added_by: str = None,
        reason: str = None,
        group_id: str = "",
    ) -> bool:
        """添加到人工白名单（group_id='' 表示全局）"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            try:
                await cursor.execute(
                    """INSERT INTO manual_whitelist (md5_hash, group_id, added_by, reason)
                       VALUES (?, ?, ?, ?)""",
                    (md5_hash, group_id or "", added_by, reason),
                )
                await conn.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def remove_manual_whitelist(
        self, md5_hash: str, group_id: str | None = None
    ) -> bool:
        """从人工白名单移除

        Args:
            md5_hash: 图片MD5
            group_id: None=移除该MD5的全部范围条目（向后兼容旧行为）；
                      ''=仅移除全局条目；具体群号=仅移除该群条目
        """
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            if group_id is None:
                await cursor.execute(
                    "DELETE FROM manual_whitelist WHERE md5_hash = ?", (md5_hash,)
                )
            else:
                await cursor.execute(
                    "DELETE FROM manual_whitelist WHERE md5_hash = ? AND group_id = ?",
                    (md5_hash, group_id),
                )
            await conn.commit()
            return cursor.rowcount > 0

    async def clear_all_manual_whitelist(self) -> int:
        """清空人工白名单（全部范围）"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute("SELECT COUNT(*) FROM manual_whitelist")
            count = (await cursor.fetchone())[0]
            await cursor.execute("DELETE FROM manual_whitelist")
            await conn.commit()
            return count

    async def get_manual_whitelist(self, limit: int = 50) -> list[dict]:
        """获取人工白名单列表"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT * FROM manual_whitelist ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # ========== 人工黑名单管理（v1.5.0 群级化） ==========

    async def check_manual_blacklist(
        self, md5_hash: str, group_id: str = ""
    ) -> tuple[RiskLevel, str] | None:
        """检查MD5是否在人工黑名单中（全局条目或指定群条目命中即拦截，群级条目优先）"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                """SELECT risk_level, risk_reason FROM manual_blacklist
                   WHERE md5_hash = ? AND (group_id = '' OR group_id = ?)
                   ORDER BY group_id DESC LIMIT 1""",
                (md5_hash, group_id),
            )
            result = await cursor.fetchone()
            if result:
                return RiskLevel(result[0]), result[1] or ""
            return None

    async def add_manual_blacklist(
        self,
        md5_hash: str,
        risk_level: RiskLevel,
        risk_reason: str,
        added_by: str = None,
        reason: str = None,
        group_id: str = "",
    ) -> bool:
        """添加到人工黑名单（group_id='' 表示全局）"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            try:
                await cursor.execute(
                    """INSERT INTO manual_blacklist
                       (md5_hash, group_id, risk_level, risk_reason, added_by, reason)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        md5_hash,
                        group_id or "",
                        risk_level.value,
                        risk_reason,
                        added_by,
                        reason,
                    ),
                )
                await conn.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def remove_manual_blacklist(
        self, md5_hash: str, group_id: str | None = None
    ) -> bool:
        """从人工黑名单移除（group_id 语义同 remove_manual_whitelist）"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            if group_id is None:
                await cursor.execute(
                    "DELETE FROM manual_blacklist WHERE md5_hash = ?", (md5_hash,)
                )
            else:
                await cursor.execute(
                    "DELETE FROM manual_blacklist WHERE md5_hash = ? AND group_id = ?",
                    (md5_hash, group_id),
                )
            await conn.commit()
            return cursor.rowcount > 0

    async def clear_all_manual_blacklist(self) -> int:
        """清空人工黑名单（全部范围）"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute("SELECT COUNT(*) FROM manual_blacklist")
            count = (await cursor.fetchone())[0]
            await cursor.execute("DELETE FROM manual_blacklist")
            await conn.commit()
            return count

    async def get_manual_blacklist(self, limit: int = 50) -> list[dict]:
        """获取人工黑名单列表"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT * FROM manual_blacklist ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_manual_whitelist_entries(self, md5_hash: str) -> list[dict]:
        """获取某 MD5 在人工白名单中的全部范围条目（全局 + 各群，v1.5.0）"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT * FROM manual_whitelist WHERE md5_hash = ? ORDER BY group_id",
                (md5_hash,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_manual_blacklist_entries(self, md5_hash: str) -> list[dict]:
        """获取某 MD5 在人工黑名单中的全部范围条目（全局 + 各群，v1.5.0）"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT * FROM manual_blacklist WHERE md5_hash = ? ORDER BY group_id",
                (md5_hash,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    # ========== 自动黑白名单管理 ==========

    async def remove_auto_whitelist(self, md5_hash: str) -> bool:
        """从自动白名单移除"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "DELETE FROM whitelist WHERE md5_hash = ?", (md5_hash,)
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def remove_auto_blacklist(self, md5_hash: str) -> bool:
        """从自动黑名单移除"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "DELETE FROM blacklist WHERE md5_hash = ?", (md5_hash,)
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def get_cache_counts(self) -> dict:
        """获取自动黑白名单数量统计"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()

            await cursor.execute("SELECT COUNT(*) FROM whitelist")
            whitelist_count = (await cursor.fetchone())[0]

            await cursor.execute("SELECT COUNT(*) FROM blacklist")
            blacklist_count = (await cursor.fetchone())[0]

            return {"whitelist": whitelist_count, "blacklist": blacklist_count}

    # ========== 审核日志（v1.5.0 新增，供 WebUI 统计与浏览） ==========

    async def record_audit(
        self,
        group_id: str,
        user_id: str,
        user_name: str,
        md5_hash: str | None,
        risk_level: RiskLevel,
        risk_reason: str,
        source: str = "",
        is_admin: bool = False,
    ) -> int | None:
        """
        记录一次图片审核结果（无论通过与否），并更新用户档案活跃时间

        Args:
            group_id: 群ID
            user_id: 用户ID
            user_name: 用户名
            md5_hash: 图片MD5
            risk_level: 风险等级
            risk_reason: 风险原因（LLM 分析结论）
            source: 审核来源（如 Aliyun / VLAI / cache）
            is_admin: 发送者是否为管理员/群主（供 WebUI 标注，与违规记录口径一致）

        Returns:
            插入的 audit_log 行 id，失败返回 None
        """
        import logging

        logger = logging.getLogger(__name__)
        row_id = None
        try:
            await self._init_db()
            async with aiosqlite.connect(self._db_path) as conn:
                cursor = await conn.cursor()
                await cursor.execute(
                    """INSERT INTO audit_log
                       (group_id, user_id, user_name, md5_hash, risk_level, risk_reason, source, created_at, is_admin)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        group_id,
                        user_id,
                        user_name,
                        md5_hash,
                        risk_level.value,
                        risk_reason,
                        source,
                        _utc_iso(),
                        1 if is_admin else 0,
                    ),
                )
                row_id = cursor.lastrowid
                await conn.commit()
        except Exception as e:
            logger.error(f"记录审核日志失败: {e}")
            return None
        # 更新用户档案（首末次出现、昵称、所在群），不影响主流程
        try:
            await self.upsert_user_profile(user_id, user_name, group_id)
        except Exception as e:
            logger.error(f"更新用户档案失败: {e}")
        return row_id

    async def set_audit_cost(self, audit_id: int, total_cost: float) -> None:
        """将本次审核的全部 LLM 成本回写到 audit_log.cost（供审核日志展示单次成本）"""
        await self._init_db()
        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "UPDATE audit_log SET cost = ? WHERE id = ?",
                (total_cost, audit_id),
            )
            await conn.commit()

    async def get_audit_cost_stats(self) -> dict:
        """审核日志成本统计：总成本 + 有成本记录的审核次数，供概览均价计算

        Returns:
            {"total_cost": float, "cost_count": int}  (cost_count = 有成本的审核数)
        """
        await self._init_db()
        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            total = (
                await (
                    await cursor.execute("SELECT COALESCE(SUM(cost), 0) FROM audit_log")
                ).fetchone()
            )[0]
            cnt = (
                await (
                    await cursor.execute(
                        "SELECT COUNT(*) FROM audit_log WHERE cost > 0"
                    )
                ).fetchone()
            )[0]
            return {"total_cost": total, "cost_count": cnt}

    async def cleanup_audit_log(self, keep_days: int = 30) -> int:
        """清理超过保留天数的审核日志"""
        import logging

        logger = logging.getLogger(__name__)
        await self._init_db()
        try:
            cutoff = (_utc_now() - timedelta(days=keep_days)).isoformat()
            async with aiosqlite.connect(self._db_path) as conn:
                cursor = await conn.cursor()
                await cursor.execute(
                    "DELETE FROM audit_log WHERE created_at < ?", (cutoff,)
                )
                await conn.commit()
                return cursor.rowcount
        except Exception as e:
            logger.error(f"清理审核日志失败: {e}")
            return 0

    # ========== 用户违规档案（v1.5.0 新增） ==========

    async def upsert_user_profile(
        self, user_id: str, nickname: str, group_id: str
    ) -> None:
        """Upsert 用户档案：首次出现建档，之后更新昵称/末次出现/所在群列表。

        不触碰 violation_count（由 inc_user_violation_count 维护）。
        """
        import json

        await self._init_db()
        now_iso = _utc_iso()
        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT group_ids FROM user_profiles WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                group_ids_json = json.dumps([group_id], ensure_ascii=False)
                await cursor.execute(
                    """INSERT INTO user_profiles
                       (user_id, nickname, group_ids, note, status,
                        violation_count, first_seen_at, last_seen_at, updated_at)
                       VALUES (?, ?, ?, '', 'normal', 0, ?, ?, ?)""",
                    (user_id, nickname, group_ids_json, now_iso, now_iso, now_iso),
                )
            else:
                try:
                    group_ids = json.loads(row[0] or "[]")
                    if not isinstance(group_ids, list):
                        group_ids = []
                except (ValueError, TypeError):
                    group_ids = []
                if group_id and group_id not in group_ids:
                    group_ids.append(group_id)
                group_ids_json = json.dumps(group_ids, ensure_ascii=False)
                await cursor.execute(
                    """UPDATE user_profiles
                       SET nickname = ?, group_ids = ?, last_seen_at = ?, updated_at = ?
                       WHERE user_id = ?""",
                    (nickname, group_ids_json, now_iso, now_iso, user_id),
                )
            await conn.commit()

    async def inc_user_violation_count(self, user_id: str) -> None:
        """用户档案全局违规计数 +1"""
        await self._init_db()
        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                """UPDATE user_profiles
                   SET violation_count = violation_count + 1, updated_at = ?
                   WHERE user_id = ?""",
                (_utc_iso(), user_id),
            )
            await conn.commit()

    async def _recalc_user_stats(self, cursor, user_id: str) -> None:
        """重算用户的群级统计与档案全局违规计数（删除违规记录后调用，不提交）"""
        await cursor.execute(
            """UPDATE user_violation_stats SET violation_count = (
                   SELECT COUNT(*) FROM violation_records
                   WHERE violation_records.user_id = user_violation_stats.user_id
                     AND violation_records.group_id = user_violation_stats.group_id
               ) WHERE user_id = ?""",
            (user_id,),
        )
        await cursor.execute(
            """UPDATE user_profiles SET violation_count = (
                   SELECT COUNT(*) FROM violation_records WHERE user_id = ?
               ), updated_at = ? WHERE user_id = ?""",
            (user_id, _utc_iso(), user_id),
        )

    # ========== WebUI：违规记录查询与 CRUD ==========

    async def list_violations(
        self,
        page: int = 1,
        page_size: int = 20,
        group_id: str | None = None,
        user_id: str | None = None,
        keyword: str | None = None,
        risk_level: int | None = None,
        sort_by: str | None = None,
        sort_dir: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> tuple[list[dict], int]:
        """分页查询违规记录；keyword 模糊匹配 user_name / risk_reason"""
        import logging

        logger = logging.getLogger(__name__)
        await self._init_db()
        page = max(1, page)
        page_size = max(1, min(page_size, 100))

        clauses: list[str] = []
        params: list = []
        if group_id:
            clauses.append("group_id = ?")
            params.append(group_id)
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if risk_level is not None:
            clauses.append("risk_level = ?")
            params.append(risk_level)
        if keyword:
            escaped = _escape_like(keyword)
            clauses.append(
                "(COALESCE(user_name, '') LIKE ? ESCAPE '\\' "
                "OR COALESCE(risk_reason, '') LIKE ? ESCAPE '\\')"
            )
            like = f"%{escaped}%"
            params.extend([like, like])
        if date_from:
            clauses.append("date(violation_time, '+8 hours') >= date(?)")
            params.append(date_from)
        if date_to:
            clauses.append("date(violation_time, '+8 hours') <= date(?)")
            params.append(date_to)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_clause = _build_order_clause(
            sort_by,
            sort_dir,
            _VIOLATION_SORT_FIELDS,
            "ORDER BY violation_time DESC",
            "id",
        )
        try:
            async with aiosqlite.connect(self._db_path) as conn:
                conn.row_factory = aiosqlite.Row
                cursor = await conn.cursor()
                total = (
                    await (
                        await cursor.execute(
                            f"SELECT COUNT(*) FROM violation_records {where}", params
                        )
                    ).fetchone()
                )[0]
                await cursor.execute(
                    f"SELECT * FROM violation_records {where} {order_clause} LIMIT ? OFFSET ?",
                    [*params, page_size, (page - 1) * page_size],
                )
                rows = [dict(r) for r in await cursor.fetchall()]
                return rows, total
        except Exception as e:
            logger.error(f"list_violations 失败: {e}")
            return [], 0

    async def get_violation(self, vid: int) -> dict | None:
        """按 ID 获取单条违规记录"""
        await self._init_db()
        async with aiosqlite.connect(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            await cursor.execute("SELECT * FROM violation_records WHERE id = ?", (vid,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def update_violation(self, vid: int, fields: dict) -> bool:
        """编辑违规记录（仅允许 user_name / note）"""
        import logging

        logger = logging.getLogger(__name__)
        allowed = {"user_name", "note"}
        filtered = {k: v for k, v in fields.items() if k in allowed}
        if not filtered:
            return False
        await self._init_db()
        try:
            async with aiosqlite.connect(self._db_path) as conn:
                cursor = await conn.cursor()
                set_clause = ", ".join(f"{k} = ?" for k in filtered)
                values = list(filtered.values())
                values.append(vid)
                await cursor.execute(
                    f"UPDATE violation_records SET {set_clause} WHERE id = ?", values
                )
                await conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"update_violation({vid}) 失败: {e}")
            return False

    async def delete_violation(self, vid: int) -> bool:
        """删除单条违规记录并重算用户统计与档案计数"""
        import logging

        logger = logging.getLogger(__name__)
        await self._init_db()
        try:
            async with aiosqlite.connect(self._db_path) as conn:
                cursor = await conn.cursor()
                await cursor.execute(
                    "SELECT user_id FROM violation_records WHERE id = ?", (vid,)
                )
                row = await cursor.fetchone()
                if not row:
                    return False
                user_id = row[0]
                await cursor.execute(
                    "DELETE FROM violation_records WHERE id = ?", (vid,)
                )
                deleted = cursor.rowcount > 0
                await self._recalc_user_stats(cursor, user_id)
                await conn.commit()
                return deleted
        except Exception as e:
            logger.error(f"delete_violation({vid}) 失败: {e}")
            return False

    async def delete_violations_batch(self, ids: list[int]) -> int:
        """批量删除违规记录，重算受影响用户的统计"""
        import logging

        logger = logging.getLogger(__name__)
        if not ids:
            return 0
        await self._init_db()
        try:
            async with aiosqlite.connect(self._db_path) as conn:
                cursor = await conn.cursor()
                placeholders = ",".join("?" for _ in ids)
                await cursor.execute(
                    f"SELECT DISTINCT user_id FROM violation_records WHERE id IN ({placeholders})",
                    ids,
                )
                affected_users = [r[0] for r in await cursor.fetchall()]
                await cursor.execute(
                    f"DELETE FROM violation_records WHERE id IN ({placeholders})", ids
                )
                deleted = cursor.rowcount
                for uid in affected_users:
                    await self._recalc_user_stats(cursor, uid)
                await conn.commit()
                return deleted
        except Exception as e:
            logger.error(f"delete_violations_batch 失败: {e}")
            return 0

    async def get_violation_evidence_path(self, vid: int) -> str | None:
        """获取违规记录的证据图片路径（WebUI 图片服务用）"""
        record = await self.get_violation(vid)
        if not record:
            return None
        path = record.get("evidence_path")
        return path if path else None

    # ========== WebUI：审核日志查询与删除 ==========

    async def list_audits(
        self,
        page: int = 1,
        page_size: int = 20,
        group_id: str | None = None,
        risk_level: int | None = None,
        keyword: str | None = None,
        sort_by: str | None = None,
        sort_dir: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> tuple[list[dict], int]:
        """分页查询审核日志"""
        import logging

        logger = logging.getLogger(__name__)
        await self._init_db()
        page = max(1, page)
        page_size = max(1, min(page_size, 100))

        clauses: list[str] = []
        params: list = []
        if group_id:
            clauses.append("group_id = ?")
            params.append(group_id)
        if risk_level is not None:
            clauses.append("risk_level = ?")
            params.append(risk_level)
        if keyword:
            escaped = _escape_like(keyword)
            clauses.append(
                "(COALESCE(user_name, '') LIKE ? ESCAPE '\\' "
                "OR COALESCE(risk_reason, '') LIKE ? ESCAPE '\\')"
            )
            like = f"%{escaped}%"
            params.extend([like, like])
        if date_from:
            clauses.append("date(created_at, '+8 hours') >= date(?)")
            params.append(date_from)
        if date_to:
            clauses.append("date(created_at, '+8 hours') <= date(?)")
            params.append(date_to)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_clause = _build_order_clause(
            sort_by, sort_dir, _AUDIT_SORT_FIELDS, "ORDER BY created_at DESC", "id"
        )
        try:
            async with aiosqlite.connect(self._db_path) as conn:
                conn.row_factory = aiosqlite.Row
                cursor = await conn.cursor()
                total = (
                    await (
                        await cursor.execute(
                            f"SELECT COUNT(*) FROM audit_log {where}", params
                        )
                    ).fetchone()
                )[0]
                await cursor.execute(
                    f"SELECT * FROM audit_log {where} {order_clause} LIMIT ? OFFSET ?",
                    [*params, page_size, (page - 1) * page_size],
                )
                rows = [dict(r) for r in await cursor.fetchall()]
                return rows, total
        except Exception as e:
            logger.error(f"list_audits 失败: {e}")
            return [], 0

    async def get_audit(self, aid: int) -> dict | None:
        """按 ID 获取单条审核日志"""
        await self._init_db()
        async with aiosqlite.connect(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            await cursor.execute("SELECT * FROM audit_log WHERE id = ?", (aid,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def delete_audit(self, aid: int) -> bool:
        """删除单条审核日志"""
        await self._init_db()
        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute("DELETE FROM audit_log WHERE id = ?", (aid,))
            await conn.commit()
            return cursor.rowcount > 0

    async def delete_audits_batch(self, ids: list[int]) -> int:
        """批量删除审核日志"""
        if not ids:
            return 0
        await self._init_db()
        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            placeholders = ",".join("?" for _ in ids)
            await cursor.execute(
                f"DELETE FROM audit_log WHERE id IN ({placeholders})", ids
            )
            await conn.commit()
            return cursor.rowcount

    # ========== WebUI：人工名单详细 CRUD ==========

    async def list_manual_whitelist_detailed(
        self,
        group_id_filter: str | None = None,
        sort_by: str | None = None,
        sort_dir: str | None = None,
    ) -> list[dict]:
        """人工白名单列表。group_id_filter: None=全部, 'global'=仅全局, 具体群号=该群"""
        await self._init_db()
        clauses: list[str] = []
        params: list = []
        if group_id_filter is not None:
            if group_id_filter == "global":
                clauses.append("group_id = ''")
            else:
                clauses.append("group_id = ?")
                params.append(group_id_filter)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_clause = _build_order_clause(
            sort_by, sort_dir, _LIST_SORT_FIELDS, "ORDER BY created_at DESC", "id"
        )
        async with aiosqlite.connect(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            await cursor.execute(
                f"SELECT * FROM manual_whitelist {where} {order_clause}", params
            )
            return [dict(r) for r in await cursor.fetchall()]

    async def update_manual_list_reason(
        self, table: str, row_id: int, reason: str
    ) -> bool:
        """更新人工名单条目的备注（table 仅限 manual_whitelist / manual_blacklist）"""
        if table not in ("manual_whitelist", "manual_blacklist"):
            return False
        await self._init_db()
        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                f"UPDATE {table} SET reason = ? WHERE id = ?", (reason, row_id)
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def delete_manual_list_by_id(self, table: str, row_id: int) -> bool:
        """按 ID 删除人工名单条目（table 仅限 manual_whitelist / manual_blacklist）"""
        if table not in ("manual_whitelist", "manual_blacklist"):
            return False
        await self._init_db()
        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
            await conn.commit()
            return cursor.rowcount > 0

    async def list_manual_blacklist_detailed(
        self,
        group_id_filter: str | None = None,
        sort_by: str | None = None,
        sort_dir: str | None = None,
    ) -> list[dict]:
        """人工黑名单列表。group_id_filter 语义同白名单"""
        await self._init_db()
        clauses: list[str] = []
        params: list = []
        if group_id_filter is not None:
            if group_id_filter == "global":
                clauses.append("group_id = ''")
            else:
                clauses.append("group_id = ?")
                params.append(group_id_filter)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_clause = _build_order_clause(
            sort_by, sort_dir, _LIST_SORT_FIELDS, "ORDER BY created_at DESC", "id"
        )
        async with aiosqlite.connect(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            await cursor.execute(
                f"SELECT * FROM manual_blacklist {where} {order_clause}", params
            )
            return [dict(r) for r in await cursor.fetchall()]

    # ========== WebUI：用户档案 CRUD ==========

    async def list_user_profiles(
        self,
        page: int = 1,
        page_size: int = 20,
        keyword: str | None = None,
        status: str | None = None,
        sort_by: str | None = None,
        sort_dir: str | None = None,
        first_seen_from: str | None = None,
        first_seen_to: str | None = None,
        last_seen_from: str | None = None,
        last_seen_to: str | None = None,
        groups: list[str] | str | None = None,
    ) -> tuple[list[dict], int]:
        """分页查询用户档案；keyword 模糊匹配 user_id / nickname / note"""
        import logging

        logger = logging.getLogger(__name__)
        await self._init_db()
        page = max(1, page)
        page_size = max(1, min(page_size, 100))

        clauses: list[str] = []
        params: list = []
        if keyword:
            escaped = _escape_like(keyword)
            clauses.append(
                "(user_id LIKE ? ESCAPE '\\' OR COALESCE(nickname, '') LIKE ? ESCAPE '\\' "
                "OR COALESCE(note, '') LIKE ? ESCAPE '\\')"
            )
            like = f"%{escaped}%"
            params.extend([like, like, like])
        if status:
            clauses.append("status = ?")
            params.append(status)
        if first_seen_from:
            clauses.append("date(first_seen_at, '+8 hours') >= date(?)")
            params.append(first_seen_from)
        if first_seen_to:
            clauses.append("date(first_seen_at, '+8 hours') <= date(?)")
            params.append(first_seen_to)
        if last_seen_from:
            clauses.append("date(last_seen_at, '+8 hours') >= date(?)")
            params.append(last_seen_from)
        if last_seen_to:
            clauses.append("date(last_seen_at, '+8 hours') <= date(?)")
            params.append(last_seen_to)
        if groups:
            if isinstance(groups, str):
                groups = [g.strip() for g in groups.split(",") if g.strip()]
            group_parts: list[str] = []
            for g in groups:
                escaped = _escape_like(g)
                group_parts.append("group_ids LIKE ? ESCAPE '\\'")
                params.append(f'%"{escaped}"%')
            if group_parts:
                clauses.append(f"({' OR '.join(group_parts)})")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_clause = _build_order_clause(
            sort_by,
            sort_dir,
            _USER_PROFILE_SORT_FIELDS,
            "ORDER BY violation_count DESC, last_seen_at DESC",
            "user_id",
        )
        try:
            async with aiosqlite.connect(self._db_path) as conn:
                conn.row_factory = aiosqlite.Row
                cursor = await conn.cursor()
                total = (
                    await (
                        await cursor.execute(
                            f"SELECT COUNT(*) FROM user_profiles {where}", params
                        )
                    ).fetchone()
                )[0]
                await cursor.execute(
                    f"SELECT * FROM user_profiles {where} {order_clause} LIMIT ? OFFSET ?",
                    [*params, page_size, (page - 1) * page_size],
                )
                rows = [dict(r) for r in await cursor.fetchall()]
                return rows, total
        except Exception as e:
            logger.error(f"list_user_profiles 失败: {e}")
            return [], 0

    async def get_user_profile(self, user_id: str) -> dict | None:
        """按 user_id 获取单个用户档案"""
        await self._init_db()
        async with aiosqlite.connect(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def create_user_profile(self, data: dict) -> bool:
        """手动创建用户档案（user_id 必填，重复返回 False）"""
        import json

        user_id = data.get("user_id")
        if not user_id:
            return False
        group_ids_val = data.get("group_ids", [])
        if isinstance(group_ids_val, list):
            group_ids_json = json.dumps(
                [str(g) for g in group_ids_val], ensure_ascii=False
            )
        elif isinstance(group_ids_val, str):
            group_ids_json = group_ids_val or "[]"
        else:
            group_ids_json = "[]"
        now_iso = _utc_iso()
        await self._init_db()
        try:
            async with aiosqlite.connect(self._db_path) as conn:
                cursor = await conn.cursor()
                await cursor.execute(
                    """INSERT INTO user_profiles
                       (user_id, nickname, group_ids, note, status,
                        violation_count, first_seen_at, last_seen_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        user_id,
                        data.get("nickname", "") or "",
                        group_ids_json,
                        data.get("note", "") or "",
                        data.get("status", "normal") or "normal",
                        int(data.get("violation_count", 0) or 0),
                        now_iso,
                        now_iso,
                        now_iso,
                    ),
                )
                await conn.commit()
                return True
        except aiosqlite.IntegrityError:
            return False

    async def update_user_profile(self, user_id: str, fields: dict) -> bool:
        """编辑用户档案（允许 nickname / note / status / group_ids）"""
        import json

        filtered: dict = {}
        for k, v in fields.items():
            if k not in ("nickname", "note", "status", "group_ids"):
                continue
            if k == "group_ids":
                if isinstance(v, list):
                    filtered[k] = json.dumps([str(x) for x in v], ensure_ascii=False)
                elif isinstance(v, str):
                    filtered[k] = v
                else:
                    continue
            else:
                filtered[k] = v
        if not filtered:
            return False
        filtered["updated_at"] = _utc_iso()
        await self._init_db()
        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            set_clause = ", ".join(f"{k} = ?" for k in filtered)
            values = list(filtered.values())
            values.append(user_id)
            await cursor.execute(
                f"UPDATE user_profiles SET {set_clause} WHERE user_id = ?", values
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def delete_user_profile(self, user_id: str) -> bool:
        """删除用户档案"""
        await self._init_db()
        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "DELETE FROM user_profiles WHERE user_id = ?", (user_id,)
            )
            await conn.commit()
            return cursor.rowcount > 0

    # ========== 账号白名单（v1.5.1 新增：QQ 账号维度，支持全局与群级） ==========

    async def check_account_whitelist(self, user_id: str, group_id: str = "") -> bool:
        """检查 QQ 账号是否在白名单中（全局条目或指定群条目命中即跳过审核）"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                """SELECT 1 FROM account_whitelist
                   WHERE user_id = ? AND (group_id = '' OR group_id = ?) LIMIT 1""",
                (user_id, group_id),
            )
            return (await cursor.fetchone()) is not None

    async def add_account_whitelist(
        self,
        user_id: str,
        group_id: str = "",
        added_by: str = None,
        note: str = None,
    ) -> bool:
        """添加账号白名单（group_id='' 表示全局；(user_id, group_id) 唯一）"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            try:
                await cursor.execute(
                    """INSERT INTO account_whitelist (user_id, group_id, added_by, note)
                       VALUES (?, ?, ?, ?)""",
                    (user_id, group_id or "", added_by, note),
                )
                await conn.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def remove_account_whitelist(
        self, user_id: str, group_id: str | None = None
    ) -> bool:
        """移除账号白名单

        Args:
            user_id: QQ 号
            group_id: None=移除该账号全部范围条目；''=仅全局条目；群号=仅该群条目
        """
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            if group_id is None:
                await cursor.execute(
                    "DELETE FROM account_whitelist WHERE user_id = ?", (user_id,)
                )
            else:
                await cursor.execute(
                    "DELETE FROM account_whitelist WHERE user_id = ? AND group_id = ?",
                    (user_id, group_id),
                )
            await conn.commit()
            return cursor.rowcount > 0

    async def get_account_whitelist_by_group(self, group_id: str = "") -> list[str]:
        """获取指定范围（默认全局）的白名单 QQ 号列表"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT user_id FROM account_whitelist WHERE group_id = ? ORDER BY created_at DESC",
                (group_id,),
            )
            return [row[0] for row in await cursor.fetchall()]

    async def list_account_whitelist_detailed(
        self,
        group_id_filter: str | None = None,
        sort_by: str | None = None,
        sort_dir: str | None = None,
    ) -> list[dict]:
        """账号白名单详细列表。group_id_filter: None=全部, 'global'=仅全局, 群号=该群"""
        await self._init_db()

        clauses: list[str] = []
        params: list = []
        if group_id_filter is not None:
            if group_id_filter == "global":
                clauses.append("group_id = ''")
            else:
                clauses.append("group_id = ?")
                params.append(group_id_filter)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_clause = _build_order_clause(
            sort_by, sort_dir, _LIST_SORT_FIELDS, "ORDER BY created_at DESC", "id"
        )
        async with aiosqlite.connect(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            await cursor.execute(
                f"SELECT * FROM account_whitelist {where} {order_clause}", params
            )
            return [dict(r) for r in await cursor.fetchall()]

    async def update_account_whitelist_note(self, wid: int, note: str) -> bool:
        """更新账号白名单备注"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "UPDATE account_whitelist SET note = ? WHERE id = ?", (note, wid)
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def delete_account_whitelist_by_id(self, wid: int) -> bool:
        """按 ID 删除账号白名单条目"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute("DELETE FROM account_whitelist WHERE id = ?", (wid,))
            await conn.commit()
            return cursor.rowcount > 0

    # ========== 成本计算（v1.5.4：模型定价 + LLM 用量记账 + 概览查询） ==========

    async def upsert_model_pricing(self, model_id: str, data: dict) -> bool:
        """插入或更新模型定价（按 model_id 唯一）"""
        await self._init_db()
        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                """INSERT INTO model_pricing (model_id, currency, price_per,
                   input_price, cached_price, output_price, label, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(model_id) DO UPDATE SET
                   currency=excluded.currency, price_per=excluded.price_per,
                   input_price=excluded.input_price, cached_price=excluded.cached_price,
                   output_price=excluded.output_price, label=excluded.label,
                   updated_at=excluded.updated_at""",
                (
                    model_id,
                    data.get("currency", "CNY"),
                    data.get("price_per", 1000000),
                    data.get("input_price", 0),
                    data.get("cached_price", 0),
                    data.get("output_price", 0),
                    data.get("label", ""),
                    _utc_iso(),
                ),
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def get_model_pricing(self, model_id: str) -> dict | None:
        """获取单个模型定价"""
        await self._init_db()
        async with aiosqlite.connect(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT * FROM model_pricing WHERE model_id = ?", (model_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def list_model_pricing(self) -> list[dict]:
        """列出全部模型定价"""
        await self._init_db()
        async with aiosqlite.connect(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            await cursor.execute("SELECT * FROM model_pricing ORDER BY updated_at DESC")
            return [dict(r) for r in await cursor.fetchall()]

    async def delete_model_pricing(self, model_id: str) -> bool:
        """删除某模型的定价"""
        await self._init_db()
        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "DELETE FROM model_pricing WHERE model_id = ?", (model_id,)
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def record_cost(
        self,
        model_id: str,
        currency: str,
        price_per: int,
        input_price: float,
        cached_price: float,
        output_price: float,
        input_other: int,
        input_cached: int,
        output: int,
    ) -> None:
        """记录一次 LLM 调用成本（写日志 + 累加到累计表）"""
        cost = (
            input_other * input_price
            + input_cached * cached_price
            + output * output_price
        ) / price_per
        now = _utc_iso()
        await self._init_db()
        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                """INSERT INTO llm_cost_log
                   (model_id, currency, price_per, input_other, input_cached, output, cost, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    model_id,
                    currency,
                    price_per,
                    input_other,
                    input_cached,
                    output,
                    cost,
                    now,
                ),
            )
            await cursor.execute(
                """INSERT INTO model_cost_total
                   (model_id, currency, total_input_other, total_input_cached,
                    total_output, total_cost, call_count, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT(model_id) DO UPDATE SET
                   currency=excluded.currency,
                   total_input_other=total_input_other+excluded.total_input_other,
                   total_input_cached=total_input_cached+excluded.total_input_cached,
                   total_output=total_output+excluded.total_output,
                   total_cost=total_cost+excluded.total_cost,
                   call_count=call_count+1,
                   updated_at=excluded.updated_at""",
                (model_id, currency, input_other, input_cached, output, cost, now),
            )
            await conn.commit()

    async def get_cost_overview(self) -> dict:
        """成本概览：各模型累计 + 按货币汇总 + 7 日费用趋势"""
        await self._init_db()
        empty = {
            "models": [],
            "total_by_currency": [],
            "trend_7days": [],
            "total_calls": 0,
        }
        try:
            async with aiosqlite.connect(self._db_path) as conn:
                conn.row_factory = aiosqlite.Row
                cursor = await conn.cursor()
                await cursor.execute(
                    "SELECT * FROM model_cost_total ORDER BY total_cost DESC"
                )
                models = [dict(r) for r in await cursor.fetchall()]
                total_calls = sum(m.get("call_count", 0) for m in models)
                # 标注每个模型是否已配置单价（供前端区分「未定价」模型，一键补价）
                await cursor.execute("SELECT model_id, label FROM model_pricing")
                priced_map = {
                    r["model_id"]: (r["label"] or "") for r in await cursor.fetchall()
                }
                for m in models:
                    mid = m.get("model_id")
                    m["priced"] = mid in priced_map
                    if priced_map.get(mid):
                        m["label"] = priced_map[mid]
                # 按货币汇总
                currency_totals: dict[str, float] = {}
                for m in models:
                    c = m.get("currency", "CNY")
                    currency_totals[c] = round(
                        currency_totals.get(c, 0) + m.get("total_cost", 0), 4
                    )
                total_by_currency = [
                    {"currency": k, "total_cost": v} for k, v in currency_totals.items()
                ]
                # 7 日趋势（按天汇总 llm_cost_log）
                today = date.today()
                start = (today - timedelta(days=6)).isoformat()
                await cursor.execute(
                    "SELECT date(created_at, '+8 hours') AS d, SUM(cost) AS c FROM llm_cost_log "
                    "WHERE date(created_at, '+8 hours') >= date(?) GROUP BY d",
                    (start,),
                )
                cost_map = {
                    r["d"]: round(r["c"] or 0, 4) for r in await cursor.fetchall()
                }
                trend = []
                for i in range(6, -1, -1):
                    d_str = (today - timedelta(days=i)).isoformat()
                    trend.append({"date": d_str, "cost": cost_map.get(d_str, 0)})
                return {
                    "models": models,
                    "total_by_currency": total_by_currency,
                    "trend_7days": trend,
                    "total_calls": total_calls,
                }
        except Exception:
            return empty

    async def cleanup_cost_log(self, keep_days: int = 30) -> int:
        """清理过期的 LLM 成本日志（累计表不受影响）"""
        import logging

        logger = logging.getLogger(__name__)
        await self._init_db()
        try:
            cutoff = (_utc_now() - timedelta(days=keep_days)).isoformat()
            async with aiosqlite.connect(self._db_path) as conn:
                cursor = await conn.cursor()
                await cursor.execute(
                    "DELETE FROM llm_cost_log WHERE created_at < ?", (cutoff,)
                )
                await conn.commit()
                return cursor.rowcount
        except Exception as e:
            logger.error(f"清理成本日志失败: {e}")
            return 0

    # ========== WebUI：概览统计 ==========

    async def get_overview_stats(self) -> dict:
        """Dashboard 概览数据：今日/累计统计、风险等级分布、7日趋势、Top 违规用户、群分布"""
        import logging

        logger = logging.getLogger(__name__)
        empty = {
            "today_audits": 0,
            "today_violations": 0,
            "total_audits": 0,
            "total_violations": 0,
            "risk_distribution": {"pass": 0, "review": 0, "block": 0},
            "whitelist_count": 0,
            "blacklist_count": 0,
            "user_profiles_count": 0,
            "trend_7days": [],
            "top_violators": [],
            "group_distribution": [],
        }
        await self._init_db()
        try:
            today_str = date.today().isoformat()
            async with aiosqlite.connect(self._db_path) as conn:
                conn.row_factory = aiosqlite.Row
                cursor = await conn.cursor()

                async def _scalar(sql: str, params: tuple = ()) -> int:
                    row = await (await cursor.execute(sql, params)).fetchone()
                    return row[0] if row and row[0] is not None else 0

                today_audits = await _scalar(
                    "SELECT COUNT(*) FROM audit_log WHERE date(created_at, '+8 hours') >= date(?)",
                    (today_str,),
                )
                today_violations = await _scalar(
                    "SELECT COUNT(*) FROM violation_records "
                    "WHERE date(violation_time, '+8 hours') >= date(?)",
                    (today_str,),
                )
                total_audits = await _scalar("SELECT COUNT(*) FROM audit_log")
                total_violations = await _scalar(
                    "SELECT COUNT(*) FROM violation_records"
                )

                # 风险等级分布（违规记录维度）
                risk_distribution = {"pass": 0, "review": 0, "block": 0}
                await cursor.execute(
                    "SELECT risk_level, COUNT(*) FROM violation_records GROUP BY risk_level"
                )
                for row in await cursor.fetchall():
                    try:
                        name = RiskLevel(row[0]).name.lower()
                    except ValueError:
                        continue
                    if name in risk_distribution:
                        risk_distribution[name] = row[1]

                whitelist_count = await _scalar("SELECT COUNT(*) FROM manual_whitelist")
                blacklist_count = await _scalar("SELECT COUNT(*) FROM manual_blacklist")
                user_profiles_count = await _scalar(
                    "SELECT COUNT(*) FROM user_profiles"
                )

                # 7 日趋势（2 次 GROUP BY 查询代替逐天查询）
                today = date.today()
                start_str = (today - timedelta(days=6)).isoformat()
                await cursor.execute(
                    "SELECT date(created_at, '+8 hours') AS d, COUNT(*) AS c FROM audit_log "
                    "WHERE date(created_at, '+8 hours') >= date(?) GROUP BY d",
                    (start_str,),
                )
                audit_counts = {r["d"]: r["c"] for r in await cursor.fetchall()}
                await cursor.execute(
                    "SELECT date(violation_time, '+8 hours') AS d, COUNT(*) AS c FROM violation_records "
                    "WHERE date(violation_time, '+8 hours') >= date(?) GROUP BY d",
                    (start_str,),
                )
                viol_counts = {r["d"]: r["c"] for r in await cursor.fetchall()}
                trend: list[dict] = []
                for i in range(6, -1, -1):
                    d_str = (today - timedelta(days=i)).isoformat()
                    trend.append(
                        {
                            "date": d_str,
                            "audits": audit_counts.get(d_str, 0),
                            "violations": viol_counts.get(d_str, 0),
                        }
                    )

                # Top 10 违规用户
                await cursor.execute(
                    """SELECT user_id, nickname, violation_count FROM user_profiles
                       WHERE violation_count > 0
                       ORDER BY violation_count DESC LIMIT 10"""
                )
                top_violators = [dict(r) for r in await cursor.fetchall()]

                # 群分布 Top 10（按审核数）
                await cursor.execute(
                    """SELECT group_id,
                              COUNT(*) AS audits,
                              COALESCE((
                                  SELECT COUNT(*) FROM violation_records v
                                  WHERE v.group_id = audit_log.group_id
                              ), 0) AS violations
                       FROM audit_log
                       WHERE group_id IS NOT NULL
                       GROUP BY group_id
                       ORDER BY audits DESC LIMIT 10"""
                )
                group_distribution = [dict(r) for r in await cursor.fetchall()]

                return {
                    "today_audits": today_audits,
                    "today_violations": today_violations,
                    "total_audits": total_audits,
                    "total_violations": total_violations,
                    "risk_distribution": risk_distribution,
                    "whitelist_count": whitelist_count,
                    "blacklist_count": blacklist_count,
                    "user_profiles_count": user_profiles_count,
                    "trend_7days": trend,
                    "top_violators": top_violators,
                    "group_distribution": group_distribution,
                }
        except Exception as e:
            logger.error(f"get_overview_stats 失败: {e}")
            return empty

    # ========== 插件运行设置（v1.6.0：保留策略等可在 WebUI 自定义，存 DB 不依赖配置文件） ==========

    _RETENTION_DEFAULT = 30
    _RETENTION_MIN = 1
    _RETENTION_MAX = 3650

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        """读取单个运行设置（key-value，存 plugin_settings 表）"""
        await self._init_db()
        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT value FROM plugin_settings WHERE key = ?", (key,)
            )
            row = await cursor.fetchone()
            return row[0] if row else default

    async def set_setting(self, key: str, value: str) -> bool:
        """写入单个运行设置（upsert）"""
        await self._init_db()
        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                """INSERT INTO plugin_settings (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                   value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, _utc_iso()),
            )
            await conn.commit()
            return cursor.rowcount > 0

    def _clamp_retention(self, value) -> int:
        """把保留天数夹紧到合法区间，非法值回退默认"""
        try:
            v = int(value) if value is not None else self._RETENTION_DEFAULT
        except (TypeError, ValueError):
            v = self._RETENTION_DEFAULT
        return max(self._RETENTION_MIN, min(self._RETENTION_MAX, v))

    async def get_retention_settings(self) -> dict:
        """读取日志保留天数（审核日志 / 成本日志），缺失或非法回退默认"""
        audit = self._clamp_retention(
            await self.get_setting("audit_log_retention_days")
        )
        cost = self._clamp_retention(
            await self.get_setting("cost_log_retention_days")
        )
        return {
            "audit_log_retention_days": audit,
            "cost_log_retention_days": cost,
        }

    async def set_retention_settings(self, audit_days, cost_days) -> dict:
        """写入日志保留天数（夹紧范围），返回实际生效值"""
        a = self._clamp_retention(audit_days)
        c = self._clamp_retention(cost_days)
        await self.set_setting("audit_log_retention_days", str(a))
        await self.set_setting("cost_log_retention_days", str(c))
        return {"audit_log_retention_days": a, "cost_log_retention_days": c}

    async def get_database_stats(self) -> dict:
        """Dashboard 概览用：数据库本身的健康/体量统计（各表行数、文件体积、时间跨度、保留策略）"""
        import logging

        logger = logging.getLogger(__name__)
        # 已知表（顺序即展示顺序）；逐表 COUNT 容错，缺表不影响其余统计
        table_names = [
            "audit_log",
            "violation_records",
            "user_profiles",
            "user_violation_stats",
            "image_hashes",
            "manual_whitelist",
            "manual_blacklist",
            "account_whitelist",
            "whitelist",
            "blacklist",
            "model_pricing",
            "llm_cost_log",
            "model_cost_total",
        ]
        empty = {
            "tables": [],
            "table_count": 0,
            "total_rows": 0,
            "db_path": self._db_path,
            "db_size_bytes": 0,
            "audit_oldest": None,
            "audit_newest": None,
            "cost_log_oldest": None,
            "cost_log_newest": None,
            "audit_retention_days": self._RETENTION_DEFAULT,
            "cost_log_retention_days": self._RETENTION_DEFAULT,
        }
        await self._init_db()
        try:
            tables: list[dict] = []
            total_rows = 0
            retention = await self.get_retention_settings()
            async with aiosqlite.connect(self._db_path) as conn:
                cursor = await conn.cursor()
                for name in table_names:
                    try:
                        row = await (
                            await cursor.execute(
                                f"SELECT COUNT(*) FROM {name}"  # noqa: S608  # 表名来自固定白名单
                            )
                        ).fetchone()
                        cnt = row[0] if row and row[0] is not None else 0
                    except aiosqlite.OperationalError:
                        continue  # 表不存在（旧库 / 未启用功能）跳过
                    tables.append({"name": name, "rows": cnt})
                    total_rows += cnt

                async def _range(table: str, col: str) -> tuple:
                    try:
                        r = await (
                            await cursor.execute(
                                f"SELECT MIN({col}), MAX({col}) FROM {table}"  # noqa: S608
                            )
                        ).fetchone()
                        return (r[0], r[1]) if r else (None, None)
                    except aiosqlite.OperationalError:
                        return (None, None)

                audit_oldest, audit_newest = await _range("audit_log", "created_at")
                cost_oldest, cost_newest = await _range("llm_cost_log", "created_at")

            # 数据库文件体积（含 -wal/-shm 旁路文件，反映真实磁盘占用）
            db_size = 0
            try:
                if self._db_path and os.path.exists(self._db_path):
                    db_size = os.path.getsize(self._db_path)
                    for suffix in ("-wal", "-shm"):
                        side = self._db_path + suffix
                        if os.path.exists(side):
                            db_size += os.path.getsize(side)
            except OSError as e:
                logger.debug(f"读取数据库文件体积失败: {e}")

            return {
                "tables": tables,
                "table_count": len(tables),
                "total_rows": total_rows,
                "db_path": self._db_path,
                "db_size_bytes": db_size,
                "audit_oldest": audit_oldest,
                "audit_newest": audit_newest,
                "cost_log_oldest": cost_oldest,
                "cost_log_newest": cost_newest,
                "audit_retention_days": retention["audit_log_retention_days"],
                "cost_log_retention_days": retention["cost_log_retention_days"],
            }
        except Exception as e:
            logger.error(f"get_database_stats 失败: {e}")
            return empty

    async def get_group_violation_stats(self, group_id: str) -> dict:
        """单个群的违规统计（/审核状态 指令与 WebUI 共用）"""
        await self._init_db()
        today_str = date.today().isoformat()
        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            total = (
                await (
                    await cursor.execute(
                        "SELECT COUNT(*) FROM violation_records WHERE group_id = ?",
                        (group_id,),
                    )
                ).fetchone()
            )[0]
            today = (
                await (
                    await cursor.execute(
                        "SELECT COUNT(*) FROM violation_records "
                        "WHERE group_id = ? AND date(violation_time, '+8 hours') >= date(?)",
                        (group_id, today_str),
                    )
                ).fetchone()
            )[0]
            today_audits = (
                await (
                    await cursor.execute(
                        "SELECT COUNT(*) FROM audit_log "
                        "WHERE group_id = ? AND date(created_at, '+8 hours') >= date(?)",
                        (group_id, today_str),
                    )
                ).fetchone()
            )[0]
            return {
                "total_violations": total,
                "today_violations": today,
                "today_audits": today_audits,
            }

    # ========== 证据图片回填（v1.5.0） ==========

    @staticmethod
    def _index_evidence_files(
        evidence_dir: str,
    ) -> tuple[dict[tuple[str, str, str], str], dict[str, str]]:
        """扫描证据目录建立两级索引（供回填与运行时兜底共用）

        证据文件命名格式: {group}_{user}_{timestamp}_{md5前8位}.ext
        （timestamp 形如 20260101_120000，内部含下划线，故 md5 前缀取末段）

        Returns:
            (精确索引 (group, user, md5前缀) -> 路径, 前缀索引 md5前缀 -> 路径)
        """
        exact_map: dict[tuple[str, str, str], str] = {}
        prefix_map: dict[str, str] = {}
        try:
            names = os.listdir(evidence_dir)
        except OSError:
            return exact_map, prefix_map
        for fname in names:
            base = os.path.splitext(fname)[0]
            parts = base.rsplit("_", 1)
            if len(parts) != 2 or len(parts[1]) != 8:
                continue
            prefix = parts[1].lower()
            full = os.path.join(evidence_dir, fname)
            # 群号/用户ID 为纯数字，文件名前两段即 group/user
            head = parts[0].split("_")
            if len(head) >= 2:
                exact_map.setdefault((head[0], head[1], prefix), full)
            prefix_map.setdefault(prefix, full)
        return exact_map, prefix_map

    async def backfill_evidence_paths(self, evidence_dir: str) -> int:
        """尽力回填旧违规记录的证据图片路径（幂等）。

        两级匹配：优先 {group}_{user}_*_{md5前8位} 精确命中，
        其次仅 md5 前缀命中（同图多次违规时可能复用同一证据，属预期兜底）。
        仅处理 evidence_path 为空的记录。
        """
        import logging

        logger = logging.getLogger(__name__)
        if not os.path.isdir(evidence_dir):
            return 0
        await self._init_db()

        exact_map, prefix_map = self._index_evidence_files(evidence_dir)
        if not prefix_map:
            return 0

        filled = 0
        try:
            async with aiosqlite.connect(self._db_path) as conn:
                cursor = await conn.cursor()
                await cursor.execute(
                    "SELECT id, group_id, user_id, md5_hash FROM violation_records "
                    "WHERE evidence_path IS NULL OR evidence_path = ''"
                )
                rows = await cursor.fetchall()
                for row_id, row_group, row_user, md5_hash in rows:
                    if not md5_hash:
                        continue
                    prefix = md5_hash[:8].lower()
                    path = exact_map.get(
                        (str(row_group or ""), str(row_user or ""), prefix)
                    ) or prefix_map.get(prefix)
                    if path and os.path.isfile(path):
                        await cursor.execute(
                            "UPDATE violation_records SET evidence_path = ? WHERE id = ?",
                            (path, row_id),
                        )
                        filled += 1
                await conn.commit()
            if filled:
                logger.info(f"证据图片路径回填完成: {filled} 条记录")
        except Exception as e:
            logger.error(f"证据图片回填失败: {e}")
        return filled

    async def set_violation_evidence_path(self, vid: int, path: str) -> None:
        """写回运行时解析出的证据路径（懒回填，WebUI 图片兜底查找后调用）"""
        await self._init_db()
        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "UPDATE violation_records SET evidence_path = ? WHERE id = ?",
                (path, vid),
            )
            await conn.commit()

    async def get_hashes_for_md5s(self, md5s: list[str]) -> dict[str, dict]:
        """按 md5 批量查询 image_hashes 中的感知哈希（旧违规记录回退展示用）

        仅返回存在且 phash/dhash 至少其一非空的条目。

        Returns:
            {md5_hash: {"phash": str|None, "dhash": str|None}}
        """
        if not md5s:
            return {}
        await self._init_db()
        result: dict[str, dict] = {}
        async with aiosqlite.connect(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.cursor()
            placeholders = ",".join("?" for _ in md5s)
            await cursor.execute(
                f"""SELECT md5_hash, phash, dhash FROM image_hashes
                    WHERE md5_hash IN ({placeholders})
                      AND (phash IS NOT NULL OR dhash IS NOT NULL)""",
                md5s,
            )
            for row in await cursor.fetchall():
                result[row["md5_hash"]] = {
                    "phash": row["phash"],
                    "dhash": row["dhash"],
                }
        return result
