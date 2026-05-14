# KPI.py
# 소파 제조 KPI 대시보드 - 생산팀 (다크 모드)
# - 엑셀 업로드 → 정제 → SQLite DB 누적 저장
# - 조회는 DB에서 (브랜드 필터, 기간 필터, 일/주/월 토글)
# - 탭 기반 레이아웃: 종합 / 브랜드 분석 / 업로드 이력

import io
import re
from datetime import date, datetime

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import db

# =========================
# 페이지 설정
# =========================
st.set_page_config(
    page_title="소파 제조 KPI - 생산팀",
    page_icon="🛋️",
    layout="wide",
    initial_sidebar_state="expanded",
)

TEAM = "production"

# =========================
# 색상 팔레트 (다크 모드)
# =========================
COLOR_PRIMARY = "#00D9FF"   # 시안 (계획)
COLOR_ACCENT = "#FF6B9D"    # 핑크 (실적)
COLOR_SUCCESS = "#10B981"   # 그린
COLOR_WARNING = "#F59E0B"   # 앰버
COLOR_BG = "#0E1117"
COLOR_CARD = "#1A1F2E"
COLOR_CARD_HOVER = "#232B3F"
COLOR_BORDER = "#2D3748"
COLOR_TEXT = "#FAFAFA"
COLOR_MUTED = "#94A3B8"

BRAND_COLORS = {
    "알로소": "#00D9FF",
    "일룸": "#FF6B9D",
    "퍼시스": "#FCD34D",
    "기타": "#94A3B8",
}
PLAN_ACTUAL_COLORS = {
    "계획수량": COLOR_PRIMARY, "실적수량": COLOR_ACCENT,
    "계획금액": COLOR_PRIMARY, "실적금액": COLOR_ACCENT,
}

PLOTLY_TEMPLATE = "plotly_dark"

# =========================
# 커스텀 CSS
# =========================
st.markdown(
    f"""
<style>
    /* 전체 폰트/배경 */
    .stApp {{
        background: {COLOR_BG};
    }}

    /* 헤더 영역 */
    .main-header {{
        background: linear-gradient(135deg, #1A1F2E 0%, #2D3748 50%, #1A1F2E 100%);
        padding: 20px 28px;
        border-radius: 16px;
        margin-bottom: 24px;
        border: 1px solid {COLOR_BORDER};
        box-shadow: 0 4px 24px rgba(0, 217, 255, 0.08);
    }}
    .main-header h1 {{
        margin: 0;
        font-size: 1.8rem;
        font-weight: 700;
        background: linear-gradient(90deg, {COLOR_PRIMARY} 0%, {COLOR_ACCENT} 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
    }}
    .main-header .subtitle {{
        color: {COLOR_MUTED};
        font-size: 0.9rem;
        margin-top: 4px;
    }}
    .status-pill {{
        display: inline-block;
        padding: 4px 12px;
        border-radius: 999px;
        background: rgba(0, 217, 255, 0.12);
        color: {COLOR_PRIMARY};
        font-size: 0.8rem;
        font-weight: 600;
        margin-right: 8px;
        border: 1px solid rgba(0, 217, 255, 0.3);
    }}

    /* 메트릭 카드 */
    [data-testid="stMetric"] {{
        background: linear-gradient(135deg, {COLOR_CARD} 0%, {COLOR_CARD_HOVER} 100%);
        border: 1px solid {COLOR_BORDER};
        border-radius: 12px;
        padding: 18px 20px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.3);
        transition: transform 0.15s, box-shadow 0.15s;
    }}
    [data-testid="stMetric"]:hover {{
        transform: translateY(-2px);
        box-shadow: 0 6px 16px rgba(0, 217, 255, 0.15);
        border-color: {COLOR_PRIMARY};
    }}
    [data-testid="stMetricLabel"] {{
        color: {COLOR_MUTED} !important;
        font-size: 0.82rem !important;
        font-weight: 500 !important;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }}
    [data-testid="stMetricValue"] {{
        color: {COLOR_TEXT} !important;
        font-size: 1.6rem !important;
        font-weight: 700 !important;
    }}

    /* 섹션 헤더 */
    h2, h3 {{
        color: {COLOR_TEXT} !important;
        font-weight: 600 !important;
    }}

    /* 사이드바 */
    [data-testid="stSidebar"] {{
        background: #0A0D14;
        border-right: 1px solid {COLOR_BORDER};
    }}
    [data-testid="stSidebar"] h2 {{
        color: {COLOR_PRIMARY} !important;
        font-size: 0.95rem !important;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }}

    /* 탭 */
    .stTabs [data-baseweb="tab-list"] {{
        gap: 8px;
        background: transparent;
    }}
    .stTabs [data-baseweb="tab"] {{
        background: {COLOR_CARD};
        border-radius: 10px 10px 0 0;
        padding: 10px 20px;
        color: {COLOR_MUTED};
        border: 1px solid {COLOR_BORDER};
        border-bottom: none;
    }}
    .stTabs [aria-selected="true"] {{
        background: linear-gradient(135deg, {COLOR_PRIMARY}22, {COLOR_ACCENT}22) !important;
        color: {COLOR_PRIMARY} !important;
        border-color: {COLOR_PRIMARY} !important;
    }}

    /* 버튼 */
    .stButton button {{
        border-radius: 8px;
        border: 1px solid {COLOR_BORDER};
        background: {COLOR_CARD};
        color: {COLOR_TEXT};
        transition: all 0.15s;
    }}
    .stButton button:hover {{
        border-color: {COLOR_PRIMARY};
        background: {COLOR_CARD_HOVER};
        color: {COLOR_PRIMARY};
    }}

    /* DataFrame */
    [data-testid="stDataFrame"] {{
        border-radius: 10px;
        overflow: hidden;
        border: 1px solid {COLOR_BORDER};
    }}

    /* expander */
    [data-testid="stExpander"] {{
        background: {COLOR_CARD};
        border-radius: 10px;
        border: 1px solid {COLOR_BORDER};
    }}

    /* 구분선 */
    hr {{
        border-color: {COLOR_BORDER} !important;
        margin: 1.5rem 0 !important;
    }}
</style>
""",
    unsafe_allow_html=True,
)

# =========================
# 시트/컬럼 상수
# =========================
SHEET_PLAN = "생산 계획 정보"
SHEET_DELETE = "삭제할 코드"
SHEET_BRAND = "브랜드 나누기"
SHEET_HOURS = "근무시간및 이월량"

KEEP_COLS = [
    "관리번호", "품목코드", "색상", "계획량", "생산량",
    "생산라인", "최초포장계획일", "포장계획일",
    "단품명칭", "입고단가",
]

EXCLUDE_PROD_LINES = {
    "의자(재단)", "소파(재단)", "소파 (고객만족)",
    "소파 (고객만족2)", "소파라인(기타)",
}
_EXCL_NORM = {v.replace(" ", "") for v in EXCLUDE_PROD_LINES}

# =========================
# 정규화 유틸
# =========================
_R_SUFFIX_RE = re.compile(r"-R\d+$")


def canon_code(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return ""
    s = str(x).strip().replace(" ", "").upper()
    if s.endswith(".0") and s[:-2].replace("-", "").isdigit():
        s = s[:-2]
    s = _R_SUFFIX_RE.sub("", s)
    return s


def canon_color(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return ""
    return str(x).strip().replace(" ", "").upper()


def norm_header(h) -> str:
    if h is None or (isinstance(h, float) and pd.isna(h)):
        return ""
    s = str(h).strip()
    for ch in "▲▼△▽":
        s = s.replace(ch, "")
    s = s.replace("\n", " ").strip()
    return s


def detect_brand_from_rule(code: str):
    if not code:
        return None
    f = code[0].upper()
    if f == "A":
        return "알로소"
    if f in {"H", "D", "I"}:
        return "일룸"
    if f in {"C", "Z"}:
        return "퍼시스"
    return None


def period_floor(d, period: str):
    if d is None or pd.isna(d):
        return None
    if isinstance(d, datetime):
        d = d.date()
    if period == "일별":
        return d
    ts = pd.Timestamp(d)
    if period == "주별":
        return ts.to_period("W-SUN").start_time.date()
    if period == "월별":
        return ts.to_period("M").start_time.date()
    return d


def style_fig(fig):
    """plotly 차트 공통 다크 스타일 — 가독성 강화"""
    fig.update_layout(
        template=PLOTLY_TEMPLATE,
        plot_bgcolor=COLOR_CARD,
        paper_bgcolor=COLOR_CARD,
        font=dict(color="#FFFFFF", family="sans-serif", size=14),
        margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(
            bgcolor="rgba(26,31,46,0.85)",
            bordercolor=COLOR_BORDER,
            borderwidth=1,
            font=dict(color="#FFFFFF", size=13),
        ),
        xaxis=dict(
            gridcolor="#3A4556",
            zerolinecolor="#3A4556",
            linecolor="#5A6578",
            tickfont=dict(color="#FFFFFF", size=13),
            title_font=dict(color="#FFFFFF", size=14),
        ),
        yaxis=dict(
            gridcolor="#3A4556",
            zerolinecolor="#3A4556",
            linecolor="#5A6578",
            tickfont=dict(color="#FFFFFF", size=13),
            title_font=dict(color="#FFFFFF", size=14),
        ),
        hoverlabel=dict(
            bgcolor=COLOR_CARD,
            bordercolor=COLOR_PRIMARY,
            font=dict(color="#FFFFFF", size=13),
        ),
    )
    # 바 차트 라벨: 막대 위쪽에 흰색으로
    fig.update_traces(
        textfont=dict(color="#FFFFFF", size=13),
        textposition="outside",
        cliponaxis=False,
        selector=dict(type="bar"),
    )
    # 파이 차트 라벨
    fig.update_traces(
        textfont=dict(color="#FFFFFF", size=14),
        insidetextfont=dict(color="#FFFFFF", size=13),
        outsidetextfont=dict(color="#FFFFFF", size=13),
        selector=dict(type="pie"),
    )
    return fig


# =========================
# 엑셀 파싱
# =========================
def parse_excel(file_bytes: bytes):
    xls = pd.ExcelFile(io.BytesIO(file_bytes))
    if SHEET_PLAN not in xls.sheet_names:
        return None, None, [f"필수 시트 '{SHEET_PLAN}' 없음"]

    df = pd.read_excel(xls, sheet_name=SHEET_PLAN)
    df.columns = [norm_header(c) for c in df.columns]

    missing = [c for c in KEEP_COLS if c not in df.columns]
    if missing:
        return None, None, missing

    df = df[KEEP_COLS].copy()

    delete_codes: set[str] = set()
    if SHEET_DELETE in xls.sheet_names:
        try:
            ddf = pd.read_excel(xls, sheet_name=SHEET_DELETE, header=None)
            if not ddf.empty:
                for v in ddf.iloc[:, 0].dropna():
                    s = str(v).strip()
                    if s.lower().replace(" ", "") in {"품목코드", "단품코드", "제품코드", "code", "코드"}:
                        continue
                    code = canon_code(s)
                    if code:
                        delete_codes.add(code)
        except Exception:
            pass

    brand_override: dict[tuple[str, str], str] = {}
    if SHEET_BRAND in xls.sheet_names:
        try:
            bdf = pd.read_excel(xls, sheet_name=SHEET_BRAND)
            bdf.columns = [norm_header(c) for c in bdf.columns]
            code_col = next(
                (c for c in ["품목코드", "제품코드", "단품코드", "코드"] if c in bdf.columns),
                None,
            )
            color_col = "색상" if "색상" in bdf.columns else None
            brand_col = next(
                (c for c in ["브랜드", "브랜드명"] if c in bdf.columns), None
            )
            if code_col and brand_col:
                for _, row in bdf.iterrows():
                    code = canon_code(row[code_col])
                    color = canon_color(row[color_col]) if color_col else ""
                    raw_brand = row[brand_col]
                    brand = "" if pd.isna(raw_brand) else str(raw_brand).strip()
                    if code and brand:
                        brand_override[(code, color)] = brand
        except Exception:
            pass

    df["_code"] = df["품목코드"].map(canon_code)
    df["_color"] = df["색상"].map(canon_color)
    df["_line_norm"] = (
        df["생산라인"].fillna("").astype(str).str.replace(" ", "", regex=False)
    )
    df = df[df["_code"] != ""]
    df = df[~df["_code"].isin(delete_codes)]
    df = df[~df["_line_norm"].isin(_EXCL_NORM)]

    def _brand(row):
        b = detect_brand_from_rule(row["_code"])
        if b is not None:
            return b
        if (row["_code"], row["_color"]) in brand_override:
            return brand_override[(row["_code"], row["_color"])]
        if (row["_code"], "") in brand_override:
            return brand_override[(row["_code"], "")]
        return "기타"

    df["브랜드"] = df.apply(_brand, axis=1)

    df["계획량"] = pd.to_numeric(df["계획량"], errors="coerce").fillna(0.0)
    df["생산량"] = pd.to_numeric(df["생산량"], errors="coerce").fillna(0.0)
    df["입고단가"] = pd.to_numeric(df["입고단가"], errors="coerce").fillna(0.0)
    df["계획금액"] = df["계획량"] * df["입고단가"]
    df["실적금액"] = df["생산량"] * df["입고단가"]

    df["최초포장계획일"] = pd.to_datetime(df["최초포장계획일"], errors="coerce").dt.date
    df["포장계획일"] = pd.to_datetime(df["포장계획일"], errors="coerce").dt.date

    df["관리번호"] = df["관리번호"].apply(
        lambda x: "" if pd.isna(x) else str(x).strip()
    )

    df_plan_clean = df[
        [
            "관리번호", "품목코드", "색상", "단품명칭", "생산라인", "브랜드",
            "계획량", "생산량", "입고단가", "계획금액", "실적금액",
            "최초포장계획일", "포장계획일",
        ]
    ].copy()

    df_hours = None
    if SHEET_HOURS in xls.sheet_names:
        try:
            hdf = pd.read_excel(xls, sheet_name=SHEET_HOURS)
            hdf.columns = [norm_header(c) for c in hdf.columns]
            if "날짜" in hdf.columns:
                hdf["날짜"] = pd.to_datetime(hdf["날짜"], errors="coerce").dt.date
                for c in ["근무시간", "이월수량", "이월금액", "근무인원"]:
                    if c in hdf.columns:
                        hdf[c] = pd.to_numeric(hdf[c], errors="coerce").fillna(0)
                    else:
                        hdf[c] = 0
                df_hours = hdf[["날짜", "근무시간", "이월수량", "이월금액", "근무인원"]].dropna(
                    subset=["날짜"]
                )
        except Exception:
            df_hours = None

    return df_plan_clean, df_hours, None


# =========================
# DB 캐시 래퍼
# =========================
db.init_db()


@st.cache_data
def load_plan(s, e):
    return db.query_plan(s, e, team=TEAM)


@st.cache_data
def load_hours(s, e):
    return db.query_hours(s, e, team=TEAM)


@st.cache_data
def load_brands():
    return db.get_active_brands(team=TEAM)


# =========================
# 헤더
# =========================
data_min, data_max = db.get_data_date_range(TEAM)
uploads_df = db.list_uploads(team=TEAM)
n_active = int(uploads_df["is_active"].sum()) if not uploads_df.empty else 0
n_total = len(uploads_df)
last_upload = (
    uploads_df.iloc[0]["uploaded_at"] if not uploads_df.empty else "-"
)

range_text = (
    f"{data_min} ~ {data_max}" if data_min and data_max else "데이터 없음"
)

st.markdown(
    f"""
<div class="main-header">
    <h1>🛋️ 생산팀 KPI 대시보드</h1>
    <div class="subtitle">
        <span class="status-pill">📊 활성 업로드 {n_active}/{n_total}</span>
        <span class="status-pill">📅 데이터 범위 {range_text}</span>
        <span class="status-pill">⏱️ 최근 업로드 {last_upload}</span>
    </div>
</div>
""",
    unsafe_allow_html=True,
)

# =========================
# 사이드바
# =========================
st.sidebar.header("📂 데이터 업로드")
uploaded = st.sidebar.file_uploader(
    "KPI 입력용 파일 (Excel)", type=["xlsx"], key="uploader"
)

if uploaded is not None:
    file_bytes = uploaded.read()
    df_plan_new, df_hours_new, err = parse_excel(file_bytes)
    if err:
        st.sidebar.error(f"파싱 실패: {err}")
    else:
        n_plan = 0 if df_plan_new is None else len(df_plan_new)
        n_hours = 0 if df_hours_new is None else len(df_hours_new)
        st.sidebar.success(f"파싱 완료 — 생산 {n_plan}행, 근무 {n_hours}행")
        if st.sidebar.button("💾 DB에 저장", use_container_width=True, type="primary"):
            uid = db.save_upload(
                team=TEAM,
                filename=uploaded.name,
                df_plan=df_plan_new,
                df_hours=df_hours_new,
                file_bytes=file_bytes,
            )
            st.sidebar.success(f"✅ 저장 완료 (#{uid})")
            st.cache_data.clear()
            st.rerun()

st.sidebar.header("📅 조회 기간")
default_start = data_min if data_min else date(2026, 4, 1)
default_end = data_max if data_max else date.today()
start_date = st.sidebar.date_input("시작일", default_start)
end_date = st.sidebar.date_input("종료일", default_end)

st.sidebar.header("📊 집계 단위")
period = st.sidebar.radio("기간 단위", ["일별", "주별", "월별"], horizontal=True, label_visibility="collapsed")

if start_date > end_date:
    st.error("시작일이 종료일보다 늦습니다.")
    st.stop()

df_all = load_plan(start_date, end_date)
df_hours_all = load_hours(start_date, end_date)
brand_list = load_brands()

if df_all.empty and df_hours_all.empty:
    st.info("📭 저장된 데이터가 없습니다. 사이드바에서 엑셀을 업로드하고 **DB에 저장**을 눌러주세요.")
    st.stop()

st.sidebar.header("🏷️ 브랜드 필터")
selected_brands = st.sidebar.multiselect(
    "브랜드 선택", brand_list, default=brand_list, label_visibility="collapsed"
)
st.sidebar.caption("ℹ️ 근무시간/이월은 브랜드 무관 전체값")

if not selected_brands:
    st.warning("브랜드를 1개 이상 선택해주세요.")
    st.stop()

# =========================
# 계산
# =========================
df_filtered = df_all[df_all["브랜드"].isin(selected_brands)].copy()

df_plan = df_filtered.dropna(subset=["최초포장계획일"]).copy()
df_plan = df_plan[
    (df_plan["최초포장계획일"] >= start_date)
    & (df_plan["최초포장계획일"] <= end_date)
]
df_plan["기간"] = df_plan["최초포장계획일"].map(lambda d: period_floor(d, period))

df_actual = df_filtered.dropna(subset=["포장계획일"]).copy()
df_actual = df_actual[
    (df_actual["포장계획일"] >= start_date) & (df_actual["포장계획일"] <= end_date)
]
df_actual["기간"] = df_actual["포장계획일"].map(lambda d: period_floor(d, period))

if df_plan.empty:
    plan_agg = pd.DataFrame(columns=["기간", "계획수량", "계획금액"])
else:
    plan_agg = df_plan.groupby("기간", as_index=False).agg(
        계획수량=("계획량", "sum"),
        계획금액=("계획금액", "sum"),
    )

if df_actual.empty:
    actual_agg = pd.DataFrame(columns=["기간", "실적수량", "실적금액"])
else:
    actual_agg = df_actual.groupby("기간", as_index=False).agg(
        실적수량=("생산량", "sum"),
        실적금액=("실적금액", "sum"),
    )

final_df = pd.merge(plan_agg, actual_agg, on="기간", how="outer")

hours_period = pd.DataFrame(columns=["기간", "근무시간", "이월수량", "이월금액", "근무인원"])
if df_hours_all is not None and not df_hours_all.empty:
    dh = df_hours_all.copy()
    if "근무인원" not in dh.columns:
        dh["근무인원"] = 0
    dh["기간"] = dh["날짜"].map(lambda d: period_floor(d, period))
    hours_period = dh.groupby("기간", as_index=False).agg(
        근무시간=("근무시간", "sum"),
        이월수량=("이월수량", "sum"),
        이월금액=("이월금액", "sum"),
        근무인원=("근무인원", "sum"),
    )

final_df = pd.merge(final_df, hours_period, on="기간", how="outer")
for c in ["계획수량", "계획금액", "실적수량", "실적금액", "근무시간", "이월수량", "이월금액", "근무인원"]:
    if c not in final_df.columns:
        final_df[c] = 0
final_df = final_df.fillna(0).sort_values("기간").reset_index(drop=True)

final_df["공당생산액"] = np.where(
    final_df["근무시간"] > 0,
    final_df["실적금액"] / final_df["근무시간"],
    0,
)
# 인당 근무시간 (그 기간 근무시간 합계 / 그 기간 근무인원 합계)
final_df["인당근무시간"] = np.where(
    final_df["근무인원"] > 0,
    final_df["근무시간"] / final_df["근무인원"],
    0,
)

# 종합 수치
total_plan_qty = final_df["계획수량"].sum()
total_plan_amt = final_df["계획금액"].sum()
total_actual_qty = final_df["실적수량"].sum()
total_actual_amt = final_df["실적금액"].sum()
total_hours = final_df["근무시간"].sum()
total_workers = final_df["근무인원"].sum()  # 근무인원의 합 (사람-일 단위)

# 이월은 합계가 아닌 "마지막 일자"의 값을 사용 (재고/잔량 의미상)
if df_hours_all is not None and not df_hours_all.empty:
    _last_hours_row = df_hours_all.sort_values("날짜").iloc[-1]
    total_carry_qty = float(_last_hours_row["이월수량"])
    total_carry_amt = float(_last_hours_row["이월금액"])
else:
    total_carry_qty = 0.0
    total_carry_amt = 0.0

weighted_productivity = total_actual_amt / total_hours if total_hours > 0 else 0
# 인당 근무시간: 총 근무시간 / 총 근무인원 (가중평균)
avg_hours_per_worker = total_hours / total_workers if total_workers > 0 else 0
qty_rate = (total_actual_qty / total_plan_qty * 100) if total_plan_qty > 0 else 0
amt_rate = (total_actual_amt / total_plan_amt * 100) if total_plan_amt > 0 else 0

# =========================
# 탭 레이아웃
# =========================
tab1, tab2, tab3 = st.tabs(["📊  종합 대시보드", "🏷️  브랜드 분석", "📜  업로드 이력"])

# ---------- 종합 ----------
with tab1:
    # KPI 카드 1열
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("계획 수량", f"{total_plan_qty:,.0f} 개")
    c2.metric("실적 수량", f"{total_actual_qty:,.0f} 개", f"달성률 {qty_rate:.1f}%")
    c3.metric("계획 금액", f"₩{total_plan_amt/1e8:,.2f} 억")
    c4.metric("실적 금액", f"₩{total_actual_amt/1e8:,.2f} 억", f"달성률 {amt_rate:.1f}%")

    # KPI 카드 2열 (5칸)
    c5, c6, c7, c8, c9 = st.columns(5)
    c5.metric("총 근무시간", f"{total_hours:,.0f} h")
    c6.metric("인당 근무시간", f"{avg_hours_per_worker:,.1f} h/일 (조회기간 평균)")
    c7.metric("공당생산액", f"₩{weighted_productivity:,.0f} /h")
    c8.metric("이월 수량 (최종일)", f"{total_carry_qty:,.0f} 개")
    c9.metric("이월 금액 (최종일)", f"₩{total_carry_amt/1e8:,.2f} 억")

    st.markdown("<br/>", unsafe_allow_html=True)

    # 차트
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f"##### 📈 {period} 계획 vs 실적 (수량)")
        if not final_df.empty:
            chart_qty = final_df.melt(
                id_vars="기간",
                value_vars=["계획수량", "실적수량"],
                var_name="구분",
                value_name="수량",
            )
            fig_qty = px.bar(
                chart_qty, x="기간", y="수량", color="구분",
                barmode="group", text_auto=".2s",
                color_discrete_map=PLAN_ACTUAL_COLORS,
            )
            fig_qty.update_layout(xaxis_title=None, yaxis_title="수량 (개)")
            st.plotly_chart(style_fig(fig_qty), use_container_width=True)
        else:
            st.info("표시할 데이터 없음")

    with col2:
        st.markdown(f"##### 💰 {period} 계획 vs 실적 (금액)")
        if not final_df.empty:
            chart_amt = final_df.melt(
                id_vars="기간",
                value_vars=["계획금액", "실적금액"],
                var_name="구분",
                value_name="금액",
            )
            fig_amt = px.bar(
                chart_amt, x="기간", y="금액", color="구분",
                barmode="group", text_auto=".2s",
                color_discrete_map=PLAN_ACTUAL_COLORS,
            )
            fig_amt.update_layout(xaxis_title=None, yaxis_title="금액 (원)")
            st.plotly_chart(style_fig(fig_amt), use_container_width=True)
        else:
            st.info("표시할 데이터 없음")

    col3, col4 = st.columns(2)
    with col3:
        st.markdown(f"##### ⚙️ {period} 공당생산액 추이")
        if not final_df.empty:
            fig_prod = go.Figure()
            fig_prod.add_trace(go.Scatter(
                x=final_df["기간"], y=final_df["공당생산액"],
                mode="lines+markers",
                line=dict(color=COLOR_PRIMARY, width=3),
                marker=dict(size=10, color=COLOR_PRIMARY, line=dict(color=COLOR_BG, width=2)),
                fill="tozeroy",
                fillcolor="rgba(0, 217, 255, 0.1)",
                name="공당생산액",
            ))
            fig_prod.update_layout(yaxis_title="원/h", xaxis_title=None)
            st.plotly_chart(style_fig(fig_prod), use_container_width=True)
        else:
            st.info("표시할 데이터 없음")

    with col4:
        st.markdown(f"##### 📦 {period} 이월 수량/금액")
        if final_df["이월금액"].sum() > 0 or final_df["이월수량"].sum() > 0:
            fig_carry = px.bar(
                final_df, x="기간", y="이월금액",
                text_auto=".2s", hover_data=["이월수량"],
                color_discrete_sequence=[COLOR_WARNING],
            )
            fig_carry.update_layout(yaxis_title="이월금액 (원)", xaxis_title=None)
            st.plotly_chart(style_fig(fig_carry), use_container_width=True)
        else:
            st.info("선택한 기간에 이월 데이터가 없습니다.")

    with st.expander("🔍 기간별 상세 데이터"):
        st.dataframe(
            final_df.style.format(
                {
                    "계획수량": "{:,.0f}",
                    "계획금액": "₩{:,.0f}",
                    "실적수량": "{:,.0f}",
                    "실적금액": "₩{:,.0f}",
                    "근무시간": "{:,.0f}",
                    "근무인원": "{:,.0f}",
                    "인당근무시간": "{:,.1f}",
                    "공당생산액": "₩{:,.0f}",
                    "이월수량": "{:,.0f}",
                    "이월금액": "₩{:,.0f}",
                }
            ),
            use_container_width=True,
            hide_index=True,
        )

# ---------- 브랜드 분석 ----------
with tab2:
    if df_plan.empty:
        plan_brand = pd.DataFrame(columns=["브랜드", "계획수량", "계획금액"])
    else:
        plan_brand = df_plan.groupby("브랜드", as_index=False).agg(
            계획수량=("계획량", "sum"),
            계획금액=("계획금액", "sum"),
        )

    if df_actual.empty:
        actual_brand = pd.DataFrame(columns=["브랜드", "실적수량", "실적금액"])
    else:
        actual_brand = df_actual.groupby("브랜드", as_index=False).agg(
            실적수량=("생산량", "sum"),
            실적금액=("실적금액", "sum"),
        )

    brand_summary = pd.merge(plan_brand, actual_brand, on="브랜드", how="outer").fillna(0)

    if not brand_summary.empty:
        brand_summary["수량 달성률(%)"] = np.where(
            brand_summary["계획수량"] > 0,
            brand_summary["실적수량"] / brand_summary["계획수량"] * 100,
            0,
        )
        brand_summary["금액 달성률(%)"] = np.where(
            brand_summary["계획금액"] > 0,
            brand_summary["실적금액"] / brand_summary["계획금액"] * 100,
            0,
        )

        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("##### 🥧 브랜드별 실적 금액 비중")
            pie_df = brand_summary[brand_summary["실적금액"] > 0]
            if not pie_df.empty:
                fig_pie = px.pie(
                    pie_df, values="실적금액", names="브랜드",
                    color="브랜드", color_discrete_map=BRAND_COLORS, hole=0.45,
                )
                fig_pie.update_traces(textposition="outside", textinfo="percent+label")
                st.plotly_chart(style_fig(fig_pie), use_container_width=True)
            else:
                st.info("실적 데이터 없음")

        with col_b:
            st.markdown("##### 📊 브랜드별 달성률 비교")
            achieve_df = brand_summary.melt(
                id_vars="브랜드",
                value_vars=["수량 달성률(%)", "금액 달성률(%)"],
                var_name="구분", value_name="달성률",
            )
            fig_ach = px.bar(
                achieve_df, x="브랜드", y="달성률", color="구분",
                barmode="group", text_auto=".1f",
                color_discrete_sequence=[COLOR_PRIMARY, COLOR_ACCENT],
            )
            fig_ach.update_layout(yaxis_title="달성률 (%)", xaxis_title=None)
            st.plotly_chart(style_fig(fig_ach), use_container_width=True)

        # 합계 행
        total_row = pd.DataFrame([{
            "브랜드": "합계",
            "계획수량": total_plan_qty,
            "계획금액": total_plan_amt,
            "실적수량": total_actual_qty,
            "실적금액": total_actual_amt,
            "수량 달성률(%)": qty_rate,
            "금액 달성률(%)": amt_rate,
        }])
        brand_summary_disp = pd.concat([brand_summary, total_row], ignore_index=True)

        st.markdown("##### 📋 브랜드별 상세")
        st.dataframe(
            brand_summary_disp.style.format({
                "계획수량": "{:,.0f}",
                "계획금액": "₩{:,.0f}",
                "실적수량": "{:,.0f}",
                "실적금액": "₩{:,.0f}",
                "수량 달성률(%)": "{:.1f}%",
                "금액 달성률(%)": "{:.1f}%",
            }),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("표시할 브랜드 데이터가 없습니다.")

# ---------- 업로드 이력 ----------
with tab3:
    uploads = db.list_uploads(team=TEAM)
    if uploads.empty:
        st.info("업로드 이력이 없습니다.")
    else:
        st.caption("**비활성화**: 데이터에서 제외 (복구 가능) / **영구삭제**: DB에서 제거")
        header = st.columns([1, 3, 2, 2, 1, 1, 1])
        header[0].markdown("**ID**")
        header[1].markdown("**파일명**")
        header[2].markdown("**업로드 일시**")
        header[3].markdown("**행 수**")
        header[4].markdown("**상태**")
        header[5].markdown("**토글**")
        header[6].markdown("**삭제**")
        st.markdown("---")
        for _, row in uploads.iterrows():
            uid = int(row["upload_id"])
            active = bool(row["is_active"])
            cols = st.columns([1, 3, 2, 2, 1, 1, 1])
            cols[0].write(f"#{uid}")
            cols[1].write(row["filename"] or "-")
            cols[2].write(row["uploaded_at"])
            cols[3].write(f"P:{int(row['plan_rows'])} / H:{int(row['hours_rows'])}")
            if active:
                cols[4].markdown(f"<span style='color:{COLOR_SUCCESS};font-weight:600;'>● 활성</span>", unsafe_allow_html=True)
                if cols[5].button("비활성", key=f"deact_{uid}"):
                    db.set_upload_active(uid, False)
                    st.cache_data.clear()
                    st.rerun()
            else:
                cols[4].markdown(f"<span style='color:{COLOR_MUTED};font-weight:600;'>○ 비활성</span>", unsafe_allow_html=True)
                if cols[5].button("복구", key=f"act_{uid}"):
                    db.set_upload_active(uid, True)
                    st.cache_data.clear()
                    st.rerun()
            if cols[6].button("🗑️", key=f"del_{uid}"):
                db.delete_upload(uid)
                st.cache_data.clear()
                st.rerun()
