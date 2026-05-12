# db.py
# Turso (libSQL) 기반 KPI 데이터 저장소
# - SQLite 호환 클라우드 DB. 기존 SQLite 쿼리 그대로 동작.
# - Streamlit Cloud 재시작에도 데이터 영구 보존.
#
# 자격증명 우선순위:
#   1) Streamlit Secrets [turso] (배포 환경)
#   2) 환경변수 TURSO_DATABASE_URL / TURSO_AUTH_TOKEN
#   3) 둘 다 없으면 로컬 SQLite 파일 (개발 모드)
#
# 사전 준비:
#   1. https://turso.tech 가입 (GitHub 로그인)
#   2. 데이터베이스 생성 (예: sofa-kpi)
#   3. URL + Auth Token 발급
#   4. Streamlit Secrets에 등록 (또는 .streamlit/secrets.toml)

import os
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path("data") / "kpi.db"
UPLOADS_DIR = Path("data") / "uploads"

# =========================
# 스키마 (CREATE 문 리스트)
# =========================
SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS upload_log (
        upload_id INTEGER PRIMARY KEY AUTOINCREMENT,
        team TEXT NOT NULL,
        filename TEXT,
        uploaded_at TEXT DEFAULT (datetime('now', 'localtime')),
        is_active INTEGER DEFAULT 1,
        plan_rows INTEGER DEFAULT 0,
        hours_rows INTEGER DEFAULT 0,
        note TEXT
    )
    """,
    """
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
        포장계획일 TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS production_hours (
        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        upload_id INTEGER NOT NULL,
        날짜 TEXT NOT NULL,
        근무시간 REAL,
        이월수량 REAL,
        이월금액 REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_plan_init_date ON production_plan(최초포장계획일)",
    "CREATE INDEX IF NOT EXISTS idx_plan_pack_date ON production_plan(포장계획일)",
    "CREATE INDEX IF NOT EXISTS idx_plan_upload  ON production_plan(upload_id)",
    "CREATE INDEX IF NOT EXISTS idx_plan_brand   ON production_plan(브랜드)",
    "CREATE INDEX IF NOT EXISTS idx_hours_date   ON production_hours(날짜)",
    "CREATE INDEX IF NOT EXISTS idx_hours_upload ON production_hours(upload_id)",
    "CREATE INDEX IF NOT EXISTS idx_upload_team  ON upload_log(team, is_active)",
]


# =========================
# 자격증명 / 연결
# =========================
def _get_turso_creds():
    """Streamlit Secrets 또는 환경변수에서 Turso URL/Token 로드"""
    try:
        if "turso" in st.secrets:
            return (
                str(st.secrets["turso"].get("url", "")),
                str(st.secrets["turso"].get("auth_token", "")),
            )
    except Exception:
        pass

    url = os.environ.get("TURSO_DATABASE_URL", "")
    token = os.environ.get("TURSO_AUTH_TOKEN", "")
    return url, token


def get_conn():
    """Turso 임베디드 레플리카 연결 (creds 없으면 순수 로컬 SQLite)"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    url, token = _get_turso_creds()

    if url and token:
        try:
            import libsql_experimental as libsql

            conn = libsql.connect(
                str(DB_PATH), sync_url=url, auth_token=token
            )
            try:
                conn.sync()  # 원격에서 최신 데이터 가져오기
            except Exception:
                pass
            return conn
        except ImportError:
            pass  # libsql 미설치 시 SQLite로 fallback

    # 로컬 fallback
    return sqlite3.connect(str(DB_PATH))


def _commit_sync(conn):
    """commit + (libsql인 경우) Turso로 sync"""
    conn.commit()
    if hasattr(conn, "sync"):
        try:
            conn.sync()
        except Exception:
            pass


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    for stmt in SCHEMA_STATEMENTS:
        cur.execute(stmt)
    _commit_sync(conn)
    try:
        conn.close()
    except Exception:
        pass


def _query_df(conn, sql: str, params: tuple = ()) -> pd.DataFrame:
    """libsql/SQLite 공용 쿼리 → DataFrame"""
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description] if cur.description else []
    return pd.DataFrame(rows, columns=cols)


# =========================
# 유틸
# =========================
def _date_to_iso(x):
    if x is None or pd.isna(x):
        return None
    if hasattr(x, "isoformat"):
        return x.isoformat()
    return str(x)


# =========================
# 저장
# =========================
def save_upload(
    team: str,
    filename: str,
    df_plan: pd.DataFrame,
    df_hours: pd.DataFrame | None,
    file_bytes: bytes | None = None,
) -> int:
    init_db()

    # 원본 파일 로컬 백업 (선택, 클라우드에선 휘발성)
    if file_bytes is not None:
        try:
            team_dir = UPLOADS_DIR / team
            team_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe = filename.replace("/", "_").replace("\\", "_")
            (team_dir / f"{ts}_{safe}").write_bytes(file_bytes)
        except Exception:
            pass

    plan_n = 0 if df_plan is None else len(df_plan)
    hours_n = 0 if df_hours is None else len(df_hours)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO upload_log (team, filename, plan_rows, hours_rows) VALUES (?, ?, ?, ?)",
        (team, filename, plan_n, hours_n),
    )

    # upload_id 가져오기 (lastrowid가 libsql에서 동작하지 않을 수 있어 SELECT 사용)
    upload_id = None
    if hasattr(cur, "lastrowid") and cur.lastrowid:
        upload_id = cur.lastrowid
    if not upload_id:
        cur2 = conn.execute(
            "SELECT upload_id FROM upload_log WHERE team=? AND filename=? ORDER BY upload_id DESC LIMIT 1",
            (team, filename),
        )
        row = cur2.fetchone()
        upload_id = int(row[0]) if row else None

    if df_plan is not None and not df_plan.empty:
        plan_cols = [
            "관리번호", "품목코드", "색상", "단품명칭", "생산라인", "브랜드",
            "계획량", "생산량", "입고단가", "계획금액", "실적금액",
            "최초포장계획일", "포장계획일",
        ]
        rows_data = []
        for _, r in df_plan.iterrows():
            rows_data.append((
                upload_id,
                str(r.get("관리번호") or ""),
                str(r.get("품목코드") or ""),
                str(r.get("색상") or ""),
                str(r.get("단품명칭") or ""),
                str(r.get("생산라인") or ""),
                str(r.get("브랜드") or ""),
                float(r.get("계획량") or 0),
                float(r.get("생산량") or 0),
                float(r.get("입고단가") or 0),
                float(r.get("계획금액") or 0),
                float(r.get("실적금액") or 0),
                _date_to_iso(r.get("최초포장계획일")),
                _date_to_iso(r.get("포장계획일")),
            ))
        placeholders = ", ".join(["?"] * (1 + len(plan_cols)))
        cols_sql = ", ".join(["upload_id"] + plan_cols)
        insert_sql = f"INSERT INTO production_plan ({cols_sql}) VALUES ({placeholders})"
        for row in rows_data:
            cur.execute(insert_sql, row)

    if df_hours is not None and not df_hours.empty:
        for _, r in df_hours.iterrows():
            cur.execute(
                "INSERT INTO production_hours (upload_id, 날짜, 근무시간, 이월수량, 이월금액) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    upload_id,
                    _date_to_iso(r.get("날짜")),
                    float(r.get("근무시간") or 0),
                    float(r.get("이월수량") or 0),
                    float(r.get("이월금액") or 0),
                ),
            )

    _commit_sync(conn)
    try:
        conn.close()
    except Exception:
        pass

    return upload_id


# =========================
# 조회
# =========================
def query_plan(start_date, end_date, team: str = "production") -> pd.DataFrame:
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
    conn = get_conn()
    df = _query_df(conn, sql, (team, s, e, s, e))
    try:
        conn.close()
    except Exception:
        pass
    for c in ["최초포장계획일", "포장계획일"]:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce").dt.date
    return df


def query_hours(start_date, end_date, team: str = "production") -> pd.DataFrame:
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
    conn = get_conn()
    df = _query_df(conn, sql, (team, _date_to_iso(start_date), _date_to_iso(end_date)))
    try:
        conn.close()
    except Exception:
        pass
    if not df.empty:
        df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce").dt.date
    return df


def list_uploads(team: str = "production") -> pd.DataFrame:
    init_db()
    sql = """
    SELECT upload_id, team, filename, uploaded_at,
           is_active, plan_rows, hours_rows, note
    FROM upload_log
    WHERE team = ?
    ORDER BY upload_id DESC
    """
    conn = get_conn()
    df = _query_df(conn, sql, (team,))
    try:
        conn.close()
    except Exception:
        pass
    return df


def set_upload_active(upload_id: int, active: bool):
    init_db()
    conn = get_conn()
    conn.execute(
        "UPDATE upload_log SET is_active = ? WHERE upload_id = ?",
        (1 if active else 0, upload_id),
    )
    _commit_sync(conn)
    try:
        conn.close()
    except Exception:
        pass


def delete_upload(upload_id: int):
    init_db()
    conn = get_conn()
    conn.execute("DELETE FROM production_plan WHERE upload_id = ?", (upload_id,))
    conn.execute("DELETE FROM production_hours WHERE upload_id = ?", (upload_id,))
    conn.execute("DELETE FROM upload_log WHERE upload_id = ?", (upload_id,))
    _commit_sync(conn)
    try:
        conn.close()
    except Exception:
        pass


def get_active_brands(team: str = "production") -> list:
    init_db()
    sql = """
    SELECT DISTINCT p.브랜드 AS brand
    FROM production_plan p
    JOIN upload_log u ON p.upload_id = u.upload_id
    WHERE u.is_active = 1 AND u.team = ? AND p.브랜드 IS NOT NULL
    """
    conn = get_conn()
    df = _query_df(conn, sql, (team,))
    try:
        conn.close()
    except Exception:
        pass
    if df.empty:
        return []
    return sorted([b for b in df["brand"].dropna().tolist() if b])


def get_data_date_range(team: str = "production"):
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
    conn = get_conn()
    cur = conn.execute(sql, (team, team))
    row = cur.fetchone()
    try:
        conn.close()
    except Exception:
        pass
    if not row or not row[0]:
        return None, None
    min_d = pd.to_datetime(row[0], errors="coerce")
    max_d = pd.to_datetime(row[1], errors="coerce")
    return (
        min_d.date() if pd.notna(min_d) else None,
        max_d.date() if pd.notna(max_d) else None,
    )
