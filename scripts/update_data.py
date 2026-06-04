"""
미국 주식 필터링 자동 갱신 스크립트

매주 월/목 오전 7시 KST (월/목 22:00 UTC 전날) GitHub Actions가 실행.
Yahoo Finance(yfinance)에서 데이터를 가져와 다음을 계산:
  - S&P 500 vs 200일 이동평균
  - VIX 변동성
  - 시장 상태(Bull/Mixed/Bear)
  - 배당주 후보 (배당락일 7~14일, 배당률 임계값, recommendationMean ≤ 2.5)
  - 추천주 후보 (recommendationMean ≤ 2.0, EPS 성장률 ≥ 10%, PEG ≤ 1.5)

결과는 ../data.json에 저장.

한계:
- yfinance recommendationMean은 Yahoo가 집계한 평균 등급 (1=Strong Buy, 5=Strong Sell)
  Finviz/TipRanks/Morningstar 컨센서스와 정확히 일치하지 않음
- 추천주의 "주간 5회 이상 언급" 조건은 무료 뉴스 API 한계로 V1에서는 제외
"""

import json
import os
from datetime import datetime, timedelta, timezone
import yfinance as yf
import pandas as pd

# 한국 시간대
KST = timezone(timedelta(hours=9))

# 제외 종목
EXCLUDED = {"GOOGL", "GOOG", "AAPL", "META", "MSFT", "AMZN", "NVDA", "TSLA"}

# 데이터 파일 경로 (스크립트 위치 기준)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, "..", "data.json")

# S&P 500 일부 + 대표 배당주/성장주 유니버스 (V1에서는 약 200개)
# V2에서 자동으로 S&P 500 전체 리스트를 위키피디아에서 가져오도록 확장 가능
UNIVERSE = [
    # 배당 귀족(Dividend Aristocrats) 일부
    "JNJ", "PG", "KO", "PEP", "MCD", "WMT", "HD", "CVX", "XOM", "IBM",
    "MMM", "CAT", "ABBV", "ABT", "MDT", "T", "VZ", "PFE", "MRK", "BMY",
    "LLY", "GIS", "K", "KMB", "CL", "ED", "SO", "DUK", "AEP", "WEC",
    "O", "STAG", "VICI", "WPC", "MAIN", "ARCC", "BX", "BLK", "JPM", "BAC",
    "WFC", "C", "GS", "MS", "USB", "PNC", "TFC", "SCHW", "AXP", "V",
    "MA", "ADP", "PAYX", "INTU", "TXN", "AVGO", "QCOM", "CSCO", "ORCL", "CRM",
    "ACN", "IBM", "NOW", "ADBE", "SAP", "BABA", "TSM", "ASML", "AMD", "INTC",
    "MU", "AMAT", "LRCX", "KLAC", "MRVL", "ON", "MCHP", "NXPI", "TXN", "STM",
    "BA", "LMT", "RTX", "NOC", "GD", "HII", "TDG", "LHX", "TXT", "BAH",
    "UPS", "FDX", "CSX", "UNP", "NSC", "DAL", "UAL", "AAL", "LUV", "ALK",
    "NEE", "D", "EXC", "PCG", "SRE", "XEL", "AEE", "EIX", "PPL", "CMS",
    "DOW", "DD", "LIN", "APD", "ECL", "SHW", "PPG", "ALB", "FCX", "NEM",
    "AMGN", "GILD", "BIIB", "REGN", "VRTX", "MRNA", "CVS", "WBA", "CI", "HUM",
    "UNH", "ANTM", "CNC", "MOH", "ISRG", "TMO", "DHR", "BDX", "SYK", "EW",
    "BSX", "ZBH", "BAX", "HOLX", "RMD", "IDXX", "WST", "TFX", "PKI", "A",
    "ELS", "AMT", "CCI", "PLD", "EQIX", "DLR", "PSA", "EXR", "AVB", "EQR",
    "SPG", "REG", "FRT", "BXP", "VTR", "WELL", "OHI", "DOC", "MAA", "ESS",
    # 성장주 (제외 종목 빼고)
    "ADBE", "NFLX", "DIS", "CMCSA", "TMUS", "VZ", "PYPL", "SQ", "SHOP", "COIN",
    "UBER", "LYFT", "DASH", "ABNB", "NET", "DDOG", "SNOW", "CRWD", "ZS", "PANW",
    "FTNT", "OKTA", "TWLO", "DOCN", "MDB", "TEAM", "WDAY", "SPOT", "ROKU", "PINS",
    "ZM", "DOCU", "U", "TWTR", "SNAP",
]

# 중복 제거
UNIVERSE = sorted(set(UNIVERSE))


def get_market_regime():
    """S&P 500과 VIX를 사용해 Bull/Mixed/Bear 판정."""
    try:
        # S&P 500
        spx = yf.Ticker("^GSPC")
        hist = spx.history(period="1y")
        if hist.empty:
            return None
        sp500_price = float(hist["Close"].iloc[-1])
        sp500_200dma = float(hist["Close"].tail(200).mean())
        sp500_vs_200dma_pct = (sp500_price - sp500_200dma) / sp500_200dma * 100

        # VIX
        vix_t = yf.Ticker("^VIX")
        vix_hist = vix_t.history(period="5d")
        vix = float(vix_hist["Close"].iloc[-1]) if not vix_hist.empty else None

        # 판정
        if sp500_price > sp500_200dma and vix is not None and vix < 20:
            regime = "bull"
        elif sp500_price < sp500_200dma and vix is not None and vix > 25:
            regime = "bear"
        else:
            regime = "mixed"

        rationale = (
            f"S&P 500이 {sp500_price:.0f}로 200일 이동평균({sp500_200dma:.0f})보다 "
            f"{sp500_vs_200dma_pct:+.2f}% 위치, VIX는 {vix:.2f}. "
            f"이를 종합하여 {regime.upper()} 시장으로 판정."
        )

        return {
            "regime": regime,
            "rationale": rationale,
            "sp500_price": round(sp500_price, 2),
            "sp500_200dma_pct": round(sp500_vs_200dma_pct, 2),
            "vix": round(vix, 2) if vix is not None else None,
        }
    except Exception as e:
        print(f"[market_regime] 오류: {e}")
        return None


def get_yield_threshold(regime):
    return {"bull": 2.5, "mixed": 2.75, "bear": 3.0}.get(regime, 2.75)


def consensus_label(mean):
    """yfinance recommendationMean(1~5)을 라벨로 변환."""
    if mean is None:
        return "—"
    if mean <= 1.5:
        return "Strong Buy"
    if mean <= 2.5:
        return "Buy"
    if mean <= 3.5:
        return "Hold"
    if mean <= 4.5:
        return "Sell"
    return "Strong Sell"


def filter_dividend_stocks(yield_threshold):
    """배당락일 7~14일 후 + 배당률 임계값 이상 + Buy 컨센서스."""
    today = datetime.now(KST).date()
    start_date = today + timedelta(days=7)
    end_date = today + timedelta(days=14)

    results = []
    for ticker in UNIVERSE:
        if ticker.upper() in EXCLUDED:
            continue
        try:
            t = yf.Ticker(ticker)
            info = t.info or {}

            div_yield = info.get("dividendYield")
            # yfinance는 0.025 또는 2.5로 반환하는 경우가 혼재 — 정규화
            if div_yield is None:
                continue
            if div_yield > 1:  # 이미 % 단위
                yield_pct = float(div_yield)
            else:
                yield_pct = float(div_yield) * 100

            if yield_pct < yield_threshold:
                continue

            # 배당락일 확인
            ex_date_raw = info.get("exDividendDate")
            if not ex_date_raw:
                continue
            try:
                ex_date = datetime.fromtimestamp(ex_date_raw, tz=KST).date()
            except (TypeError, ValueError):
                continue
            if not (start_date <= ex_date <= end_date):
                continue

            # 애널리스트 컨센서스
            rec_mean = info.get("recommendationMean")
            if rec_mean is None or rec_mean > 2.5:
                continue

            results.append({
                "ticker": ticker,
                "name": info.get("shortName") or info.get("longName") or ticker,
                "ex_date": ex_date.isoformat(),
                "yield_pct": round(yield_pct, 2),
                "consensus": consensus_label(rec_mean),
                "sources": ["yahoo"],
                "note_kr": f"배당락일 {ex_date.isoformat()}, 배당률 {yield_pct:.2f}%",
            })

            if len(results) >= 10:
                break
        except Exception as e:
            print(f"[dividend:{ticker}] 오류: {e}")
            continue

    return results


def filter_recommend_stocks():
    """recommendationMean ≤ 2.0 + EPS 성장률 ≥ 10% + PEG ≤ 1.5."""
    results = []
    for ticker in UNIVERSE:
        if ticker.upper() in EXCLUDED:
            continue
        try:
            t = yf.Ticker(ticker)
            info = t.info or {}

            rec_mean = info.get("recommendationMean")
            if rec_mean is None or rec_mean > 2.0:
                continue

            # EPS 성장률 (earningsGrowth는 비율로 반환 - 0.10이 10%)
            eps_growth = info.get("earningsGrowth")
            if eps_growth is None:
                # forwardEps / trailingEps로 추정
                f = info.get("forwardEps")
                tr = info.get("trailingEps")
                if f and tr and tr > 0:
                    eps_growth = (f - tr) / tr
                else:
                    continue
            eps_growth_pct = float(eps_growth) * 100
            if eps_growth_pct < 10:
                continue

            # PEG
            peg = info.get("pegRatio") or info.get("trailingPegRatio")
            if peg is None or peg > 1.5:
                continue

            results.append({
                "ticker": ticker,
                "name": info.get("shortName") or info.get("longName") or ticker,
                "mentions_7d": None,  # V1에서는 측정 안 함
                "consensus": consensus_label(rec_mean),
                "eps_growth_12m_pct": round(eps_growth_pct, 1),
                "peg": round(float(peg), 2),
                "sources": ["yahoo"],
                "note_kr": f"EPS 성장 {eps_growth_pct:.1f}%, PEG {peg:.2f}",
            })

            if len(results) >= 10:
                break
        except Exception as e:
            print(f"[recommend:{ticker}] 오류: {e}")
            continue

    return results


def main():
    print("=== 미국 주식 필터링 시작 ===")
    now_kst = datetime.now(KST)
    print(f"실행 시각: {now_kst.isoformat()}")

    # 1. 시장 상태
    market = get_market_regime()
    if market is None:
        print("⚠️ 시장 데이터 수집 실패 - 기본값 mixed 사용")
        market = {"regime": "mixed", "rationale": "데이터 수집 실패", "sp500_price": None, "sp500_200dma_pct": None, "vix": None}

    yield_threshold = get_yield_threshold(market["regime"])
    print(f"시장 상태: {market['regime']} / 배당률 임계값: {yield_threshold}%")

    # 2. 배당주
    print("배당주 필터링 중...")
    dividend = filter_dividend_stocks(yield_threshold)
    print(f"→ {len(dividend)}개 발견")

    # 3. 추천주
    print("추천주 필터링 중...")
    recommend = filter_recommend_stocks()
    print(f"→ {len(recommend)}개 발견")

    # 4. 저장
    output = {
        "updated_at": now_kst.strftime("%Y-%m-%d %H:%M KST"),
        "regime": market["regime"],
        "regime_rationale": market["rationale"],
        "excluded": sorted(EXCLUDED),
        "market": {
            "sp500_price": market.get("sp500_price"),
            "sp500_200dma_pct": market.get("sp500_200dma_pct"),
            "vix": market.get("vix"),
            "new_highs": None,
            "new_lows": None,
        },
        "dividend": dividend,
        "recommend": recommend,
        "notes": "yfinance(Yahoo Finance) 데이터 기준. 매수 전 증권사 공식 데이터로 재확인 필요.",
    }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"✓ data.json 저장 완료: {DATA_FILE}")
    print(f"  배당주 {len(dividend)}개 / 추천주 {len(recommend)}개 / 시장: {market['regime']}")


if __name__ == "__main__":
    main()
