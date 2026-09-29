"""SQLite 存储层。

所有采集结果统一入一个库，方便跨任务检索和增量去重。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from .models import Comment, Item, Task

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT,
    mode           TEXT,
    platform       TEXT,
    query          TEXT,
    max_count      INTEGER,
    fetch_comments INTEGER,
    do_download    INTEGER,
    do_transcribe  INTEGER,
    output_dir     TEXT,
    created_at     REAL
);

CREATE TABLE IF NOT EXISTS items (
    platform      TEXT,
    item_id       TEXT,
    title         TEXT,
    author        TEXT,
    author_id     TEXT,
    url           TEXT,
    duration      INTEGER,
    view_count    INTEGER,
    like_count    INTEGER,
    comment_count INTEGER,
    publish_time  TEXT,
    desc          TEXT,
    cover         TEXT,
    status        TEXT,
    selected      INTEGER,
    local_path    TEXT,
    transcript    TEXT,
    segments      TEXT,
    asr_engine    TEXT,
    error         TEXT,
    created_at    REAL,
    updated_at    REAL,
    PRIMARY KEY (platform, item_id)
);

CREATE TABLE IF NOT EXISTS comments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    platform     TEXT,
    item_id      TEXT,
    user         TEXT,
    content      TEXT,
    like_count   INTEGER,
    publish_time TEXT,
    created_at   REAL
);

CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_comments_item ON comments(platform, item_id);
"""

# 后加的列：老库不会自动带上，启动时补齐
EXTRA_COLUMNS = {
    "stream_url": "TEXT", "warning": "TEXT", "media_kind": "TEXT",
    # 每个作品单独设的评论抓取条数；-1 / NULL 表示跟随任务级默认值
    "comment_limit": "INTEGER",
    # 图文作品专用：图片列表 JSON / 图片张数 / 平台原生话题标签
    "images": "TEXT", "image_count": "INTEGER", "tags": "TEXT",
}


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    for name, ddl in EXTRA_COLUMNS.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE items ADD COLUMN {name} {ddl}")


class Database:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # ⚠️ check_same_thread=False 之后就必须自己加锁。
        # 之前没加：处理线程还在写库时导出线程已经开始读同一个连接，
        # 结果是未定义行为 —— 实测直接把进程搞成段错误（Segmentation fault）。
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            _migrate(self.conn)
            self.conn.commit()

    # 所有 SQL 都走这两个包装方法，保证线程安全
    def execute(self, sql: str, args: Any = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, args)

    def executemany(self, sql: str, seq: Any) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.executemany(sql, seq)

    def commit(self) -> None:
        with self.lock:
            self.conn.commit()

    # ---------- tasks ----------
    def save_task(self, task: Task) -> int:
        cur = self.execute(
            """INSERT INTO tasks
               (name, mode, platform, query, max_count, fetch_comments,
                do_download, do_transcribe, output_dir, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                task.name, task.mode, task.platform, task.query, task.max_count,
                int(task.fetch_comments), int(task.do_download),
                int(task.do_transcribe), task.output_dir, task.created_at,
            ),
        )
        self.commit()
        return int(cur.lastrowid or 0)

    # ---------- items ----------
    def upsert_items(self, items: Iterable[Item]) -> int:
        """写入作品。已存在的只更新统计字段，保留用户的勾选和转录结果。"""
        count = 0
        for it in items:
            self.execute(
                """INSERT INTO items (platform,item_id,title,author,author_id,url,duration,
                       view_count,like_count,comment_count,publish_time,desc,cover,status,
                       selected,local_path,stream_url,transcript,segments,asr_engine,error,
                       warning,media_kind,images,image_count,tags,comment_limit,
                       created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(platform,item_id) DO UPDATE SET
                       title=excluded.title,
                       view_count=excluded.view_count,
                       like_count=excluded.like_count,
                       comment_count=excluded.comment_count,
                       stream_url=excluded.stream_url,
                       -- 图文内容来自详情接口，抓到就更新；抓不到（空串）时
                       -- 保留库里已有的，别把之前抓到的好数据覆盖成空
                       desc=CASE WHEN excluded.desc != '' THEN excluded.desc
                                 ELSE items.desc END,
                       cover=CASE WHEN excluded.cover != '' THEN excluded.cover
                                  ELSE items.cover END,
                       images=CASE WHEN excluded.images != '' THEN excluded.images
                                   ELSE items.images END,
                       image_count=CASE WHEN excluded.image_count > 0
                                        THEN excluded.image_count
                                        ELSE items.image_count END,
                       tags=CASE WHEN excluded.tags != '' THEN excluded.tags
                                 ELSE items.tags END,
                       updated_at=excluded.updated_at""",
                (
                    it.platform, it.item_id, it.title, it.author, it.author_id, it.url,
                    it.duration, it.view_count, it.like_count, it.comment_count,
                    it.publish_time, it.desc, it.cover, it.status, int(it.selected),
                    it.local_path, it.stream_url, it.transcript, it.segments,
                    it.asr_engine, it.error, it.warning, it.media_kind,
                    it.images, int(it.image_count), it.tags,
                    int(it.comment_limit), it.created_at, it.updated_at,
                ),
            )
            count += 1
        self.commit()
        return count

    def get_items(self, platform: str | None = None, status: str | None = None) -> list[Item]:
        sql = "SELECT * FROM items"
        conds, args = [], []
        if platform:
            conds.append("platform=?")
            args.append(platform)
        if status:
            conds.append("status=?")
            args.append(status)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC"
        rows = self.execute(sql, args).fetchall()
        return [Item.from_row(r) for r in rows]

    def set_selected(self, platform: str, item_id: str, selected: bool) -> None:
        self.execute(
            "UPDATE items SET selected=?, status=?, updated_at=? WHERE platform=? AND item_id=?",
            (int(selected), "queued" if selected else "skipped", __import__("time").time(),
             platform, item_id),
        )
        self.commit()

    # ---------- 历史记录（界面「历史」页用） ----------
    def list_tasks(self, limit: int = 200) -> list[dict[str, Any]]:
        """历史任务，附带每个任务的作品数 / 已完成数。"""
        rows = self.execute(
            """SELECT t.*,
                      (SELECT COUNT(*) FROM items i
                        WHERE i.platform = t.platform) AS item_count,
                      (SELECT COUNT(*) FROM items i
                        WHERE i.platform = t.platform AND i.status='done') AS done_count
                 FROM tasks t
                ORDER BY t.created_at DESC
                LIMIT ?""",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_transcripts(self, limit: int = 300,
                         platform: str | None = None,
                         keyword: str = "") -> list[dict[str, Any]]:
        """历史记录：转出过文字的条目 + 采集到的图文作品。

        带上字符数与所用引擎，方便用户判断「这条是什么时候、用什么模型转的」。

        ⚠️ 过滤条件不能只判 transcript：
        图文作品**没有逐字稿**，正文在 desc 里。以前条件是
        `WHERE transcript <> ''`，图文作品全被挡在外面，历史页
        根本看不到它们（用户会以为没采集到）。
        改成「有逐字稿 或 有正文/图片」都算有效记录。
        """
        sql = ("SELECT platform, item_id, title, author, duration, url, "
                      "local_path, asr_engine, status, updated_at, "
                      "LENGTH(COALESCE(transcript,'')) AS words, error, "
                      "desc, media_kind, images, image_count, tags, cover "
                 "FROM items "
                "WHERE COALESCE(transcript,'') <> '' "
                   "OR (media_kind = 'image' AND ("
                       "COALESCE(desc,'') <> '' OR COALESCE(images,'') <> '' "
                       "OR COALESCE(image_count,0) > 0))")
        args: list[Any] = []
        if platform:
            sql += " AND platform=?"
            args.append(platform)
        if keyword:
            # 也搜 desc / tags：图文作品的正文和话题都在这两列里，
            # 不带上就搜不到图文内容
            sql += (" AND (title LIKE ? OR author LIKE ? OR transcript LIKE ? "
                    "OR COALESCE(desc,'') LIKE ? OR COALESCE(tags,'') LIKE ?)")
            kw = f"%{keyword}%"
            args += [kw, kw, kw, kw, kw]
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in self.execute(sql, args).fetchall()]

    def get_transcript(self, platform: str, item_id: str) -> dict[str, Any] | None:
        """取**单条**记录的完整逐字稿正文。

        ⚠️ 为什么不复用 list_transcripts：
        那个方法为了列表速度只 SELECT 了 `LENGTH(transcript)`（字数），
        根本没有正文。历史页要做「就地预览全文」，必须单独把 transcript
        取回来。一条一条按需取，比把 500 条正文全塞进内存划算得多。
        """
        row = self.execute(
            "SELECT platform, item_id, title, author, duration, url, "
            "       local_path, asr_engine, status, updated_at, error, "
            "       desc, media_kind, images, image_count, tags, cover, "
            "       COALESCE(transcript,'') AS transcript "
            "  FROM items WHERE platform=? AND item_id=?",
            (platform, item_id)).fetchone()
        return dict(row) if row else None

    def stats(self) -> dict[str, int]:
        """总览数字，给「历史」页顶部的概览卡用。"""
        q = lambda sql: int(self.execute(sql).fetchone()[0] or 0)  # noqa: E731
        return {
            "items": q("SELECT COUNT(*) FROM items"),
            "done": q("SELECT COUNT(*) FROM items WHERE status='done'"),
            "transcripts": q(
                "SELECT COUNT(*) FROM items WHERE COALESCE(transcript,'') <> ''"),
            "comments": q("SELECT COUNT(*) FROM comments"),
            "tasks": q("SELECT COUNT(*) FROM tasks"),
            "words": q("SELECT SUM(LENGTH(COALESCE(transcript,''))) FROM items"),
            "failed": q("SELECT COUNT(*) FROM items WHERE status='failed'"),
            "media": q("SELECT COUNT(*) FROM items "
                       "WHERE COALESCE(local_path,'') <> ''"),
        }

    def duration_stats(self) -> dict[str, float]:
        """时长统计（秒），历史页用来显示「一共转了多少小时的素材」。"""
        row = self.execute(
            "SELECT SUM(COALESCE(duration,0)), AVG(COALESCE(duration,0)) "
            "FROM items WHERE COALESCE(transcript,'') <> ''").fetchone()
        return {"total_sec": float(row[0] or 0), "avg_sec": float(row[1] or 0)}

    def platform_breakdown(self) -> list[dict[str, Any]]:
        """按平台统计：各平台采集了多少、转了多少、多少字。"""
        rows = self.execute(
            """SELECT platform,
                      COUNT(*) AS items,
                      SUM(CASE WHEN COALESCE(transcript,'') <> '' THEN 1 ELSE 0 END)
                          AS transcripts,
                      SUM(LENGTH(COALESCE(transcript,''))) AS words,
                      SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
                 FROM items GROUP BY platform
                ORDER BY items DESC""").fetchall()
        return [dict(r) for r in rows]

    def recent_activity(self, limit: int = 30) -> list[dict[str, Any]]:
        """最近有变动的条目（按 updated_at 倒序），给历史页做「最近动态」时间线。"""
        rows = self.execute(
            """SELECT platform, item_id, title, author, status, updated_at,
                      COALESCE(local_path,'') AS local_path,
                      LENGTH(COALESCE(transcript,'')) AS words
                 FROM items
                WHERE status IN ('done','failed','transcribing','downloading')
                ORDER BY updated_at DESC LIMIT ?""",
            (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def update_item(self, platform: str, item_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = __import__("time").time()
        cols = ", ".join(f"{k}=?" for k in fields)
        args = list(fields.values()) + [platform, item_id]
        self.execute(f"UPDATE items SET {cols} WHERE platform=? AND item_id=?", args)
        self.commit()

    # ---------- comments ----------
    def save_comments(self, platform: str, comments: Iterable[Comment]) -> int:
        count = 0
        for c in comments:
            self.execute(
                """INSERT INTO comments (platform,item_id,user,content,like_count,publish_time,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (platform, c.item_id, c.user, c.content, c.like_count, c.publish_time, c.created_at),
            )
            count += 1
        self.commit()
        return count

    def get_comments(self, platform: str, item_id: str) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT * FROM comments WHERE platform=? AND item_id=? ORDER BY like_count DESC",
            (platform, item_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self.lock:
            self.conn.close()
