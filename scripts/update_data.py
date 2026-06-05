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

애널리스트 컨센서스:
- 1순위: Alpha Vantage OVERVIEW의 애널리스트 등급 분포(StrongBuy~StrongSell)를
  1~5 스케일 가중평균으로 환산해 사용 (출처가 명확해 더 정확).
  API 키는 환경변수 ALPHA_VANTAGE_API_KEY(=GitHub Secrets)로 주입.
  무료 티어 분당 5회 한도를 슬라이딩 윈도우로 준수.
- 폴백: 키가 없거나 호출/데이터에 실패하면 yfinance recommendationMean 사용.

한계:
- yfinance recommendationMean은 Yahoo가 집계한 평균 등급 (1=Strong Buy, 5=Strong Sell)
  Finviz/TipRanks/Morningstar 컨센서스와 정확히 일치하지 않음
- 추천주의 "주간 5회 이상 언급" 조건은 무료 뉴스 API 한계로 V1에서는 제외
"""

import io
import json
import os
import time
import urllib.parse
import urllib.request
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

# S&P 500 구성종목 목록이 정리되어 있는 위키피디아 페이지
SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
# 위키피디아 응답을 저장해 두는 로컬 캐시 파일 (스크립트 위치 기준)
SP500_CACHE_FILE = os.path.join(SCRIPT_DIR, "sp500_cache.json")
# 캐시 유효 기간 — 24시간(초 단위). 이 기간 안에는 위키피디아를 다시 호출하지 않음
SP500_CACHE_TTL_SECONDS = 24 * 60 * 60

# --- Alpha Vantage (애널리스트 컨센서스 소스) ---------------------------------
# API 키는 GitHub Secrets로 주입한 환경변수에서 읽는다 (소스코드에 하드코딩 금지).
# 키가 없으면 Alpha Vantage 호출을 건너뛰고 yfinance로 폴백한다.
ALPHA_VANTAGE_API_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip()
ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"
# 무료 티어 한도: 분당 5회. 60초 창 안에서 호출 횟수를 추적해 한도를 넘기지 않는다.
AV_MAX_CALLS_PER_MIN = 5
_av_call_times = []  # 최근 Alpha Vantage 호출 시각(epoch 초) 기록

# 위키피디아 호출이 실패했을 때 사용할 폴백(fallback) 유니버스.
# 네트워크 오류나 페이지 구조 변경으로 자동 수집이 실패하더라도
# 스크립트가 빈 리스트로 동작하지 않도록 대표 배당주/성장주를 보존해 둔다.
FALLBACK_UNIVERSE = [
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


def _load_cached_universe():
    """캐시 파일에서 유효한(24시간 이내) S&P 500 리스트를 읽어온다.

    반환:
        list[str] | None — 캐시가 유효하면 종목 리스트, 없거나 만료되면 None
    """
    if not os.path.exists(SP500_CACHE_FILE):
        return None
    try:
        with open(SP500_CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        # fetched_at(저장 시각, epoch 초)과 현재 시각을 비교해 TTL 초과 여부 판단
        fetched_at = cache.get("fetched_at", 0)
        tickers = cache.get("tickers")
        if not tickers:
            return None
        if time.time() - fetched_at > SP500_CACHE_TTL_SECONDS:
            # 24시간이 지났으면 만료 처리 → 호출부에서 위키피디아를 재요청
            return None
        return tickers
    except (json.JSONDecodeError, OSError) as e:
        print(f"[sp500_cache] 캐시 읽기 실패: {e}")
        return None


def _save_cached_universe(tickers):
    """수집한 S&P 500 리스트와 현재 시각을 캐시 파일에 저장한다."""
    try:
        with open(SP500_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.time(), "tickers": tickers}, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[sp500_cache] 캐시 저장 실패: {e}")


def fetch_sp500_tickers():
    """위키피디아에서 S&P 500 전체 구성종목 티커를 가져온다.

    pandas.read_html로 페이지의 표를 파싱하며, 첫 번째 표의 'Symbol' 열이
    구성종목 목록이다. 위키피디아는 'BRK.B'처럼 점(.) 표기를 쓰지만
    yfinance는 'BRK-B'처럼 하이픈(-)을 쓰므로 변환해 준다.

    반환:
        list[str] — 정렬·중복 제거된 티커 리스트 (실패 시 빈 리스트)
    """
    try:
        # 위키피디아는 기본 urllib User-Agent를 차단(HTTP 403)하므로
        # 브라우저처럼 보이는 User-Agent 헤더를 붙여 직접 HTML을 받아온다.
        req = urllib.request.Request(
            SP500_WIKI_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; stock-dashboard/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8")

        # read_html은 페이지 내 모든 <table>을 DataFrame 리스트로 반환.
        # 첫 번째 표가 구성종목 목록이며 'Symbol' 열에 티커가 들어 있다.
        df = pd.read_html(io.StringIO(html))[0]
        # yfinance 호환을 위해 점(.)을 하이픈(-)으로 치환 (예: BRK.B → BRK-B)
        tickers = [str(sym).replace(".", "-").strip() for sym in df["Symbol"].tolist()]
        # 빈 값 제거 후 중복 제거·정렬
        return sorted({t for t in tickers if t})
    except Exception as e:
        print(f"[sp500] 위키피디아 수집 실패: {e}")
        return []


def get_universe():
    """필터링에 사용할 종목 유니버스를 결정한다.

    우선순위:
      1) 24시간 이내 캐시가 있으면 그대로 사용 (위키피디아 재요청 방지)
      2) 캐시가 없거나 만료됐으면 위키피디아에서 새로 수집 후 캐시에 저장
      3) 수집까지 실패하면 폴백 유니버스 사용
    """
    cached = _load_cached_universe()
    if cached:
        print(f"[sp500] 캐시 사용: {len(cached)}개 종목")
        return cached

    tickers = fetch_sp500_tickers()
    if tickers:
        _save_cached_universe(tickers)
        print(f"[sp500] 위키피디아에서 {len(tickers)}개 종목 수집 (24시간 캐시 저장)")
        return tickers

    print(f"[sp500] 수집 실패 → 폴백 유니버스 {len(FALLBACK_UNIVERSE)}개 사용")
    return sorted(set(FALLBACK_UNIVERSE))


# 모듈 로드 시 한 번 결정되는 종목 유니버스 (캐시 → 위키피디아 → 폴백 순)
UNIVERSE = get_universe()


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
    """recommendationMean(1~5)을 라벨로 변환. (Alpha Vantage·yfinance 공통 스케일)"""
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


def _alpha_vantage_rate_limit():
    """분당 5회 한도를 지키도록, 필요하면 다음 호출 전까지 sleep 한다.

    최근 60초 안의 호출 시각을 _av_call_times에 모아두고,
    이미 5회를 채웠다면 가장 오래된 호출이 60초 창을 벗어날 때까지 대기한다.
    (단순 고정 sleep 대신 슬라이딩 윈도우로 관리해 불필요한 대기를 줄임)
    """
    now = time.time()
    # 60초보다 오래된 호출 기록은 윈도우에서 제거
    while _av_call_times and now - _av_call_times[0] >= 60:
        _av_call_times.pop(0)
    if len(_av_call_times) >= AV_MAX_CALLS_PER_MIN:
        # 가장 오래된 호출이 60초를 넘길 때까지 대기 (+0.5초 안전 여유)
        wait = 60 - (now - _av_call_times[0]) + 0.5
        if wait > 0:
            print(f"[alpha_vantage] rate limit 대기 {wait:.1f}s")
            time.sleep(wait)
    _av_call_times.append(time.time())


def fetch_alpha_consensus(ticker):
    """Alpha Vantage OVERVIEW에서 애널리스트 등급 분포를 받아 1~5 평균으로 환산.

    OVERVIEW 응답의 AnalystRatingStrongBuy/Buy/Hold/Sell/StrongSell 카운트를
    가중평균하여 yfinance recommendationMean과 동일한
    1(Strong Buy)~5(Strong Sell) 스케일의 컨센서스 값을 만든다.
    이 분포 기반 값은 yfinance의 집계 평균보다 출처가 명확하고 정확하다.

    반환:
        {"mean": float, "label": str, "counts": {...}} — 정상 수집
        None — 키 없음 / rate limit 안내 / 데이터 없음 / 오류 (호출부에서 폴백)
    """
    if not ALPHA_VANTAGE_API_KEY:
        return None
    try:
        # 호출 직전에 rate limit을 점검·대기 (분당 5회 보장)
        _alpha_vantage_rate_limit()
        query = urllib.parse.urlencode({
            "function": "OVERVIEW",
            "symbol": ticker,
            "apikey": ALPHA_VANTAGE_API_KEY,
        })
        req = urllib.request.Request(
            f"{ALPHA_VANTAGE_URL}?{query}",
            headers={"User-Agent": "stock-dashboard/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        # 한도 초과나 안내 메시지는 정상 데이터가 아님 → None 반환해 폴백 유도.
        # Alpha Vantage는 한도 초과 시 'Note'/'Information' 키로 메시지를 보낸다.
        if not data or "Symbol" not in data:
            note = (data or {}).get("Note") or (data or {}).get("Information")
            if note:
                print(f"[alpha_vantage:{ticker}] API 메시지: {note}")
            return None

        # 등급별 카운트 (값이 없거나 비어 있으면 0 처리)
        sb = int(data.get("AnalystRatingStrongBuy") or 0)
        b = int(data.get("AnalystRatingBuy") or 0)
        h = int(data.get("AnalystRatingHold") or 0)
        s = int(data.get("AnalystRatingSell") or 0)
        ss = int(data.get("AnalystRatingStrongSell") or 0)
        total = sb + b + h + s + ss
        if total == 0:
            return None  # 애널리스트 커버리지 없음 → 폴백

        # 1=Strong Buy … 5=Strong Sell 가중평균
        mean = (1 * sb + 2 * b + 3 * h + 4 * s + 5 * ss) / total
        return {
            "mean": mean,
            "label": consensus_label(mean),
            "counts": {"strong_buy": sb, "buy": b, "hold": h, "sell": s, "strong_sell": ss},
        }
    except Exception as e:
        print(f"[alpha_vantage:{ticker}] 오류: {e}")
        return None


def get_consensus(ticker, yf_info):
    """애널리스트 컨센서스를 Alpha Vantage 우선으로, 실패 시 yfinance로 가져온다.

    반환: (mean: float|None, label: str, source: "alphavantage"|"yahoo")
    """
    av = fetch_alpha_consensus(ticker)
    if av is not None:
        return av["mean"], av["label"], "alphavantage"
    # 폴백: yfinance recommendationMean
    rec_mean = yf_info.get("recommendationMean")
    return rec_mean, consensus_label(rec_mean), "yahoo"


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

            # 애널리스트 컨센서스 — Alpha Vantage 우선, 실패 시 yfinance 폴백.
            # rate limit 비용이 있는 호출이므로, 위의 배당률·배당락일을 통과한
            # 후보에 대해서만 (가장 마지막에) 컨센서스를 조회한다.
            rec_mean, label, source = get_consensus(ticker, info)
            if rec_mean is None or rec_mean > 2.5:
                continue

            results.append({
                "ticker": ticker,
                "name": info.get("shortName") or info.get("longName") or ticker,
                "ex_date": ex_date.isoformat(),
                "yield_pct": round(yield_pct, 2),
                "consensus": label,
                # 배당 정보는 yahoo, 컨센서스는 실제 사용된 출처를 함께 표기
                "sources": sorted({"yahoo", source}),
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

            # rate limit이 걸리는 Alpha Vantage 호출 횟수를 줄이기 위해,
            # 비용이 없는 yfinance 펀더멘털(EPS 성장·PEG)을 먼저 검사하고
            # 모두 통과한 후보에 대해서만 컨센서스를 조회한다.

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

            # 애널리스트 컨센서스 — Alpha Vantage 우선, 실패 시 yfinance 폴백
            rec_mean, label, source = get_consensus(ticker, info)
            if rec_mean is None or rec_mean > 2.0:
                continue

            results.append({
                "ticker": ticker,
                "name": info.get("shortName") or info.get("longName") or ticker,
                "mentions_7d": None,  # V1에서는 측정 안 함
                "consensus": label,
                "eps_growth_12m_pct": round(eps_growth_pct, 1),
                "peg": round(float(peg), 2),
                # 펀더멘털은 yahoo, 컨센서스는 실제 사용된 출처를 함께 표기
                "sources": sorted({"yahoo", source}),
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
        "notes": "가격·펀더멘털은 yfinance(Yahoo Finance), 애널리스트 컨센서스는 Alpha Vantage 우선(실패 시 yfinance 폴백) 기준. 매수 전 증권사 공식 데이터로 재확인 필요.",
    }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"✓ data.json 저장 완료: {DATA_FILE}")
    print(f"  배당주 {len(dividend)}개 / 추천주 {len(recommend)}개 / 시장: {market['regime']}")


if __name__ == "__main__":
    main()
