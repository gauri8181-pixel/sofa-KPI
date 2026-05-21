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
        note TEXT,
        claims_rows INTEGER DEFAULT 0
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
        이월금액 REAL,
        근무인원 REAL
    )""",
    """CREATE TABLE IF NOT EXISTS quality_claims (
        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        upload_id INTEGER NOT NULL,
        날짜 TEXT NOT NULL,
        브랜드 TEXT NOT NULL,
        건수 INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_plan_init_date ON production_plan(최초포장계획일)",
    "CREATE INDEX IF NOT EXISTS idx_plan_pack_date ON production_plan(포장계획일)",
    "CREATE INDEX IF NOT EXISTS idx_plan_upload  ON production_plan(upload_id)",
    "CREATE INDEX IF NOT EXISTS idx_plan_brand   ON production_plan(브랜드)",
    "CREATE INDEX IF NOT EXISTS idx_hours_date   ON production_hours(날짜)",
    "CREATE INDEX IF NOT EXISTS idx_hours_upload ON production_hours(upload_id)",
    "CREATE INDEX IF NOT EXISTS idx_claims_date  ON quality_claims(날짜)",
    "CREATE INDEX IF NOT EXISTS idx_claims_brand ON quality_claims(브랜드)",
    "CREATE INDEX IF NOT EXISTS idx_claims_upload ON quality_claims(upload_id)",
    "CREATE INDEX IF NOT EXISTS idx_upload_team  ON upload_log(team, is_active)",
]

# 기존 DB에 컬럼이 없으면 추가 (성공/실패와 무관하게 1회 시도)
MIGRATIONS = [
    "ALTER TABLE production_hours ADD COLUMN 근무인원 REAL",
    "ALTER TABLE upload_log ADD COLUMN claims_rows INTEGER DEFAULT 0",
]


# =========================
# 자격증명
# =========================
def _get_turso_creds():
    url, token = "", ""
    try:
        if "turso" in st.secrets:
            url = str(st.secrets["turso"].get("url", ""))
            token = str(st.secrets["turso"].get("auth_token", ""))
    except Exception:
        pass
    if not (url and token):
        url = os.environ.get("TURSO_DATABASE_URL", "")
        token = os.environ.get("TURSO_AUTH_TOKEN", "")
    # 토큰 양끝의 공백/줄바꿈/따옴표 제거 (Secrets 붙여넣기 시 흔한 오류)
    token = token.strip().strip('"').strip("'").strip()
    url = url.strip().strip('"').strip("'").strip()
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


_http_session = None


def _get_http_session():
    """HTTP 연결 재사용 (TCP/TLS 핸드셰이크 절약)"""
    global _http_session
    if _http_session is None:
        _http_session = requests.Session()
    return _http_session


def _execute_pipeline(url: str, token: str, statements: list[tuple]) -> list[dict]:
    """
    statements: [(sql, params), ...] 형식의 리스트
    여러 SQL을 한 HTTP 요청으로 전송 → 원자적(transaction)으로 실행
    각 statement의 결과를 dict 리스트로 반환
    """
    endpoint = _http_endpoint(url)
    # 토큰은 이미 _get_turso_creds에서 정리됨, 그대로 사용
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
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

    try:
        r = _get_http_session().post(endpoint, headers=headers, data=body_bytes, timeout=60)
    except Exception as e:
        msg = f"Turso 연결 실패: {type(e).__name__}: {e} @ {endpoint}"
        print(f"[TURSO_ERROR] {msg}", flush=True)
        try:
            st.error(f"❌ {msg}")
        except Exception:
            pass
        raise RuntimeError(msg) from e

    r.encoding = "utf-8"
    if r.status_code != 200:
        # 서버가 돌려준 실제 에러 메시지
        try:
            body_preview = r.text[:2000]
        except Exception:
            body_preview = "(text decoding failed)"
        if not body_preview:
            body_preview = "(empty body)"
        first_sql = statements[0][0][:300] if statements else "(no sql)"
        msg = (
            f"Turso HTTP {r.status_code} @ {endpoint}\n"
            f"First SQL: {first_sql}\n"
            f"Response: {body_preview}"
        )
        print(f"[TURSO_ERROR] {msg}", flush=True)
        try:
            st.error(
                f"❌ **Turso HTTP {r.status_code}**\n\n"
                f"**Endpoint:** `{endpoint}`\n\n"
                f"**First SQL:** `{first_sql}`\n\n"
                f"**Server response:**\n```\n{body_preview}\n```"
            )
        except Exception:
            pass
        raise RuntimeError(msg)

    try:
        data = r.json()
    except Exception as e:
        msg = f"Turso 응답 JSON 파싱 실패: {e}\nResponse: {r.text[:1000]}"
        print(f"[TURSO_ERROR] {msg}", flush=True)
        raise RuntimeError(msg) from e

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
_db_initialized = False


def init_db():
    """스키마 생성 + 마이그레이션 (모듈 레벨 플래그로 1회만 실행)"""
    global _db_initialized
    if _db_initialized:
        return True
    _exec_many([(stmt, ()) for stmt in SCHEMA_STATEMENTS])
    # 기존 테이블에 컬럼 추가 시도 (이미 있으면 에러 무시)
    for migration in MIGRATIONS:
        try:
            _exec_one(migration)
        except Exception:
            pass
    _db_initialized = True
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


def _build_multi_insert(table: str, columns: list, rows: list) -> list:
    """
    여러 행을 한 INSERT 문에 묶어 SQL 파싱 횟수를 줄임.
    SQLite 파라미터 한도(999) 내에서 자동 분할.
    """
    if not rows:
        return []
    cols_sql = ",".join(columns)
    n_cols = len(columns)
    # 안전 마진을 두어 999 / n_cols 보다 약간 적게
    max_rows = max(1, 950 // n_cols)
    placeholder_row = "(" + ",".join(["?"] * n_cols) + ")"

    statements = []
    for i in range(0, len(rows), max_rows):
        chunk = rows[i:i + max_rows]
        all_placeholders = ",".join([placeholder_row] * len(chunk))
        sql = f"INSERT INTO {table} ({cols_sql}) VALUES {all_placeholders}"
        flat_params = []
        for row in chunk:
            flat_params.extend(row)
        statements.append((sql, tuple(flat_params)))
    return statements


# =========================
# 저장
# =========================
def save_upload(
    team: str,
    filename: str,
    df_plan: pd.DataFrame | None = None,
    df_hours: pd.DataFrame | None = None,
    df_claims: pd.DataFrame | None = None,
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

    plan_n = 0 if df_plan is None or df_plan.empty else len(df_plan)
    hours_n = 0 if df_hours is None or df_hours.empty else len(df_hours)
    claims_n = 0 if df_claims is None or df_claims.empty else len(df_claims)

    # 1) upload_log INSERT
    res = _exec_one(
        "INSERT INTO upload_log (team, filename, plan_rows, hours_rows, claims_rows) VALUES (?, ?, ?, ?, ?)",
        (team, filename, plan_n, hours_n, claims_n),
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

    # 2) production_plan 일괄 INSERT (multi-row VALUES)
    if df_plan is not None and not df_plan.empty:
        plan_cols = [
            "upload_id", "관리번호", "품목코드", "색상", "단품명칭", "생산라인", "브랜드",
            "계획량", "생산량", "입고단가", "계획금액", "실적금액", "최초포장계획일", "포장계획일",
        ]
        plan_rows = []
        for _, r in df_plan.iterrows():
            plan_rows.append((
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
        plan_statements = _build_multi_insert("production_plan", plan_cols, plan_rows)
        # HTTP body 크기 제한을 위해 한 HTTP 요청당 multi-INSERT 5개씩 묶어 보냄
        HTTP_GROUP = 5
        for i in range(0, len(plan_statements), HTTP_GROUP):
            _exec_many(plan_statements[i:i + HTTP_GROUP])

    # 3) production_hours 일괄 INSERT
    if df_hours is not None and not df_hours.empty:
        hours_cols = ["upload_id", "날짜", "근무시간", "이월수량", "이월금액", "근무인원"]
        hours_rows = []
        for _, r in df_hours.iterrows():
            hours_rows.append((
                upload_id,
                _date_to_iso(r.get("날짜")),
                float(r.get("근무시간") or 0),
                float(r.get("이월수량") or 0),
                float(r.get("이월금액") or 0),
                float(r.get("근무인원") or 0),
            ))
        hours_statements = _build_multi_insert("production_hours", hours_cols, hours_rows)
        if hours_statements:
            _exec_many(hours_statements)

    # 4) quality_claims 일괄 INSERT
    if df_claims is not None and not df_claims.empty:
        claims_cols = ["upload_id", "날짜", "브랜드", "건수"]
        claims_rows_data = []
        for _, r in df_claims.iterrows():
            claims_rows_data.append((
                upload_id,
                _date_to_iso(r.get("날짜")),
                str(r.get("브랜드") or "").strip(),
                int(r.get("건수") or 0),
            ))
        claims_statements = _build_multi_insert("quality_claims", claims_cols, claims_rows_data)
        if claims_statements:
            _exec_many(claims_statements)

    return upload_id


# =========================
# 품질팀 조회 함수
# =========================
def query_claims(start_date, end_date, team: str = "quality") -> pd.DataFrame:
    """활성 업로드 + (날짜, 브랜드)별 최신 버전만 반환"""
    init_db()
    sql = """
    WITH ranked AS (
        SELECT c.*,
               ROW_NUMBER() OVER (
                 PARTITION BY c.날짜, c.브랜드
                 ORDER BY c.upload_id DESC, c.row_id DESC
               ) AS rn
        FROM quality_claims c
        JOIN upload_log u ON c.upload_id = u.upload_id
        WHERE u.is_active = 1 AND u.team = ?
          AND c.날짜 BETWEEN ? AND ?
    )
    SELECT 날짜, 브랜드, 건수, upload_id
    FROM ranked
    WHERE rn = 1
    """
    res = _exec_one(sql, (team, _date_to_iso(start_date), _date_to_iso(end_date)))
    df = _result_to_df(res)
    if not df.empty:
        df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce").dt.date
        df["건수"] = pd.to_numeric(df["건수"], errors="coerce").fillna(0).astype(int)
    return df


def get_claims_brands(team: str = "quality") -> list:
    init_db()
    res = _exec_one(
        """
        SELECT DISTINCT c.브랜드 AS brand
        FROM quality_claims c
        JOIN upload_log u ON c.upload_id = u.upload_id
        WHERE u.is_active = 1 AND u.team = ? AND c.브랜드 IS NOT NULL
        """,
        (team,),
    )
    df = _result_to_df(res)
    if df.empty:
        return []
    return sorted([b for b in df["brand"].dropna().tolist() if b])


def get_claims_date_range(team: str = "quality"):
    init_db()
    res = _exec_one(
        """
        SELECT MIN(c.날짜) AS min_d, MAX(c.날짜) AS max_d
        FROM quality_claims c
        JOIN upload_log u ON c.upload_id = u.upload_id
        WHERE u.is_active = 1 AND u.team = ? AND c.날짜 IS NOT NULL
        """,
        (team,),
    )
    if not res["rows"] or not res["rows"][0][0]:
        return None, None
    min_d = pd.to_datetime(res["rows"][0][0], errors="coerce")
    max_d = pd.to_datetime(res["rows"][0][1], errors="coerce")
    return (
        min_d.date() if pd.notna(min_d) else None,
        max_d.date() if pd.notna(max_d) else None,
    )


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
    SELECT 날짜, 근무시간, 이월수량, 이월금액, 근무인원, upload_id
    FROM ranked
    WHERE rn = 1
    """
    res = _exec_one(sql, (team, _date_to_iso(start_date), _date_to_iso(end_date)))
    df = _result_to_df(res)
    if not df.empty:
        df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce").dt.date
        if "근무인원" in df.columns:
            df["근무인원"] = pd.to_numeric(df["근무인원"], errors="coerce").fillna(0)
        else:
            df["근무인원"] = 0
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
        ("DELETE FROM quality_claims WHERE upload_id = ?", (upload_id,)),
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
