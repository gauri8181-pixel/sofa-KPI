# db.py
# Turso HTTP API (Hrana over HTTP) 기반 KPI 데이터 저장소
# - 별도 네이티브 패키지 불필요 (requests만 사용)
# - 어떤 Python 버전에서도 동작
# - 인터페이스는 기존 SQLite/libsql 버전과 동일 → KPI.py 수정 불필요
#
# 자격증명:
#   1) Streamlit Secrets [turso] (배포)
#   2) 환경변수 TURSO_DATABASE_URL / TURSO_AUTH_TOKEN
#   3) 둘 다 없으면 로컬 SQLite (개발)

import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

DB_PATH = Path("data") / "kpi.db"
UPLOADS_DIR = Path("data") / "uploads"

# =========================
# 스키마
# =========================
SCHEMA_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS upload_log (
        upload_id INTEGER PRIMARY KEY AUTOINCREMENT,
        team TEXT NOT NULL,
        filename TEXT,
        uploaded_at TEXT DEFAULT (datetime('now', 'localtime')),
        is_active INTEGER DEFAULT 1,
        plan_rows INTEGER DEFAULT 0,
        hours_rows INTEGER DEFAULT 0,
        note TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS production_plan (
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
    )""",
    """CREATE TABLE IF NOT EXISTS production_hours (
        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        upload_id INTEGER NOT NULL,
        날짜 TEXT NOT NULL,
        근무시간 REAL,
        이월수량 REAL,
        이월금액 REAL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_plan_init_date ON production_plan(최초포장계획일)",
    "CREATE INDEX IF NOT EXISTS idx_plan_pack_date ON production_plan(포장계획일)",
    "CREATE INDEX IF NOT EXISTS idx_plan_upload  ON production_plan(upload_id)",
    "CREATE INDEX IF NOT EXISTS idx_plan_brand   ON production_plan(브랜드)",
    "CREATE INDEX IF NOT EXISTS idx_hours_date   ON production_hours(날짜)",
    "CREATE INDEX IF NOT EXISTS idx_hours_upload ON production_hours(upload_id)",
    "CREATE INDEX IF NOT EXISTS idx_upload_team  ON upload_log(team, is_active)",
]


# =========================
# 자격증명
# =========================
def _get_turso_creds():
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


def _http_endpoint(url: str) -> str:
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    return url.rstrip("/") + "/v2/pipeline"


# =========================
# Hrana HTTP 변환
# =========================
def _to_hrana_arg(val):
    if val is None:
        return {"type": "null"}
    if isinstance(val, bool):
        return {"type": "integer", "value": "1" if val else "0"}
    if isinstance(val, int):
        return {"type": "integer", "value": str(val)}
    if isinstance(val, float):
        return {"type": "float", "value": val}
    if isinstance(val, (datetime, date)):
        return {"type": "text", "value": val.isoformat()}
    return {"type": "text", "value": str(val)}


def _from_hrana_cell(cell):
    t = cell.get("type")
    v = cell.get("value")
    if t == "null" or v is None:
        return None
    if t == "integer":
        try:
            return int(v)
        except Exception:
            return None
    if t == "float":
        try:
            return float(v)
        except Exception:
            return None
    return v


def _execute_pipeline(url: str, token: str, statements: list[tuple]) -> list[dict]:
    """
    statements: [(sql, params), ...] 형식의 리스트
    여러 SQL을 한 HTTP 요청으로 전송 → 원자적(transaction)으로 실행
    각 statement의 결과를 dict 리스트로 반환
    """
    endpoint = _http_endpoint(url)
    # 모든 헤더는 ASCII로 강제
    headers = {
        "Authorization": f"Bearer {token}".encode("ascii", "ignore").decode("ascii"),
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "Connection": "close",
    }
    requests_body = []
    for sql, params in statements:
        requests_body.append({
            "type": "execute",
            "stmt": {
                "sql": sql,
                "args": [_to_hrana_arg(p) for p in (params or ())],
            },
        })
    requests_body.append({"type": "close"})

    body = {"requests": requests_body}
    # ensure_ascii=True → 한글 등 비ASCII를 \uXXXX로 이스케이프, 결과는 순수 ASCII
    body_bytes = json.dumps(body, ensure_ascii=True).encode("ascii")
    r = requests.post(endpoint, headers=headers, data=body_bytes, timeout=60)
    # 응답 본문을 항상 UTF-8로 디코딩
    r.encoding = "utf-8"
    if r.status_code != 200:
        # 서버가 돌려준 실제 에러 메시지를 그대로 노출
        body_preview = r.text[:1000] if r.text else "(empty body)"
        raise RuntimeError(
            f"Turso HTTP {r.status_code} @ {endpoint}\n"
            f"Response: {body_preview}"
        )
    data = r.json()

    results = []
    for item in data.get("results", []):
        if item.get("type") != "ok":
            # close 응답은 type='ok'지만 다른 응답 형태일 수 있음, 에러는 위로
            err = item.get("error", {}).get("message", "Unknown error")
            if "type" in item and item["type"] == "error":
                raise RuntimeError(f"Turso error: {err}")
            results.append(None)
            continue
        resp = item.get("response", {})
        if resp.get("type") == "execute":
            res = resp.get("result", {})
            cols = [c.get("name") for c in res.get("cols", [])]
            rows = []
            for raw_row in res.get("rows", []):
                rows.append(tuple(_from_hrana_cell(c) for c in raw_row))
            last_insert = res.get("last_insert_rowid")
            try:
                last_insert = int(last_insert) if last_insert is not None else None
            except Exception:
                last_insert = None
            results.append({
                "cols": cols,
                "rows": rows,
                "last_insert_rowid": last_insert,
            })
    return results


# =========================
# 통합 실행 함수 (Turso HTTP or 로컬 SQLite)
# =========================
def _is_turso() -> bool:
    url, token = _get_turso_creds()
    return bool(url and token)


def _exec_one(sql: str, params: tuple = ()) -> dict:
    """단일 SQL 실행 → {'cols','rows','last_insert_rowid'} 반환"""
    url, token = _get_turso_creds()
    if url and token:
        return _execute_pipeline(url, token, [(sql, params)])[0]
    # 로컬 SQLite fallback
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        last = cur.lastrowid
        conn.commit()
        return {"cols": cols, "rows": rows, "last_insert_rowid": last}
    finally:
        conn.close()


def _exec_many(statements: list[tuple]) -> list[dict]:
    """여러 SQL 일괄 실행 (단일 HTTP 또는 단일 SQLite 트랜잭션)"""
    url, token = _get_turso_creds()
    if url and token:
        return _execute_pipeline(url, token, statements)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        results = []
        for sql, params in statements:
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
            results.append({
                "cols": cols,
                "rows": rows,
                "last_insert_rowid": cur.lastrowid,
            })
        conn.commit()
        return results
    finally:
        conn.close()


# =========================
# 초기화
# =========================
@st.cache_resource
def init_db():
    """스키마 생성 (캐시 — 세션당 1회)"""
    _exec_many([(stmt, ()) for stmt in SCHEMA_STATEMENTS])
    return True


def _result_to_df(result: dict) -> pd.DataFrame:
    return pd.DataFrame(result["rows"], columns=result["cols"])


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

    # 1) upload_log INSERT
    res = _exec_one(
        "INSERT INTO upload_log (team, filename, plan_rows, hours_rows) VALUES (?, ?, ?, ?)",
        (team, filename, plan_n, hours_n),
    )
    upload_id = res.get("last_insert_rowid")
    if not upload_id:
        # last_insert_rowid가 0/None이면 직접 SELECT
        sel = _exec_one(
            "SELECT upload_id FROM upload_log WHERE team=? AND filename=? ORDER BY upload_id DESC LIMIT 1",
            (team, filename),
        )
        if sel["rows"]:
            upload_id = int(sel["rows"][0][0])

    # 2) production_plan 일괄 INSERT (배치)
    if df_plan is not None and not df_plan.empty:
        plan_sql = (
            "INSERT INTO production_plan "
            "(upload_id, 관리번호, 품목코드, 색상, 단품명칭, 생산라인, 브랜드, "
            "계획량, 생산량, 입고단가, 계획금액, 실적금액, 최초포장계획일, 포장계획일) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        batch = []
        for _, r in df_plan.iterrows():
            batch.append((
                plan_sql,
                (
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
                ),
            ))
        # HTTP 한도/안전을 위해 200행씩 분할
        CHUNK = 200
        for i in range(0, len(batch), CHUNK):
            _exec_many(batch[i:i + CHUNK])

    # 3) production_hours 일괄 INSERT
    if df_hours is not None and not df_hours.empty:
        hours_sql = (
            "INSERT INTO production_hours (upload_id, 날짜, 근무시간, 이월수량, 이월금액) "
            "VALUES (?,?,?,?,?)"
        )
        batch = []
        for _, r in df_hours.iterrows():
            batch.append((
                hours_sql,
                (
                    upload_id,
                    _date_to_iso(r.get("날짜")),
                    float(r.get("근무시간") or 0),
                    float(r.get("이월수량") or 0),
                    float(r.get("이월금액") or 0),
                ),
            ))
        if batch:
            _exec_many(batch)

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
    res = _exec_one(sql, (team, s, e, s, e))
    df = _result_to_df(res)
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
    res = _exec_one(sql, (team, _date_to_iso(start_date), _date_to_iso(end_date)))
    df = _result_to_df(res)
    if not df.empty:
        df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce").dt.date
    return df


def list_uploads(team: str = "production") -> pd.DataFrame:
    init_db()
    res = _exec_one(
        """
        SELECT upload_id, team, filename, uploaded_at,
               is_active, plan_rows, hours_rows, note
        FROM upload_log
        WHERE team = ?
        ORDER BY upload_id DESC
        """,
        (team,),
    )
    return _result_to_df(res)


def set_upload_active(upload_id: int, active: bool):
    init_db()
    _exec_one(
        "UPDATE upload_log SET is_active = ? WHERE upload_id = ?",
        (1 if active else 0, upload_id),
    )


def delete_upload(upload_id: int):
    init_db()
    _exec_many([
        ("DELETE FROM production_plan WHERE upload_id = ?", (upload_id,)),
        ("DELETE FROM production_hours WHERE upload_id = ?", (upload_id,)),
        ("DELETE FROM upload_log WHERE upload_id = ?", (upload_id,)),
    ])


def get_active_brands(team: str = "production") -> list:
    init_db()
    res = _exec_one(
        """
        SELECT DISTINCT p.브랜드 AS brand
        FROM production_plan p
        JOIN upload_log u ON p.upload_id = u.upload_id
        WHERE u.is_active = 1 AND u.team = ? AND p.브랜드 IS NOT NULL
        """,
        (team,),
    )
    df = _result_to_df(res)
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
    res = _exec_one(sql, (team, team))
    if not res["rows"] or not res["rows"][0][0]:
        return None, None
    min_d = pd.to_datetime(res["rows"][0][0], errors="coerce")
    max_d = pd.to_datetime(res["rows"][0][1], errors="coerce")
    return (
        min_d.date() if pd.notna(min_d) else None,
        max_d.date() if pd.notna(max_d) else None,
    )
