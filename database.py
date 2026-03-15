"""
数据库管理模块
用于管理黑白名单、违规记录等数据的持久化存储
"""

import hashlib
import os
from datetime import datetime, timedelta
from enum import Enum

import aiosqlite


class RiskLevel(Enum):
    """风险等级枚举"""

    Pass = 0
    Review = 1
    Block = 2


class DatabaseManager:
    """数据库管理器"""

    def __init__(self, data_dir: str):
        """
        初始化数据库管理器

        Args:
            data_dir: 数据存储目录
        """
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
                    message_id TEXT
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

            await conn.commit()
            logger.debug("数据库表结构初始化完成")
        self._initialized = True

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

    async def check_whitelist(self, md5_hash: str) -> bool:
        """
        检查MD5是否在白名单中

        Args:
            md5_hash: MD5哈希值

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
            if expires_at and datetime.now() > datetime.fromisoformat(expires_at):
                # 过期删除
                logger.debug(f"白名单记录已过期，删除记录，ID: {record_id}")
                await cursor.execute("DELETE FROM whitelist WHERE id = ?", (record_id,))
                await conn.commit()
                return False

            # 更新命中次数
            new_hit_count = hit_count + 1
            logger.debug(
                f"更新白名单命中次数，ID: {record_id}, 旧次数: {hit_count}, 新次数: {new_hit_count}"
            )
            await cursor.execute(
                "UPDATE whitelist SET hit_count = ? WHERE id = ?",
                (new_hit_count, record_id),
            )
            await conn.commit()
            logger.debug(f"白名单检查通过，MD5: {md5_hash}")
            return True

    async def check_blacklist(self, md5_hash: str) -> tuple[RiskLevel, str] | None:
        """
        检查MD5是否在黑名单中

        Args:
            md5_hash: MD5哈希值

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
            if expires_at and datetime.now() > datetime.fromisoformat(expires_at):
                # 过期删除
                logger.debug(f"黑名单记录已过期，删除记录，ID: {record_id}")
                await cursor.execute("DELETE FROM blacklist WHERE id = ?", (record_id,))
                await conn.commit()
                return None

            # 更新命中次数
            new_hit_count = hit_count + 1
            logger.debug(
                f"更新黑名单命中次数，ID: {record_id}, 旧次数: {hit_count}, 新次数: {new_hit_count}"
            )
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
                expire_hours = min(
                    int(
                        base_expire_hours * (1.5 ** min(hit_count, 10))
                    ),  # 限制最大10次翻倍
                    max_expire_days * 24,
                )
                logger.debug(f"延长过期时间: {expire_hours}小时")
            else:
                expire_hours = base_expire_hours
                logger.debug(f"白名单中不存在，设置基础过期时间: {expire_hours}小时")

            expires_at = datetime.now() + timedelta(hours=expire_hours)
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
                expire_hours = min(
                    int(
                        base_expire_hours * (1.5 ** min(hit_count, 10))
                    ),  # 限制最大10次翻倍
                    max_expire_days * 24,
                )
                logger.debug(f"延长过期时间: {expire_hours}小时")
            else:
                expire_hours = base_expire_hours
                logger.debug(f"黑名单中不存在，设置基础过期时间: {expire_hours}小时")

            expires_at = datetime.now() + timedelta(hours=expire_hours)
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
                   (user_id, group_id, md5_hash, image_url, risk_level, risk_reason, mute_duration, message_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    group_id,
                    md5_hash,
                    image_url,
                    risk_level.value,
                    risk_reason,
                    mute_duration,
                    message_id,
                ),
            )
            logger.debug("违规记录插入完成")

            # 更新用户违规统计
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
            now = datetime.now().isoformat()
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
                expire_hours = min(
                    int(base_expire_hours * (1.5 ** min(hit_count, 10))),
                    max_expire_days * 24,
                )
            else:
                expire_hours = base_expire_hours

            expires_at = datetime.now() + timedelta(hours=expire_hours)
            risk_level_value = risk_level.value if risk_level else None

            await cursor.execute(
                """INSERT OR REPLACE INTO image_hashes
                   (md5_hash, phash, dhash, risk_level, risk_reason, expires_at, hit_count)
                   VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT hit_count FROM image_hashes WHERE md5_hash = ?), 0))""",
                (
                    md5_hash,
                    phash,
                    dhash,
                    risk_level_value,
                    risk_reason,
                    expires_at.isoformat(),
                    md5_hash,
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
            now = datetime.now().isoformat()
            hash_column = "phash" if hash_type == "phash" else "dhash"

            await cursor.execute(
                f"""SELECT md5_hash, {hash_column} as hash_value, risk_level, risk_reason, expires_at
                    FROM image_hashes
                    WHERE {hash_column} IS NOT NULL AND expires_at > ?""",
                (now,),
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

    # ========== 人工白名单管理 ==========

    async def check_manual_whitelist(self, md5_hash: str) -> bool:
        """检查MD5是否在人工白名单中"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT id FROM manual_whitelist WHERE md5_hash = ?", (md5_hash,)
            )
            result = await cursor.fetchone()
            return result is not None

    async def add_manual_whitelist(
        self, md5_hash: str, added_by: str = None, reason: str = None
    ) -> bool:
        """添加到人工白名单"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            try:
                await cursor.execute(
                    "INSERT INTO manual_whitelist (md5_hash, added_by, reason) VALUES (?, ?, ?)",
                    (md5_hash, added_by, reason),
                )
                await conn.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def remove_manual_whitelist(self, md5_hash: str) -> bool:
        """从人工白名单移除"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "DELETE FROM manual_whitelist WHERE md5_hash = ?", (md5_hash,)
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def clear_all_manual_whitelist(self) -> int:
        """清空人工白名单"""
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

    # ========== 人工黑名单管理 ==========

    async def check_manual_blacklist(
        self, md5_hash: str
    ) -> tuple[RiskLevel, str] | None:
        """检查MD5是否在人工黑名单中"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT risk_level, risk_reason FROM manual_blacklist WHERE md5_hash = ?",
                (md5_hash,),
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
    ) -> bool:
        """添加到人工黑名单"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            try:
                await cursor.execute(
                    """INSERT INTO manual_blacklist
                       (md5_hash, risk_level, risk_reason, added_by, reason)
                       VALUES (?, ?, ?, ?, ?)""",
                    (md5_hash, risk_level.value, risk_reason, added_by, reason),
                )
                await conn.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def remove_manual_blacklist(self, md5_hash: str) -> bool:
        """从人工黑名单移除"""
        await self._init_db()

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "DELETE FROM manual_blacklist WHERE md5_hash = ?", (md5_hash,)
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def clear_all_manual_blacklist(self) -> int:
        """清空人工黑名单"""
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
