# db.py
# SQLite 기반 KPI 데이터 누적 저장소
#
# 정책: B (누적 + 최신 우선)
# - 모든 업로드는 upload_log에 기록되고, 행은 upload_id를 참조
# - 같은 관리번호(생산계획) / 날짜(근무시간)는 최신 업로드 행이 사용됨
# - 비활성화(is_active=0) 또는 영구삭제(DELETE)로 롤백 가능
#
# 향후 팀 추가 시: team 컬럼만 늘리면 됨 (production / quality / tech / purchase)

import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

DB_PATH = Path("data") / "kpi.db"
UPLOADS_DIR = Path("data") / "uploads"

SCHEMA = """
CREATE TABLE IF NOT EXISTS upload_log (
    upload_id INTEGER PRIMARY KEY AUTOINCREMENT,
    team TEXT NOT NULL,
    filename TEXT,
    uploaded_at TEXT DEFAULT (datetime('now', 'localtime')),
    is_active INTEGER DEFAULT 1,
    plan_rows INTEGER DEFAULT 0,
    hours_rows INTEGER DEFAULT 0,
    note TEXT
);

CREATE TABLE IF NOT EXISTS production_plan (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id INTEGER NOT NULL,
    관리번호 TEXT,
    품목코드 TEXT,
    색상 TEXT,
    단품명칭 TEXT,
    생산라인 TEXT,
    브랜드 TEXT,
    계획량 REAL,
    생산량 REAL,
    입고단가 REAL,
    계획금액 REAL,
    실적금액 REAL,
    최초포장계획일 TEXT,
    포장계획일 TEXT,
    FOREIGN KEY (upload_id) REFERENCES upload_log(upload_id)
);

CREATE TABLE IF NOT EXISTS production_hours (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id INTEGER NOT NULL,
    날짜 TEXT NOT NULL,
    근무시간 REAL,
    이월수량 REAL,
    이월금액 REAL,
    FOREIGN KEY (upload_id) REFERENCES upload_log(upload_id)
);

CREATE INDEX IF NOT EXISTS idx_plan_init_date ON production_plan(최초포장계획일);
CREATE INDEX IF NOT EXISTS idx_plan_pack_date ON production_plan(포장계획일);
CREATE INDEX IF NOT EXISTS idx_plan_upload  ON production_plan(upload_id);
CREATE INDEX IF NOT EXISTS idx_plan_brand   ON production_plan(브랜드);
CREATE INDEX IF NOT EXISTS idx_hours_date   ON production_hours(날짜);
CREATE INDEX IF NOT EXISTS idx_hours_upload ON production_hours(upload_id);
CREATE INDEX IF NOT EXISTS idx_upload_team  ON upload_log(team, is_active);
"""


def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def _date_to_iso(x):
    if x is None or pd.isna(x):
        return None
    if hasattr(x, "isoformat"):
        return x.isoformat()
    return str(x)


def save_upload(
    team: str,
    filename: str,
    df_plan: pd.DataFrame,
    df_hours: pd.DataFrame | None,
    file_bytes: bytes | None = None,
) -> int:
    init_db()

    if file_bytes is not None:
        team_dir = UPLOADS_DIR / team
        team_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = filename.replace("/", "_").replace("\\", "_")
        backup_path = team_dir / f"{ts}_{safe_name}"
        backup_path.write_bytes(file_bytes)

    plan_n = 0 if df_plan is None else len(df_plan)
    hours_n = 0 if df_hours is None else len(df_hours)

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO upload_log (team, filename, plan_rows, hours_rows) VALUES (?, ?, ?, ?)",
            (team, filename, plan_n, hours_n),
        )
        upload_id = cur.lastrowid

        if df_plan is not None and not df_plan.empty:
            plan_cols = [
                "관리번호", "품목코드", "색상", "단품명칭", "생산라인", "브랜드",
                "계획량", "생산량", "입고단가", "계획금액", "실적금액",
                "최초포장계획일", "포장계획일",
            ]
            df_p = df_plan.copy()
            for c in plan_cols:
                if c not in df_p.columns:
                    df_p[c] = None
            df_p = df_p[plan_cols].copy()
            df_p["최초포장계획일"] = df_p["최초포장계획일"].map(_date_to_iso)
            df_p["포장계획일"] = df_p["포장계획일"].map(_date_to_iso)
            df_p["upload_id"] = upload_id
            df_p[["upload_id"] + plan_cols].to_sql(
                "production_plan", conn, if_exists="append", index=False
            )

        if df_hours is not None and not df_hours.empty:
            df_h = df_hours.copy()
            for c in ["근무시간", "이월수량", "이월금액"]:
                if c not in df_h.columns:
                    df_h[c] = 0
            df_h = df_h[["날짜", "근무시간", "이월수량", "이월금액"]].copy()
            df_h["날짜"] = df_h["날짜"].map(_date_to_iso)
            df_h = df_h.dropna(subset=["날짜"])
            df_h["upload_id"] = upload_id
            df_h[["upload_id", "날짜", "근무시간", "이월수량", "이월금액"]].to_sql(
                "production_hours", conn, if_exists="append", index=False
            )

        conn.commit()

    return upload_id


def query_plan(start_date, end_date, team: str = "production") -> pd.DataFrame:
    """
    활성 업로드의 행 중 관리번호별 최신 버전만 반환.
    관리번호가 없는 행은 모두 포함 (dedup 불가).
    날짜 필터는 최초포장계획일 OR 포장계획일이 구간에 걸치는 행을 포함.
    """
    init_db()
    sql = """
    WITH ranked AS (
        SELECT p.*,
               CASE
                 WHEN p.관리번호 IS NULL OR p.관리번호 = '' THEN 1
                 ELSE ROW_NUMBER() OVER (
                   PARTITION BY p.관리번호
                   ORDER BY p.upload_id DESC, p.row_id DESC
                 )
               END AS rn
        FROM production_plan p
        JOIN upload_log u ON p.upload_id = u.upload_id
        WHERE u.is_active = 1 AND u.team = ?
    )
    SELECT *
    FROM ranked
    WHERE rn = 1
      AND (
        (최초포장계획일 IS NOT NULL AND 최초포장계획일 BETWEEN ? AND ?)
        OR
        (포장계획일 IS NOT NULL AND 포장계획일 BETWEEN ? AND ?)
      )
    """
    s, e = _date_to_iso(start_date), _date_to_iso(end_date)
    with get_conn() as conn:
        df = pd.read_sql_query(sql, conn, params=(team, s, e, s, e))

    for c in ["최초포장계획일", "포장계획일"]:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce").dt.date
    return df


def query_hours(start_date, end_date, team: str = "production") -> pd.DataFrame:
    """활성 업로드 + 날짜별 최신 버전만 반환"""
    init_db()
    sql = """
    WITH ranked AS (
        SELECT h.*,
               ROW_NUMBER() OVER (
                 PARTITION BY h.날짜
                 ORDER BY h.upload_id DESC, h.row_id DESC
               ) AS rn
        FROM production_hours h
        JOIN upload_log u ON h.upload_id = u.upload_id
        WHERE u.is_active = 1 AND u.team = ?
          AND h.날짜 BETWEEN ? AND ?
    )
    SELECT 날짜, 근무시간, 이월수량, 이월금액, upload_id
    FROM ranked
    WHERE rn = 1
    """
    with get_conn() as conn:
        df = pd.read_sql_query(
            sql,
            conn,
            params=(team, _date_to_iso(start_date), _date_to_iso(end_date)),
        )
    df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce").dt.date
    return df


def list_uploads(team: str = "production") -> pd.DataFrame:
    init_db()
    with get_conn() as conn:
        df = pd.read_sql_query(
            """
            SELECT upload_id, team, filename, uploaded_at,
                   is_active, plan_rows, hours_rows, note
            FROM upload_log
            WHERE team = ?
            ORDER BY upload_id DESC
            """,
            conn,
            params=(team,),
        )
    return df


def set_upload_active(upload_id: int, active: bool):
    init_db()
    with get_conn() as conn:
        conn.execute(
            "UPDATE upload_log SET is_active = ? WHERE upload_id = ?",
            (1 if active else 0, upload_id),
        )
        conn.commit()


def delete_upload(upload_id: int):
    """업로드 + 관련 행 모두 영구 삭제"""
    init_db()
    with get_conn() as conn:
        conn.execute("DELETE FROM production_plan WHERE upload_id = ?", (upload_id,))
        conn.execute("DELETE FROM production_hours WHERE upload_id = ?", (upload_id,))
        conn.execute("DELETE FROM upload_log WHERE upload_id = ?", (upload_id,))
        conn.commit()


def get_active_brands(team: str = "production") -> list[str]:
    init_db()
    with get_conn() as conn:
        df = pd.read_sql_query(
            """
            SELECT DISTINCT p.브랜드 AS brand
            FROM production_plan p
            JOIN upload_log u ON p.upload_id = u.upload_id
            WHERE u.is_active = 1 AND u.team = ? AND p.브랜드 IS NOT NULL
            """,
            conn,
            params=(team,),
        )
    return sorted([b for b in df["brand"].dropna().tolist() if b])


def get_data_date_range(team: str = "production"):
    """활성 데이터의 (최소일자, 최대일자) 반환. 없으면 (None, None)."""
    init_db()
    sql = """
    SELECT MIN(d) AS min_d, MAX(d) AS max_d FROM (
        SELECT 최초포장계획일 AS d FROM production_plan p
        JOIN upload_log u ON p.upload_id = u.upload_id
        WHERE u.is_active = 1 AND u.team = ? AND p.최초포장계획일 IS NOT NULL
        UNION ALL
        SELECT 포장계획일 AS d FROM production_plan p
        JOIN upload_log u ON p.upload_id = u.upload_id
        WHERE u.is_active = 1 AND u.team = ? AND p.포장계획일 IS NOT NULL
    )
    """
    with get_conn() as conn:
        row = conn.execute(sql, (team, team)).fetchone()
    if not row or not row[0]:
        return None, None
    min_d = pd.to_datetime(row[0], errors="coerce")
    max_d = pd.to_datetime(row[1], errors="coerce")
    return (
        min_d.date() if pd.notna(min_d) else None,
        max_d.date() if pd.notna(max_d) else None,
    )
