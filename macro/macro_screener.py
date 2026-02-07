#!/usr/bin/env python3
"""Macro Indicator Screener - 거시경제 지표 대시보드 CLI 도구"""

import sys
import io
import json
import pathlib
import yfinance as yf
from datetime import datetime, timezone, timedelta

# Windows cp949 인코딩 문제 방지
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ── 상수 ──────────────────────────────────────────────────────────────────────
INDICATORS = {
    "us10y":   {"symbol": "^TNX",     "name": "US 10Y Yield",      "fmt": "{:.3f}%"},
    "dxy":     {"symbol": "DX-Y.NYB", "name": "Dollar Index (DXY)", "fmt": "{:.2f}"},
    "usdjpy":  {"symbol": "JPY=X",    "name": "USD/JPY",           "fmt": "¥{:.2f}"},
    "usdkrw":  {"symbol": "KRW=X",    "name": "USD/KRW",           "fmt": "₩{:,.1f}"},
    "vix":     {"symbol": "^VIX",     "name": "VIX",               "fmt": "{:.2f}"},
    "sp500":   {"symbol": "^GSPC",    "name": "S&P 500",           "fmt": "{:,.0f}"},
    "nasdaq":  {"symbol": "^IXIC",    "name": "NASDAQ",            "fmt": "{:,.0f}"},
    "russell": {"symbol": "^RUT",     "name": "Russell 2000",      "fmt": "{:,.0f}"},
    "kospi":   {"symbol": "^KS11",    "name": "KOSPI",             "fmt": "{:,.0f}"},
    "kosdaq":  {"symbol": "^KQ11",    "name": "KOSDAQ",            "fmt": "{:,.0f}"},
    "gold":    {"symbol": "GC=F",     "name": "Gold",              "fmt": "${:,.2f}"},
    "silver":  {"symbol": "SI=F",     "name": "Silver",            "fmt": "${:,.2f}"},
    "wti":     {"symbol": "CL=F",     "name": "Crude Oil (WTI)",   "fmt": "${:,.2f}"},
    "copper":  {"symbol": "HG=F",     "name": "Copper",            "fmt": "${:.4f}"},
    "btc":     {"symbol": "BTC-USD",  "name": "Bitcoin",           "fmt": "${:,.0f}"},
    # 섹터 ETF
    "xlk":     {"symbol": "XLK",     "name": "Technology",        "fmt": "${:,.2f}"},
    "xlf":     {"symbol": "XLF",     "name": "Financials",        "fmt": "${:,.2f}"},
    "xly":     {"symbol": "XLY",     "name": "Cons. Discret.",    "fmt": "${:,.2f}"},
    "xli":     {"symbol": "XLI",     "name": "Industrials",       "fmt": "${:,.2f}"},
    "xlb":     {"symbol": "XLB",     "name": "Materials",         "fmt": "${:,.2f}"},
    "xle":     {"symbol": "XLE",     "name": "Energy",            "fmt": "${:,.2f}"},
    "xlu":     {"symbol": "XLU",     "name": "Utilities",         "fmt": "${:,.2f}"},
    "xlv":     {"symbol": "XLV",     "name": "Health Care",       "fmt": "${:,.2f}"},
    "xlp":     {"symbol": "XLP",     "name": "Cons. Staples",     "fmt": "${:,.2f}"},
    "xlre":    {"symbol": "XLRE",    "name": "Real Estate",       "fmt": "${:,.2f}"},
    "xlc":     {"symbol": "XLC",     "name": "Communication",     "fmt": "${:,.2f}"},
}

SECTIONS = [
    ("금리",     ["us10y"]),
    ("통화",     ["dxy", "usdjpy", "usdkrw"]),
    ("변동성",   ["vix"]),
    ("미국 시장", ["sp500", "nasdaq", "russell"]),
    ("섹터 (공격)", ["xlk", "xlf", "xly", "xli", "xlb", "xle"]),
    ("섹터 (방어)", ["xlu", "xlv", "xlp"]),
    ("섹터 (보조)", ["xlre", "xlc"]),
    ("한국 시장", ["kospi", "kosdaq"]),
    ("원자재",   ["gold", "silver", "wti", "copper"]),
    ("크립토",   ["btc"]),
]

CYCLE_PHASES = {
    "초기 회복": {"leaders": ["xlk", "xlf", "xly"], "laggards": ["xle", "xlb"]},
    "중기 확장": {"leaders": ["xli", "xlb", "xle"], "laggards": ["xlu", "xlp"]},
    "후기 과열": {"leaders": ["xle", "xlb"],         "laggards": ["xlk", "xlf", "xly"]},
    "침체/방어": {"leaders": ["xlu", "xlv", "xlp"],   "laggards": ["xly", "xlk", "xli"]},
}

OFFENSIVE_SECTORS = ["xlk", "xlf", "xly", "xli", "xlb", "xle"]
DEFENSIVE_SECTORS = ["xlu", "xlv", "xlp"]

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent  # scripts/macro/
HISTORY_DIR = SCRIPT_DIR / "history"

_errors = []


# ── 데이터 수집 ───────────────────────────────────────────────────────────────
def fetch_all_data():
    """yf.download()로 1년치 일봉 배치 다운로드"""
    tickers = [ind["symbol"] for ind in INDICATORS.values()]
    try:
        df = yf.download(tickers, period="1y", progress=False, auto_adjust=True)
        return df
    except Exception as e:
        _errors.append(f"yfinance download: {e}")
        return None


# ── 지표 계산 ─────────────────────────────────────────────────────────────────
def process_indicators(df):
    """각 지표의 파생 메트릭 계산"""
    results = {}

    if df is None or df.empty:
        _errors.append("데이터프레임이 비어있습니다")
        return results

    for key, cfg in INDICATORS.items():
        ticker = cfg["symbol"]
        name = cfg["name"]
        try:
            # 종가 시리즈 추출 (MultiIndex)
            if isinstance(df.columns, __import__('pandas').MultiIndex):
                series = df["Close"][ticker].dropna()
            else:
                series = df["Close"].dropna()

            if series.empty:
                _errors.append(f"{name}: 데이터 없음")
                continue

            current = series.iloc[-1]

            # 전일비, 등락률
            if len(series) >= 2:
                prev = series.iloc[-2]
                change = current - prev
                change_pct = (change / prev) * 100
            else:
                change, change_pct = 0.0, 0.0

            # 200일 이동평균
            if len(series) >= 200:
                ma200 = series.rolling(200).mean().iloc[-1]
                vs_ma200 = ((current - ma200) / ma200) * 100
            else:
                ma200, vs_ma200 = None, None

            # 52주 고점/저점
            high_52w = series.max()
            low_52w = series.min()
            off_high = ((current - high_52w) / high_52w) * 100

            results[key] = {
                "current": current,
                "change": change,
                "change_pct": change_pct,
                "ma200": ma200,
                "vs_ma200": vs_ma200,
                "high_52w": high_52w,
                "low_52w": low_52w,
                "off_high": off_high,
            }
        except Exception as e:
            _errors.append(f"{name}: {e}")

    return results


# ── 히스토리 저장/로드 ────────────────────────────────────────────────────────
def _json_default(obj):
    """numpy/pandas 타입을 JSON 직렬화 가능한 파이썬 타입으로 변환"""
    import numpy as np
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def save_history(indicators, sentiment_label, reasons, cycle_phase=None, cycle_details=None):
    """오늘 데이터를 macro/history/yyyy_mm_dd.json으로 저장"""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc)
    fname = today.strftime("%Y_%m_%d") + ".json"

    data = {
        "date": today.strftime("%Y-%m-%d"),
        "timestamp": today.isoformat(),
        "sentiment": sentiment_label,
        "reasons": reasons,
        "cycle_phase": cycle_phase,
        "cycle_details": cycle_details,
        "indicators": indicators,
    }

    path = HISTORY_DIR / fname
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return path


def _find_nearest_file(target_date, max_drift=5):
    """target_date 근처(±max_drift일) 가장 가까운 히스토리 파일 반환"""
    if not HISTORY_DIR.exists():
        return None
    for drift in range(0, max_drift + 1):
        for delta in ([0] if drift == 0 else [-drift, drift]):
            d = target_date + timedelta(days=delta)
            f = HISTORY_DIR / (d.strftime("%Y_%m_%d") + ".json")
            if f.exists():
                return f
    return None


def load_history(target_date, max_drift=5):
    """특정 날짜(±drift) 히스토리 로드, 없으면 None"""
    f = _find_nearest_file(target_date, max_drift)
    if f is None:
        return None
    data = json.loads(f.read_text(encoding="utf-8"))
    return data


def calc_period_changes(indicators):
    """1W, 1M, 3M 전 대비 변동률 계산"""
    today = datetime.now(timezone.utc).date()
    periods = [
        ("1W", timedelta(weeks=1)),
        ("1M", timedelta(days=30)),
        ("3M", timedelta(days=90)),
    ]
    result = {}  # {key: {"1W": pct, "1M": pct, "3M": pct}}

    past_data = {}
    for label, delta in periods:
        target = today - delta
        hist = load_history(target, max_drift=5)
        past_data[label] = hist

    for key in indicators:
        cur = indicators[key]["current"]
        result[key] = {}
        for label, _ in periods:
            hist = past_data[label]
            if hist and key in hist.get("indicators", {}):
                old = hist["indicators"][key].get("current")
                if old and old != 0:
                    result[key][label] = ((cur - old) / old) * 100
                else:
                    result[key][label] = None
            else:
                result[key][label] = None

    return result, [l for l, _ in periods]


# ── 포맷팅 ────────────────────────────────────────────────────────────────────
def fmt_price(key, price):
    """지표별 가격 포맷팅 (INDICATORS fmt 사용)"""
    if price is None:
        return "N/A"
    return INDICATORS[key]["fmt"].format(price)


def fmt_change(change, change_pct):
    """등락 포맷팅: +45.21 (+0.87%)"""
    if change is None:
        return "N/A"
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:,.2f} ({sign}{change_pct:.2f}%)"


def fmt_ma200(vs_ma200):
    """MA200 대비 포맷팅: ▲2.3% / ▼1.5%"""
    if vs_ma200 is None:
        return "N/A"
    arrow = "▲" if vs_ma200 >= 0 else "▼"
    return f"{arrow}{abs(vs_ma200):.1f}%"


# ── 경기사이클 판정 ──────────────────────────────────────────────────────────
def assess_cycle(indicators):
    """섹터 로테이션 기반 경기국면 판정

    Returns: (phase_label, details_list, offensive_avg, defensive_avg)
    """
    # 각 섹터의 vs_ma200 수집
    sector_ma200 = {}
    for key in list(OFFENSIVE_SECTORS) + list(DEFENSIVE_SECTORS) + ["xlre", "xlc"]:
        ind = indicators.get(key)
        if ind and ind["vs_ma200"] is not None:
            sector_ma200[key] = ind["vs_ma200"]

    if not sector_ma200:
        return "판정 불가", ["섹터 데이터 부족"], None, None

    # 각 국면별 점수 계산
    phase_scores = {}
    for phase, cfg in CYCLE_PHASES.items():
        score = 0
        leader_vals = []
        for k in cfg["leaders"]:
            v = sector_ma200.get(k)
            if v is not None:
                leader_vals.append(v)
                if v > 0:
                    score += 1
        for k in cfg["laggards"]:
            v = sector_ma200.get(k)
            if v is not None:
                if v < 0:
                    score += 1
        # leaders의 평균 vs_ma200로 가중
        if leader_vals:
            avg_leader = sum(leader_vals) / len(leader_vals)
            score += avg_leader / 10  # 10%당 +1점 가중
        phase_scores[phase] = score

    # 최고 점수 국면
    best_phase = max(phase_scores, key=phase_scores.get)

    # 공격형 vs 방어형 평균
    off_vals = [sector_ma200[k] for k in OFFENSIVE_SECTORS if k in sector_ma200]
    def_vals = [sector_ma200[k] for k in DEFENSIVE_SECTORS if k in sector_ma200]
    offensive_avg = sum(off_vals) / len(off_vals) if off_vals else None
    defensive_avg = sum(def_vals) / len(def_vals) if def_vals else None

    # 상세 정보
    details = []
    # 강세 섹터 (vs_ma200 상위)
    sorted_sectors = sorted(sector_ma200.items(), key=lambda x: x[1], reverse=True)
    strong = [f"{INDICATORS[k]['name']}({v:+.1f}%)" for k, v in sorted_sectors if v > 0]
    weak = [f"{INDICATORS[k]['name']}({v:+.1f}%)" for k, v in sorted_sectors if v < 0]
    if strong:
        details.append("강세: " + ", ".join(strong[:3]))
    if weak:
        details.append("약세: " + ", ".join(weak[:3]))

    return best_phase, details, offensive_avg, defensive_avg


# ── 매크로 심리 판정 ──────────────────────────────────────────────────────────
def assess_sentiment(indicators):
    """Risk-On/Off 판정"""
    reasons = []
    score = 0  # 양수=Risk-On, 음수=Risk-Off

    # 1) VIX 기반
    vix = indicators.get("vix")
    if vix:
        vix_val = vix["current"]
        if vix_val < 20:
            score += 2
            reasons.append(f"VIX {vix_val:.1f} < 20 (안정)")
        elif vix_val > 30:
            score -= 2
            reasons.append(f"VIX {vix_val:.1f} > 30 (공포)")
        else:
            reasons.append(f"VIX {vix_val:.1f} (20~30 경계)")

    # 2) US 10Y Yield
    us10y = indicators.get("us10y")
    if us10y:
        y = us10y["current"]
        if y > 4.5:
            score -= 1
            reasons.append(f"10Y {y:.2f}% > 4.5% (긴축)")
        elif y < 3.5:
            score += 1
            reasons.append(f"10Y {y:.2f}% < 3.5% (완화)")
        else:
            reasons.append(f"10Y {y:.2f}% (중립)")

    # 3) 주식 MA200 위 비율 (Russell 포함)
    equity_keys = ["sp500", "nasdaq", "kospi", "kosdaq", "russell"]
    above_ma200 = 0
    total_equity = 0
    for k in equity_keys:
        ind = indicators.get(k)
        if ind and ind["vs_ma200"] is not None:
            total_equity += 1
            if ind["vs_ma200"] > 0:
                above_ma200 += 1

    if total_equity > 0:
        ratio = above_ma200 / total_equity
        if ratio >= 0.75:
            score += 1
        elif ratio <= 0.25:
            score -= 1
        reasons.append(f"주식 {above_ma200}/{total_equity} MA200 위")

    # 4) 금 52주 고점 근접 여부 (고점 대비 -3% 이내)
    gold = indicators.get("gold")
    if gold and gold["off_high"] is not None:
        if gold["off_high"] > -3:
            score -= 1
            reasons.append("금 52주 고점 근처")
        else:
            score += 1
            reasons.append(f"금 고점 대비 {gold['off_high']:.1f}%")

    # 5) 구리 MA200 위 → Risk-On
    copper = indicators.get("copper")
    if copper and copper["vs_ma200"] is not None:
        if copper["vs_ma200"] > 0:
            score += 1
            reasons.append(f"구리 MA200 위 ({copper['vs_ma200']:+.1f}%)")
        else:
            reasons.append(f"구리 MA200 아래 ({copper['vs_ma200']:.1f}%)")

    # 6) USD/KRW MA200 위(원화 약세) → Risk-Off
    usdkrw = indicators.get("usdkrw")
    if usdkrw and usdkrw["vs_ma200"] is not None:
        if usdkrw["vs_ma200"] > 0:
            score -= 1
            reasons.append(f"USD/KRW MA200 위 (원화 약세)")
        else:
            reasons.append(f"USD/KRW MA200 아래 (원화 강세)")

    # 7) USD/JPY 급락 (엔캐리 청산 위험) - 전일비 -1% 이상 하락 시 경고
    usdjpy = indicators.get("usdjpy")
    if usdjpy:
        if usdjpy["change_pct"] < -1.0:
            reasons.append(f"⚠ USD/JPY 급락 {usdjpy['change_pct']:.2f}% (엔캐리 청산 위험)")

    # 8) BTC MA200 위 → Risk-On
    btc = indicators.get("btc")
    if btc and btc["vs_ma200"] is not None:
        if btc["vs_ma200"] > 0:
            score += 1
            reasons.append(f"BTC MA200 위 ({btc['vs_ma200']:+.1f}%)")
        else:
            reasons.append(f"BTC MA200 아래 ({btc['vs_ma200']:.1f}%)")

    # 9) 경기사이클 판정
    cycle_phase, _, _, _ = assess_cycle(indicators)
    if cycle_phase in ("초기 회복", "중기 확장"):
        score += 1
        reasons.append(f"경기사이클: {cycle_phase} (확장)")
    elif cycle_phase == "침체/방어":
        score -= 1
        reasons.append(f"경기사이클: {cycle_phase} (수축)")
    elif cycle_phase == "후기 과열":
        reasons.append(f"경기사이클: {cycle_phase} (경계)")

    # 판정
    if score >= 2:
        label = "[+] Risk-On (위험선호)"
    elif score <= -2:
        label = "[!] Risk-Off (위험회피)"
    else:
        label = "[.] Neutral (중립)"

    return label, reasons


# ── 기간 변동률 출력 ─────────────────────────────────────────────────────────
def _print_period_changes(indicators, W):
    """기간 변동률 테이블 출력"""
    period_changes, period_labels = calc_period_changes(indicators)
    has_any = any(
        period_changes.get(k, {}).get(l) is not None
        for k in indicators for l in period_labels
    )
    if not has_any:
        return

    print()
    print(f" {'[기간 변동률]':<24s}", end="")
    for l in period_labels:
        print(f" {l:>8s}", end="")
    print()
    print(" " + "-" * (24 + 9 * len(period_labels)))
    for _section_name, keys in SECTIONS:
        for key in keys:
            if key not in period_changes:
                continue
            pc = period_changes[key]
            if all(pc.get(l) is None for l in period_labels):
                continue
            name = INDICATORS[key]["name"]
            print(f" {name:<24s}", end="")
            for l in period_labels:
                v = pc.get(l)
                if v is None:
                    print(f" {'---':>8s}", end="")
                else:
                    sign = "+" if v >= 0 else ""
                    print(f" {sign}{v:>6.1f}%", end="")
            print()
    print("=" * W)


# ── 대시보드 출력 ─────────────────────────────────────────────────────────────
def print_dashboard(indicators, cycle_result=None):
    """대시보드 출력 후 (sentiment, reasons) 튜플 반환"""
    W = 74
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print()
    print("=" * W)
    print(f"  Macro Indicator Screener  |  {now}")
    print("=" * W)
    print(f" {'지표':<24s} {'현재가':>10s}  {'등락':>20s}  {'vs MA200':>8s}  {'52주고점':>7s}")
    print("-" * W)

    for section_name, keys in SECTIONS:
        print(f"\n [{section_name}]")
        for key in keys:
            ind = indicators.get(key)
            name = INDICATORS[key]["name"]
            if ind is None:
                print(f" {name:<24s} {'N/A':>10s}  {'N/A':>20s}  {'N/A':>8s}  {'N/A':>7s}")
                continue

            price_str = fmt_price(key, ind["current"])
            change_str = fmt_change(ind["change"], ind["change_pct"])
            ma200_str = fmt_ma200(ind["vs_ma200"])
            off_high_str = f"{ind['off_high']:.1f}%" if ind["off_high"] is not None else "N/A"

            print(f" {name:<24s} {price_str:>10s}  {change_str:>20s}  {ma200_str:>8s}  {off_high_str:>7s}")

    # 매크로 심리 (1회만 호출)
    sentiment, reasons = assess_sentiment(indicators)
    print()
    print("-" * W)
    print(f" 매크로 심리: {sentiment}")
    print(f" 근거: {' / '.join(reasons)}")

    # 경기사이클 판정 출력
    if cycle_result:
        phase, details, off_avg, def_avg = cycle_result
        off_str = f"{off_avg:+.1f}%" if off_avg is not None else "N/A"
        def_str = f"{def_avg:+.1f}%" if def_avg is not None else "N/A"
        print(f" 경기 국면: [{phase}] (공격형 avg {off_str} vs 방어형 avg {def_str})")
        for d in details:
            print(f" {d}")
    print("=" * W)

    # 기간 변동률
    _print_period_changes(indicators, W)

    # 에러 표시
    if _errors:
        print(f"\n [!] {len(_errors)}개 오류:")
        for e in _errors:
            print(f"   - {e}")
    print()

    return sentiment, reasons


# ── 메인 ──────────────────────────────────────────────────────────────────────
def main():
    _errors.clear()
    print("\n 데이터 수집 중...")

    df = fetch_all_data()
    indicators = process_indicators(df)

    if not indicators:
        print(" [!] 데이터를 가져올 수 없습니다.")
        if _errors:
            for e in _errors:
                print(f"   - {e}")
        return

    # 경기사이클 판정
    cycle_result = assess_cycle(indicators)

    # print_dashboard가 sentiment를 반환 → 이중 호출 제거
    sentiment, reasons = print_dashboard(indicators, cycle_result=cycle_result)

    # 히스토리 저장
    phase, details, _, _ = cycle_result
    path = save_history(indicators, sentiment, reasons, cycle_phase=phase, cycle_details=details)
    print(f" 히스토리 저장: {path}")



if __name__ == "__main__":
    main()
