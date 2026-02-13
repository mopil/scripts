"""
US Insider Trading Screener
finvizfinance Insider 모듈로 내부자 매수/매도 클러스터 탐지 → 스코어링
"""

import sys, io, datetime, argparse, math, time, warnings

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*day of month without a year.*")

import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from finvizfinance.insider import Insider
from finvizfinance.quote import finvizfinance as Finviz

_errors = []

W = 80
KRW_RATE = 1450

SECTOR_KR = {
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
}

# Relationship → 가중치 (직급 높을수록 높음)
RELATIONSHIP_WEIGHTS = {
    "CEO": 3.0, "Chief Executive Officer": 3.0,
    "CFO": 2.5, "Chief Financial Officer": 2.5,
    "COO": 2.0, "Chief Operating Officer": 2.0,
    "President": 2.5,
    "Chairman": 2.5, "Chairman of the Board": 2.5,
    "VP": 1.5, "Vice President": 1.5, "EVP": 1.5, "SVP": 1.5,
    "Director": 1.0, "Dir": 1.0,
    "Officer": 1.5, "General Counsel": 1.5,
}

RELATIONSHIP_SHORT = {
    "CEO": "CEO", "Chief Executive Officer": "CEO",
    "CFO": "CFO", "Chief Financial Officer": "CFO",
    "COO": "COO", "Chief Operating Officer": "COO",
    "President": "Pres",
    "Chairman": "Chair", "Chairman of the Board": "Chair",
    "VP": "VP", "Vice President": "VP", "EVP": "EVP", "SVP": "SVP",
    "Director": "Dir", "Dir": "Dir",
    "Officer": "Off", "General Counsel": "GC",
}


# ──────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────

def _get_relationship_weight(relationship):
    """Relationship 문자열에서 최고 직급 가중치 추출"""
    if pd.isna(relationship):
        return 0.5
    rel = str(relationship)
    best = 0.5
    for key, w in RELATIONSHIP_WEIGHTS.items():
        if key.lower() in rel.lower():
            best = max(best, w)
    return best


def _get_relationship_short(relationship):
    """Relationship → 축약형"""
    if pd.isna(relationship):
        return "-"
    rel = str(relationship)
    for key, short in RELATIONSHIP_SHORT.items():
        if key.lower() in rel.lower():
            return short
    return rel[:8]


def _parse_insider_date(date_str):
    """Insider 날짜 문자열 → datetime"""
    if pd.isna(date_str):
        return None
    try:
        s = str(date_str).strip()
        for fmt in ["%b %d", "%b %d '%y", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"]:
            try:
                dt = datetime.datetime.strptime(s, fmt)
                if dt.year < 2000:
                    dt = dt.replace(year=datetime.datetime.now().year)
                return dt
            except ValueError:
                continue
        return None
    except Exception:
        return None


def _parse_value(val):
    """Value ($) 컬럼 파싱 → float"""
    if pd.isna(val):
        return 0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0


def _fmt_value(val):
    """달러 금액 포맷"""
    if val >= 1e9:
        return f"${val/1e9:.1f}B"
    if val >= 1e6:
        return f"${val/1e6:.1f}M"
    if val >= 1e3:
        return f"${val/1e3:.0f}K"
    return f"${val:,.0f}"


# ──────────────────────────────────────────────────────────────
# 1단계: Insider 데이터 수집
# ──────────────────────────────────────────────────────────────

def fetch_insider_data(mode, period):
    """finvizfinance Insider API로 데이터 수집"""
    option_map = {
        ("buy", "latest"): "latest buys",
        ("buy", "week"): "top week buys",
        ("sell", "latest"): "latest sales",
        ("sell", "week"): "top week sales",
    }

    if mode == "all":
        options = []
        if period == "latest":
            options = ["latest buys", "latest sales"]
        else:
            options = ["top week buys", "top week sales"]
    else:
        key = (mode, period)
        opt = option_map.get(key)
        if not opt:
            print(f"  [!] 알 수 없는 모드: mode={mode}, period={period}")
            return pd.DataFrame()
        options = [opt]

    frames = []
    for opt in options:
        try:
            print(f"       '{opt}' 조회 중...")
            insider = Insider(option=opt)
            df = insider.get_insider()
            if df is not None and not df.empty:
                df["_source"] = opt
                frames.append(df)
                print(f"       → {len(df)}건")
            else:
                print(f"       → 0건")
        except Exception as e:
            _errors.append(f"Insider({opt}): {e}")
            print(f"       → 오류: {e}")

    if not frames:
        return pd.DataFrame()

    result = pd.concat(frames, ignore_index=True)
    return result


# ──────────────────────────────────────────────────────────────
# 2단계: 티커별 그룹화 + 클러스터 분석
# ──────────────────────────────────────────────────────────────

def analyze_by_ticker(df, min_value):
    """티커별 그룹화하여 클러스터 분석"""
    if df.empty:
        return []

    # Value 파싱
    val_col = None
    for c in df.columns:
        if "value" in c.lower() or "val" in c.lower():
            val_col = c
            break
    if val_col is None:
        val_col = df.columns[-3] if len(df.columns) > 3 else df.columns[-1]

    df = df.copy()
    df["_value"] = df[val_col].apply(_parse_value)

    # 최소 매수액 필터
    df = df[df["_value"] >= min_value]
    if df.empty:
        return []

    # Relationship 컬럼 탐색
    rel_col = "Relationship"
    if rel_col not in df.columns:
        for c in df.columns:
            if "relation" in c.lower():
                rel_col = c
                break

    # Owner 컬럼 탐색
    owner_col = "Owner"
    if owner_col not in df.columns:
        for c in df.columns:
            if "owner" in c.lower() or "insider" in c.lower():
                owner_col = c
                break

    # Date 컬럼 탐색
    date_col = "Date"
    if date_col not in df.columns:
        for c in df.columns:
            if "date" in c.lower():
                date_col = c
                break

    # Cost 컬럼 탐색
    cost_col = "Cost"
    if cost_col not in df.columns:
        for c in df.columns:
            if "cost" in c.lower() or "price" in c.lower():
                cost_col = c
                break

    today = datetime.datetime.now()
    results = []

    for ticker, group in df.groupby("Ticker"):
        buyers = []
        total_value = 0
        best_weight = 0
        best_role = "-"
        most_recent_date = None
        most_recent_days = 999

        for _, row in group.iterrows():
            val = row["_value"]
            total_value += val

            rel = row.get(rel_col, "")
            w = _get_relationship_weight(rel)
            if w > best_weight:
                best_weight = w
                best_role = _get_relationship_short(rel)

            owner = row.get(owner_col, "Unknown")
            cost = row.get(cost_col, None)
            cost_val = _parse_value(cost) if cost is not None else 0

            dt = _parse_insider_date(row.get(date_col))
            if dt is not None:
                days = (today - dt).days
                if days < 0:
                    days = 0
                if days < most_recent_days:
                    most_recent_days = days
                    most_recent_date = dt
            else:
                days = None

            buyers.append({
                "name": str(owner)[:30] if pd.notna(owner) else "Unknown",
                "role": _get_relationship_short(rel),
                "value": val,
                "cost": cost_val,
                "days_ago": days,
            })

        buyer_count = len(set(b["name"] for b in buyers))

        results.append({
            "ticker": ticker,
            "buyer_count": buyer_count,
            "total_value": total_value,
            "best_weight": best_weight,
            "best_role": best_role,
            "most_recent_days": most_recent_days if most_recent_days < 999 else None,
            "buyers": buyers,
            "tx_count": len(group),
        })

    return results


# ──────────────────────────────────────────────────────────────
# 3단계: 펀더멘탈 보강 (Overview screener 배치 조회)
# ──────────────────────────────────────────────────────────────

def _fetch_single_fundamental(ticker):
    """개별 종목 펀더멘탈 조회"""
    try:
        stock = Finviz(ticker)
        fund = stock.ticker_fundament()
        desc = stock.ticker_description()
        return ticker, fund, desc
    except Exception as e:
        return ticker, None, str(e)


def _parse_mcap(mcap_str):
    """시총 문자열 → float ($)"""
    if not mcap_str or mcap_str == "-":
        return 0
    s = str(mcap_str).strip().upper()
    multiplier = 1
    if s.endswith("B"):
        multiplier = 1e9
        s = s[:-1]
    elif s.endswith("M"):
        multiplier = 1e6
        s = s[:-1]
    elif s.endswith("K"):
        multiplier = 1e3
        s = s[:-1]
    try:
        return float(s) * multiplier
    except ValueError:
        return 0


def _parse_float(val):
    """문자열/숫자 → float (안전 변환)"""
    if val is None or val == "-" or val == "":
        return None
    try:
        s = str(val).replace("%", "").replace(",", "").strip()
        return float(s)
    except (ValueError, TypeError):
        return None


def enrich_with_fundamentals(data, max_cap_b):
    """finvizfinance.quote로 개별 종목 펀더멘탈 보강"""
    if not data:
        return data

    tickers = [d["ticker"] for d in data]
    fundamentals = {}
    done = 0

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_single_fundamental, t): t for t in tickers}
        for fut in as_completed(futures):
            ticker, fund, desc = fut.result()
            if fund:
                fundamentals[ticker] = fund
            done += 1
            if done % 10 == 0 or done == len(tickers):
                print(f"       펀더멘탈 {done}/{len(tickers)}개 조회...")

    enriched = []
    for d in data:
        ticker = d["ticker"]
        f = fundamentals.get(ticker)
        if f:
            mcap = _parse_mcap(f.get("Market Cap", "0"))

            if max_cap_b and mcap > 0 and mcap > max_cap_b * 1e9:
                continue

            mcap_b = mcap / 1e9 if mcap > 0 else 0
            mcap_krw_t = mcap * KRW_RATE / 1e12 if mcap > 0 else 0

            price = _parse_float(f.get("Price", 0))
            change = _parse_float(f.get("Change", 0))
            pe = _parse_float(f.get("P/E", None))

            d["name"] = f.get("Company", ticker)
            d["sector"] = f.get("Sector", "-")
            d["mcap"] = mcap
            d["mcap_b"] = mcap_b
            d["mcap_krw_t"] = mcap_krw_t
            d["price"] = price if price else 0
            d["change"] = (change / 100) if change else 0
            d["pe"] = pe
        else:
            d["name"] = ticker
            d["sector"] = "-"
            d["mcap"] = 0
            d["mcap_b"] = 0
            d["mcap_krw_t"] = 0
            d["price"] = 0
            d["change"] = 0
            d["pe"] = None

        enriched.append(d)

    return enriched


# ──────────────────────────────────────────────────────────────
# 4단계: 스코어링 (10점 만점, 가중합)
# ──────────────────────────────────────────────────────────────

def calc_scores(data):
    """
    | 컴포넌트 | 가중치 | 계산 |
    | 클러스터 | 3.0 | min(1, (buyer_count-1)/3) |
    | 매수액   | 2.5 | min(1, log10(value/100K)/2) |
    | 직급     | 2.0 | best_weight / 3.0 |
    | 최근성   | 1.5 | max(0, (7-days_ago)/7) |
    | 확신도   | 1.0 | total_value/market_cap 비율 |
    """
    for d in data:
        # 클러스터 (3.0)
        bc = d.get("buyer_count", 1)
        cluster_raw = min(1.0, (bc - 1) / 3.0)
        cluster_score = cluster_raw * 3.0

        # 매수액 (2.5)
        tv = d.get("total_value", 0)
        if tv > 0:
            value_raw = min(1.0, math.log10(max(tv, 1) / 1e5) / 2.0)
            value_raw = max(0.0, value_raw)
        else:
            value_raw = 0
        value_score = value_raw * 2.5

        # 직급 (2.0)
        role_raw = min(1.0, d.get("best_weight", 0.5) / 3.0)
        role_score = role_raw * 2.0

        # 최근성 (1.5)
        days = d.get("most_recent_days")
        if days is not None and days <= 7:
            recency_raw = max(0, (7 - days) / 7.0)
        else:
            recency_raw = 0
        recency_score = recency_raw * 1.5

        # 확신도 (1.0) — total_value / market_cap
        mcap = d.get("mcap", 0)
        if mcap > 0 and tv > 0:
            ratio = tv / mcap
            # 0.1% 이상이면 만점
            conviction_raw = min(1.0, ratio / 0.001)
        else:
            conviction_raw = 0
        conviction_score = conviction_raw * 1.0

        d["score"] = round(cluster_score + value_score + role_score + recency_score + conviction_score, 1)
        d["_scores"] = {
            "cluster": round(cluster_score, 1),
            "value": round(value_score, 1),
            "role": round(role_score, 1),
            "recency": round(recency_score, 1),
            "conviction": round(conviction_score, 1),
        }

    data.sort(key=lambda x: x["score"], reverse=True)
    return data


# ──────────────────────────────────────────────────────────────
# 출력
# ──────────────────────────────────────────────────────────────

def print_results(results, args):
    now = datetime.datetime.now().strftime("%Y-%m-%d")
    mode_kr = {"buy": "매수", "sell": "매도", "all": "전체"}
    period_kr = {"latest": "최근", "week": "주간"}

    pd.set_option("display.unicode.east_asian_width", True)
    pd.set_option("display.max_rows", 100)
    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 20)

    print()
    print("=" * W)
    print(f"  US Insider Trading Screener  |  {now}")
    print(f"  모드: {mode_kr.get(args.mode, args.mode)} | 기간: {period_kr.get(args.period, args.period)} | 최소매수액: {_fmt_value(args.min_value)}")
    if args.market_cap:
        print(f"  시총상한: ${args.market_cap}B | 클러스터: {'only' if args.cluster_only else 'all'}")
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
        sector_short = SECTOR_KR.get(r.get("sector", ""), r.get("sector", "-")[:6])

        mcap_b = r.get("mcap_b", 0)
        mcap_str = f"{mcap_b:.1f}" if mcap_b > 0 else "-"
        mcap_krw = r.get("mcap_krw_t", 0)
        mcap_krw_str = f"{mcap_krw:.2f}" if mcap_krw > 0 else "-"

        price = r.get("price", 0)
        if isinstance(price, (int, float)) and price > 0:
            price_str = f"${price:.2f}" if price < 1000 else f"${price:,.0f}"
        else:
            price_str = "-"

        change = r.get("change", 0)
        if isinstance(change, str):
            change_str = change
        elif pd.notna(change) and change != 0:
            change_str = f"{change*100:+.1f}%" if abs(change) < 1 else f"{change:+.1f}%"
        else:
            change_str = "-"

        pe = r.get("pe", None)
        pe_str = f"{pe:.1f}" if pe is not None and pd.notna(pe) and pe > 0 else "-"

        days_str = f"{r['most_recent_days']}일전" if r.get("most_recent_days") is not None else "-"

        name = r.get("name", r["ticker"])
        name_str = (str(name)[:14] + "..") if len(str(name)) > 16 else str(name)

        rows.append({
            "티커": r["ticker"],
            "종목명": name_str,
            "섹터": sector_short,
            "시총($B)": mcap_str,
            "시총(조원)": mcap_krw_str,
            "현재가": price_str,
            "등락": change_str,
            "거래자": f"{r['buyer_count']}명",
            "거래액": _fmt_value(r["total_value"]),
            "최고직급": r["best_role"],
            "최근일": days_str,
            "P/E": pe_str,
            "점수": f"{r['score']:.1f}",
        })

    result_df = pd.DataFrame(rows)
    result_df.index = range(1, len(result_df) + 1)
    result_df.index.name = "#"
    print(result_df.to_string())

    print()
    print("=" * W)

    if args.detail:
        _print_cluster_details(top)

    _print_errors()
    print()


def _print_cluster_details(results):
    """클러스터 매수 종목별 개별 매수자 상세"""
    clusters = [r for r in results if r.get("buyer_count", 0) >= 2]
    if not clusters:
        print("\n [클러스터 매수 종목 없음]")
        return

    print(f"\n [클러스터 매수 상세] ({len(clusters)}개 종목)")
    print("-" * W)

    for r in clusters:
        name = r.get("name", r["ticker"])
        scores = r.get("_scores", {})
        print(f"\n  {r['ticker']} ({name}) — 점수 {r['score']:.1f}/10")
        print(f"  점수분해: 클러스터 {scores.get('cluster',0)}/3 | 매수액 {scores.get('value',0)}/2.5 | "
              f"직급 {scores.get('role',0)}/2 | 최근성 {scores.get('recency',0)}/1.5 | 확신도 {scores.get('conviction',0)}/1")

        for b in r.get("buyers", []):
            days_str = f"{b['days_ago']}일전" if b.get("days_ago") is not None else "-"
            print(f"    {b['role']:>5s} | {b['name']:<28s} | {_fmt_value(b['value']):>8s} | @${b['cost']:,.2f} | {days_str}")

    print("-" * W)


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
    parser = argparse.ArgumentParser(description="US Insider Trading Screener")
    parser.add_argument("--mode", choices=["buy", "sell", "all"], default="buy", help="매수/매도/전체 (기본: buy)")
    parser.add_argument("--period", choices=["latest", "week"], default="week", help="최근/주간 (기본: week)")
    parser.add_argument("--min-value", type=float, default=50000, help="최소 거래액 $ (기본: 50000)")
    parser.add_argument("--market-cap", type=float, default=None, help="시총 상한 $B (기본: 무제한)")
    parser.add_argument("--cluster-only", action="store_true", help="2명 이상 클러스터만")
    parser.add_argument("--top", type=int, default=20, help="상위 N개 출력 (기본: 20)")
    parser.add_argument("--detail", action="store_true", help="클러스터 상세 출력")
    args = parser.parse_args()

    _errors.clear()

    print()
    print(f" [1/4] Insider 데이터 수집 중... (mode={args.mode}, period={args.period})")
    df = fetch_insider_data(args.mode, args.period)
    if df.empty:
        print("       데이터 없음")
        print_results([], args)
        return
    print(f"       총 {len(df)}건 수집")

    print(f" [2/4] 티커별 그룹화 + 클러스터 분석... (최소 {_fmt_value(args.min_value)})")
    data = analyze_by_ticker(df, args.min_value)
    if not data:
        print("       조건 충족 종목 없음")
        print_results([], args)
        return

    if args.cluster_only:
        data = [d for d in data if d["buyer_count"] >= 2]
        print(f"       {len(data)}개 클러스터 종목")
    else:
        print(f"       {len(data)}개 종목 (클러스터: {sum(1 for d in data if d['buyer_count'] >= 2)}개)")

    if not data:
        print_results([], args)
        return

    cap_str = f"<${args.market_cap}B" if args.market_cap else "무제한"
    print(f" [3/4] 펀더멘탈 보강 중... (시총 {cap_str})")
    data = enrich_with_fundamentals(data, args.market_cap)
    print(f"       {len(data)}개 종목 보강 완료")

    print(f" [4/4] 스코어링... ({len(data)}개 종목)")
    data = calc_scores(data)
    if data:
        print(f"       완료 (최고 {data[0]['score']:.1f}점 ~ 최저 {data[-1]['score']:.1f}점)")
    else:
        print("       결과 없음")

    print_results(data, args)


if __name__ == "__main__":
    main()
