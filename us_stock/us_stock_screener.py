"""
US Consolidation Breakout Screener
Finviz로 SMA200 돌파 + 거래량 급증 후보 수집 → yfinance로 횡보 판정 정밀 필터
"""

import sys, io, datetime, argparse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import pandas as pd
import numpy as np
import yfinance as yf
from finvizfinance.screener.overview import Overview
from finvizfinance.screener.technical import Technical
from finvizfinance.screener.ownership import Ownership

_errors = []

W = 80
KRW_RATE = 1450

# Finviz Market Cap 필터 매핑 (상한)
MCAP_FILTERS = {
    10:  "-Mid (under $10bln)",
    50:  "-Large (under $200bln)",   # Finviz에 $50B 정확한 필터 없음 → $200B 이하로 넓게 받고 후처리
    200: "-Large (under $200bln)",
}

# Finviz Relative Volume 필터 매핑
RVOL_FILTERS = {
    1.0: "Over 1",
    1.5: "Over 1.5",
    2.0: "Over 2",
    3.0: "Over 3",
    5.0: "Over 5",
}


# ──────────────────────────────────────────────────────────────
# 1단계: Finviz 스크리닝 (서버 측 필터)
# ──────────────────────────────────────────────────────────────

def _get_mcap_filter(max_cap_b):
    """시총 상한에 맞는 Finviz 필터 반환 (넓게 잡고 후처리)"""
    if max_cap_b <= 2:
        return "-Small (under $2bln)"
    elif max_cap_b <= 10:
        return "-Mid (under $10bln)"
    else:
        return "-Large (under $200bln)"


def _get_rvol_filter(vol_ratio):
    """볼륨 배수에 맞는 Finviz 필터 반환"""
    for threshold in sorted(RVOL_FILTERS.keys(), reverse=True):
        if vol_ratio >= threshold:
            return RVOL_FILTERS[threshold]
    return "Over 1"


def fetch_finviz_candidates(max_cap_b=50, vol_ratio=1.5):
    """Finviz에서 SMA200 돌파 + 거래량 급증 후보 수집

    Overview: 종목명, 시총, 가격
    Technical: SMA200%, Relative Volume, RSI 등
    """
    mcap_filter = _get_mcap_filter(max_cap_b)
    rvol_filter = _get_rvol_filter(vol_ratio)

    filters = {
        "Market Cap.": mcap_filter,
        "200-Day Simple Moving Average": "Price above SMA200",
        "Relative Volume": rvol_filter,
        "Average Volume": "Over 100K",
        "Country": "USA",
        "Industry": "Stocks only (ex-Funds)",
    }

    # Overview → 종목명, 시총
    print(f"       필터: 시총 {mcap_filter} | SMA200 위 | 상대거래량 {rvol_filter} | 평균거래량 >100K")
    try:
        print("       Overview 데이터 조회 중...")
        ov = Overview()
        ov.set_filter(filters_dict=filters)
        df_ov = ov.screener_view(verbose=0)
        print(f"       Overview: {len(df_ov) if df_ov is not None else 0}개")
    except Exception as e:
        _errors.append(f"Finviz Overview: {e}")
        df_ov = None

    # Technical → SMA200%, RSI, Relative Volume
    try:
        print("       Technical 데이터 조회 중...")
        tech = Technical()
        tech.set_filter(filters_dict=filters)
        df_tech = tech.screener_view(verbose=0)
        print(f"       Technical: {len(df_tech) if df_tech is not None else 0}개")
    except Exception as e:
        _errors.append(f"Finviz Technical: {e}")
        df_tech = None

    # Ownership → 기관/내부자 보유, 공매도, Float
    try:
        print("       Ownership 데이터 조회 중...")
        own = Ownership()
        own.set_filter(filters_dict=filters)
        df_own = own.screener_view(verbose=0)
        print(f"       Ownership: {len(df_own) if df_own is not None else 0}개")
    except Exception as e:
        _errors.append(f"Finviz Ownership: {e}")
        df_own = None

    if df_ov is None or df_tech is None:
        return None

    if df_ov.empty:
        return pd.DataFrame()

    # 병합
    merged = df_ov.merge(df_tech[["Ticker", "SMA200", "RSI", "ATR"]], on="Ticker", how="left")
    if df_own is not None and not df_own.empty:
        own_cols = ["Ticker", "Insider Own", "Insider Trans", "Inst Own", "Inst Trans", "Short Float", "Short Ratio"]
        available = [c for c in own_cols if c in df_own.columns]
        merged = merged.merge(df_own[available], on="Ticker", how="left")
    return merged


# ──────────────────────────────────────────────────────────────
# 2단계: 시가총액 정밀 필터
# ──────────────────────────────────────────────────────────────

MIN_MCAP_KRW = 5000e8  # 5000억원

def filter_market_cap(df, max_cap_b):
    """Finviz 결과에서 시총 정밀 필터 (Market Cap은 이미 float)"""
    df = df.copy()
    df = df.dropna(subset=["Market Cap"])
    df = df[df["Market Cap"] < max_cap_b * 1e9]
    min_cap = MIN_MCAP_KRW / KRW_RATE  # 달러 환산
    df = df[df["Market Cap"] >= min_cap]
    df["mcap_b"] = df["Market Cap"] / 1e9
    df["mcap_krw_t"] = df["Market Cap"] * KRW_RATE / 1e12
    return df


# ──────────────────────────────────────────────────────────────
# 3단계: 횡보 판정 (yfinance 정밀 분석)
# ──────────────────────────────────────────────────────────────

def analyze_consolidation(tickers, consolidation_threshold=0.30):
    """후보 종목의 1년 데이터 다운로드 → 횡보 판정 + 돌파 최근도 계산"""
    if not tickers:
        return {}

    print(f"       {len(tickers)}개 후보 가격 데이터 다운로드...")
    try:
        df = yf.download(tickers if len(tickers) > 1 else tickers[0],
                         period="1y", progress=False, threads=True)
        if df.empty:
            _errors.append("yfinance 다운로드 실패")
            return {}
        print(f"       다운로드 완료 ({len(df)}일치 데이터)")
    except Exception as e:
        _errors.append(f"yfinance: {e}")
        return {}

    multi = isinstance(df.columns, pd.MultiIndex) and len(tickers) > 1
    results = {}
    analyzed = 0
    passed = 0

    for ticker in tickers:
        try:
            if multi:
                close = df["Close"][ticker].dropna()
            else:
                close = df["Close"].dropna()

            if len(close) < 200:
                continue

            sma200 = close.rolling(200).mean()
            current_close = close.iloc[-1]
            current_sma200 = sma200.iloc[-1]

            if pd.isna(current_sma200) or current_close <= current_sma200:
                continue

            # 돌파 최근도 (20일 이내만 통과)
            below_mask = close < sma200
            if below_mask.any():
                last_below_loc = close.index.get_loc(below_mask[below_mask].index[-1])
                days_since = len(close) - 1 - last_below_loc
            else:
                days_since = 999

            if days_since > 20:
                continue

            # 횡보 판정: 돌파 전 3~6개월(60~120일) 구간에서 최적 횡보 구간 탐색
            breakout_loc = len(close) - 1 - days_since
            best_range = None
            for window in [120, 100, 80, 60]:
                c_start = max(0, breakout_loc - window)
                c_period = close.iloc[c_start:breakout_loc]
                if len(c_period) < 40:
                    continue
                c_avg = c_period.mean()
                if c_avg == 0:
                    continue
                c_rng = (c_period.max() - c_period.min()) / c_avg
                if best_range is None or c_rng < best_range:
                    best_range = c_rng
                    consol_period = c_period

            if best_range is None:
                continue
            consol_range = best_range

            is_passed = consol_range <= consolidation_threshold
            results[ticker] = {
                "consol_range": consol_range,
                "days_since_breakout": days_since,
                "passed": is_passed,
            }
            analyzed += 1
            if is_passed:
                passed += 1
            if analyzed % 20 == 0:
                print(f"       분석 {analyzed}/{len(tickers)}... (통과: {passed}개)")
        except Exception:
            continue

    print(f"       분석 완료: {analyzed}개 분석, {passed}개 횡보 조건 충족")
    return results


# ──────────────────────────────────────────────────────────────
# 4단계: 스코어링
# ──────────────────────────────────────────────────────────────

def calc_scores(df, consol_data, consolidation_threshold):
    """횡보 강도 + SMA200% + 볼륨 → 10점 만점"""
    rows = []
    for _, r in df.iterrows():
        ticker = r["Ticker"]
        cd = consol_data.get(ticker)
        if cd is None:
            consol_range = None
            days_since = None
        else:
            consol_range = cd["consol_range"]
            days_since = cd["days_since_breakout"]

        # SMA200% (Finviz에서 소수로 반환, 예: 0.05 = 5%)
        sma200_pct = r.get("SMA200", 0)
        if pd.isna(sma200_pct):
            sma200_pct = 0
        sma200_pct_display = sma200_pct * 100  # 퍼센트로 변환

        # 횡보 강도: 좁을수록 높음 (0~4점)
        if consol_range is not None:
            consol_score = max(0, (consolidation_threshold - consol_range) / consolidation_threshold) * 4.0
        else:
            consol_score = 2.0  # 횡보 데이터 없으면 중간값

        # SMA200 근접도: 0~10% 범위에서 가까울수록 높음 (0~3점) → 막 돌파한 것이 좋음
        sma_score = max(0, (10 - abs(sma200_pct_display)) / 10) * 3.0

        # 돌파 최근도 (0~3점, 20일 이내)
        if days_since is not None and days_since <= 20:
            recency_score = max(0, (20 - days_since) / 20) * 3.0
        else:
            recency_score = 0.0

        score = round(consol_score + sma_score + recency_score, 1)

        rows.append({
            "ticker": ticker,
            "name": r.get("Company", ticker),
            "sector": r.get("Sector", "-"),
            "mcap_b": r["mcap_b"],
            "mcap_krw_t": r["mcap_krw_t"],
            "price": r["Price"],
            "sma200_pct": sma200_pct_display,
            "consol_range": consol_range,
            "days_since": days_since,
            "volume": r.get("Volume", 0),
            "change": r.get("Change", 0),
            "rsi": r.get("RSI", None),
            "Insider Own": r.get("Insider Own", None),
            "Inst Own": r.get("Inst Own", None),
            "Short Float": r.get("Short Float", None),
            "score": score,
        })

    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows


# ──────────────────────────────────────────────────────────────
# 출력
# ──────────────────────────────────────────────────────────────

def print_results(results, args):
    now = datetime.datetime.now().strftime("%Y-%m-%d")

    pd.set_option("display.unicode.east_asian_width", True)
    pd.set_option("display.max_rows", 100)
    pd.set_option("display.width", 140)
    pd.set_option("display.max_columns", 20)

    print()
    print("=" * W)
    print(f"  US Consolidation Breakout Screener  |  {now}")
    print(f"  시총 <${args.market_cap}B | 횡보 <{int(args.consolidation*100)}% | SMA200 돌파 | 상대거래량 >{args.volume_ratio}x")
    print("=" * W)

    if not results:
        print()
        print("  조건에 맞는 종목이 없습니다.")
        print("=" * W)
        _print_errors()
        return

    top = results[:args.top]

    print()
    print(f" [스크리닝 결과] ({len(results)}개 종목, 상위 {min(args.top, len(results))}개 표시)")
    print()

    rows = []
    for r in top:
        consol_str = f"{r['consol_range']*100:.0f}%" if r["consol_range"] is not None else "-"
        days_str = f"{r['days_since']}일" if r["days_since"] is not None else "-"
        rsi_str = f"{r['rsi']:.0f}" if r["rsi"] is not None and not pd.isna(r["rsi"]) else "-"
        change_val = r["change"]
        if isinstance(change_val, str):
            change_str = change_val
        elif pd.notna(change_val):
            change_str = f"{change_val*100:+.1f}%" if abs(change_val) < 1 else f"{change_val:+.1f}%"
        else:
            change_str = "-"

        # 섹터 한글 매핑
        sector_short = {
            "Technology": "기술",
            "Healthcare": "헬스케어",
            "Financial": "금융",
            "Consumer Cyclical": "경기소비재",
            "Consumer Defensive": "필수소비재",
            "Communication Services": "커뮤니케이션",
            "Industrials": "산업재",
            "Basic Materials": "소재",
            "Real Estate": "부동산",
            "Utilities": "유틸리티",
            "Energy": "에너지",
        }.get(r.get("sector", ""), r.get("sector", "-")[:6])

        # Ownership 정보 (Insider Own, Inst Own: float 소수, Short Float: "19.59%" 문자열)
        insider_own = r.get("Insider Own", None)
        inst_own = r.get("Inst Own", None)
        short_float = r.get("Short Float", None)
        insider_own_str = f"{insider_own*100:.1f}%" if pd.notna(insider_own) and isinstance(insider_own, (int, float)) else "-"
        inst_own_str = f"{inst_own*100:.1f}%" if pd.notna(inst_own) and isinstance(inst_own, (int, float)) else "-"
        float_short_str = str(short_float) if pd.notna(short_float) else "-"

        rows.append({
            "티커": r["ticker"],
            "종목명": (r["name"][:14] + "..") if len(str(r["name"])) > 16 else r["name"],
            "섹터": sector_short,
            "시총($B)": f"{r['mcap_b']:.1f}",
            "시총(조원)": f"{r['mcap_krw_t']:.2f}",
            "현재가": f"${r['price']:.2f}" if r["price"] < 1000 else f"${r['price']:,.0f}",
            "등락": change_str,
            "SMA200%": f"+{r['sma200_pct']:.1f}%",
            "횡보%": consol_str,
            "돌파": days_str,
            "RSI": rsi_str,
            "내부자": insider_own_str,
            "기관": inst_own_str,
            "공매도": float_short_str,
            "점수": f"{r['score']:.1f}",
        })

    result_df = pd.DataFrame(rows)
    result_df.index = range(1, len(result_df) + 1)
    result_df.index.name = "#"
    print(result_df.to_string())

    print()
    print("=" * W)
    _print_errors()
    print()


def _print_errors():
    if _errors:
        print(f"\n [!] {len(_errors)}개 오류:")
        for e in _errors[:5]:
            print(f"   - {e}")
        if len(_errors) > 5:
            print(f"   ... 외 {len(_errors) - 5}개")


# ──────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="US Consolidation Breakout Screener")
    parser.add_argument("--market-cap", type=float, default=50, help="시총 상한 ($B, 기본: 50)")
    parser.add_argument("--consolidation", type=float, default=0.30, help="횡보 임계값 (가격범위/평균, 기본: 0.30)")
    parser.add_argument("--volume-ratio", type=float, default=1.5, help="거래량 급증 배수 (기본: 1.5)")
    parser.add_argument("--top", type=int, default=30, help="상위 N개 출력 (기본: 30)")
    parser.add_argument("--skip-consolidation", action="store_true", help="횡보 필터 건너뛰기 (빠른 실행)")
    args = parser.parse_args()

    print()
    print(" [1/4] Finviz 스크리닝 중... (SMA200 돌파 + 거래량 급증)")
    df = fetch_finviz_candidates(max_cap_b=args.market_cap, vol_ratio=args.volume_ratio)
    if df is None or df.empty:
        print("  Finviz 결과 없음")
        print_results([], args)
        return
    print(f"       {len(df)}개 후보 수집")

    min_krw = int(MIN_MCAP_KRW / 1e8)
    print(f" [2/4] 시가총액 필터... ({min_krw}억원 ~ ${args.market_cap}B)")
    before = len(df)
    df = filter_market_cap(df, args.market_cap)
    print(f"       {before}개 → {len(df)}개 통과")

    if df.empty:
        print_results([], args)
        return

    # 횡보 필터
    tickers = df["Ticker"].tolist()
    consol_data = {}

    if not args.skip_consolidation:
        print(f" [3/4] 횡보 판정 중... (yfinance 1년 데이터)")
        consol_data = analyze_consolidation(tickers, args.consolidation)

        # 횡보 통과 종목만 필터
        passed_tickers = {t for t, d in consol_data.items() if d["passed"]}
        if passed_tickers:
            df = df[df["Ticker"].isin(passed_tickers)]
            print(f"       {len(df)}개 횡보 필터 통과 (< {int(args.consolidation*100)}%)")
        else:
            # 횡보 필터 통과 종목이 없으면 전체 유지 (완화)
            print(f"       횡보 필터 통과 종목 없음 → 전체 {len(df)}개 유지")
    else:
        print(" [3/4] 횡보 필터 건너뜀 (--skip-consolidation)")

    print(f" [4/4] 스코어링... ({len(df)}개 종목)")
    results = calc_scores(df, consol_data, args.consolidation)
    print(f"       완료 (최고 {results[0]['score']:.1f}점 ~ 최저 {results[-1]['score']:.1f}점)" if results else "       결과 없음")

    print_results(results, args)


if __name__ == "__main__":
    main()
