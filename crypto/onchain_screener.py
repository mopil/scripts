#!/usr/bin/env python3
"""BTC Bottom Screener - 비트코인 바닥 판별 CLI 도구"""

import requests
import time
import sys
import io
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# Windows cp949 인코딩 문제 방지
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ── 상수 ──────────────────────────────────────────────────────────────────────
BG_BASE = "https://bitcoin-data.com/v1"
BG_ENDPOINTS = {
    "mvrv": "/mvrv/1",
    "nupl": "/nupl/1",
    "sopr": "/sopr/1",
    "puell": "/puell-multiple/1",
    "etf": "/etf-btc/7",
    "netflow": "/exchange-netflow/1",
    "reserve": "/exchange-reserve/1",
    "profit": "/profit-loss/1",
}

TIMEOUT = 15
_errors = []  # 에러 수집용

# 지표별 (바닥값, 과열값) — 바닥일수록 10점, 과열일수록 0점
THRESHOLDS = {
    "mvrv":     (1.0, 3.5),
    "nupl":     (0.0, 0.75),
    "sopr":     (1.0, 1.05),
    "puell":    (0.5, 4.0),
    "profit":   (50.0, 95.0),
    "fng":      (20, 80),
    "funding":  (0.0, 0.03),
    "cbprem":   (-1.0, 1.0),  # Coinbase Premium: -1% 바닥, +1% 과열
    "mayer":    (0.8, 2.4),
    "drawdown": (-70, -10),
    "etf":      None,
    "netflow":  None,
    "reserve":  None,
}

# 가중치: 온체인 핵심 지표 1.5x, 나머지 1.0x
WEIGHTS = {
    "mvrv": 1.5, "nupl": 1.5, "sopr": 1.5, "profit": 1.5,
    "puell": 1.0, "mayer": 1.0, "drawdown": 1.0,
    "fng": 1.0, "funding": 1.0, "cbprem": 1.0,
    "etf": 1.0, "netflow": 1.0, "reserve": 1.0,
}


# ── 데이터 수집 ───────────────────────────────────────────────────────────────
def fetch_bg(key):
    """BGeometrics API에서 단일 지표 가져오기"""
    url = BG_BASE + BG_ENDPOINTS[key]
    try:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return key, r.json()
    except Exception as e:
        _errors.append(f"BGeometrics/{key}: {e}")
        return key, None


def fetch_fear_greed():
    """Alternative.me Fear & Greed Index"""
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return int(data["data"][0]["value"])
    except Exception as e:
        _errors.append(f"Fear&Greed: {e}")
        return None


def fetch_funding_rate():
    """Binance BTC 무기한선물 Funding Rate"""
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": "BTCUSDT", "limit": 1},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        return float(data[0]["fundingRate"]) * 100  # % 변환
    except Exception as e:
        _errors.append(f"Funding Rate: {e}")
        return None


def fetch_coinbase_premium():
    """Coinbase vs Binance 프리미엄 계산 (%)"""
    try:
        # Coinbase BTC-USD
        r1 = requests.get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            timeout=TIMEOUT,
        )
        r1.raise_for_status()
        coinbase_price = float(r1.json()["data"]["amount"])

        # Binance BTCUSDT
        r2 = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=TIMEOUT,
        )
        r2.raise_for_status()
        binance_price = float(r2.json()["price"])

        # 프리미엄 계산 (%)
        premium = (coinbase_price - binance_price) / binance_price * 100
        return premium
    except Exception as e:
        _errors.append(f"Coinbase Premium: {e}")
        return None


def fetch_price_history():
    """CoinGecko 365일 가격 → (현재가, mayer_multiple, ath_drawdown)"""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
            params={"vs_currency": "usd", "days": 365, "interval": "daily"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        prices = [p[1] for p in r.json()["prices"]]
        current = prices[-1]
        ma200 = sum(prices[-200:]) / min(len(prices), 200)
        mayer = current / ma200

        # ATH
        r2 = requests.get(
            "https://api.coingecko.com/api/v3/coins/bitcoin",
            params={"localization": "false", "tickers": "false",
                    "market_data": "true", "community_data": "false",
                    "developer_data": "false"},
            timeout=TIMEOUT,
        )
        r2.raise_for_status()
        ath = r2.json()["market_data"]["ath"]["usd"]
        drawdown = ((current - ath) / ath) * 100

        return current, mayer, drawdown
    except Exception as e:
        _errors.append(f"CoinGecko: {e}")
        return None, None, None


# ── 스코어링 ──────────────────────────────────────────────────────────────────
def linear_score(val, bottom, top, invert=False):
    """선형 매핑: bottom=10점, top=0점. invert=True면 반전."""
    if val is None:
        return None
    if not invert:
        score = (top - val) / (top - bottom) * 10
    else:
        score = (val - top) / (bottom - top) * 10
    return max(0.0, min(10.0, score))


def score_etf(data):
    """ETF 7일 데이터: 유출일수 비율 기반 스코어링"""
    if data is None:
        return None, "N/A", "N/A"
    try:
        flows = []
        for item in data:
            val = item.get("value") or item.get("total_net")
            if val is not None:
                flows.append(float(val))
        if not flows:
            return None, "N/A", "N/A"

        total = sum(flows)
        outflow_days = sum(1 for f in flows if f < 0)

        score = outflow_days / len(flows) * 10
        total_m = total / 1_000_000
        label = f"${total_m:+,.0f}M"
        return min(10.0, max(0.0, score)), label, total
    except Exception:
        return None, "N/A", "N/A"


def score_netflow(data):
    """Exchange Netflow: 음수(유출)=바닥신호, 양수(유입)=과열"""
    if data is None:
        return None, "N/A", "N/A"
    try:
        if isinstance(data, list):
            val = float(data[0].get("value", 0))
        else:
            val = float(data.get("value", 0))

        score = (-val + 20000) / 40000 * 10
        score = max(0.0, min(10.0, score))
        label = f"{val:+,.0f} BTC"
        return score, label, val
    except Exception:
        return None, "N/A", "N/A"


def score_reserve(data):
    """Exchange Reserve: 낮을수록 바닥. 2.0M~3.0M BTC 범위"""
    if data is None:
        return None, "N/A", "N/A"
    try:
        if isinstance(data, list):
            val = float(data[0].get("value", 0))
        else:
            val = float(data.get("value", 0))

        score = (3_000_000 - val) / 1_000_000 * 10
        score = max(0.0, min(10.0, score))
        label = f"{val/1_000_000:.2f}M BTC"
        return score, label, val
    except Exception:
        return None, "N/A", "N/A"


# ── 출력 ──────────────────────────────────────────────────────────────────────
def score_emoji(score):
    if score is None:
        return "?"
    if score >= 8:
        return "+"
    if score >= 6:
        return "o"
    if score >= 4:
        return "."
    if score >= 2:
        return "-"
    return "!"


def score_label(score):
    if score is None:
        return "N/A"
    if score >= 8:
        return "바닥"
    if score >= 6:
        return "저평가"
    if score >= 4:
        return "중립"
    if score >= 2:
        return "과열주의"
    return "과열"


def verdict(total):
    if total >= 8:
        return "[+] 역사적 바닥 구간 - 적극 매수 고려"
    if total >= 6:
        return "[o] 저평가 구간 - 분할 매수 고려"
    if total >= 4:
        return "[.] 중립 구간 - 관망"
    if total >= 2:
        return "[-] 과열 주의 - 비중 축소 고려"
    return "[!] 극단적 과열 - 매도 고려"


def fmt_val(name, raw):
    """지표별 값 포맷팅"""
    if raw is None or raw == "N/A":
        return "N/A"
    fmts = {
        "mvrv": "{:.2f}", "nupl": "{:.2f}", "sopr": "{:.3f}",
        "puell": "{:.2f}", "profit": "{:.1f}%", "fng": "{}",
        "funding": "{:.4f}%", "cbprem": "{:+.2f}%", "mayer": "{:.2f}",
        "drawdown": "{:.1f}%",
    }
    if name == "fng":
        return str(int(raw))
    if name in fmts:
        return fmts[name].format(raw)
    return str(raw)


DISPLAY = {
    "mvrv": "MVRV Ratio", "nupl": "NUPL", "sopr": "SOPR",
    "profit": "Supply in Profit", "puell": "Puell Multiple",
    "mayer": "Mayer Multiple", "drawdown": "ATH Drawdown",
    "fng": "Fear & Greed", "funding": "Funding Rate",
    "cbprem": "Coinbase Premium",
    "etf": "ETF Flow (7d)", "netflow": "Exchange Netflow",
    "reserve": "Exchange Reserve",
}

SECTIONS = [
    ("온체인", ["mvrv", "nupl", "sopr", "profit"]),
    ("매크로", ["puell", "mayer", "drawdown"]),
    ("심리/파생", ["fng", "funding", "cbprem"]),
    ("자금흐름", ["etf", "netflow", "reserve"]),
]


def print_dashboard(indicators, total_score, btc_price):
    W = 58
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    price_str = f"${btc_price:,.0f}" if btc_price else "N/A"

    print()
    print("=" * W)
    title = f"  BTC Bottom Screener  |  {now}  |  {price_str}"
    print(title)
    print("=" * W)

    for section_name, keys in SECTIONS:
        print(f"\n [{section_name}]")
        for key in keys:
            ind = indicators.get(key)
            if ind is None:
                print(f" {DISPLAY[key]:<22s} {'N/A':>10s}   {'N/A':>5s}   ? N/A")
                continue
            score, val_str = ind["score"], ind["display"]
            if score is not None:
                em = score_emoji(score)
                lb = score_label(score)
                print(f" {DISPLAY[key]:<22s} {val_str:>10s}   {score:4.1f}/10  {em} {lb}")
            else:
                print(f" {DISPLAY[key]:<22s} {val_str:>10s}    N/A    ? N/A")

    print()
    print("-" * W)
    scored = [(k, v) for k, v in indicators.items() if v and v["score"] is not None]
    if scored:
        print(f" 종합 점수:  {total_score:.1f} / 10  ({len(scored)}/{len(DISPLAY)}개 지표)")
    else:
        print(" 종합 점수:  데이터 부족")
    print(f" 판정: {verdict(total_score)}")
    print("=" * W)

    # 에러가 있으면 하단에 표시
    if _errors:
        print(f"\n [!] {len(_errors)}개 API 오류:")
        for e in _errors:
            print(f"   - {e}")
    print()


# ── 메인 ──────────────────────────────────────────────────────────────────────
def parse_bg_value(key, raw):
    """BGeometrics 응답에서 숫자 값 추출"""
    if raw is None:
        return None
    try:
        if isinstance(raw, list):
            item = raw[0] if raw else {}
        elif isinstance(raw, dict):
            item = raw
        else:
            return float(raw)

        # 다양한 키 이름 시도
        for field in ["value", "mvrv", "nupl", "sopr", "puell_multiple",
                       "puell", "supply_in_profit", "percentage", "ratio"]:
            if field in item:
                return float(item[field])
        # 첫 번째 숫자 값 반환
        for v in item.values():
            try:
                return float(v)
            except (ValueError, TypeError):
                continue
    except Exception:
        pass
    return None


def main():
    _errors.clear()
    print("\n 데이터 수집 중...")

    # 1. BGeometrics 8개 순차 호출 (rate limit 대응: 1초 간격)
    bg_data = {}
    for i, key in enumerate(BG_ENDPOINTS):
        if i > 0:
            time.sleep(1.0)
        k, data = fetch_bg(key)
        bg_data[k] = data

    # 2. 나머지 4개 병렬 호출
    fng_val = None
    funding_val = None
    cbprem_val = None
    btc_price, mayer_val, drawdown_val = None, None, None

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(fetch_fear_greed): "fng",
            pool.submit(fetch_funding_rate): "funding",
            pool.submit(fetch_coinbase_premium): "cbprem",
            pool.submit(fetch_price_history): "price",
        }
        for fut in as_completed(futures):
            tag = futures[fut]
            try:
                result = fut.result()
                if tag == "fng":
                    fng_val = result
                elif tag == "funding":
                    funding_val = result
                elif tag == "cbprem":
                    cbprem_val = result
                elif tag == "price":
                    btc_price, mayer_val, drawdown_val = result
            except Exception as e:
                _errors.append(f"{tag}: {e}")

    # 3. 스코어링
    indicators = {}

    # BGeometrics 단순 선형 지표
    for key in ["mvrv", "nupl", "sopr", "puell"]:
        val = parse_bg_value(key, bg_data.get(key))
        bottom, top = THRESHOLDS[key]
        sc = linear_score(val, bottom, top)
        indicators[key] = {"score": sc, "raw": val, "display": fmt_val(key, val)}

    # Supply in Profit
    profit_val = parse_bg_value("profit", bg_data.get("profit"))
    if profit_val is not None and profit_val <= 1.0:
        profit_val *= 100  # 0~1 범위면 %로 변환
    sc = linear_score(profit_val, *THRESHOLDS["profit"])
    indicators["profit"] = {"score": sc, "raw": profit_val, "display": fmt_val("profit", profit_val)}

    # ETF / Netflow / Reserve (커스텀 스코어링)
    etf_score, etf_label, _ = score_etf(bg_data.get("etf"))
    indicators["etf"] = {"score": etf_score, "raw": etf_label, "display": etf_label}

    nf_score, nf_label, _ = score_netflow(bg_data.get("netflow"))
    indicators["netflow"] = {"score": nf_score, "raw": nf_label, "display": nf_label}

    rs_score, rs_label, _ = score_reserve(bg_data.get("reserve"))
    indicators["reserve"] = {"score": rs_score, "raw": rs_label, "display": rs_label}

    # Fear & Greed
    sc = linear_score(fng_val, *THRESHOLDS["fng"])
    indicators["fng"] = {"score": sc, "raw": fng_val, "display": fmt_val("fng", fng_val)}

    # Funding Rate
    sc = linear_score(funding_val, *THRESHOLDS["funding"])
    indicators["funding"] = {"score": sc, "raw": funding_val, "display": fmt_val("funding", funding_val)}

    # Coinbase Premium (invert: 음수 프리미엄 = 바닥 신호)
    sc = linear_score(cbprem_val, *THRESHOLDS["cbprem"], invert=True)
    indicators["cbprem"] = {"score": sc, "raw": cbprem_val, "display": fmt_val("cbprem", cbprem_val)}

    # Mayer Multiple
    sc = linear_score(mayer_val, *THRESHOLDS["mayer"])
    indicators["mayer"] = {"score": sc, "raw": mayer_val, "display": fmt_val("mayer", mayer_val)}

    # ATH Drawdown (invert: 더 음수일수록 바닥=10점)
    sc = linear_score(drawdown_val, *THRESHOLDS["drawdown"], invert=True)
    indicators["drawdown"] = {"score": sc, "raw": drawdown_val, "display": fmt_val("drawdown", drawdown_val)}

    # 4. 가중평균 종합 점수
    weighted_sum = 0.0
    weight_total = 0.0
    for key, ind in indicators.items():
        if ind["score"] is not None:
            w = WEIGHTS.get(key, 1.0)
            weighted_sum += ind["score"] * w
            weight_total += w

    total_score = weighted_sum / weight_total if weight_total > 0 else 0.0

    # 5. 출력
    print_dashboard(indicators, total_score, btc_price)


if __name__ == "__main__":
    main()
