"""
MA Touch Analyzer
주요 지수(NDX, RUT 등)의 이동평균선 터치 분석 → 매수 기회 판단
"""

import sys, io, datetime, argparse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import pandas as pd
import numpy as np
import yfinance as yf

_errors = []

# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────

TOUCH_THRESHOLD = 2.0       # MA ±% 이내를 "터치"로 판정
CLUSTER_GAP = 5             # 이 이하 간격의 터치는 하나의 이벤트로 클러스터링

FORWARD_PERIODS_BASIC = {"3M": 63, "6M": 126, "1Y": 252}
FORWARD_PERIODS_DETAIL = {"1W": 5, "1M": 21, "3M": 63, "6M": 126, "1Y": 252}

# 닷컴버블/2008 금융위기 — 통계 제외 구간 (발생 안 한다는 가정)
EXCLUDED_PERIODS = [
    (pd.Timestamp("2000-01-01"), pd.Timestamp("2003-12-31")),
    (pd.Timestamp("2007-10-01"), pd.Timestamp("2009-06-30")),
]

# ── 티커별 설정 ──

TICKER_CONFIGS = {
    "NDX": {
        "symbol": "^NDX",
        "name": "NDX (나스닥100)",
        "start": "1996-01-01",
        "black_swans": [
            {"name": "2011 유럽재정위기",   "peak": "2011-07-22", "trough": "2011-10-03"},
            {"name": "2018 금리인상 쇼크",  "peak": "2018-10-01", "trough": "2018-12-24"},
            {"name": "COVID-19 팬데믹",     "peak": "2020-02-19", "trough": "2020-03-23"},
            {"name": "2022 인플레 베어마켓", "peak": "2021-11-19", "trough": "2022-10-13"},
            {"name": "2025 관세전쟁",       "peak": "2025-02-19", "trough": "2025-04-07"},
        ],
    },
    "RUT": {
        "symbol": "^RUT",
        "name": "RUT (러셀2000)",
        "start": "1996-01-01",
        "black_swans": [
            {"name": "2011 유럽재정위기",   "peak": "2011-07-07", "trough": "2011-10-04"},
            {"name": "2015 중국발 쇼크",    "peak": "2015-06-23", "trough": "2016-02-11"},
            {"name": "2018 금리인상 쇼크",  "peak": "2018-08-31", "trough": "2018-12-24"},
            {"name": "COVID-19 팬데믹",     "peak": "2020-01-16", "trough": "2020-03-18"},
            {"name": "2022 인플레 베어마켓", "peak": "2021-11-08", "trough": "2022-06-16"},
            {"name": "2025 관세전쟁",       "peak": "2024-11-25", "trough": "2025-04-07"},
        ],
    },
    "SPX": {
        "symbol": "^GSPC",
        "name": "SPX (S&P500)",
        "start": "1996-01-01",
        "black_swans": [
            {"name": "2011 유럽재정위기",   "peak": "2011-07-07", "trough": "2011-10-03"},
            {"name": "2015 중국발 쇼크",    "peak": "2015-07-20", "trough": "2016-02-11"},
            {"name": "2018 금리인상 쇼크",  "peak": "2018-09-20", "trough": "2018-12-24"},
            {"name": "COVID-19 팬데믹",     "peak": "2020-02-19", "trough": "2020-03-23"},
            {"name": "2022 인플레 베어마켓", "peak": "2022-01-03", "trough": "2022-10-12"},
            {"name": "2025 관세전쟁",       "peak": "2025-02-19", "trough": "2025-04-07"},
        ],
    },
    "NQ": {
        "symbol": "NQ=F",
        "name": "NQ (나스닥100 선물)",
        "start": "2000-01-01",
        "black_swans": [
            {"name": "2011 유럽재정위기",   "peak": "2011-07-22", "trough": "2011-10-03"},
            {"name": "2018 금리인상 쇼크",  "peak": "2018-10-01", "trough": "2018-12-24"},
            {"name": "COVID-19 팬데믹",     "peak": "2020-02-19", "trough": "2020-03-23"},
            {"name": "2022 인플레 베어마켓", "peak": "2021-11-19", "trough": "2022-10-13"},
            {"name": "2025 관세전쟁",       "peak": "2025-02-19", "trough": "2025-04-07"},
        ],
    },
    "BTC": {
        "symbol": "BTC-USD",
        "name": "BTC (비트코인)",
        "start": "2014-09-01",
        "black_swans": [
            {"name": "2018 크립토 겨울",   "peak": "2017-12-17", "trough": "2018-12-15"},
            {"name": "COVID-19 팬데믹",     "peak": "2020-02-14", "trough": "2020-03-12"},
            {"name": "2022 크립토 겨울",   "peak": "2021-11-10", "trough": "2022-11-21"},
            {"name": "2025 관세전쟁",       "peak": "2025-01-20", "trough": "2025-04-07"},
        ],
    },
}

DEFAULT_TICKER = "NDX"

# 액션 요약에서 MA 우선순위 (높을수록 의미있음)
MA_PRIORITY = [
    ("주봉", "200주선", "MA200"),
    ("주봉", "100주선", "MA100"),
    ("일봉", "200일선", "MA200"),
    ("일봉", "100일선", "MA100"),
]

W = 80

# ──────────────────────────────────────────────────────────────
# 공통 유틸
# ──────────────────────────────────────────────────────────────

def _pct_diff(close, ma_val):
    """종가 vs MA 괴리율 (%)"""
    return (close - ma_val) / ma_val * 100


def _is_in_excluded(date):
    for start, end in EXCLUDED_PERIODS:
        if start <= date <= end:
            return True
    return False


def _fmt_pct(val):
    if val is None:
        return "---"
    return f"{val:+.1f}%"


def _fmt_price(val):
    return f"{val:,.0f}"


# ──────────────────────────────────────────────────────────────
# 데이터 수집
# ──────────────────────────────────────────────────────────────

def fetch_data(ticker_cfg, interval="1d"):
    symbol = ticker_cfg["symbol"]
    start = ticker_cfg["start"]
    try:
        df = yf.download(symbol, start=start, interval=interval, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df = df.droplevel(level=1, axis=1)
        if df.empty:
            _errors.append(f"{symbol} 데이터 비어있음 (interval={interval})")
            return pd.DataFrame()
        df.index = pd.to_datetime(df.index)
        return df
    except Exception as e:
        _errors.append(f"{symbol} 다운로드 실패 ({interval}): {e}")
        return pd.DataFrame()


# ──────────────────────────────────────────────────────────────
# 이동평균 계산
# ──────────────────────────────────────────────────────────────

def calc_moving_averages(df):
    df = df.copy()
    df["MA100"] = df["Close"].rolling(100).mean()
    df["MA200"] = df["Close"].rolling(200).mean()
    return df


# ──────────────────────────────────────────────────────────────
# 터치 감지
# ──────────────────────────────────────────────────────────────

def detect_touches(df, ma_col, threshold=TOUCH_THRESHOLD):
    pct = _pct_diff(df["Close"], df[ma_col])
    return pct.abs() <= threshold


def cluster_touch_events(df, touch_mask, ma_col, min_gap=CLUSTER_GAP):
    events = []
    touch_dates = df.index[touch_mask]
    if len(touch_dates) == 0:
        return events

    cluster_start = touch_dates[0]
    cluster_end = touch_dates[0]

    for i in range(1, len(touch_dates)):
        gap = len(df.loc[cluster_end:touch_dates[i]]) - 1
        if gap <= min_gap:
            cluster_end = touch_dates[i]
        else:
            events.append(_make_event(df, cluster_start, ma_col))
            cluster_start = touch_dates[i]
            cluster_end = touch_dates[i]

    events.append(_make_event(df, cluster_start, ma_col))
    return events


def _make_event(df, date, ma_col):
    row = df.loc[date]
    close = row["Close"]
    ma_val = row[ma_col]
    pct = _pct_diff(close, ma_val)

    loc = df.index.get_loc(date)
    lookback = max(0, loc - 5)
    prev_closes = df["Close"].iloc[lookback:loc]
    prev_ma = df[ma_col].iloc[lookback:loc]
    if len(prev_closes) > 0:
        above_count = (prev_closes > prev_ma).sum()
        direction = "Above" if above_count > len(prev_closes) / 2 else "Below"
    else:
        direction = "---"

    return {
        "date": date,
        "close": close,
        "ma_val": ma_val,
        "pct_diff": pct,
        "direction": direction,
    }


# ──────────────────────────────────────────────────────────────
# 수익률 분석
# ──────────────────────────────────────────────────────────────

def calc_forward_returns(df, events, detail=False):
    closes = df["Close"]
    periods = FORWARD_PERIODS_DETAIL if detail else FORWARD_PERIODS_BASIC
    for ev in events:
        loc = df.index.get_loc(ev["date"])
        base = ev["close"]

        for label, days in periods.items():
            target_loc = loc + days
            if target_loc < len(closes):
                ev[label] = (closes.iloc[target_loc] - base) / base * 100
            else:
                ev[label] = None

        # MDD (1Y 이내)
        end_loc = min(loc + 252, len(closes))
        if loc + 1 < end_loc:
            future = closes.iloc[loc:end_loc]
            running_max = future.cummax()
            drawdown = (future - running_max) / running_max * 100
            ev["MDD"] = drawdown.min()
        else:
            ev["MDD"] = None

    return events


# ──────────────────────────────────────────────────────────────
# 통계 집계
# ──────────────────────────────────────────────────────────────

def calc_statistics(events, detail=False):
    periods = FORWARD_PERIODS_DETAIL if detail else FORWARD_PERIODS_BASIC
    stats = {}
    for label in periods:
        vals = [ev[label] for ev in events if ev.get(label) is not None]
        if vals:
            wins = [v for v in vals if v > 0]
            stats[label] = {
                "count": len(vals),
                "mean": np.mean(vals),
                "median": np.median(vals),
                "win_rate": len(wins) / len(vals) * 100,
                "max": max(vals),
                "min": min(vals),
            }
        else:
            stats[label] = None

    mdds = [ev["MDD"] for ev in events if ev.get("MDD") is not None]
    stats["avg_mdd"] = np.mean(mdds) if mdds else None
    return stats


def calc_buy_score(stats):
    score = 0.0
    weights = 0.0

    s3 = stats.get("3M")
    if s3:
        score += (s3["win_rate"] / 100) * 4.0
        weights += 4.0

    s6 = stats.get("6M")
    if s6:
        r = min(s6["mean"] / 20.0, 1.0) * 3.0
        score += max(r, 0)
        weights += 3.0

    if s6:
        score += (s6["win_rate"] / 100) * 2.0
        weights += 2.0

    avg_mdd = stats.get("avg_mdd")
    if avg_mdd is not None:
        mdd_score = max(0, min(1.0, (avg_mdd + 30) / 25)) * 1.0
        score += mdd_score
        weights += 1.0

    return round(score / weights * 10, 1) if weights > 0 else 0.0


# ──────────────────────────────────────────────────────────────
# MA200 하방 돌파 → 회복 분석
# ──────────────────────────────────────────────────────────────

def detect_breakdowns(df, ma_col="MA200"):
    closes = df["Close"]
    ma = df[ma_col]
    below = closes < ma

    events = []
    in_breakdown = False
    start_date = None
    start_close = None
    start_ma = None
    max_drawdown_pct = 0.0

    for i in range(1, len(df)):
        if pd.isna(ma.iloc[i]):
            continue

        was_below = below.iloc[i - 1] if not pd.isna(below.iloc[i - 1]) else False
        is_below = below.iloc[i]

        if not in_breakdown and not was_below and is_below:
            in_breakdown = True
            start_date = df.index[i]
            start_close = closes.iloc[i]
            start_ma = ma.iloc[i]
            max_drawdown_pct = _pct_diff(start_close, start_ma)

        elif in_breakdown:
            dd = _pct_diff(closes.iloc[i], ma.iloc[i])
            if dd < max_drawdown_pct:
                max_drawdown_pct = dd

            if not is_below:
                end_date = df.index[i]
                days_below = len(df.loc[start_date:end_date]) - 1
                recovery_return = (closes.iloc[i] - start_close) / start_close * 100
                events.append({
                    "돌파일": start_date.strftime("%Y-%m-%d"),
                    "회복일": end_date.strftime("%Y-%m-%d"),
                    "돌파시 종가": _fmt_price(start_close),
                    "회복시 종가": _fmt_price(closes.iloc[i]),
                    "거래일": days_below,
                    "최대괴리": f"{max_drawdown_pct:.1f}%",
                    "회복수익률": f"{recovery_return:+.1f}%",
                })
                in_breakdown = False

    if in_breakdown:
        days_so_far = len(df.loc[start_date:]) - 1
        current_return = (closes.iloc[-1] - start_close) / start_close * 100
        events.append({
            "돌파일": start_date.strftime("%Y-%m-%d"),
            "회복일": "진행중",
            "돌파시 종가": _fmt_price(start_close),
            "회복시 종가": _fmt_price(closes.iloc[-1]),
            "거래일": days_so_far,
            "최대괴리": f"{max_drawdown_pct:.1f}%",
            "회복수익률": f"{current_return:+.1f}%",
        })

    events = [e for e in events if not _is_in_excluded(pd.Timestamp(e["돌파일"]))]
    return events


def breakdown_stats(events):
    completed = [e for e in events if e["회복일"] != "진행중"]
    if not completed:
        return None
    days = [e["거래일"] for e in completed]
    return {
        "건수": len(completed),
        "평균": f"{np.mean(days):.0f}일",
        "중앙값": f"{np.median(days):.0f}일",
        "최소": f"{min(days)}일",
        "최대": f"{max(days)}일",
    }


# ──────────────────────────────────────────────────────────────
# 블랙스완 이벤트 분석
# ──────────────────────────────────────────────────────────────

def analyze_black_swans(df, ticker_cfg):
    closes = df["Close"]
    ma200 = df["MA200"]
    results = []

    for ev in ticker_cfg["black_swans"]:
        peak_date = pd.Timestamp(ev["peak"])
        trough_date = pd.Timestamp(ev["trough"])

        if peak_date < df.index[0] or peak_date > df.index[-1]:
            continue

        peak_close = closes.asof(peak_date)
        trough_close = closes.asof(trough_date)
        if pd.isna(peak_close) or pd.isna(trough_close):
            continue

        drawdown = (trough_close - peak_close) / peak_close * 100

        mask_pt = (df.index >= peak_date) & (df.index <= trough_date)
        days_to_trough = mask_pt.sum()

        after_trough = closes[df.index > trough_date]
        recovered = after_trough[after_trough >= peak_close]
        if len(recovered) > 0:
            recovery_date = recovered.index[0]
            days_to_recover = ((df.index > trough_date) & (df.index <= recovery_date)).sum()
            recovery_str = recovery_date.strftime("%Y-%m-%d")
            total_days = ((df.index >= peak_date) & (df.index <= recovery_date)).sum()
        else:
            days_to_recover = None
            recovery_str = "미회복"
            total_days = None

        below_ma = closes < ma200
        trough_loc = df.index.searchsorted(trough_date, side="right") - 1
        if trough_loc < 0:
            trough_loc = 0

        ma_start = trough_loc
        while ma_start > 0 and below_ma.iloc[ma_start - 1]:
            ma_start -= 1

        ma_end = trough_loc
        while ma_end < len(df) - 1 and below_ma.iloc[ma_end + 1]:
            ma_end += 1

        if below_ma.iloc[trough_loc]:
            ma200_days = ma_end - ma_start + 1
        else:
            ma200_days = 0

        results.append({
            "이벤트": ev["name"],
            "고점": peak_date.strftime("%Y-%m-%d"),
            "저점": trough_date.strftime("%Y-%m-%d"),
            "낙폭": f"{drawdown:.1f}%",
            "하락기간": f"{days_to_trough}일",
            "고점회복일": recovery_str,
            "회복기간": f"{days_to_recover}일" if days_to_recover else "---",
            "총 기간": f"{total_days}일" if total_days else "---",
            "MA200하방": f"{ma200_days}일",
        })

    return results


# ──────────────────────────────────────────────────────────────
# 블랙스완 구간 MA200 매수 시 최대손실 / 손절기회
# ──────────────────────────────────────────────────────────────

def analyze_touch_in_black_swan(df, touch_events, tf_label, ma_label, ticker_cfg):
    closes = df["Close"]
    results = []

    for bs in ticker_cfg["black_swans"]:
        peak = pd.Timestamp(bs["peak"])
        trough = pd.Timestamp(bs["trough"])
        window_start = peak - pd.Timedelta(days=90)
        window_end = trough

        hits = [ev for ev in touch_events
                if window_start <= ev["date"] <= window_end]
        if not hits:
            continue

        for ev in hits:
            loc = df.index.get_loc(ev["date"])
            entry = ev["close"]

            future_to_trough = closes.iloc[loc:]
            future_to_trough = future_to_trough[future_to_trough.index <= trough]
            if len(future_to_trough) > 1:
                min_price = future_to_trough.min()
                max_loss = (min_price - entry) / entry * 100
                min_date = future_to_trough.idxmin()
                days_to_min = len(df.loc[ev["date"]:min_date]) - 1
            else:
                max_loss = 0
                days_to_min = 0
                min_date = ev["date"]

            exit_opps = {}
            for window_name, window_days in [("5일", 5), ("10일", 10), ("20일", 20)]:
                end_loc = min(loc + window_days, len(closes) - 1)
                if loc + 1 <= end_loc:
                    window_high = closes.iloc[loc + 1:end_loc + 1].max()
                    exit_ret = (window_high - entry) / entry * 100
                    exit_opps[window_name] = f"{exit_ret:+.1f}%"
                else:
                    exit_opps[window_name] = "---"

            loc_1y = min(loc + 252, len(closes) - 1)
            ret_1y = (closes.iloc[loc_1y] - entry) / entry * 100 if loc + 252 < len(closes) else None

            results.append({
                "이벤트": bs["name"],
                "매수일": ev["date"].strftime("%Y-%m-%d"),
                "매수가": _fmt_price(entry),
                "최대손실": f"{max_loss:.1f}%",
                "최저일": min_date.strftime("%Y-%m-%d"),
                "손실기간": f"{days_to_min}일",
                "탈출5일": exit_opps["5일"],
                "탈출10일": exit_opps["10일"],
                "탈출20일": exit_opps["20일"],
                "버틴1Y": f"{ret_1y:+.1f}%" if ret_1y is not None else "---",
            })

    if not results:
        return None, None

    result_df = pd.DataFrame(results)

    max_losses = [float(r["최대손실"].replace("%", "")) for r in results]
    ret_1y_vals = [float(r["버틴1Y"].replace("%", "").replace("+", ""))
                   for r in results if r["버틴1Y"] != "---"]
    exit5_vals = [float(r["탈출5일"].replace("%", "").replace("+", ""))
                  for r in results if r["탈출5일"] != "---"]

    summary = {
        "구분": f"{tf_label} {ma_label}",
        "블랙스완 매수건": len(results),
        "평균 최대손실": f"{np.mean(max_losses):.1f}%",
        "최악 최대손실": f"{min(max_losses):.1f}%",
        "5일내 평균탈출": f"{np.mean(exit5_vals):+.1f}%" if exit5_vals else "---",
        "버틴1Y 평균": f"{np.mean(ret_1y_vals):+.1f}%" if ret_1y_vals else "---",
        "버틴1Y 승률": f"{sum(1 for v in ret_1y_vals if v > 0)/len(ret_1y_vals)*100:.0f}%" if ret_1y_vals else "---",
    }

    return result_df, summary


# ──────────────────────────────────────────────────────────────
# 현재 상태
# ──────────────────────────────────────────────────────────────

def check_current_status(df):
    last = df.iloc[-1]
    close = last["Close"]
    result = {"close": close, "date": df.index[-1]}

    for ma in ["MA100", "MA200"]:
        val = last[ma]
        if pd.notna(val):
            pct = _pct_diff(close, val)
            touching = abs(pct) <= TOUCH_THRESHOLD
            result[ma] = {"value": val, "pct": pct, "touching": touching}
        else:
            result[ma] = None

    return result


# ──────────────────────────────────────────────────────────────
# 손절/목표가 계산
# ──────────────────────────────────────────────────────────────

def calc_trade_levels(current_price, stats):
    """매수 시 손절/목표가 계산
    손절: MDD 중앙값의 절반 (보수적)
    목표: 3M 중앙값
    """
    avg_mdd = stats.get("avg_mdd")
    s3 = stats.get("3M")

    if avg_mdd is None or s3 is None:
        return None

    # 손절: 평균 MDD의 절반 (보수적)
    stop_pct = avg_mdd / 2
    stop_price = current_price * (1 + stop_pct / 100)

    # 목표: 3M 평균수익률
    target_pct = s3["mean"]
    target_price = current_price * (1 + target_pct / 100)

    # 리스크리워드 비율
    risk = abs(stop_pct)
    reward = target_pct
    rr = reward / risk if risk > 0 else 0

    return {
        "stop_pct": stop_pct,
        "stop_price": stop_price,
        "target_pct": target_pct,
        "target_price": target_price,
        "rr": rr,
    }


# ──────────────────────────────────────────────────────────────
# 액션 요약
# ──────────────────────────────────────────────────────────────

def build_action_summary(analyses, daily_status, weekly_status):
    """터치 중인 MA 중 가장 의미있는 것 기준으로 액션 요약 생성"""
    current_price = daily_status["close"]

    # 우선순위별로 터치 여부 확인
    best_match = None
    for tf_label, ma_label, ma_col in MA_PRIORITY:
        status = weekly_status if tf_label == "주봉" else daily_status
        ma_info = status.get(ma_col)
        if ma_info and ma_info["touching"]:
            # 해당 분석 결과 찾기
            for a in analyses:
                if a["tf_label"] == tf_label and a["ma_label"] == ma_label:
                    best_match = a
                    best_pct = ma_info["pct"]
                    best_tf = tf_label
                    best_ma = ma_label
                    break
            if best_match:
                break

    if best_match is None:
        # 터치 중이 아님 → 대기 + 다음 MA까지 거리
        lines = []
        lines.append("  현재: 터치 없음 → 대기")
        # 가장 가까운 MA 거리 표시
        closest_dist = None
        closest_name = None
        for tf_label, ma_label, ma_col in reversed(MA_PRIORITY):
            status = weekly_status if tf_label == "주봉" else daily_status
            ma_info = status.get(ma_col)
            if ma_info:
                dist = abs(ma_info["pct"])
                if closest_dist is None or dist < closest_dist:
                    closest_dist = dist
                    closest_name = f"{tf_label} {ma_label}"
                    closest_price = ma_info["value"]
        if closest_name:
            lines.append(f"  가장 가까운 MA: {closest_name} ({_fmt_price(closest_price)}, {closest_dist:.1f}% 거리)")
        return lines

    stats = best_match["stats"]
    score = best_match["score"]
    s3 = stats.get("3M")
    s6 = stats.get("6M")
    s1y = stats.get("1Y")

    trade = calc_trade_levels(current_price, stats)

    lines = []
    lines.append(f"  현재: {best_tf} {best_ma} 터치중 ({best_pct:+.2f}%) → 매수 기회 ({score:.1f}/10)")

    if trade:
        lines.append(
            f"  매수 시: 손절 {_fmt_price(trade['stop_price'])} ({trade['stop_pct']:+.1f}%)"
            f" | 목표 3M {_fmt_price(trade['target_price'])} ({trade['target_pct']:+.1f}%)"
            f" | R:R 1:{trade['rr']:.1f}"
        )

    wr3 = f"{s3['win_rate']:.1f}%" if s3 else "---"
    wr6 = f"{s6['win_rate']:.1f}%" if s6 else "---"
    wr1y = f"{s1y['win_rate']:.1f}%" if s1y else "---"
    lines.append(f"  근거: 3M 승률 {wr3}, 6M 승률 {wr6}, 1Y 승률 {wr1y}")

    return lines


# ──────────────────────────────────────────────────────────────
# 블랙스완 1줄 요약
# ──────────────────────────────────────────────────────────────

def build_risk_check(bs_touch_summaries, bd_sections):
    """리스크 체크 1~2줄 요약"""
    lines = []

    # 블랙스완 매수 요약 (MA200 기준)
    for s in bs_touch_summaries:
        name = s["구분"]
        avg_loss = s["평균 최대손실"]
        exit5 = s["5일내 평균탈출"]
        worst = s["최악 최대손실"]
        lines.append(f"  블랙스완 시 {name} 매수: 평균손실 {avg_loss} | 5일내 탈출 {exit5} | 최악 {worst}")

    # 하방 돌파 회복 통계 (일봉 200일선)
    for tf_label, ma_label, bd_events, bd_stats in bd_sections:
        if tf_label == "일봉" and bd_stats:
            lines.append(
                f"  {ma_label} 하방 돌파 시: 평균 {bd_stats['평균']} 회복"
                f" (중앙값 {bd_stats['중앙값']}, 최대 {bd_stats['최대']})"
            )

    return lines


# ──────────────────────────────────────────────────────────────
# CLI 출력 — DataFrame 포매터
# ──────────────────────────────────────────────────────────────

def _events_to_df(events, detail=False):
    periods = FORWARD_PERIODS_DETAIL if detail else FORWARD_PERIODS_BASIC
    rows = []
    for ev in events:
        row = {
            "날짜": ev["date"].strftime("%Y-%m-%d"),
            "종가": _fmt_price(ev["close"]),
            "MA대비": f"{ev['pct_diff']:+.1f}%",
            "방향": ev["direction"],
        }
        for label in periods:
            row[label] = _fmt_pct(ev.get(label))
        rows.append(row)
    df = pd.DataFrame(rows)
    df = df.set_index("날짜")
    return df


def _stats_to_df(stats, detail=False):
    periods = FORWARD_PERIODS_DETAIL if detail else FORWARD_PERIODS_BASIC
    rows = []
    for label in periods:
        s = stats.get(label)
        if s:
            rows.append({
                "기간": label,
                "건수": s["count"],
                "평균수익률": f"{s['mean']:+.1f}%",
                "중앙값": f"{s['median']:+.1f}%",
                "승률": f"{s['win_rate']:.1f}%",
                "최대": f"{s['max']:+.1f}%",
                "최소": f"{s['min']:+.1f}%",
            })
        else:
            rows.append({
                "기간": label, "건수": "---", "평균수익률": "---",
                "중앙값": "---", "승률": "---", "최대": "---", "최소": "---",
            })
    df = pd.DataFrame(rows)
    df = df.set_index("기간")
    return df


def _verdict_to_df(analyses):
    rows = []
    for a in analyses:
        score = a["score"]
        stats = a["stats"]

        if score >= 8:
            verdict = "[+] 강력 매수"
        elif score >= 6:
            verdict = "[o] 매수 기회"
        elif score >= 4:
            verdict = "[-] 중립"
        else:
            verdict = "[x] 약세"

        s3 = stats.get("3M")
        s6 = stats.get("6M")
        s1y = stats.get("1Y")
        row = {
            "구분": f"{a['tf_label']} {a['ma_label']}",
            "판정": verdict,
            "점수": f"{score:.1f}/10",
            "3M 승률": f"{s3['win_rate']:.1f}%" if s3 else "---",
            "6M 승률": f"{s6['win_rate']:.1f}%" if s6 else "---",
            "1Y 승률": f"{s1y['win_rate']:.1f}%" if s1y else "---",
        }
        rows.append(row)
    df = pd.DataFrame(rows)
    df = df.set_index("구분")
    return df


def _compact_stats_df(analyses):
    """통계 요약 — 한 테이블로 압축"""
    rows = []
    for a in analyses:
        stats = a["stats"]
        s3 = stats.get("3M")
        s6 = stats.get("6M")
        s1y = stats.get("1Y")
        avg_mdd = stats.get("avg_mdd")
        rows.append({
            "구분": f"{a['tf_label']} {a['ma_label']}",
            "건수": len(a["events"]),
            "3M 승률": f"{s3['win_rate']:.1f}%" if s3 else "---",
            "3M 평균": f"{s3['mean']:+.1f}%" if s3 else "---",
            "6M 승률": f"{s6['win_rate']:.1f}%" if s6 else "---",
            "6M 평균": f"{s6['mean']:+.1f}%" if s6 else "---",
            "1Y 승률": f"{s1y['win_rate']:.1f}%" if s1y else "---",
            "1Y 평균": f"{s1y['mean']:+.1f}%" if s1y else "---",
            "MDD": f"{avg_mdd:.1f}%" if avg_mdd is not None else "---",
        })
    df = pd.DataFrame(rows)
    df = df.set_index("구분")
    return df


# ──────────────────────────────────────────────────────────────
# 메인 대시보드 출력
# ──────────────────────────────────────────────────────────────

def print_dashboard(daily_df, weekly_df, ticker_cfg, detail=False):
    now = datetime.datetime.now().strftime("%Y-%m-%d")
    ticker_name = ticker_cfg["name"]

    pd.set_option("display.unicode.east_asian_width", True)
    pd.set_option("display.max_rows", 200)
    pd.set_option("display.width", 120)

    print()
    print("=" * W)
    print(f"  {ticker_name} MA Touch Analyzer  |  {now}")
    print(f"  제외: 닷컴버블, 2008 금융위기")
    print("=" * W)

    # ── 분석 실행 ──
    daily_status = check_current_status(daily_df)
    weekly_status = check_current_status(weekly_df)
    analyses = []

    for timeframe, df, tf_label in [("daily", daily_df, "일봉"), ("weekly", weekly_df, "주봉")]:
        for ma_col, ma_label in [("MA100", "100일선" if timeframe == "daily" else "100주선"),
                                  ("MA200", "200일선" if timeframe == "daily" else "200주선")]:
            touch_mask = detect_touches(df, ma_col)
            events = cluster_touch_events(df, touch_mask, ma_col)
            events = [e for e in events if not _is_in_excluded(e["date"])]
            events = calc_forward_returns(df, events, detail=detail)
            stats = calc_statistics(events, detail=detail)
            score = calc_buy_score(stats)
            analyses.append({
                "tf_label": tf_label,
                "ma_label": ma_label,
                "ma_col": ma_col,
                "events": events,
                "stats": stats,
                "score": score,
                "src_df": df,
            })

    # ── 하방 돌파 분석 ──
    bd_sections = []
    for timeframe, src_df, tf_label in [("daily", daily_df, "일봉"), ("weekly", weekly_df, "주봉")]:
        ma_label = "200일선" if timeframe == "daily" else "200주선"
        bd_events = detect_breakdowns(src_df, "MA200")
        bd_stats = breakdown_stats(bd_events)
        bd_sections.append((tf_label, ma_label, bd_events, bd_stats))

    # ── 블랙스완 매수 요약 (리스크 체크용) ──
    bs_touch_summaries = []
    daily_ma200 = [a for a in analyses if a["tf_label"] == "일봉" and a["ma_col"] == "MA200"]
    weekly_ma200 = [a for a in analyses if a["tf_label"] == "주봉" and a["ma_col"] == "MA200"]

    for a_list, src_df in [(daily_ma200, daily_df), (weekly_ma200, weekly_df)]:
        if a_list:
            a = a_list[0]
            _, summary = analyze_touch_in_black_swan(
                src_df, a["events"], a["tf_label"], a["ma_label"], ticker_cfg)
            if summary:
                bs_touch_summaries.append(summary)

    # ═══════════════════════════════════════════════════════════
    # [1] 액션 요약
    # ═══════════════════════════════════════════════════════════
    print()
    print(" [액션 요약]")
    action_lines = build_action_summary(analyses, daily_status, weekly_status)
    for line in action_lines:
        print(line)

    # ═══════════════════════════════════════════════════════════
    # [2] 종합 판단
    # ═══════════════════════════════════════════════════════════
    print()
    print(" [종합 판단]")
    print(_verdict_to_df(analyses).to_string())

    # ═══════════════════════════════════════════════════════════
    # [3] 리스크 체크
    # ═══════════════════════════════════════════════════════════
    print()
    print(" [리스크 체크]")
    risk_lines = build_risk_check(bs_touch_summaries, bd_sections)
    if risk_lines:
        for line in risk_lines:
            print(line)
    else:
        print("  데이터 부족")

    # ═══════════════════════════════════════════════════════════
    # [4] 통계 요약 (압축)
    # ═══════════════════════════════════════════════════════════
    print()
    print(" [통계 요약]")
    print(_compact_stats_df(analyses).to_string())

    # ═══════════════════════════════════════════════════════════
    # --detail: 기존 전체 출력
    # ═══════════════════════════════════════════════════════════
    if detail:
        print()
        print("=" * W)
        print(" [상세 모드]")
        print("=" * W)

        # 현재 상태
        print()
        close_str = _fmt_price(daily_status["close"])
        line = f" {ticker_name} 종가: {close_str}"
        for ma in ["MA100", "MA200"]:
            s = daily_status[ma]
            if s:
                line += f"  |  {ma}: {_fmt_price(s['value'])} ({s['pct']:+.2f}%)"
                if s["touching"]:
                    line += " [터치중!]"
        print(line)

        # 블랙스완 이벤트 상세
        bs_results = analyze_black_swans(daily_df, ticker_cfg)
        if bs_results:
            print()
            print(" [블랙스완 이벤트 - 낙폭 / 회복 분석]")
            bs_df = pd.DataFrame(bs_results).set_index("이벤트")
            print(bs_df.to_string())

        # 블랙스완 구간 MA200 매수 상세
        for a_list, src_df in [(daily_ma200, daily_df), (weekly_ma200, weekly_df)]:
            if a_list:
                a = a_list[0]
                detail_df, summary = analyze_touch_in_black_swan(
                    src_df, a["events"], a["tf_label"], a["ma_label"], ticker_cfg)
                if detail_df is not None:
                    print()
                    print(f" [{a['tf_label']} {a['ma_label']} - 블랙스완 구간 매수 분석]")
                    print(detail_df.to_string(index=False))

        if bs_touch_summaries:
            print()
            print(" [블랙스완 매수 요약 통계]")
            sum_df = pd.DataFrame(bs_touch_summaries).set_index("구분")
            print(sum_df.to_string())

        # 하방 돌파 상세
        for tf_label, ma_label, bd_events, bd_stats in bd_sections:
            print()
            print(f" [{tf_label} - {ma_label} 하방 돌파 → 회복] ({len(bd_events)}건)")
            if bd_events:
                bd_df = pd.DataFrame(bd_events).set_index("돌파일")
                print(bd_df.to_string())
                if bd_stats:
                    stats_line = "  ".join(f"{k}: {v}" for k, v in bd_stats.items())
                    print(f" 회복 통계 | {stats_line}")
            else:
                print("   이벤트 없음")

        # 통계 상세 (1W, 1M 포함)
        for a in analyses:
            stats = a["stats"]
            header = f"{a['tf_label']} - {a['ma_label']} 통계"
            print()
            print(f" [{header}] ({len(a['events'])}건)")
            if not a["events"]:
                print("   이벤트 없음")
                continue
            print(_stats_to_df(stats, detail=True).to_string())
            avg_mdd = stats.get("avg_mdd")
            if avg_mdd is not None:
                print(f" 평균 MDD: {avg_mdd:.1f}%")

        # 개별 이벤트 상세
        print()
        print("-" * W)
        print(" [터치 이벤트 상세]")
        for a in analyses:
            events = a["events"]
            header = f"{a['tf_label']} - {a['ma_label']} 터치"
            print()
            print(f" [{header}] (총 {len(events)}건)")
            if not events:
                print("   이벤트 없음")
                continue
            print(_events_to_df(events, detail=True).to_string())

    print()
    print("=" * W)

    if _errors:
        print(f"\n [!] {len(_errors)}개 오류:")
        for e in _errors:
            print(f"   - {e}")

    print()


# ──────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────

def main():
    tickers = ", ".join(TICKER_CONFIGS.keys())
    parser = argparse.ArgumentParser(description="MA Touch Analyzer")
    parser.add_argument("--ticker", default=DEFAULT_TICKER, choices=TICKER_CONFIGS.keys(),
                        help=f"분석 대상 티커 ({tickers}, 기본: {DEFAULT_TICKER})")
    parser.add_argument("--detail", action="store_true", help="상세 모드 (이벤트 전체, 블랙스완 개별, 1W/1M 포함)")
    args = parser.parse_args()

    ticker_cfg = TICKER_CONFIGS[args.ticker]
    print(f" {ticker_cfg['name']} 데이터 다운로드 중...")
    daily_df = fetch_data(ticker_cfg, "1d")
    weekly_df = fetch_data(ticker_cfg, "1wk")

    if daily_df.empty or weekly_df.empty:
        print(" [!] 데이터 다운로드 실패")
        return

    daily_df = calc_moving_averages(daily_df)
    weekly_df = calc_moving_averages(weekly_df)

    print_dashboard(daily_df, weekly_df, ticker_cfg, detail=args.detail)


if __name__ == "__main__":
    main()
