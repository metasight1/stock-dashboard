# 📊 US Stock Filter Dashboard

매주 월·목 오전 7시(KST) 자동으로 미국 주식을 필터링하여 정적 웹페이지로 게시하는 개인 프로젝트.

## 작동 방식

1. **GitHub Actions**가 매주 월·목 KST 7시(UTC 일/수 22시)에 자동 실행
2. **Python 스크립트** (`scripts/update_data.py`)가 yfinance로 데이터 수집·필터링
3. 결과를 `data.json`에 저장하고 자동 커밋
4. **GitHub Pages**가 `index.html`을 자동 게시 → 어디서나 접속 가능

## 필터 기준

### 배당주 (최대 10개)
- 배당락일이 오늘로부터 7~14일 사이
- 배당률 ≥ Bull 2.5% / Mixed 2.75% / Bear 3.0%
- Yahoo Finance recommendationMean ≤ 2.5 (Buy 이상)

### 추천주 (최대 10개)
- Yahoo Finance recommendationMean ≤ 2.0 (Buy 이상)
- 향후 12개월 EPS 성장률 ≥ 10%
- PEG ≤ 1.5

### 제외 종목
GOOGL, GOOG, AAPL, META, MSFT, AMZN, NVDA, TSLA

### 시장 상태 (Bull / Mixed / Bear)
- Bull: S&P 500 > 200일 MA AND VIX < 20
- Bear: S&P 500 < 200일 MA AND VIX > 25
- Mixed: 그 외

## 데이터 소스
- **Yahoo Finance** (`yfinance` Python 라이브러리, 무료)

## 한계
- 무료 데이터 한계로 Finviz/TipRanks/Morningstar 컨센서스가 아닌 Yahoo의 평균 등급(recommendationMean) 사용
- 추천주의 "주간 5회 이상 언급" 조건은 V1에서 제외
- 실제 매수 전 증권사 공식 수치 재확인 필수

## 폴더 구조
```
.
├── index.html              # 대시보드 페이지
├── data.json               # 필터 결과 (자동 갱신)
├── scripts/
│   └── update_data.py      # 필터링 스크립트
├── requirements.txt        # Python 의존성
├── .github/
│   └── workflows/
│       └── update.yml      # GitHub Actions 스케줄
├── README.md
└── SETUP_GUIDE.md          # 설정 매뉴얼
```

## 수동 실행
GitHub 저장소 → Actions 탭 → "Update Stock Data" 워크플로 → "Run workflow" 버튼.

## 라이선스
개인용 프로젝트. 투자 자문이 아닙니다.
