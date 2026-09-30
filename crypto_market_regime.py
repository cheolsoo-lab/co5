"""
crypto_market_regime.py
========================
멀티거래소(Binance / OKX / Bitget) 실시간 데이터 기반
크립토 시장 국면(상승/횡보/하락) 판단 + 코인 스크리너 + TP/SL 제안 도구

⚠️ 중요 전제 (반드시 읽고 사용하세요)
--------------------------------
1. 이 스크립트는 "수익을 보장하는 시그널 생성기"가 아닙니다.
   - 시장 국면을 객관적 지표로 요약하고, 조건에 맞는 후보를 걸러주는 "의사결정 보조 도구"입니다.
   - 여기서 나온 TP/SL/방향은 참고용이며, 최종 진입/청산 판단과 책임은 사용자 본인에게 있습니다.
2. 실전 투입 전 반드시 아래 순서를 거치세요:
   a) 최소 3~6개월 페이퍼 트레이딩(모의투자)으로 신호 품질 검증
   b) backtest_wfo() 로 워크포워드 검증 (과거 특정 구간에만 맞춰진 과최적화 여부 확인)
   c) 실전 투입 시 레버리지는 규칙 기반으로 상한을 강제 (이 스크립트는 레버리지 추천을 하지 않습니다)
3. 네트워크가 막힌 환경(샌드박스)에서는 실행되지 않습니다. 로컬/서버에서 다음을 설치 후 실행하세요:
   pip install ccxt requests pandas numpy

작성 방식: 단일 파일, 모듈형 함수 구성. main() 에서 전체 파이프라인을 한 번에 실행합니다.
"""

import time
import json
import os
import math
import statistics
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Literal

import requests
import pandas as pd
import numpy as np

try:
    import ccxt
except ImportError:
    ccxt = None  # 실행 시 pip install ccxt 안내

# --------------------------------------------------------------------------
# 0. 설정
# --------------------------------------------------------------------------

EXCHANGES = ["bitget", "okx", "binance"]          # 앞쪽일수록 우선 사용(Bitget = 실제 거래 거래소). 일부 거래소는 서버 지역에 따라 차단될 수 있음
QUOTE = "USDT"
TOP_N_BY_VOLUME = 100                             # 거래량 상위 N개 코인만 스크리닝
TIMEFRAME = "4h"                                  # 스윙 트레이딩 기준 봉
OHLCV_LIMIT = 600                                 # 4h 캔들 개수 (EMA200이 제대로 계산되려면 수백 개 필요 — 200개면 첫 봉 비중이 13%나 남음)
HTF_LIMIT = 300                                   # 일봉 캔들 개수 (일봉 EMA200용)
ADX_TREND_MIN = 25.0                              # 추세 판정: Wilder ADX 최소값
ER_TREND_MIN = 0.20                               # 추세 판정: 효율비율(50봉 순이동 ÷ 총이동) 최소값
MIN_QUOTE_VOLUME_USDT = 3_000_000                 # 24h 거래대금 최소 — 유동성 얇아 휘둘리기 쉬운 코인 제외
STOP_BUFFER_ATR = 0.3                             # 손절을 뻔한 구조 레벨(오더블록·박스 끝)보다 이만큼 더 바깥에
MIN_STOP_ATR = 1.0                                # 손절폭 최소값 — 너무 좁은 손절은 노이즈·손절 사냥에 취약
CHASE_ATR = 0.5                                   # 현재가가 진입가에서 이만큼(ATR 배수) 넘게 벗어나면 '추격 구간'
PARTIAL_TP_FRACTION = 0.5                         # 청산: 목표1에서 이 비율만큼 익절
TRAIL_ATR = 2.5                                   # 청산: 나머지는 최고가(롱)/최저가(숏)에서 이만큼 떨어지면 정리(추적 손절)
LONG_BIASES = ("long", "wait_breakout_long", "range_fade_long")

# 신호 봉 모드. 1시간봉은 '비교용' — 상대강도 기간은 시간으로 환산(≈1·2·4주 유지), 상위추세는 4시간봉.
# 나머지 기준(박스 60봉, 임펄스 30봉 등)은 봉 개수 그대로라 기간이 4분의 1로 짧아지는 '더 빠른 전략'이 됩니다.
TIMEFRAME_PRESETS = {
    "4h": {"limit": 600, "htf": "1d", "rs_windows": [42, 84, 168], "bars_per_8h": 2, "label": "4시간봉"},
    "1h": {"limit": 700, "htf": "4h", "rs_windows": [168, 336, 672], "bars_per_8h": 8, "label": "1시간봉"},
}
HTF_TIMEFRAME = "1d"
RS_WINDOWS = [42, 84, 168]
BARS_PER_8H = 2


def set_timeframe(tf: str) -> None:
    """신호 봉 모드 전환(4h 기본 / 1h 비교용). 모듈 전역 설정을 바꿉니다."""
    global TIMEFRAME, OHLCV_LIMIT, HTF_TIMEFRAME, RS_WINDOWS, BARS_PER_8H
    p = TIMEFRAME_PRESETS[tf]
    TIMEFRAME, OHLCV_LIMIT, HTF_TIMEFRAME = tf, p["limit"], p["htf"]
    RS_WINDOWS, BARS_PER_8H = list(p["rs_windows"]), p["bars_per_8h"]


def tf_label(tf: Optional[str] = None) -> str:
    return TIMEFRAME_PRESETS.get(tf or TIMEFRAME, {}).get("label", tf or TIMEFRAME)
HISTORY_FILE = "market_regime_history.csv"        # BTC.D / USDT.D / TOTAL2,3 스냅샷 누적 저장 (트렌드 판단용)
REGIME_LOG_FILE = "regime_confirmation_log.csv"   # 국면 whipsaw 방지용 확정 이력

CoinGeckoGlobalURL = "https://api.coingecko.com/api/v3/global"
CoinPaprikaGlobalURL = "https://api.coinpaprika.com/v1/global"
CoinPaprikaTickerURL = "https://api.coinpaprika.com/v1/tickers/{coin_id}"

RegimeType = Literal["uptrend", "downtrend", "sideways"]


@dataclass
class RiskConfig:
    """계좌 단위 리스크 관리 설정. '얼마나 걸지'는 신호와 완전히 분리해서 관리해야 합니다."""
    account_balance: float          # 계좌 총 잔고 (USDT 기준)
    risk_per_trade_pct: float = 1.0  # 트레이드 1건당 허용 손실 (계좌 대비 %). 권장 0.5~2%
    max_correlated_exposure_pct: float = 3.0  # BTC 방향에 동조된 포지션들의 합산 리스크 상한(%)
    max_concurrent_setups: int = 10  # 같은 방향(롱 또는 숏) 동시 보유 최대 개수
    max_total_risk_pct: float = 5.0  # 동시에 들고 있는 모든 포지션의 손절 시 합산 손실 상한(계좌 대비 %)
    open_positions: int = 0          # 지금 거래소에서 이미 보유 중인 포지션 수(직접 입력 — 프로그램은 계좌를 모름)


def available_slots(risk_cfg: "RiskConfig") -> int:
    """총 리스크 상한 안에서 새로 잡을 수 있는 포지션 수.
    예) 상한 5%, 건당 1%, 보유 2개 → 3개. 알트코인은 BTC와 같이 움직여서, 같은 방향 여러 개는
    한 번에 같이 손절될 수 있으므로 '합계'로 묶어 제한합니다."""
    r = max(risk_cfg.risk_per_trade_pct, 1e-9)
    left = risk_cfg.max_total_risk_pct - risk_cfg.open_positions * r
    return max(0, int(np.floor(left / r + 1e-9)))


def calculate_position_size(entry: float, sl: float, risk_cfg: RiskConfig) -> Dict:
    """RR이 아무리 좋아도 '얼마를 걸지'는 항상 이 공식으로만 결정합니다.
    포지션 크기 = (계좌잔고 * 리스크%) / |entry - sl|
    → SL에 닿아도 계좌 손실이 risk_per_trade_pct를 넘지 않도록 강제."""
    risk_amount = risk_cfg.account_balance * (risk_cfg.risk_per_trade_pct / 100)
    per_unit_risk = abs(entry - sl)
    if per_unit_risk <= 0:
        return {"size": 0, "risk_amount": risk_amount, "notional": 0}
    size = risk_amount / per_unit_risk
    notional = size * entry
    return {"size": size, "risk_amount": risk_amount, "notional": notional}


def cap_correlated_exposure(setups: List["CoinSetup"], risk_cfg: RiskConfig,
                             assumed_correlation: float = 0.6) -> List["CoinSetup"]:
    """같은 방향(롱/숏) 알트코인들은 BTC와 0.5~0.8 수준으로 동조화되는 경우가 흔해서
    (여러 개 들고 있어도 사실상 '하나의 큰 베팅'과 비슷) 두 단계로 제한합니다:

    1) 개수 제한: RR 상위 max_concurrent_setups개만 남김
    2) 상관조정: '유효 독립 베팅 수' = n / (1 + (n-1) * 평균상관계수) 공식으로
       실제 분산 효과가 얼마나 되는지 계산해서 함께 출력 (n=1이면 전혀 분산 안 된 것)
    """
    def _long_rank(s):
        return (s.rs, s.asymmetry if s.asymmetry is not None else 0.0, s.rr_ratio)

    def _short_rank(s):
        return (-s.rs, -(s.asymmetry if s.asymmetry is not None else 0.0), s.rr_ratio)

    long_like = sorted([s for s in setups if s.bias in ("long", "wait_breakout_long", "range_fade_long")],
                        key=_long_rank, reverse=True)[:risk_cfg.max_concurrent_setups]
    short_like = sorted([s for s in setups if s.bias in ("short", "wait_breakout_short", "range_fade_short")],
                         key=_short_rank, reverse=True)[:risk_cfg.max_concurrent_setups]

    for group, label in [(long_like, "롱"), (short_like, "숏")]:
        n = len(group)
        if n > 1:
            n_eff = n / (1 + (n - 1) * assumed_correlation)
            print(f"[상관관계] {label} {n}개 동시 보유 → 유효 독립 베팅 수 ≈ {n_eff:.1f}개 "
                  f"(가정 상관계수 {assumed_correlation}) — 실제 분산 효과는 숫자보다 훨씬 작습니다.")

    return long_like + short_like


# --------------------------------------------------------------------------
# 1. 거시 지표: BTC.D, USDT.D, TOTAL2, TOTAL3
# --------------------------------------------------------------------------

def _fetch_snapshot_coingecko() -> Optional[Dict]:
    """1차 공급처. 실패 시 None (예외를 던지지 않음 — 호출부가 다음 공급처로 넘어감)."""
    try:
        resp = requests.get(CoinGeckoGlobalURL, timeout=10)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"[warn] CoinGecko 조회 실패: {e}")
        return None
    data = resp.json()["data"]
    total_mcap = data["total_market_cap"]["usd"]
    btc_pct = data["market_cap_percentage"].get("btc", 0)
    eth_pct = data["market_cap_percentage"].get("eth", 0)
    usdt_pct = data["market_cap_percentage"].get("usdt", 0)
    return {
        "total_mcap": total_mcap, "btc_d": btc_pct, "usdt_d": usdt_pct, "eth_d": eth_pct,
        "total2": total_mcap * (1 - btc_pct / 100),
        "total3": total_mcap * (1 - btc_pct / 100 - eth_pct / 100),
    }


def _fetch_snapshot_coinpaprika() -> Optional[Dict]:
    """2차 공급처(CoinGecko 실패 시). CoinGecko와 완전히 다른 회사·서버라 같은 이유로
    동시에 막힐 가능성이 낮습니다. 월 2만 회 무료, API 키 불필요.
    ⚠️ ETH/USDT 개별 시가총액을 구하려 티커를 2번 더 호출합니다(그래도 무료 한도에 넉넉히 여유)."""
    try:
        g = requests.get(CoinPaprikaGlobalURL, timeout=10)
        g.raise_for_status()
        gd = g.json()
        total_mcap = gd["market_cap_usd"]
        btc_pct = gd["bitcoin_dominance_percentage"]

        eth = requests.get(CoinPaprikaTickerURL.format(coin_id="eth-ethereum"), timeout=10)
        eth.raise_for_status()
        eth_mcap = eth.json()["quotes"]["USD"]["market_cap"]

        usdt = requests.get(CoinPaprikaTickerURL.format(coin_id="usdt-tether"), timeout=10)
        usdt.raise_for_status()
        usdt_mcap = usdt.json()["quotes"]["USD"]["market_cap"]
    except (requests.exceptions.RequestException, KeyError) as e:
        print(f"[warn] CoinPaprika 조회 실패: {e}")
        return None

    eth_pct = eth_mcap / total_mcap * 100
    usdt_pct = usdt_mcap / total_mcap * 100
    return {
        "total_mcap": total_mcap, "btc_d": btc_pct, "usdt_d": usdt_pct, "eth_d": eth_pct,
        "total2": total_mcap * (1 - btc_pct / 100),
        "total3": total_mcap * (1 - btc_pct / 100 - eth_pct / 100),
    }


def fetch_global_snapshot() -> Optional[Dict]:
    """BTC.D, USDT.D, TOTAL2, TOTAL3 스냅샷. CoinGecko를 1차로 시도하고, 실패하면(429 등)
    완전히 별도 회사·서버인 CoinPaprika로 넘어갑니다 — 같은 원인으로 둘 다 막힐 가능성은 낮습니다.
    둘 다 실패하면 None을 반환해서, 호출부(determine_overall_regime)가 이번 회차는
    새 스냅샷 없이 기존에 쌓인 기록으로만 판단하도록 합니다.
    (CoinGecko/CoinPaprika 무료 API는 '현재 스냅샷'만 주고 과거 시계열은 안 줘서, 추세는
    HISTORY_FILE에 스냅샷을 직접 누적해서 계산합니다 — 이 스크립트를 자주 돌릴수록 정확해집니다.)
    """
    fields = _fetch_snapshot_coingecko()
    source = "coingecko"
    if fields is None:
        fields = _fetch_snapshot_coinpaprika()
        source = "coinpaprika"
    if fields is None:
        return None

    snapshot = {"timestamp": int(time.time()), "source": source, **fields}
    _append_history(snapshot)
    return snapshot


def _append_history(snapshot: Dict) -> None:
    df_new = pd.DataFrame([snapshot])
    if os.path.exists(HISTORY_FILE):
        df_old = pd.read_csv(HISTORY_FILE)
        df = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df = df_new
    df.to_csv(HISTORY_FILE, index=False)


def macro_history_span_hours(window_days: float = 7.0) -> float:
    """BTC.D/USDT.D/TOTAL2·3 스냅샷이 몇 시간 분량 쌓였는지 (추세 판단 가능 여부 확인용)."""
    if not os.path.exists(HISTORY_FILE):
        return 0.0
    df = pd.read_csv(HISTORY_FILE)
    if df.empty or "timestamp" not in df.columns:
        return 0.0
    df = df[df["timestamp"] >= df["timestamp"].max() - window_days * 86400]
    if len(df) < 2:
        return 0.0
    return float((df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]) / 3600)


_MACRO_THRESHOLD_PCT = {"btc_d": 1.0, "usdt_d": 1.0, "total2": 5.0, "total3": 5.0}


def macro_trend_from_history(column: str, window_days: float = 7.0,
                              min_span_hours: float = 24.0) -> RegimeType:
    """누적 스냅샷으로 지표(btc_d, usdt_d, total2, total3)의 추세를 '시간' 기준으로 판단.
    (실행 빈도와 무관하게 일관되도록 '몇 개 쌓였나'가 아니라 '최근 window_days일 변화율'을 봅니다.)
    - 기록 기간이 min_span_hours 미만이면 판단 근거 부족 → 'sideways'
    - 변화율 기준: 도미넌스는 ±1%, TOTAL2/3는 ±5% (상대 변화율)"""
    if not os.path.exists(HISTORY_FILE):
        return "sideways"
    df = pd.read_csv(HISTORY_FILE)
    if df.empty or column not in df.columns or "timestamp" not in df.columns:
        return "sideways"
    df = df[df["timestamp"] >= df["timestamp"].max() - window_days * 86400]
    if len(df) < 2:
        return "sideways"
    span_hours = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]) / 3600
    if span_hours < min_span_hours:
        return "sideways"
    first, last = float(df[column].iloc[0]), float(df[column].iloc[-1])
    if first == 0:
        return "sideways"
    pct_change = (last - first) / first * 100
    th = _MACRO_THRESHOLD_PCT.get(column, 2.0)
    if pct_change > th:
        return "uptrend"
    if pct_change < -th:
        return "downtrend"
    return "sideways"


# --------------------------------------------------------------------------
# 2. BTC 가격 추세 (거시 국면의 핵심 축)
# --------------------------------------------------------------------------

_EX_CACHE: Dict = {}


def _get_ex(exchange_id: str):
    """거래소 연결을 한 번만 만들어 재사용. (호출마다 새로 만들면 시장 목록을 매번 다시 받아
    매우 느려지고 API 차단 위험이 커집니다.)"""
    if ccxt is None:
        raise RuntimeError("ccxt가 설치되어 있지 않습니다. `pip install ccxt` 후 재시도하세요.")
    if exchange_id not in _EX_CACHE:
        _EX_CACHE[exchange_id] = getattr(ccxt, exchange_id)({"enableRateLimit": True, "timeout": 15000})
    return _EX_CACHE[exchange_id]


def fetch_btc_df() -> pd.DataFrame:
    """BTC 4h 캔들. 한 거래소가 지역 차단/장애여도 다음 거래소로 넘어가도록 순차 시도."""
    for ex_id in EXCHANGES:
        df = fetch_ohlcv(ex_id, f"BTC/{QUOTE}")
        if df is not None and len(df) > 0:
            return df
    raise RuntimeError("모든 거래소에서 BTC 데이터를 가져오지 못했습니다 (네트워크/지역 차단/거래소 장애 확인)")


def _fetch_ohlcv_paged(ex, symbol: str, timeframe: str, total: int, max_calls: int = 8) -> List:
    """거래소마다 한 번에 주는 캔들 수가 달라서(예: OKX 300개, Bitget·Binance 1000개),
    필요한 개수(total)를 채울 때까지 과거→현재 방향으로 나눠 받습니다."""
    tf_ms = ex.parse_timeframe(timeframe) * 1000
    now = ex.milliseconds()
    since = now - total * tf_ms
    rows: List = []
    for _ in range(max_calls):
        batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=min(total, 1000))
        if not batch:
            break
        rows += batch
        next_since = batch[-1][0] + tf_ms
        if len(rows) >= total or next_since <= since or next_since > now:
            break
        since = next_since
    return rows


def fetch_ohlcv(exchange_id: str, symbol: str, timeframe: Optional[str] = None,
                 limit: Optional[int] = None) -> Optional[pd.DataFrame]:
    """캔들 조회. 마지막 봉은 아직 진행 중일 수 있으니, 신호 계산 전에 split_live()로 분리하세요."""
    timeframe = timeframe or TIMEFRAME
    limit = limit or OHLCV_LIMIT
    if ccxt is None:
        raise RuntimeError("ccxt가 설치되어 있지 않습니다. `pip install ccxt` 실행 후 재시도하세요.")
    try:
        ex = _get_ex(exchange_id)
        try:
            raw = _fetch_ohlcv_paged(ex, symbol, timeframe, limit)
        except Exception:
            raw = []
        if len(raw) < 60:  # 나눠 받기가 안 되는 거래소면 한 번에 받을 수 있는 만큼이라도
            raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=min(limit, 1000))
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df = df.drop_duplicates(subset="ts").sort_values("ts").tail(limit).reset_index(drop=True)
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        return df
    except Exception as e:
        print(f"[warn] {exchange_id} {symbol} OHLCV 조회 실패: {e}")
        return None


def drop_unclosed(df: Optional[pd.DataFrame], timeframe: Optional[str] = None) -> Optional[pd.DataFrame]:
    """아직 마감되지 않은 마지막 봉을 제거. 신호는 완성된 봉으로만 계산해야
    몇 분 사이에 신호가 생겼다 사라지는 일이 없고, 백테스트와도 같은 조건이 됩니다."""
    if df is None or df.empty:
        return df
    timeframe = timeframe or TIMEFRAME
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    if df["ts"].iloc[-1] + pd.Timedelta(timeframe) > now:
        return df.iloc[:-1].reset_index(drop=True)
    return df


def split_live(df: pd.DataFrame, timeframe: Optional[str] = None):
    """(완성봉만 남긴 df, 실시간 현재가). 현재가는 진행 중 봉의 종가(=가장 최근 체결가)."""
    return drop_unclosed(df, timeframe), float(df["close"].iloc[-1])


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _adx_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    alpha = 1 / period
    atr_w = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_w
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_w
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    return dx.ewm(alpha=alpha, adjust=False).mean()


def trend_series(df: pd.DataFrame) -> np.ndarray:
    """백테스트 속도용: 전체 구간에 대해 봉별 국면(classify_price_trend와 같은 규칙)을 한 번에 계산.
    라이브는 최근 600봉 창으로 계산하는데, 600봉이면 EMA 초기값 영향이 0.3% 미만이라 결과가 사실상 같습니다."""
    c = df["close"]
    adx_s = _adx_series(df).to_numpy()
    er = ((c - c.shift(50)).abs() / c.diff().abs().rolling(50).sum()).to_numpy()
    e50, e200 = ema(c, 50).to_numpy(), ema(c, 200).to_numpy()
    net = (c - c.shift(50)).to_numpy()
    with np.errstate(invalid="ignore"):
        strong = (adx_s >= ADX_TREND_MIN) & (er >= ER_TREND_MIN)
        up = strong & (e50 > e200) & (net > 0)
        down = strong & (e50 < e200) & (net < 0)
    reg = np.full(len(df), "sideways", dtype=object)
    reg[up], reg[down] = "uptrend", "downtrend"
    reg[:60] = "sideways"
    return reg


def adx(df: pd.DataFrame, period: int = 14) -> float:
    """추세 강도(ADX), Wilder 표준 방식(지수 평활).
    - 한 봉에서 +DM/-DM은 상호 배타적(더 크게 움직인 쪽만 인정)
    - 예전 단순평균 방식은 박스권의 77%를 추세로 오판해서 교체했습니다."""
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    alpha = 1 / period
    atr_w = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_w
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_w
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    val = dx.ewm(alpha=alpha, adjust=False).mean().iloc[-1] if not dx.empty else 0.0
    return float(val) if pd.notna(val) else 0.0


def efficiency_ratio(df: pd.DataFrame, n: int = 50) -> float:
    """효율비율 = |n봉 동안 순이동| ÷ n봉 동안 움직인 총거리 (0~1).
    한 방향으로 곧게 가면 1에 가깝고, 위아래로 오가기만 하면 0에 가까움."""
    c = df["close"].to_numpy()
    n = min(n, len(c) - 1)
    if n < 5:
        return 0.0
    seg = c[-n - 1:]
    path = np.abs(np.diff(seg)).sum()
    return float(abs(seg[-1] - seg[0]) / path) if path > 0 else 0.0


def classify_price_trend(df: pd.DataFrame, adx_threshold: float = ADX_TREND_MIN,
                          er_threshold: float = ER_TREND_MIN) -> RegimeType:
    """상승/하락/횡보 분류.
    - 강도: Wilder ADX ≥ 25 그리고 효율비율(50봉) ≥ 0.20 — 둘 다 만족해야 '추세'
      (무작위 실험에서 박스권 오판율 77% → 5%, 뚜렷한 추세 인식률 약 80%)
    - 방향: EMA50 vs EMA200 배열과 최근 50봉 순이동 방향이 '일치'해야 함.
      장기 배열과 최근 움직임이 엇갈리면 전환 구간으로 보고 '횡보' 처리."""
    if df is None or len(df) < 60:
        return "sideways"
    if adx(df) < adx_threshold or efficiency_ratio(df) < er_threshold:
        return "sideways"
    ema_fast = ema(df["close"], 50).iloc[-1]
    ema_slow = ema(df["close"], min(200, len(df) - 1)).iloc[-1]
    n = min(50, len(df) - 1)
    net = df["close"].iloc[-1] - df["close"].iloc[-n - 1]
    if ema_fast > ema_slow and net > 0:
        return "uptrend"
    if ema_fast < ema_slow and net < 0:
        return "downtrend"
    return "sideways"


# --------------------------------------------------------------------------
# 3. 전체 국면 종합 판단
# --------------------------------------------------------------------------

@dataclass
class MarketRegime:
    btc_trend: RegimeType
    btc_d_trend: RegimeType
    usdt_d_trend: RegimeType
    total2_trend: RegimeType
    total3_trend: RegimeType
    overall: RegimeType
    snapshot: Dict = field(default_factory=dict)
    headline: str = ""            # 한 줄 결론 (예: "횡보 후 상승 우세")
    score: float = 0.0            # -1(강한 하락)~+1(강한 상승) 종합 방향 점수
    confidence_label: str = ""    # "높음"/"보통"/"낮음" — 근거들이 서로 얼마나 일치하는지
    explanation: str = ""         # 근거를 풀어 쓴 설명 문장
    breakout_up: Optional[float] = None    # 횡보일 때: 이 가격 위로 뚫으면 상승 전환으로 볼 기준선
    breakout_down: Optional[float] = None  # 횡보일 때: 이 가격 아래로 이탈하면 하락 전환으로 볼 기준선
    lean_verified: Optional[bool] = None   # 횡보 기울기 근거가 검증됐는지(=validate_lean_auto 결과 반영 여부)
    daily_trend: RegimeType = "sideways"   # 큰 흐름: BTC 일봉 추세
    action: str = ""                       # 행동 가이드 한 줄
    breadth_up_pct: Optional[float] = None    # 스캔 코인 중 상승 추세 비율(%)
    breadth_down_pct: Optional[float] = None  # 스캔 코인 중 하락 추세 비율(%)
    breadth_n: int = 0
    shock: bool = False                    # BTC 급변 감지(국면 전환 즉시 반영)
    transition_pending: bool = False       # 새 방향이 나왔지만 아직 확정 전(다음 봉 마감 때 확정)
    timeframe: str = "4h"                  # 신호 봉 모드


def fetch_btc_daily_trend() -> RegimeType:
    """BTC 일봉(HTF) 추세. 한 거래소가 막혀도 다음 거래소로 순차 시도."""
    for ex_id in EXCHANGES:
        try:
            return get_htf_trend(ex_id, f"BTC/{QUOTE}", "1d")
        except Exception:
            continue
    return "sideways"


def _confidence_label(agreement: float) -> str:
    if agreement >= 0.6:
        return "높음"
    if agreement >= 0.3:
        return "보통"
    return "낮음"


def fmt_range(x: float) -> str:
    if x >= 1000:
        return f"{x:,.0f}"
    if x >= 1:
        return f"{x:,.4g}"
    return f"{x:.6g}"


def _is_btc_shock(btc_df: pd.DataFrame, atr_mult: float = 2.5) -> bool:
    """최근 완성봉 1~2개 동안 BTC가 ATR의 2.5배 이상 움직였으면 '급변'으로 판단."""
    if btc_df is None or len(btc_df) < 20:
        return False
    a = atr(btc_df)
    c = btc_df["close"]
    move = max(abs(c.iloc[-1] - c.iloc[-2]), abs(c.iloc[-1] - c.iloc[-3]))
    return bool(a > 0 and move >= atr_mult * a)


def collect_market_inputs() -> Dict:
    """시장 전체 판단에 필요한 데이터를 한 번에 수집 (API 호출은 여기서만)."""
    snap = fetch_global_snapshot()
    if snap is None:
        print("[warn] 이번 회차는 새 거시 스냅샷 없이 기존 기록만으로 판단합니다 (BTC.D 등 값 갱신 안 됨)")
        snap = {}
    btc_closed, btc_live = split_live(fetch_btc_df())
    span_h = macro_history_span_hours()
    snap["macro_span_hours"] = span_h
    return {
        "snap": snap, "btc_df": btc_closed, "btc_live": btc_live,
        "btc_trend": classify_price_trend(btc_closed),
        "daily_trend": fetch_btc_daily_trend(),
        "macro": {k: macro_trend_from_history(k) for k in ("btc_d", "usdt_d", "total2", "total3")},
        "span_h": span_h,
        "candle_ts": str(btc_closed["ts"].iloc[-1]),
        "shock": _is_btc_shock(btc_closed),
    }


_TREND_NUM = {"uptrend": 1.0, "downtrend": -1.0, "sideways": 0.0}
_TREND_KR = {"uptrend": "상승", "downtrend": "하락", "sideways": "횡보"}


def compose_market_regime(inputs: Dict, breadth: Optional[Dict] = None, commit: bool = True) -> MarketRegime:
    """시장 전체 방향을 하나의 점수(-1~+1)와 한 줄 결론으로 종합.
    - 큰 흐름: BTC 일봉 추세 (가중치 0.35)
    - 중기 흐름: BTC 4시간봉 추세 (0.30)
    - 시장 참여: 스캔한 코인 중 상승 추세 비율 − 하락 추세 비율 (0.25) — 스캔 후에만 반영
    - 도미넌스·TOTAL: 기록이 24시간 이상 쌓였을 때만 (0.10)
    점수 ≥ +0.25 상승 / ≤ −0.25 하락 / 그 사이 횡보. 같은 4시간봉 안에서는 결과가 흔들리지 않고,
    BTC 급변 때만 즉시 전환합니다."""
    btc_df, btc_trend, daily = inputs["btc_df"], inputs["btc_trend"], inputs["daily_trend"]
    macro, span_h = inputs["macro"], inputs["span_h"]

    parts = {"daily": (0.35, _TREND_NUM[daily]), "h4": (0.30, _TREND_NUM[btc_trend])}
    up_pct = down_pct = None
    n = 0
    if breadth and breadth.get("n", 0) >= 10:
        n = breadth["n"]
        up_pct, down_pct = breadth["uptrend"] / n * 100, breadth["downtrend"] / n * 100
        parts["breadth"] = (0.25, (up_pct - down_pct) / 100)
    macro_ready = span_h >= 24
    if macro_ready:
        mv = [-_TREND_NUM[macro["btc_d"]], -_TREND_NUM[macro["usdt_d"]],
              _TREND_NUM[macro["total2"]], _TREND_NUM[macro["total3"]]]
        parts["macro"] = (0.10, float(np.mean(mv)))

    wsum = sum(w for w, _ in parts.values())
    score = float(np.clip(sum(w * v for w, v in parts.values()) / wsum, -1, 1))
    raw = "uptrend" if score >= 0.25 else ("downtrend" if score <= -0.25 else "sideways")
    overall = _apply_regime_hysteresis(raw, inputs["candle_ts"], inputs["shock"], commit=commit)

    # 신뢰도: 결론 방향과 같은 쪽을 가리키는 근거의 가중 비율
    breakout_up = breakout_down = None
    lean_verified = None
    if raw != "sideways":
        sign = 1 if raw == "uptrend" else -1
        agreement = sum(w for w, v in parts.values() if v * sign > 0) / wsum
    else:
        agreement = 0.0

    # 한 줄 결론: 큰 흐름(일봉)과 중기 흐름(4시간)의 조합으로 표현
    if btc_trend == "sideways":
        lean = compute_sideways_lean(btc_df, daily)
        agreement = min(abs(lean["score"]) / 0.6, 1.0)
        lean_word = "상승 우세" if lean["score"] > 0.3 else ("하락 우세" if lean["score"] < -0.3 else "방향 대기")
        prefix = {"uptrend": "상승 추세 속 횡보", "downtrend": "하락 추세 속 횡보", "sideways": "횡보 후"}[daily]
        headline = f"{prefix} {lean_word}" if daily == "sideways" and lean_word != "방향 대기" else \
                   ("횡보 · 방향 대기" if daily == "sideways" else f"{prefix} · {lean_word}")
        breakout_up = float(btc_df["high"].tail(20).max())
        breakout_down = float(btc_df["low"].tail(20).min())
        lean_verified = False
        action = (f"박스 경계에서만 진입하고 중간 구간은 관망. 위로 {fmt_range(breakout_up)} 돌파 시 롱 쪽, "
                  f"아래로 {fmt_range(breakout_down)} 이탈 시 숏 쪽으로 무게를 옮기세요")
    elif daily == btc_trend:
        strong = abs(score) >= 0.6
        if btc_trend == "uptrend":
            headline = "강한 상승장" if strong else "상승장"
            action = "롱 위주. 눌림목 지정가에서만 진입하고, 숏은 개별적으로 약한 코인만 짧게"
        else:
            headline = "강한 하락장" if strong else "하락장"
            action = "숏 위주. 반등 지정가에서만 진입하고, 롱은 개별적으로 강한 코인만 짧게"
    elif daily == "sideways":
        headline = f"단기 {_TREND_KR[btc_trend]} (큰 흐름은 중립)"
        action = "큰 흐름이 불분명해요. 목표는 짧게(목표1 위주), 비중은 평소보다 작게"
    elif btc_trend == "downtrend":
        headline = "상승 추세 속 조정"
        action = "큰 흐름은 상승. 조정이 끝나는 지지 자리에서 롱을 준비하고, 숏은 짧게만"
    else:
        headline = "하락 추세 속 반등"
        action = "큰 흐름은 하락. 반등이 끝나는 저항 자리에서 숏을 준비하고, 롱은 짧게만"

    # 설명 문장
    parts_txt = [f"큰 흐름(BTC 일봉)은 {_TREND_KR[daily]}, 중기 흐름(BTC {tf_label()})은 {_TREND_KR[btc_trend]}"]
    if up_pct is not None:
        parts_txt.append(f"스캔한 {n}개 코인 중 {up_pct:.0f}%가 상승 추세, {down_pct:.0f}%가 하락 추세예요")
        if btc_trend == "uptrend" and up_pct < 35:
            parts_txt.append("BTC만 강하고 알트코인 다수는 따라오지 못하고 있어 알트 롱은 선별이 필요해요")
        elif btc_trend == "downtrend" and up_pct >= 50:
            parts_txt.append("BTC는 약하지만 알트코인 다수가 버티는 중이에요")
    if macro_ready:
        if macro["usdt_d"] == "uptrend":
            parts_txt.append("현금성 자금(USDT) 비중이 늘고 있어 위험 회피 분위기예요")
        elif macro["usdt_d"] == "downtrend":
            parts_txt.append("현금성 자금(USDT) 비중이 줄고 있어 위험자산 선호 분위기예요")
    else:
        parts_txt.append(f"도미넌스·TOTAL 지표는 기록이 {span_h:.0f}시간 쌓여 아직 반영 전이에요")
    if inputs["shock"]:
        parts_txt.append("BTC 급변이 감지되어 국면을 즉시 반영했어요")
    explanation = ". ".join(parts_txt) + "."

    return MarketRegime(
        btc_trend, macro["btc_d"], macro["usdt_d"], macro["total2"], macro["total3"], overall,
        dict(inputs["snap"]), headline=headline, score=score,
        confidence_label=_confidence_label(agreement), explanation=explanation,
        breakout_up=breakout_up, breakout_down=breakout_down, lean_verified=lean_verified,
        daily_trend=daily, action=action, breadth_up_pct=up_pct, breadth_down_pct=down_pct,
        breadth_n=n, shock=inputs["shock"], transition_pending=(raw != overall), timeframe=TIMEFRAME,
    )


def determine_overall_regime() -> MarketRegime:
    """시장 전체 국면(스캔 없이 BTC·거시 지표만). 콘솔 실행(main) 등에서 사용."""
    return compose_market_regime(collect_market_inputs(), None, commit=True)


def _apply_regime_hysteresis(raw_regime: RegimeType, candle_ts: str, shock: bool = False,
                             commit: bool = True, confirm_count: int = 2) -> RegimeType:
    """국면 전환 확정 규칙 (시간 기준).
    - 같은 4시간봉 안에서는 몇 번을 새로 분석해도 한 번으로 셈 (버튼 연타로 국면이 바뀌지 않음)
    - 완성된 4시간봉 기준 confirm_count번 연속 같은 결과가 나와야 전환 인정
    - BTC 급변(shock)이면 즉시 인정
    commit=False면 기록을 남기지 않고 결과만 계산 (스캔 전 임시 판단용)."""
    cols = ["candle_ts", "raw_regime", "confirmed_regime"]
    path = REGIME_LOG_FILE if TIMEFRAME == "4h" else REGIME_LOG_FILE.replace(".csv", f"_{TIMEFRAME}.csv")
    log = pd.read_csv(path) if os.path.exists(path) else pd.DataFrame(columns=cols)
    if "candle_ts" not in log.columns:  # 예전 형식(호출 횟수 기준) 기록은 버리고 새로 시작
        log = pd.DataFrame(columns=cols)
    log = log.astype(object)
    if len(log) and str(log["candle_ts"].iloc[-1]) == str(candle_ts):
        log.loc[log.index[-1], "raw_regime"] = raw_regime
    else:
        log = pd.concat([log, pd.DataFrame([{"candle_ts": str(candle_ts), "raw_regime": raw_regime,
                                             "confirmed_regime": None}])], ignore_index=True)
    prior = log.iloc[:-1]
    prev = prior["confirmed_regime"].dropna() if len(prior) else pd.Series(dtype=object)
    prev_conf = prev.iloc[-1] if len(prev) else None
    recent = log["raw_regime"].tail(confirm_count).tolist()
    if shock or prev_conf is None or (len(recent) >= confirm_count and len(set(recent)) == 1):
        confirmed = raw_regime
    else:
        confirmed = prev_conf
    log.loc[log.index[-1], "confirmed_regime"] = confirmed
    if commit:
        log.tail(500).to_csv(path, index=False)
    return confirmed


# --------------------------------------------------------------------------
# 4. 코인 스크리너 (거래량 상위 + 상대강도 + 오더블록 + 변동성)
# --------------------------------------------------------------------------

@dataclass
class CoinSetup:
    symbol: str
    exchange: str
    bias: Literal["long", "short", "wait_breakout_long", "wait_breakout_short",
                  "range_fade_long", "range_fade_short"]
    entry_note: str
    entry_price: float   # 추격이 아닌, 되돌림 지정가(limit) 진입가
    current_price: float # 참고용 현재가 (추격 여부 비교용)
    tp1: float
    tp2: float
    sl: float
    rr_ratio: float
    is_chase: bool = False  # True면 아직 되돌림 전(=추격 구간)이라는 경고 플래그
    poc_confluence: bool = False  # True면 진입가가 POC/Value Area와도 겹치는 고신뢰 구간
    rs: float = 0.0  # 지수(BTC) 대비 상대강도(%) — 클수록 시장 대비 강함
    asymmetry: Optional[float] = None  # 상승포착률-하락포착률 — 클수록 '오를 때 크게 빠질 때 작게'
    bitget_perp: Optional[bool] = None  # Bitget USDT 무기한 선물 거래 가능 여부(None=확인 불가)
    counter_trend: bool = False  # True면 이 코인의 개별 국면이 시장 전체 국면과 반대 방향
    sweep_confluence: bool = False  # True면 진입 자리에서 유동성 스윕(손절 사냥 후 반전)이 확인됨
    coin_regime: str = ""  # 이 코인 자체의 국면 (시장 전체와 비교해 역행 여부 판단)
    atr: float = 0.0       # 신호 계산 시점의 ATR (추격 판단·추적 손절 폭 계산용)
    live_status: str = ""  # ready(진입가 근처) / chase(추격 구간) / invalid(손절선 먼저 이탈) / missed(목표 먼저 도달)


def get_top_volume_symbols(exchange_id: str, top_n: int = TOP_N_BY_VOLUME) -> List[str]:
    ex = _get_ex(exchange_id)
    markets = ex.load_markets()
    tickers = ex.fetch_tickers()
    usdt_pairs = [
        s for s in markets
        if s.endswith(f"/{QUOTE}") and markets[s].get("active", True) and markets[s].get("spot", True)
    ]
    def qv(sym: str) -> float:
        return tickers.get(sym, {}).get("quoteVolume") or 0

    ranked = sorted(usdt_pairs, key=qv, reverse=True)
    # 거래대금 정보를 주지 않는 거래소면 필터를 건너뜀 (전부 0으로 보여 통째로 빠지는 것 방지)
    if sum(1 for sym in ranked if qv(sym) > 0) >= 10:
        ranked = [sym for sym in ranked if qv(sym) >= MIN_QUOTE_VOLUME_USDT]
    return ranked[:top_n]


def relative_strength_vs_btc(coin_df: pd.DataFrame, btc_df: pd.DataFrame,
                              windows: Optional[List[int]] = None) -> float:
    """지수(BTC) 대비 상대강도를 여러 구간(4시간봉 42/84/168개 ≈ 1주·2주·4주)의 평균으로 계산.
    - 예전(약 2·3·8일)처럼 너무 짧으면 '추세가 강한 코인'이 아니라 '단기 과열 코인'을 고르게 됨
      (크립토는 1주 이내 짧은 기간에 오히려 되돌림이 나타난다는 연구들이 있음)
    - 한 구간만 보면 우연한 단기 스파이크에 흔들리기 쉬워서, 여러 구간을 평균내
      '꾸준히 지수보다 강한' 코인을 더 정확히 골라내도록 함
    - 값이 클수록 같은 기간 동안 BTC보다 더 많이 오르고(하락장이면 덜 빠지고) 있다는 뜻
    - 반환값(%): 각 구간별 (코인 수익률 - BTC 수익률)의 단순 평균"""
    windows = windows or RS_WINDOWS
    scores = []
    for w in windows:
        n = min(len(coin_df), len(btc_df), w)
        if n < 2:
            continue
        coin_ret = coin_df["close"].iloc[-1] / coin_df["close"].iloc[-n] - 1
        btc_ret = btc_df["close"].iloc[-1] / btc_df["close"].iloc[-n] - 1
        scores.append((coin_ret - btc_ret) * 100)
    return float(np.mean(scores)) if scores else 0.0


def detect_order_block(df: pd.DataFrame, lookback: int = 30) -> Dict:
    """단순화된 오더블록 탐지:
    - 강한 상승 임펄스 직전의 마지막 음봉 = 불리시 오더블록
    - 강한 하락 임펄스 직전의 마지막 양봉 = 베어리시 오더블록
    엄밀한 스마트머니 컨셉 정의와는 차이가 있는 '실전 근사치'입니다."""
    recent = df.tail(lookback)
    o, c = recent["open"].to_numpy(), recent["close"].to_numpy()
    h, l = recent["high"].to_numpy(), recent["low"].to_numpy()
    avg_body = np.abs(c - o).mean()
    bullish_ob, bearish_ob = None, None
    for i in range(1, len(recent) - 1):
        if (c[i] - o[i]) > 2 * avg_body and c[i - 1] < o[i - 1]:
            bullish_ob = {"low": float(l[i - 1]), "high": float(h[i - 1])}
        if (o[i] - c[i]) > 2 * avg_body and c[i - 1] > o[i - 1]:
            bearish_ob = {"low": float(l[i - 1]), "high": float(h[i - 1])}
    return {"bullish_ob": bullish_ob, "bearish_ob": bearish_ob}


def get_htf_trend(exchange_id: str, symbol: str, htf_timeframe: Optional[str] = None) -> RegimeType:
    """상위 타임프레임(HTF) 추세 필터. 진입 타임프레임(4h) 대비 6배 비율(1d)을 사용.
    ⚠️ 반드시 '마감된 봉'만 사용 — 마지막 봉은 아직 진행 중일 수 있으므로 제외합니다
    (미래참조/look-ahead 오류 방지)."""
    htf_timeframe = htf_timeframe or HTF_TIMEFRAME
    df = fetch_ohlcv(exchange_id, symbol, timeframe=htf_timeframe, limit=HTF_LIMIT)
    closed_df = drop_unclosed(df, htf_timeframe)  # 진행 중인 오늘 일봉 제외
    if closed_df is None or len(closed_df) < 60:
        return "sideways"
    return classify_price_trend(closed_df)


def get_spread_pct(exchange_id: str, symbol: str) -> Optional[float]:
    """호가 스프레드(%) 조회 — 스프레드가 넓으면 그만큼 즉시 손실을 안고 시작하는 셈이라
    슬리피지 리스크가 큰 종목을 걸러내는 데 사용."""
    try:
        ex = _get_ex(exchange_id)
        ticker = ex.fetch_ticker(symbol)
        bid, ask = ticker.get("bid"), ticker.get("ask")
        if not bid or not ask:
            return None
        return (ask - bid) / bid * 100
    except Exception:
        return None


# --------------------------------------------------------------------------
# 4.1 서킷브레이커 (일일/주간 손실 한도) — 실제 체결 결과를 기록해두면
#     이 한도를 넘었을 때 스크립트가 신규 신호를 아예 막아버립니다.
# --------------------------------------------------------------------------

TRADE_LOG_FILE = "trade_results_log.csv"


def log_trade_result(pnl_usdt: float, symbol: str = "", note: str = "") -> None:
    """실제 체결 후 손익을 여기에 직접 기록하세요 (수동). 이 로그가 쌓여야
    서킷브레이커와, 나중에 Kelly 기반 사이징으로 넘어갈 때의 승률/손익비 계산이 가능합니다."""
    entry = pd.DataFrame([{"ts": int(time.time()), "pnl_usdt": pnl_usdt, "symbol": symbol, "note": note}])
    if os.path.exists(TRADE_LOG_FILE):
        log = pd.concat([pd.read_csv(TRADE_LOG_FILE), entry], ignore_index=True)
    else:
        log = entry
    log.to_csv(TRADE_LOG_FILE, index=False)


def circuit_breaker_triggered(risk_cfg: RiskConfig, max_daily_loss_pct: float = 5.0,
                               max_weekly_loss_pct: float = 10.0) -> Optional[str]:
    """오늘/이번 주 실현 손실이 한도를 넘었으면 신규 진입을 전면 차단.
    이유: 손실 중 감정적으로 만회하려는 시도(revenge trading)가 계좌를 가장 크게
    파괴하는 패턴이므로, 규칙 기반으로 강제 중단하는 것이 중요합니다."""
    if not os.path.exists(TRADE_LOG_FILE):
        return None
    log = pd.read_csv(TRADE_LOG_FILE)
    if log.empty:
        return None

    log["dt"] = pd.to_datetime(log["ts"], unit="s")
    now = pd.Timestamp.now()

    today_pnl = log[log["dt"].dt.date == now.date()]["pnl_usdt"].sum()
    week_pnl = log[log["dt"] >= now - pd.Timedelta(days=7)]["pnl_usdt"].sum()

    today_loss_pct = -today_pnl / risk_cfg.account_balance * 100
    week_loss_pct = -week_pnl / risk_cfg.account_balance * 100

    if today_loss_pct >= max_daily_loss_pct:
        return f"일일 손실 한도 초과 ({today_loss_pct:.1f}% ≥ {max_daily_loss_pct}%) — 오늘 신규 진입 중단"
    if week_loss_pct >= max_weekly_loss_pct:
        return f"주간 손실 한도 초과 ({week_loss_pct:.1f}% ≥ {max_weekly_loss_pct}%) — 이번 주 신규 진입 중단"
    return None


def get_funding_rate(exchange_id: str, symbol: str) -> Optional[float]:
    """USDT-M 무기한 선물(예: 'SOL/USDT:USDT')의 현재 펀딩비 조회.
    (현물 심볼로 조회하면 항상 실패해서 필터가 무력화되므로 반드시 선물 심볼을 사용)
    선물이 없거나 조회 실패 시 None → 이 경우 필터를 건너뜁니다.
    ⚠️ 연환산은 8시간 정산 기준 근사치입니다(코인/거래소에 따라 정산 주기가 다를 수 있음)."""
    try:
        ex = _get_ex(exchange_id)
        ex.load_markets()
        swap_symbol = f"{symbol}:{QUOTE}"
        if swap_symbol not in ex.markets:
            return None
        fr = ex.fetch_funding_rate(swap_symbol)
        return fr.get("fundingRate")
    except Exception:
        return None


def funding_rate_ok(exchange_id: str, symbol: str, bias: str,
                     max_annualized_pct: float = 20.0) -> bool:
    """펀딩비가 과열(쏠림)된 방향으로는 진입하지 않도록 걸러냄.
    - 8시간마다 정산 → 하루 3회 → 연 1095회
    - 롱인데 펀딩비가 크게 플러스(롱 과열, 내가 숏에게 계속 돈을 냄) → 제외
    - 숏인데 펀딩비가 크게 마이너스(숏 과열, 내가 롱에게 계속 돈을 냄) → 제외
    데이터가 없으면(현물 등) 통과시킴 — 무기한 선물이 아니면 해당 없음."""
    rate = get_funding_rate(exchange_id, symbol)
    if rate is None:
        return True
    annualized_pct = rate * 1095 * 100

    if "long" in bias and annualized_pct > max_annualized_pct:
        return False
    if "short" in bias and annualized_pct < -max_annualized_pct:
        return False
    return True


def atr(df: pd.DataFrame, period: int = 14) -> float:
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def calculate_capture_ratios(coin_df: pd.DataFrame, btc_df: pd.DataFrame, lookback: int = 180) -> Dict:
    """지수(BTC) 상승/하락 국면에서 코인이 얼마나 비대칭적으로 반응하는지 측정.
    - 상승 포착률(up_capture) > 1  : 지수 오를 때 그 이상으로 따라 오름 (베타가 큼)
    - 하락 포착률(down_capture) < 1 (0에 가깝거나 음수면 더 좋음) : 지수 빠질 때 덜 빠지거나 버팀
    - asymmetry = up_capture - down_capture : 클수록 '오를 땐 크게, 빠질 땐 작게'인 이상적 비대칭

    ⚠️ 표본이 적으면(횡보장이 길게 이어져 상승/하락 구간 수가 적으면) 신뢰도가 떨어집니다.
    최소 각 구간 10봉 이상 확보되지 않으면 None으로 처리해서 오판을 막습니다."""
    n = min(len(coin_df), len(btc_df), lookback)
    if n < 30:
        return {"up_capture": None, "down_capture": None, "asymmetry": None}

    coin_ret = coin_df["close"].tail(n).pct_change().dropna()
    btc_ret = btc_df["close"].tail(n).pct_change().dropna()
    m = min(len(coin_ret), len(btc_ret))
    coin_ret, btc_ret = coin_ret.iloc[-m:].reset_index(drop=True), btc_ret.iloc[-m:].reset_index(drop=True)

    up_mask = btc_ret > 0
    down_mask = btc_ret < 0

    if up_mask.sum() < 10 or down_mask.sum() < 10:
        return {"up_capture": None, "down_capture": None, "asymmetry": None}

    btc_up_avg = btc_ret[up_mask].mean()
    btc_down_avg = btc_ret[down_mask].mean()
    coin_up_avg = coin_ret[up_mask].mean()
    coin_down_avg = coin_ret[down_mask].mean()

    up_capture = coin_up_avg / btc_up_avg if btc_up_avg != 0 else None
    down_capture = coin_down_avg / btc_down_avg if btc_down_avg != 0 else None

    if up_capture is None or down_capture is None:
        return {"up_capture": up_capture, "down_capture": down_capture, "asymmetry": None}

    return {"up_capture": float(up_capture), "down_capture": float(down_capture),
             "asymmetry": float(up_capture - down_capture)}


def calculate_volume_profile(df: pd.DataFrame, num_bins: int = 50, lookback: int = 100) -> Dict:
    """POC(Point of Control)·Value Area 근사 계산.
    - 각 캔들의 거래량을 그 캔들의 고가~저가 구간에 균등 분산시켜 가격대별 거래량을 누적
    - 거래량이 가장 많이 쌓인 구간 = POC (매물대 핵심, '가격 자석' 역할)
    - POC를 중심으로 누적거래량 70%에 도달할 때까지 확장한 상/하단 = Value Area High/Low
      (그 안쪽은 '시장이 공정가로 받아들인 가격대', 바깥쪽은 '거부된 가격대'로 해석)
    ⚠️ 캔들(OHLCV) 데이터 기반 근사치입니다. 틱/오더북 데이터 기반 진짜 볼륨프로파일보다는
    정밀도가 떨어지지만, 실전에서 널리 쓰이는 근사 방식입니다."""
    recent = df.tail(lookback)
    price_min, price_max = recent["low"].min(), recent["high"].max()
    if price_max <= price_min:
        return {"poc": None, "vah": None, "val": None}

    bins = np.linspace(price_min, price_max, num_bins + 1)
    lo_idx = np.maximum(np.searchsorted(bins, recent["low"].to_numpy(), side="right") - 1, 0)
    hi_idx = np.minimum(np.searchsorted(bins, recent["high"].to_numpy(), side="right") - 1, num_bins - 1)
    span = np.maximum(hi_idx - lo_idx + 1, 1)
    per = recent["volume"].to_numpy() / span
    valid = lo_idx <= hi_idx
    diff = np.zeros(num_bins + 1)
    np.add.at(diff, lo_idx[valid], per[valid])
    np.add.at(diff, hi_idx[valid] + 1, -per[valid])
    vol_by_bin = np.cumsum(diff)[:num_bins]

    poc_idx = int(np.argmax(vol_by_bin))
    poc_price = (bins[poc_idx] + bins[poc_idx + 1]) / 2

    total_vol = vol_by_bin.sum()
    target = total_vol * 0.7
    lo, hi = poc_idx, poc_idx
    acc = vol_by_bin[poc_idx]
    while acc < target and (lo > 0 or hi < num_bins - 1):
        expand_lo = vol_by_bin[lo - 1] if lo > 0 else -1
        expand_hi = vol_by_bin[hi + 1] if hi < num_bins - 1 else -1
        if expand_hi >= expand_lo:
            hi = min(hi + 1, num_bins - 1)
            acc += vol_by_bin[hi]
        else:
            lo = max(lo - 1, 0)
            acc += vol_by_bin[lo]

    return {"poc": float(poc_price), "val": float(bins[lo]), "vah": float(bins[hi + 1])}


def detect_liquidity_sweep(df: pd.DataFrame, lookback: int = 20) -> Dict:
    """최근 완결봉이 직전 lookback봉의 스윙 고점/저점을 꼬리(wick)로 살짝 넘었다가
    종가는 다시 그 안으로 들어온 '유동성 스윕(손절 사냥 후 반전)' 패턴 탐지.
    - bullish_sweep: 직전 스윙 저점을 저가로 이탈했다가 종가는 그 위로 복귀 → 매수세 유입(반전 상승 신호)
    - bearish_sweep: 직전 스윙 고점을 고가로 이탈했다가 종가는 그 아래로 복귀 → 매도세 유입(반전 하락 신호)
    ⚠️ 4시간봉 기준입니다. 원래 이 컨셉은 1~15분봉처럼 훨씬 짧은 타임프레임에서 정밀 진입용으로
    쓰이는 경우가 많아, 4시간봉에서는 일반적인 변동성과 뚜렷이 구분되지 않을 수 있습니다.
    여기서는 '있으면 신뢰도를 살짝 높여주는 보조 컨플루언스'로만 쓰고, 진입 조건 자체를 바꾸지
    않습니다 — 이렇게 해야 이 신호가 실제로 도움이 되는지 나중에 백테스트로 따로 검증할 수 있습니다."""
    if len(df) < lookback + 2:
        return {"bullish_sweep": None, "bearish_sweep": None}
    recent = df.tail(lookback + 1)
    prior, last = recent.iloc[:-1], recent.iloc[-1]
    prior_low, prior_high = prior["low"].min(), prior["high"].max()

    bullish_sweep = None
    if last["low"] < prior_low and last["close"] > prior_low:
        bullish_sweep = {"swept_level": float(prior_low), "close": float(last["close"])}

    bearish_sweep = None
    if last["high"] > prior_high and last["close"] < prior_high:
        bearish_sweep = {"swept_level": float(prior_high), "close": float(last["close"])}

    return {"bullish_sweep": bullish_sweep, "bearish_sweep": bearish_sweep}


def near_level(price: float, level: Optional[float], tolerance_atr: float, a: float) -> bool:
    if level is None or a <= 0:
        return False
    return abs(price - level) <= tolerance_atr * a


def compute_sideways_lean(df: pd.DataFrame, htf_trend: RegimeType, lookback: int = 30,
                           weights: tuple = (0.5, 0.3, 0.2)) -> Dict:
    """횡보장에서 다음 방향에 대한 '확률적 기울기'를 계산.
    ⚠️ 이건 확정 예측이 아니라 여러 객관적 근거를 가중평균한 확률적 기울기입니다.
    score는 -1(하락 우세)~+1(상승 우세), |score| < 0.3이면 '중립'으로 판단해 방향을 강제하지 않습니다."""
    recent = df.tail(lookback)
    htf_component = {"uptrend": 1.0, "downtrend": -1.0, "sideways": 0.0}[htf_trend]

    half = len(recent) // 2
    first_half, second_half = recent.iloc[:half], recent.iloc[half:]
    if len(first_half) and len(second_half):
        low_diff = second_half["low"].min() - first_half["low"].min()    # 저점 상승폭
        high_diff = second_half["high"].max() - first_half["high"].max()  # 고점 상승폭
        rng = max(recent["high"].max() - recent["low"].min(), 1e-9)
        structure_component = float(np.clip(((low_diff - high_diff) / 2) / rng, -1, 1))
    else:
        structure_component = 0.0

    up_bars = recent[recent["close"] > recent["open"]]
    down_bars = recent[recent["close"] < recent["open"]]
    if len(up_bars) and len(down_bars):
        up_vol, down_vol = up_bars["volume"].mean(), down_bars["volume"].mean()
        accum_component = float(np.clip((up_vol - down_vol) / max(up_vol + down_vol, 1e-9), -1, 1))
    else:
        accum_component = 0.0

    w_htf, w_struct, w_accum = weights
    score = w_htf * htf_component + w_struct * structure_component + w_accum * accum_component
    if score > 0.3:
        label = "상승쪽 우세"
    elif score < -0.3:
        label = "하락쪽 우세"
    else:
        label = "중립(방향성 불명확)"

    return {"score": score, "label": label, "htf": htf_component,
            "structure": structure_component, "accumulation": accum_component}


def find_recent_impulse(df: pd.DataFrame, direction: str, lookback: int = 30,
                         vol_multiple: float = 2.0, body_atr: float = 1.0) -> Optional[float]:
    """최근 lookback봉 안에 '가격을 실제로 밀어낸' 임펄스 봉이 있었는지 확인.
    - 방향이 맞는 봉(상승이면 양봉, 하락이면 음봉)
    - 거래량이 그 봉 직전 20봉 평균의 vol_multiple배(기본 2배) 이상
    - 몸통이 그 시점 ATR의 body_atr배(기본 1배) 이상 — 거래량만 많고 가격은 안 움직인 봉 제외
    있으면 그중 가장 큰 거래량 배수를, 없으면 None을 반환.
    (예전 기준 '거래량 1.3배, 몸통 조건 없음'은 무작위 데이터에서도 98%가 통과해 필터 역할을 못 했음)"""
    if len(df) < 40:
        return None
    w = df.tail(lookback + 35)
    vol_avg = w["volume"].rolling(20).mean().shift(1)
    tr = pd.concat([w["high"] - w["low"], (w["high"] - w["close"].shift()).abs(),
                    (w["low"] - w["close"].shift()).abs()], axis=1).max(axis=1)
    atr_s = tr.rolling(14).mean().shift(1)
    body = w["close"] - w["open"]
    dir_ok = body > 0 if direction == "up" else body < 0
    rel = w["volume"] / vol_avg
    ok = (dir_ok & (rel >= vol_multiple) & (body.abs() >= body_atr * atr_s)).tail(lookback)
    matched = rel.tail(lookback)[ok]
    return float(matched.max()) if len(matched) else None


def detect_box(df: pd.DataFrame, a: float, lookback: int = 60, min_height_atr: float = 2.0,
               max_height_atr: float = 10.0, touch_tol_atr: float = 0.5, min_touches: int = 2,
               min_gap: int = 3) -> Dict:
    """박스권 판정. 예전에는 '최근 20봉 고점·저점'을 그냥 박스로 봐서 어느 차트에나 박스가 있었음.
    이제는 마지막 완성봉을 뺀 최근 lookback봉(기본 60봉 ≈ 10일)에서
    - 박스 높이가 ATR의 2~10배 사이이고
    - 위·아래 경계를 각각 서로 떨어진 시점에 2번 이상 터치했을 때만 유효한 박스로 인정."""
    if len(df) < lookback + 2 or not a or a <= 0:
        return {"valid": False}
    prior = df.iloc[-lookback - 1:-1]
    hi, lo = float(prior["high"].max()), float(prior["low"].min())
    height = hi - lo

    def touches(mask) -> int:
        count, last = 0, -10 ** 6
        for i, m in enumerate(mask):
            if m:
                if i - last > min_gap:
                    count += 1
                last = i
        return count

    th = touches((prior["high"] >= hi - touch_tol_atr * a).tolist())
    tl = touches((prior["low"] <= lo + touch_tol_atr * a).tolist())
    valid = (min_height_atr * a <= height <= max_height_atr * a) and th >= min_touches and tl >= min_touches
    return {"valid": bool(valid), "high": hi, "low": lo, "height": height, "touch_high": th, "touch_low": tl}


_REJECTS: Dict[str, int] = {}
REJECT_LABELS = {
    "not_perp": "Bitget 선물 미지원", "data_short": "데이터 부족", "error": "조회/분석 오류",
    "no_impulse": "최근 임펄스 없음", "weak_rs": "상대강도 방향 불일치", "no_room": "목표까지 여유 없음",
    "invalid_price": "이미 손절선을 넘음(무효)", "target_reached": "이미 목표가 도달(놓침)",
    "no_box": "유효한 박스 아님", "lean_against": "횡보 기울기와 반대", "mid_box": "박스 중간(관망)",
    "htf_against": "일봉 추세와 반대", "wide_spread": "스프레드 넓음", "funding_hot": "펀딩비 과열",
    "low_rr": "손익비 부족",
}


def _rej(reason: str) -> None:
    _REJECTS[reason] = _REJECTS.get(reason, 0) + 1


def _order_targets(direction: str, tp1: float, tp2: float, a: float):
    """목표2가 목표1보다 불리해지지 않도록 순서 보장."""
    if direction == "long":
        return tp1, max(tp2, tp1 + 0.5 * a)
    return tp1, min(tp2, tp1 - 0.5 * a)


def _signal_time_problem(direction: str, price: float, sl: float, tp1: float) -> Optional[str]:
    """신호가 뜨는 시점에 이미 무효(손절선 통과)이거나 이미 목표에 도달한 셋업은 추천하지 않음."""
    if direction == "long":
        if price <= sl:
            return "invalid_price"
        if price >= tp1:
            return "target_reached"
    else:
        if price >= sl:
            return "invalid_price"
        if price <= tp1:
            return "target_reached"
    return None


def build_setup(symbol: str, exchange_id: str, df: pd.DataFrame, btc_df: pd.DataFrame,
                regime: RegimeType, htf_trend: RegimeType = "sideways",
                current_price: Optional[float] = None) -> Optional[CoinSetup]:
    """완성된 봉(df)으로 신호를 계산하고, 현재가(current_price)로 추격·무효 여부를 판단.
    - 상승/하락: 최근 임펄스 → 눌림목(오더블록 또는 EMA20)에 지정가, 목표는 직전 고점/저점(구조적 목표)
    - 횡보: 유효한 박스에서만 (A) 돌파 후 리테스트 또는 (B) 조용한 경계 역매매
    - 손절: 구조 레벨보다 0.3 ATR 바깥, 그리고 최소 1 ATR (좁은 손절은 사냥당하기 쉬움)
    - 추격: 현재가가 진입가에서 0.5 ATR 넘게 벗어나면 '대기'"""
    if df is None or len(df) < 60:
        _rej("data_short")
        return None
    a = atr(df)
    if not a or np.isnan(a) or a <= 0:
        _rej("data_short")
        return None
    price = float(current_price) if current_price is not None else float(df["close"].iloc[-1])
    vol_avg20 = df["volume"].iloc[-21:-1].mean()
    rel_vol = float(df["volume"].iloc[-1] / vol_avg20) if vol_avg20 else 1.0  # 마지막 완성봉 거래량 배수
    rs = relative_strength_vs_btc(df, btc_df)

    def _extras():
        """조건을 통과한 경우에만 계산하는 무거운 지표들."""
        cap_ = calculate_capture_ratios(df, btc_df)
        asym_ = cap_["asymmetry"]
        tag_ = (f", 비대칭 {asym_:+.2f}(상승↑{cap_['up_capture']:.2f}/하락↓{cap_['down_capture']:.2f})"
                if asym_ is not None else "")
        return calculate_volume_profile(df), detect_liquidity_sweep(df), asym_, tag_

    hi20, lo20 = float(df["high"].tail(20).max()), float(df["low"].tail(20).min())
    hi50, lo50 = float(df["high"].tail(50).max()), float(df["low"].tail(50).min())

    if regime in ("uptrend", "downtrend"):
        long_side = regime == "uptrend"
        impulse = find_recent_impulse(df, "up" if long_side else "down")
        if impulse is None:
            _rej("no_impulse")
            return None
        if (long_side and rs <= 0) or (not long_side and rs >= 0):
            _rej("weak_rs")
            return None
        ob = detect_order_block(df)
        ema20 = float(ema(df["close"], 20).iloc[-1])
        vp, sweep, asym, asym_tag = _extras()
        if long_side:
            zone = ob["bullish_ob"]
            entry = zone["high"] if zone else ema20
            structural_sl = zone["low"] - STOP_BUFFER_ATR * a if zone else entry - 1.5 * a
            sl = min(structural_sl, entry - MIN_STOP_ATR * a)
            tp1 = hi20
            if tp1 <= entry + 0.5 * a:
                _rej("no_room")
                return None
            tp1, tp2 = _order_targets("long", tp1, hi50, a)
            rr = (tp1 - entry) / (entry - sl)
            is_chase = price > entry + CHASE_ATR * a
            poc_conf = near_level(entry, vp["poc"], 0.5, a) or near_level(entry, vp["val"], 0.5, a)
            sweep_conf = sweep["bullish_sweep"] is not None
            direction, bias = "long", "long"
        else:
            zone = ob["bearish_ob"]
            entry = zone["low"] if zone else ema20
            structural_sl = zone["high"] + STOP_BUFFER_ATR * a if zone else entry + 1.5 * a
            sl = max(structural_sl, entry + MIN_STOP_ATR * a)
            tp1 = lo20
            if tp1 >= entry - 0.5 * a:
                _rej("no_room")
                return None
            tp1, tp2 = _order_targets("short", tp1, lo50, a)
            rr = (entry - tp1) / (sl - entry)
            is_chase = price < entry - CHASE_ATR * a
            poc_conf = near_level(entry, vp["poc"], 0.5, a) or near_level(entry, vp["vah"], 0.5, a)
            sweep_conf = sweep["bearish_sweep"] is not None
            direction, bias = "short", "short"
        problem = _signal_time_problem(direction, price, sl, tp1)
        if problem:
            _rej(problem)
            return None
        tags = (" + 매물대 겹침" if poc_conf else "") + (" + 유동성 스윕" if sweep_conf else "")
        where = "오더블록" if zone else "EMA20"
        note = (f"상대강도 {rs:+.1f}%, 임펄스(거래량 {impulse:.1f}배) 후 {where} "
                f"{'눌림목' if long_side else '반등'}{tags}{asym_tag} → "
                f"{'⏳ 지정가 대기(지금은 추격)' if is_chase else '✅ 진입가 근처'}")
        return CoinSetup(symbol, exchange_id, bias, note, entry, price, tp1, tp2, sl, rr, is_chase,
                         poc_conf, rs, asym, sweep_confluence=sweep_conf, atr=a,
                         live_status="chase" if is_chase else "ready")

    # ---------------- 횡보: 유효한 박스에서만
    box = detect_box(df, a)
    if not box["valid"]:
        _rej("no_box")
        return None
    vp, sweep, asym, _ = _extras()
    lean = compute_sideways_lean(df, htf_trend)
    lean_tag = f" [기울기: {lean['label']} ({lean['score']:+.2f})]"
    hi, lo, h = box["high"], box["low"], box["height"]
    last_close = float(df["close"].iloc[-1])
    box_tag = f"박스({fmt_range(lo)}~{fmt_range(hi)}, 위 {box['touch_high']}회·아래 {box['touch_low']}회 터치)"

    def _make(direction, bias, entry, sl, tp1, tp2, note_core, poc_conf=False, sweep_conf=False):
        tp1, tp2 = _order_targets(direction, tp1, tp2, a)
        problem = _signal_time_problem(direction, price, sl, tp1)
        if problem:
            _rej(problem)
            return None
        if direction == "long":
            rr = (tp1 - entry) / (entry - sl)
            is_chase = price > entry + CHASE_ATR * a
        else:
            rr = (entry - tp1) / (sl - entry)
            is_chase = price < entry - CHASE_ATR * a
        tags = (" + 매물대 겹침" if poc_conf else "") + (" + 유동성 스윕" if sweep_conf else "")
        note = (f"{note_core}{tags}{lean_tag} → "
                f"{'⏳ 지정가 대기(지금은 추격)' if is_chase else '✅ 진입가 근처'}")
        return CoinSetup(symbol, exchange_id, bias, note, entry, price, tp1, tp2, sl, rr, is_chase,
                         poc_conf, rs, asym, sweep_confluence=sweep_conf, atr=a,
                         live_status="chase" if is_chase else "ready")

    # (A) 돌파: 마지막 완성봉이 박스 밖에서 마감 + 거래량 1.5배 이상 → 돌파선 리테스트에 지정가
    #     손절은 박스 반대편 끝이 아니라 돌파선 너머 1 ATR (예전 방식은 손익비가 구조상 0.5~0.8이라 절대 추천 불가였음)
    if last_close > hi and rel_vol >= 1.5:
        if lean["score"] < -0.3:
            _rej("lean_against")
            return None
        return _make("long", "wait_breakout_long", hi, hi - MIN_STOP_ATR * a, hi + h, hi + 1.6 * h,
                     f"{box_tag} 상단 돌파(거래량 {rel_vol:.1f}배) 후 리테스트")
    if last_close < lo and rel_vol >= 1.5:
        if lean["score"] > 0.3:
            _rej("lean_against")
            return None
        return _make("short", "wait_breakout_short", lo, lo + MIN_STOP_ATR * a, lo - h, lo - 1.6 * h,
                     f"{box_tag} 하단 이탈(거래량 {rel_vol:.1f}배) 후 리테스트")

    # (B) 박스 안, 거래량이 잠잠한 상태에서 경계 근처 → 역매매(평균회귀)
    if lo <= last_close <= hi and rel_vol <= 1.1:
        if last_close <= lo + 1.0 * a:
            if lean["score"] < -0.3:
                _rej("lean_against")
                return None
            entry = lo + 0.25 * a
            sl = min(lo - STOP_BUFFER_ATR * a, entry - MIN_STOP_ATR * a)
            poc = vp["poc"]
            tp1 = poc if poc and poc > entry + 0.5 * a else lo + h / 2
            return _make("long", "range_fade_long", entry, sl, tp1, hi - 0.2 * a,
                         f"{box_tag} 하단 지지, 거래량 잠잠({rel_vol:.1f}배)",
                         near_level(entry, vp["val"], 0.5, a), sweep["bullish_sweep"] is not None)
        if last_close >= hi - 1.0 * a:
            if lean["score"] > 0.3:
                _rej("lean_against")
                return None
            entry = hi - 0.25 * a
            sl = max(hi + STOP_BUFFER_ATR * a, entry + MIN_STOP_ATR * a)
            poc = vp["poc"]
            tp1 = poc if poc and poc < entry - 0.5 * a else hi - h / 2
            return _make("short", "range_fade_short", entry, sl, tp1, lo + 0.2 * a,
                         f"{box_tag} 상단 저항, 거래량 잠잠({rel_vol:.1f}배)",
                         near_level(entry, vp["vah"], 0.5, a), sweep["bearish_sweep"] is not None)

    _rej("mid_box")
    return None


BITGET_ONLY = True  # True면 Bitget USDT 무기한 선물이 있는 코인만 추천 (Bitget에서 거래하므로)
_EXCLUDED_BASES = {"USDC", "FDUSD", "TUSD", "DAI", "USDE", "USDD", "PYUSD", "BUSD", "USD1", "USDP", "EUR"}


def _is_excluded_symbol(symbol: str) -> bool:
    """스테이블코인·레버리지 토큰은 추천 대상에서 제외."""
    base = symbol.split("/")[0]
    return base in _EXCLUDED_BASES or base.endswith(("3L", "3S", "5L", "5S"))


def bitget_perp_symbols() -> set:
    """Bitget USDT-M 무기한 선물 심볼 집합(예: 'SOL/USDT:USDT'). 조회 실패 시 빈 집합."""
    try:
        ex = _get_ex("bitget")
        ex.load_markets()
        return {m["symbol"] for m in ex.markets.values()
                if m.get("swap") and m.get("linear") and m.get("active", True) and m.get("quote") == QUOTE}
    except Exception as e:
        print(f"[warn] Bitget 선물 목록 조회 실패: {e}")
        return set()


def build_universe() -> Dict[str, str]:
    """거래소별 거래량 상위 코인을 합쳐 중복 제거. 같은 코인은 EXCHANGES 순서상 먼저 나온
    거래소(기본 Bitget)의 캔들을 사용 → 실제 거래하는 곳의 가격 기준으로 분석하고 API 호출도 절약."""
    universe: Dict[str, str] = {}
    for exchange_id in EXCHANGES:
        try:
            symbols = get_top_volume_symbols(exchange_id, TOP_N_BY_VOLUME)
        except Exception as e:
            print(f"[warn] {exchange_id} 심볼 조회 실패: {e}")
            continue
        for sym in symbols:
            if not _is_excluded_symbol(sym):
                universe.setdefault(sym, exchange_id)
    return universe


LAST_SCAN_STATS: Dict = {}


def live_status_of(setup: "CoinSetup", price: float) -> str:
    """현재가 기준 상태. 손절선을 먼저 넘으면 무효, 목표1에 먼저 닿으면 놓침."""
    tol = CHASE_ATR * setup.atr if setup.atr else setup.entry_price * 0.005
    if setup.bias in LONG_BIASES:
        if price <= setup.sl:
            return "invalid"
        if price >= setup.tp1:
            return "missed"
        return "chase" if price > setup.entry_price + tol else "ready"
    if price >= setup.sl:
        return "invalid"
    if price <= setup.tp1:
        return "missed"
    return "chase" if price < setup.entry_price - tol else "ready"


def refresh_live_status(setups: List["CoinSetup"]) -> int:
    """전체 스캔 없이 현재가만 받아 추천들의 상태를 갱신 (거래소당 한 번의 일괄 조회, 보통 몇 초).
    한 번 무효·놓침이 된 추천은 다음 전체 스캔까지 그 상태를 유지합니다.
    ⚠️ 갱신 사이(예: 30초)에 잠깐 손절선을 찍고 돌아온 경우는 잡지 못할 수 있습니다."""
    by_ex: Dict[str, List[str]] = {}
    for x in setups:
        by_ex.setdefault(x.exchange, []).append(x.symbol)
    prices: Dict = {}
    for ex_id, syms in by_ex.items():
        try:
            ex = _get_ex(ex_id)
            try:
                tickers = ex.fetch_tickers(syms)
            except Exception:
                tickers = ex.fetch_tickers()
            for sym in syms:
                t = tickers.get(sym) or {}
                last = t.get("last") or t.get("close")
                if last:
                    prices[(ex_id, sym)] = float(last)
        except Exception as e:
            print(f"[warn] {ex_id} 실시간 가격 조회 실패: {e}")
    updated = 0
    for x in setups:
        p = prices.get((x.exchange, x.symbol))
        if p is None:
            continue
        x.current_price = p
        updated += 1
        if x.live_status in ("invalid", "missed"):
            continue
        x.live_status = live_status_of(x, p)
        x.is_chase = x.live_status == "chase"
    return updated


def utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").tz_localize(None)


def current_candle_start(timeframe: Optional[str] = None) -> pd.Timestamp:
    """지금 진행 중인 봉의 시작 시각(UTC). 거래소 4시간봉은 UTC 00·04·08·12·16·20시에 시작."""
    return utc_now().floor(pd.Timedelta(timeframe or TIMEFRAME))


def next_candle_close(timeframe: Optional[str] = None) -> pd.Timestamp:
    return current_candle_start(timeframe) + pd.Timedelta(timeframe or TIMEFRAME)


def needs_full_rescan(last_scan_utc: Optional[pd.Timestamp], timeframe: Optional[str] = None,
                      grace_sec: int = 60) -> bool:
    """마지막 전체 스캔 뒤에 새 봉이 마감됐으면 True (마감 직후 거래소 반영을 위해 1분 여유)."""
    if last_scan_utc is None:
        return True
    start = current_candle_start(timeframe)
    return last_scan_utc < start and (utc_now() - start).total_seconds() >= grace_sec


def screen_market(market_regime: RegimeType, progress_cb=None,
                  btc_df: Optional[pd.DataFrame] = None) -> List[CoinSetup]:
    """코인마다 자기 차트의 '완성된 4시간봉'으로 개별 국면과 신호를 계산하고,
    현재가(진행 중 봉의 최근 체결가)로 추격·무효 여부를 판단합니다.
    스캔이 끝나면 LAST_SCAN_STATS에 필터별 제외 개수와 시장 참여도(상승/하락 추세 코인 수)를 남깁니다."""
    _REJECTS.clear()
    if btc_df is None:
        btc_df, _ = split_live(fetch_btc_df())
    universe = build_universe()
    perps = bitget_perp_symbols() if BITGET_ONLY else set()
    if BITGET_ONLY and not perps:
        print("[warn] Bitget 선물 목록을 못 가져와 '선물 거래 가능 여부' 필터를 건너뜁니다.")
    fund_ex = "bitget" if perps else None
    setups: List[CoinSetup] = []
    breadth = {"uptrend": 0, "downtrend": 0, "sideways": 0, "n": 0}
    items = list(universe.items())

    for idx, (symbol, exchange_id) in enumerate(items):
        if progress_cb:
            progress_cb(idx, len(items), symbol)
        swap_symbol = f"{symbol}:{QUOTE}"
        if BITGET_ONLY and perps and swap_symbol not in perps:
            _rej("not_perp")
            continue
        try:
            full = fetch_ohlcv(exchange_id, symbol)
            if full is None or len(full) < 61:
                _rej("data_short")
                continue
            df, live = split_live(full)
            coin_regime = classify_price_trend(df)
            breadth[coin_regime] += 1
            breadth["n"] += 1
            # 일봉은 횡보 기울기 계산에 먼저 필요, 추세 신호는 신호가 난 뒤에만 조회(API 절약)
            htf_trend = get_htf_trend(exchange_id, symbol) if coin_regime == "sideways" else None
            setup = build_setup(symbol, exchange_id, df, btc_df, coin_regime, htf_trend or "sideways",
                                current_price=live)
            if not setup:
                continue
            setup.coin_regime = coin_regime
            setup.counter_trend = coin_regime in ("uptrend", "downtrend") and coin_regime != market_regime

            if setup.bias in ("long", "short"):
                if htf_trend is None:
                    htf_trend = get_htf_trend(exchange_id, symbol)
                if (setup.bias == "long" and htf_trend == "downtrend") or \
                   (setup.bias == "short" and htf_trend == "uptrend"):
                    _rej("htf_against")
                    print(f"[skip] {symbol}: 일봉 추세와 반대 방향 신호라 제외")
                    continue

            spread = get_spread_pct(exchange_id, symbol)
            if spread is not None and spread > 0.3:
                _rej("wide_spread")
                print(f"[skip] {symbol}: 스프레드 {spread:.2f}% — 슬리피지 위험으로 제외")
                continue

            if not funding_rate_ok(fund_ex or exchange_id, symbol, setup.bias):
                _rej("funding_hot")
                print(f"[skip] {symbol}: 펀딩비 과열 방향이라 제외 ({setup.bias})")
                continue

            setup.bitget_perp = (swap_symbol in perps) if perps else None
            setups.append(setup)
        except Exception as e:  # 코인 하나의 실패가 전체 스캔을 멈추지 않도록
            _rej("error")
            print(f"[warn] {symbol} 분석 실패: {e}")

    if progress_cb:
        progress_cb(len(items), len(items), "")

    # 손익비 최소 기준 — 매물대 겹침 또는 유동성 스윕이 있으면 1.3, 없으면 1.5
    passed = []
    for x in setups:
        if x.rr_ratio >= (1.3 if (x.poc_confluence or x.sweep_confluence) else 1.5):
            passed.append(x)
        else:
            _rej("low_rr")
    passed.sort(key=lambda x: x.rr_ratio, reverse=True)

    LAST_SCAN_STATS.clear()
    LAST_SCAN_STATS.update({"rejects": dict(_REJECTS), "breadth": dict(breadth),
                            "universe": len(items), "passed": len(passed)})
    summary = ", ".join(f"{REJECT_LABELS.get(k, k)} {v}" for k, v in sorted(_REJECTS.items(), key=lambda kv: -kv[1]))
    print(f"[필터 통과율] 대상 {len(items)}개 → 최종 {len(passed)}개 | 제외: {summary or '없음'}")
    return passed


def run_analysis(risk_cfg: Optional[RiskConfig] = None, progress_cb=None) -> Dict:
    """전체 파이프라인: 시장 데이터 수집 → (임시 국면) → 코인 스캔 → 시장 참여도까지 넣어 국면 최종 확정.
    (웹 화면(app.py)이 사용. main()은 같은 내용을 콘솔에 출력하는 버전)"""
    if risk_cfg is None:
        risk_cfg = RiskConfig(account_balance=1000, risk_per_trade_pct=1.0, max_concurrent_setups=10)
    inputs = collect_market_inputs()
    prelim = compose_market_regime(inputs, None, commit=False)
    breaker = circuit_breaker_triggered(risk_cfg)
    all_setups: List[CoinSetup] = []
    stats: Dict = {}
    if not breaker:
        all_setups = screen_market(prelim.overall, progress_cb, btc_df=inputs["btc_df"])
        stats = dict(LAST_SCAN_STATS)
    regime = compose_market_regime(inputs, stats.get("breadth"), commit=True)
    for x in all_setups:  # 최종 국면 기준으로 역행 표시 다시 계산
        x.counter_trend = x.coin_regime in ("uptrend", "downtrend") and x.coin_regime != regime.overall
    setups = cap_correlated_exposure(all_setups, risk_cfg) if all_setups else []
    return {"regime": regime, "breaker": breaker, "setups": setups, "all_setups": all_setups,
            "risk_cfg": risk_cfg, "asof": pd.Timestamp.now(), "asof_utc": utc_now(),
            "timeframe": TIMEFRAME, "filter_stats": stats}


# --------------------------------------------------------------------------
# 5.1 실전 로직 기반 WFO — build_setup()을 과거 데이터에 그대로 재사용
# --------------------------------------------------------------------------

def fetch_extended_ohlcv(exchange_id: str, symbol: str, timeframe: Optional[str] = None,
                          total_bars: int = 3000) -> pd.DataFrame:
    """긴 과거 기간을 나눠 받아 수집 (완성봉만). WFO는 데이터가 많을수록 신뢰도가 올라갑니다."""
    if ccxt is None:
        raise RuntimeError("ccxt가 설치되어 있지 않습니다.")
    timeframe = timeframe or TIMEFRAME
    ex = _get_ex(exchange_id)
    rows = _fetch_ohlcv_paged(ex, symbol, timeframe, total_bars, max_calls=40)
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    df = drop_unclosed(df, timeframe)
    return df.tail(total_bars).reset_index(drop=True)


def htf_trend_at(htf_df: pd.DataFrame, current_ts, htf_timeframe: Optional[str] = None) -> RegimeType:
    """백테스트용: 현재 봉(current_ts에 시작해 한 봉 뒤 마감)이 끝난 시점까지 '이미 마감된'
    상위 봉들만으로 상위 추세 판단 (미래 참조 방지). 4시간봉 모드면 일봉, 1시간봉 모드면 4시간봉."""
    htf_tf = pd.Timedelta(htf_timeframe or HTF_TIMEFRAME)
    cutoff = pd.Timestamp(current_ts) + pd.Timedelta(TIMEFRAME)
    closed = htf_df[htf_df["ts"] + htf_tf <= cutoff]
    if len(closed) < 60:
        return "sideways"
    return classify_price_trend(closed)


def _htf_bars_needed(total_bars: int) -> int:
    ratio = pd.Timedelta(HTF_TIMEFRAME) / pd.Timedelta(TIMEFRAME)
    return int(total_bars / ratio) + 100


def compute_trade_R(direction: str, entry: float, sl: float, exit_price: float,
                     holding_bars: int, fee_pct: float = 0.05, slippage_pct: float = 0.03,
                     funding_pct_per_8h: float = 0.01, bars_per_8h: Optional[float] = None) -> float:
    """손익을 'R 배수'(최초 리스크 대비 몇 배)로 환산. 계좌 크기와 무관하게 전략 자체의
    품질을 비교할 수 있어서 WFO/기대값 계산에 표준적으로 쓰입니다. 수수료·슬리피지·펀딩비를
    전부 비용으로 차감한 '순(net) R'입니다."""
    bars_per_8h = bars_per_8h or BARS_PER_8H
    risk_per_unit = abs(entry - sl)
    if risk_per_unit <= 0:
        return 0.0
    raw = (exit_price - entry) if direction == "long" else (entry - exit_price)
    cost_price = entry * 2 * (fee_pct + slippage_pct) / 100          # 왕복 수수료+슬리피지
    funding_price = entry * (funding_pct_per_8h / 100) * (holding_bars / bars_per_8h)
    net = raw - cost_price - funding_price
    return net / risk_per_unit


SETUP_FAMILY = {"long": "추세", "short": "추세", "wait_breakout_long": "돌파", "wait_breakout_short": "돌파",
                "range_fade_long": "박스 역매매", "range_fade_short": "박스 역매매"}


def generate_signals(df: pd.DataFrame, btc_df: pd.DataFrame, htf_df: Optional[pd.DataFrame],
                     warmup: int = 250) -> Dict[int, Dict]:
    """과거 모든 봉에서 라이브와 같은 규칙으로 신호를 계산 (그 봉까지의 데이터만 사용).
    - 국면: 코인 자신의 추세 (전체 구간 한 번 계산, 라이브와 99.9% 일치)
    - 상위추세: 그 시점까지 '마감된' 상위 봉 기준
    - 라이브와 같은 필터: 상위추세 역행 제외, 손익비 1.5(보조 근거 있으면 1.3) 미만 제외
    - 과거 기록이 없는 필터(스프레드·펀딩비·거래대금)는 적용하지 못함"""
    df = df.reset_index(drop=True)
    btc_df = btc_df.reset_index(drop=True)
    reg = trend_series(df)
    htf_reg, htf_close = None, None
    if htf_df is not None and len(htf_df) >= 60:
        htf_df = htf_df.reset_index(drop=True)
        htf_reg = trend_series(htf_df)
        htf_close = (htf_df["ts"] + pd.Timedelta(HTF_TIMEFRAME)).to_numpy()
    tf = pd.Timedelta(TIMEFRAME)
    ts = df["ts"].to_numpy()
    window = max(260, max(RS_WINDOWS) + 20)
    signals: Dict[int, Dict] = {}
    for i in range(max(warmup, 60), len(df)):
        htf_i = "sideways"
        if htf_reg is not None:
            k = int(np.searchsorted(htf_close, ts[i] + tf, side="right")) - 1
            if k >= 59:
                htf_i = htf_reg[k]
        lo = max(0, i + 1 - window)
        x = build_setup("bt", "backtest", df.iloc[lo:i + 1], btc_df.iloc[lo:i + 1], reg[i], htf_i)
        if x is None:
            continue
        if (x.bias == "long" and htf_i == "downtrend") or (x.bias == "short" and htf_i == "uptrend"):
            continue
        if x.rr_ratio < (1.3 if (x.poc_confluence or x.sweep_confluence) else 1.5):
            continue
        signals[i] = {"direction": "long" if x.bias in LONG_BIASES else "short", "bias": x.bias,
                      "entry": x.entry_price, "sl": x.sl, "tp1": x.tp1, "atr": x.atr,
                      "is_chase": x.is_chase, "rr": x.rr_ratio}
    return signals


def simulate_exits(df: pd.DataFrame, signals: Dict[int, Dict], exit_mode: str = "partial_trail",
                   pending_expiry_bars: int = 15, max_hold_bars: int = 60) -> List[Dict]:
    """신호를 봉 단위로 재생하며 체결·청산을 시뮬레이션. 거래별 상세(순 R, 진입·청산 봉, 사유)를 반환.
    - 추격 신호는 지정가 대기 → 이후 봉에서 닿아야 체결, 체결 전 종가가 손절선을 넘으면 취소
    - exit_mode="partial_trail": 목표1 절반 익절 → 남은 절반 본전 손절 → 최고/최저가에서 TRAIL_ATR×ATR 되돌리면 정리
    - exit_mode="fixed": 목표1에서 전량 청산 (비교용)
    - 같은 봉에서 손절·목표 동시 도달 시 손절 처리(보수적), 추적 손절선은 직전 봉까지 기준(미래 참조 방지)
    - 한 번에 하나의 포지션만"""
    lows, highs, closes = df["low"].to_numpy(), df["high"].to_numpy(), df["close"].to_numpy()
    trades: List[Dict] = []
    pending: Optional[Dict] = None
    t: Optional[Dict] = None

    def _R(exit_price: float, hold: int, weight: float) -> float:
        return weight * compute_trade_R(t["direction"], t["entry"], t["sl"], exit_price, hold)

    def _done(total_r: float, i: int, reason: str) -> None:
        trades.append({"R": float(total_r), "entry_idx": t["entry_idx"], "exit_idx": i,
                       "direction": t["direction"], "bias": t["bias"], "reason": reason})

    for i in range(len(df)):
        if t:
            long_side = t["direction"] == "long"
            hold = i - t["entry_idx"]
            if t["stage"] == 0:
                hit_sl = lows[i] <= t["sl"] if long_side else highs[i] >= t["sl"]
                hit_tp = highs[i] >= t["tp1"] if long_side else lows[i] <= t["tp1"]
                if hit_sl:
                    _done(_R(t["sl"], hold, 1.0), i, "손절"); t = None
                elif hit_tp and exit_mode == "fixed":
                    _done(_R(t["tp1"], hold, 1.0), i, "목표1"); t = None
                elif hit_tp:
                    t["realized"] = _R(t["tp1"], hold, PARTIAL_TP_FRACTION)
                    t["stage"], t["stop"] = 1, t["entry"]
                    t["best"] = highs[i] if long_side else lows[i]
                elif hold >= max_hold_bars:
                    _done(_R(closes[i], hold, 1.0), i, "기간만료"); t = None
            else:
                rest = 1.0 - PARTIAL_TP_FRACTION
                if long_side:
                    t["stop"] = max(t["stop"], t["best"] - TRAIL_ATR * t["atr"])
                    if lows[i] <= t["stop"]:
                        _done(t["realized"] + _R(t["stop"], hold, rest), i, "추적손절"); t = None
                    else:
                        t["best"] = max(t["best"], highs[i])
                else:
                    t["stop"] = min(t["stop"], t["best"] + TRAIL_ATR * t["atr"])
                    if highs[i] >= t["stop"]:
                        _done(t["realized"] + _R(t["stop"], hold, rest), i, "추적손절"); t = None
                    else:
                        t["best"] = min(t["best"], lows[i])
                if t and hold >= max_hold_bars:
                    _done(t["realized"] + _R(closes[i], hold, rest), i, "기간만료"); t = None
            continue
        if pending:
            long_side = pending["direction"] == "long"
            filled = lows[i] <= pending["entry"] if long_side else highs[i] >= pending["entry"]
            invalid = closes[i] < pending["sl"] if long_side else closes[i] > pending["sl"]
            if filled:
                t = {**pending, "entry_idx": i, "stage": 0, "realized": 0.0}
                pending = None
            elif invalid or i >= pending["expiry_idx"]:
                pending = None
            continue
        sig = signals.get(i)
        if sig:
            if sig["is_chase"]:
                pending = {**sig, "expiry_idx": i + pending_expiry_bars}
            else:
                t = {**sig, "entry_idx": i, "stage": 0, "realized": 0.0}
    return trades


def simulate_strategy_history(df: pd.DataFrame, btc_df: pd.DataFrame, daily_df: pd.DataFrame,
                               pending_expiry_bars: int = 15, max_hold_bars: int = 60,
                               min_lookback: int = 80, exit_mode: str = "partial_trail") -> List[float]:
    """(기존 호출 방식 유지) 신호 계산 → 체결·청산 시뮬레이션 → 거래별 순 R 목록."""
    sig = generate_signals(df, btc_df, daily_df, warmup=max(min_lookback, 220))
    return [x["R"] for x in simulate_exits(df.reset_index(drop=True), sig, exit_mode,
                                           pending_expiry_bars, max_hold_bars)]


def summarize_trades(trades: List[Dict]) -> Dict:
    """거래 목록 요약: 승률, 평균 R, 손익비(PF), 최대 연속 손실, 최대 낙폭(R), 전반·후반 일관성."""
    if not trades:
        return {"n": 0}
    tr = sorted(trades, key=lambda x: x.get("exit_ts", x["exit_idx"]))
    R_ = np.array([x["R"] for x in tr])
    wins, losses = R_[R_ > 0], R_[R_ <= 0]
    streak = best_streak = 0
    for r in R_:
        streak = streak + 1 if r <= 0 else 0
        best_streak = max(best_streak, streak)
    cum = np.cumsum(R_)
    max_dd = float(np.max(np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:] - cum)) if len(cum) else 0.0
    half = len(R_) // 2
    return {"n": int(len(R_)), "win_rate": float(len(wins) / len(R_)), "avg_R": float(R_.mean()),
            "total_R": float(R_.sum()),
            "pf": float(wins.sum() / abs(losses.sum())) if losses.sum() != 0 else float("inf"),
            "avg_win": float(wins.mean()) if len(wins) else 0.0,
            "avg_loss": float(losses.mean()) if len(losses) else 0.0,
            "best": float(R_.max()), "max_consec_loss": int(best_streak), "max_dd_R": max_dd,
            "first_half_avg": float(R_[:half].mean()) if half else None,
            "second_half_avg": float(R_[half:].mean()) if len(R_) - half else None}


def interpret_backtest(summ: Dict, risk_pct: float = 1.0) -> List[str]:
    """요약 수치를 사람이 읽을 판정 문장으로."""
    out: List[str] = []
    n = summ.get("n", 0)
    if n == 0:
        return ["거래가 한 건도 없어요. 기간이나 코인 수를 늘려보세요."]
    if n < 100:
        out.append(f"⚠️ 거래 {n}건 — 표본이 적어서 우연과 구분하기 어려워요. 코인 수나 기간을 늘려 100건 이상 확보하세요.")
    avg, fh, sh = summ["avg_R"], summ.get("first_half_avg"), summ.get("second_half_avg")
    if avg <= 0:
        out.append(f"❌ 비용을 뺀 거래당 평균 {avg:+.2f}R — 이 기간·코인에서는 엣지가 확인되지 않았어요. 실전 투입은 권하지 않습니다.")
    elif avg < 0.1:
        out.append(f"🟡 거래당 평균 {avg:+.2f}R — 약한 플러스라 수수료·슬리피지가 조금만 커져도 사라질 수 있는 수준이에요.")
    else:
        out.append(f"🟢 거래당 평균 {avg:+.2f}R (손익비 PF {summ['pf']:.2f}) — 이 기간에는 비용을 빼고도 플러스였어요.")
    if fh is not None and sh is not None:
        if fh > 0 and sh > 0:
            out.append(f"✅ 전반부 {fh:+.2f}R · 후반부 {sh:+.2f}R — 두 구간 모두 플러스라 특정 시기에만 통한 건 아닐 가능성이 커요.")
        elif (fh > 0) != (sh > 0):
            out.append(f"⚠️ 전반부 {fh:+.2f}R · 후반부 {sh:+.2f}R — 한쪽 구간에서만 통했어요. 시장 상황에 따라 성과가 뒤집힐 수 있어요.")
    out.append(f"📉 최대 연속 손실 {summ['max_consec_loss']}회, 최대 낙폭 {summ['max_dd_R']:.1f}R — 거래당 {risk_pct:g}% 리스크면 "
               f"계좌가 한때 약 {summ['max_dd_R'] * risk_pct:.0f}% 줄어드는 구간을 견뎌야 해요.")
    return out


def run_backtest_suite(n_coins: int = 15, days: int = 365, progress_cb=None) -> Dict:
    """거래량 상위 코인들의 실제 과거 데이터로 현재 전략을 검증 (두 청산 방식 비교 포함).
    데이터 거래소는 EXCHANGES 순서(기본 Bitget → OKX → Binance)로 먼저 되는 곳을 씁니다."""
    bar_hours = pd.Timedelta(TIMEFRAME) / pd.Timedelta("1h")
    warmup = 250
    total_bars = int(days * 24 / bar_hours) + warmup
    ex_id, btc = None, None
    for cand in EXCHANGES:
        try:
            b = fetch_extended_ohlcv(cand, f"BTC/{QUOTE}", TIMEFRAME, total_bars)
            if len(b) >= warmup + 100:
                ex_id, btc = cand, b
                break
        except Exception as e:
            print(f"[warn] {cand} 과거 데이터 조회 실패: {e}")
    if ex_id is None:
        raise RuntimeError("과거 데이터를 받을 수 있는 거래소가 없어요 (네트워크·지역 차단 확인)")

    perps = bitget_perp_symbols() if BITGET_ONLY else set()
    cands = [x for x in get_top_volume_symbols(ex_id, n_coins * 3)
             if not _is_excluded_symbol(x) and x != f"BTC/{QUOTE}"
             and (not perps or f"{x}:{QUOTE}" in perps)][:n_coins]
    trades = {"partial_trail": [], "fixed": []}
    coins, errors = [], []
    for k, sym in enumerate(cands):
        if progress_cb:
            progress_cb(k, len(cands), sym)
        try:
            d = fetch_extended_ohlcv(ex_id, sym, TIMEFRAME, total_bars)
            htf = fetch_extended_ohlcv(ex_id, sym, HTF_TIMEFRAME, _htf_bars_needed(total_bars) + 250)
            m = d.merge(btc[["ts", "close"]].rename(columns={"close": "btc_close"}), on="ts", how="inner")
            if len(m) < warmup + 100:
                errors.append(f"{sym}: 데이터 부족({len(m)}봉)")
                continue
            coin_df = m[["ts", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
            btc_al = pd.DataFrame({"ts": m["ts"], "close": m["btc_close"]}).reset_index(drop=True)
            sig = generate_signals(coin_df, btc_al, htf, warmup=warmup)
            for mode in trades:
                for x in simulate_exits(coin_df, sig, mode):
                    x.update(symbol=sym, entry_ts=coin_df["ts"].iloc[x["entry_idx"]],
                             exit_ts=coin_df["ts"].iloc[x["exit_idx"]])
                    trades[mode].append(x)
            coins.append(sym)
        except Exception as e:
            errors.append(f"{sym}: {e}")
    if progress_cb:
        progress_cb(len(cands), len(cands), "")
    return {"exchange": ex_id, "timeframe": TIMEFRAME, "days": days, "coins": coins, "errors": errors,
            "trades": trades, "start": btc["ts"].iloc[warmup] if len(btc) > warmup else None,
            "end": btc["ts"].iloc[-1], "ran_at": utc_now()}


def backtest_report_text(bt: Dict, risk_pct: float = 1.0) -> str:
    """결과를 복사해서 보내기 좋은 텍스트로."""
    lines = [f"[과거 검증] {bt['exchange']} · {tf_label(bt['timeframe'])} · {bt['days']}일 · 코인 {len(bt['coins'])}개",
             f"기간 {pd.Timestamp(bt['start']):%Y-%m-%d} ~ {pd.Timestamp(bt['end']):%Y-%m-%d}"]
    for mode, name in (("partial_trail", "분할익절+추적손절"), ("fixed", "목표1 전량")):
        sm = summarize_trades(bt["trades"][mode])
        if sm["n"] == 0:
            lines.append(f"- {name}: 거래 없음"); continue
        lines.append(f"- {name}: {sm['n']}건, 승률 {sm['win_rate']:.0%}, 평균 {sm['avg_R']:+.2f}R, PF {sm['pf']:.2f}, "
                     f"합계 {sm['total_R']:+.1f}R, 최대연속손실 {sm['max_consec_loss']}, 최대낙폭 {sm['max_dd_R']:.1f}R, "
                     f"전반/후반 {sm['first_half_avg']:+.2f}/{sm['second_half_avg']:+.2f}R")
    tr = bt["trades"]["partial_trail"]
    for key, label in (("direction", "방향"), ("family", "유형")):
        groups: Dict[str, List[Dict]] = {}
        for x in tr:
            g = x["direction"] if key == "direction" else SETUP_FAMILY.get(x["bias"], x["bias"])
            groups.setdefault(g, []).append(x)
        parts = [f"{g} {summarize_trades(v)['n']}건 {summarize_trades(v)['avg_R']:+.2f}R" for g, v in groups.items()]
        lines.append(f"- {label}별(분할익절): " + (", ".join(parts) or "없음"))
    if bt["errors"]:
        lines.append(f"- 제외된 코인: {len(bt['errors'])}개")
    return "\n".join(lines)


def compare_exit_modes(df: pd.DataFrame, btc_df: pd.DataFrame, htf_df: pd.DataFrame) -> pd.DataFrame:
    """같은 신호로 두 청산 방식(분할익절+추적손절 vs 목표1 전량)을 비교."""
    sig = generate_signals(df, btc_df, htf_df)
    rows = []
    for mode in ("partial_trail", "fixed"):
        r = [x["R"] for x in simulate_exits(df.reset_index(drop=True), sig, mode)]
        rows.append({"청산 방식": mode, "거래 수": len(r), "평균 R": float(np.mean(r)) if r else 0.0,
                     "합계 R": float(np.sum(r)) if r else 0.0,
                     "승률": float(np.mean([x > 0 for x in r])) if r else 0.0,
                     "최대 R": float(np.max(r)) if r else 0.0})
    return pd.DataFrame(rows)


def backtest_wfo_real(exchange_id: str, symbol: str, timeframe: Optional[str] = None,
                       total_bars: int = 2000, train_bars: int = 1000,
                       test_bars: int = 200) -> pd.DataFrame:
    """실제 build_setup() 로직으로 구간별 워크포워드 검증.
    ⚠️ 지금 build_setup()에는 그리드서치할 자유 파라미터가 없으므로(임계값이 코드에 고정),
    이건 엄밀히는 '최적화 후 검증'이 아니라 '고정 규칙의 롤링 아웃오브샘플 검증'입니다.
    (오히려 파라미터를 데이터에 맞출 기회가 없다는 점에서 과최적화 위험은 더 낮습니다.)

    train_avg_R/test_avg_R가 구간마다 꾸준히 비슷한 부호·크기로 나오면 신뢰할 만한 신호,
    구간마다 들쭉날쭉하거나 test 구간에서 계속 마이너스면 이 전략은 재검토가 필요합니다."""
    df = fetch_extended_ohlcv(exchange_id, symbol, timeframe, total_bars)
    btc_symbol = f"BTC/{QUOTE}"
    btc_df = df.copy() if symbol == btc_symbol else fetch_extended_ohlcv(exchange_id, btc_symbol, timeframe, total_bars)
    daily_df = fetch_extended_ohlcv(exchange_id, symbol, HTF_TIMEFRAME, _htf_bars_needed(total_bars))

    rows = []
    start = 0
    while start + train_bars + test_bars <= len(df):
        train_df = df.iloc[start:start + train_bars].reset_index(drop=True)
        train_btc = btc_df.iloc[start:start + train_bars].reset_index(drop=True)
        test_df = df.iloc[start + train_bars:start + train_bars + test_bars].reset_index(drop=True)
        test_btc = btc_df.iloc[start + train_bars:start + train_bars + test_bars].reset_index(drop=True)

        train_R = simulate_strategy_history(train_df, train_btc, daily_df)
        test_R = simulate_strategy_history(test_df, test_btc, daily_df)

        rows.append({
            "train_start": train_df["ts"].iloc[0], "train_end": train_df["ts"].iloc[-1],
            "test_start": test_df["ts"].iloc[0], "test_end": test_df["ts"].iloc[-1],
            "train_trades": len(train_R), "train_avg_R": float(np.mean(train_R)) if train_R else 0.0,
            "train_total_R": float(sum(train_R)),
            "test_trades": len(test_R), "test_avg_R": float(np.mean(test_R)) if test_R else 0.0,
            "test_total_R": float(sum(test_R)),
        })
        start += test_bars

    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# 5.15 횡보 '기울기(lean)'의 예측력 검증 — 점수가 실제로 방향을 맞추는가?
# --------------------------------------------------------------------------

def evaluate_lean_predictions(df: pd.DataFrame, btc_df: pd.DataFrame, daily_df: pd.DataFrame,
                               weights: tuple = (0.5, 0.3, 0.2), forward_bars: int = 12,
                               min_lookback: int = 80, threshold: float = 0.3,
                               cost_pct: float = 0.16,
                               ctx: Optional[Dict] = None) -> List[Dict]:
    """이 코인 자신의 국면이 '횡보'인 시점마다 lean 점수를 계산하고, 강한 기울기(|score|>threshold)가
    나온 경우 이후 forward_bars봉 뒤 실제 수익률과 비교합니다.
    - 겹치는 표본으로 적중률이 부풀려지지 않도록 forward_bars 간격으로만 샘플링
    - hit = 방향 적중(부호), net_ret_pct = 왕복 비용(cost_pct, 수수료+슬리피지 근사) 차감 후 수익률
    - 미래 데이터는 예측 시점 이후 forward_bars 결과 확인에만 사용"""
    records: List[Dict] = []
    ctx = ctx if ctx is not None else {}   # 봉별 (국면, HTF) 캐시 — 가중치를 바꿔 반복 평가할 때 재사용
    i = min_lookback
    while i + forward_bars < len(df):
        key = df["ts"].iloc[i]
        if key not in ctx:
            # 라이브(screen_market)·백테스트와 동일하게: 이 코인 자신의 국면 기준으로 판단
            # (BTC 국면으로 판단하면 실제 신호 생성 로직과 다른 걸 검증하게 됨)
            window_df = df.iloc[max(0, i + 1 - OHLCV_LIMIT):i + 1]
            regime_i = classify_price_trend(window_df)
            htf_i = htf_trend_at(daily_df, key) if regime_i == "sideways" else None
            ctx[key] = (regime_i, htf_i)
        regime_i, htf_i = ctx[key]
        if regime_i == "sideways":
            lean = compute_sideways_lean(df.iloc[max(0, i + 1 - OHLCV_LIMIT):i + 1], htf_i, weights=weights)
            if abs(lean["score"]) > threshold:
                direction = 1 if lean["score"] > 0 else -1
                fwd_ret_pct = (df["close"].iloc[i + forward_bars] / df["close"].iloc[i] - 1) * 100
                records.append({
                    "ts": df["ts"].iloc[i], "score": lean["score"], "direction": direction,
                    "fwd_ret_pct": fwd_ret_pct,
                    # 방향 적중은 부호만으로 판정(무엇도 못 맞히면 50% → z검정 기준이 유효).
                    # 비용은 net_ret_pct(평균 순수익률)에서 따로 반영합니다.
                    "hit": direction * fwd_ret_pct > 0,
                    "net_ret_pct": direction * fwd_ret_pct - cost_pct,
                })
                i += forward_bars   # 표본 간 겹침 방지
                continue
        i += 1
    return records


def summarize_lean(records: List[Dict]) -> Dict:
    """적중률과 '우연 대비 유의성'(z-score, 50% 기준)을 요약. |z|<2면 우연과 구분 어렵다는 뜻."""
    n = len(records)
    if n == 0:
        return {"n": 0, "hit_rate": None, "z": None, "avg_net_ret_pct": None}
    hits = sum(1 for r in records if r["hit"])
    hit_rate = hits / n
    z = (hit_rate - 0.5) / math.sqrt(0.25 / n)
    return {"n": n, "hit_rate": hit_rate, "z": z,
            "avg_net_ret_pct": float(np.mean([r["net_ret_pct"] for r in records]))}


def backtest_lean_wfo(exchange_id: str, symbol: str, timeframe: Optional[str] = None,
                       total_bars: int = 3000, train_bars: int = 1200, test_bars: int = 400,
                       weight_grid: Optional[List[tuple]] = None, forward_bars: int = 12,
                       min_train_samples: int = 15) -> pd.DataFrame:
    """횡보 기울기 점수의 예측력을 롤링 워크포워드로 검증.
    - weight_grid를 주면: 학습 구간에서 적중률 최고 가중치를 고르고(표본 min_train_samples 이상만),
      다음 test 구간에서 그 가중치로 검증 (진짜 OOS)
    - 안 주면: 기본 가중치(0.5/0.3/0.2) 고정으로 구간별 적중률만 측정
    test_hit_rate가 구간마다 꾸준히 50%를 넘고 z가 커야 의미 있는 엣지입니다."""
    df = fetch_extended_ohlcv(exchange_id, symbol, timeframe, total_bars)
    btc_symbol = f"BTC/{QUOTE}"
    btc_df = df.copy() if symbol == btc_symbol else fetch_extended_ohlcv(exchange_id, btc_symbol, timeframe, total_bars)
    daily_df = fetch_extended_ohlcv(exchange_id, symbol, HTF_TIMEFRAME, _htf_bars_needed(total_bars))
    grid = weight_grid or [(0.5, 0.3, 0.2)]

    rows, start = [], 0
    while start + train_bars + test_bars <= len(df):
        tr = slice(start, start + train_bars)
        te = slice(start + train_bars, start + train_bars + test_bars)
        # 지표 계산에 lookback이 필요하므로 test 구간은 앞선 이력을 포함해 평가하되,
        # 기록은 test 구간 안의 시점만 채택
        def _eval(sl_start, sl_end, w):
            recs = evaluate_lean_predictions(df.iloc[:sl_end].reset_index(drop=True),
                                             btc_df.iloc[:sl_end].reset_index(drop=True),
                                             daily_df, weights=w, forward_bars=forward_bars,
                                             min_lookback=max(80, sl_start))
            return recs

        best_w, best_hit, best_train = grid[0], -1.0, {"n": 0}
        for w in grid:
            s = summarize_lean(_eval(tr.start, tr.stop, w))
            if s["n"] >= min_train_samples and s["hit_rate"] is not None and s["hit_rate"] > best_hit:
                best_w, best_hit, best_train = w, s["hit_rate"], s
        if best_hit < 0:
            best_train = summarize_lean(_eval(tr.start, tr.stop, best_w))

        test_s = summarize_lean(_eval(te.start, te.stop, best_w))
        rows.append({
            "test_start": df["ts"].iloc[te.start], "test_end": df["ts"].iloc[te.stop - 1],
            "weights": best_w, "train_n": best_train.get("n"), "train_hit": best_train.get("hit_rate"),
            "test_n": test_s["n"], "test_hit": test_s["hit_rate"], "test_z": test_s["z"],
            "test_avg_net_ret_pct": test_s["avg_net_ret_pct"],
        })
        start += test_bars
    return pd.DataFrame(rows)


def pooled_lean_evaluation(exchange_id: str, symbols: List[str], timeframe: Optional[str] = None,
                            total_bars: int = 3000, weight_grid: Optional[List[tuple]] = None,
                            split_ratio: float = 0.6, forward_bars: int = 12) -> pd.DataFrame:
    """여러 코인의 기울기 신호를 합쳐서 평가 (코인 1개는 표본이 너무 적어 통계적으로 무의미).
    - 각 코인의 앞쪽 split_ratio 구간 = 학습(가중치 선택), 뒤쪽 = 검증(OOS)
    - 가중치는 '학습 풀 전체'에서 적중률 최고인 조합 1개를 고르고, 검증 풀에서 그대로 평가
    - 결과의 n과 z를 반드시 확인: n이 100 미만이거나 |z|<2면 '엣지 있다'고 결론내리지 마세요."""
    grid = weight_grid or [(0.5, 0.3, 0.2), (0.7, 0.2, 0.1), (0.3, 0.4, 0.3), (0.34, 0.33, 0.33)]
    btc_symbol = f"BTC/{QUOTE}"
    btc_full = fetch_extended_ohlcv(exchange_id, btc_symbol, timeframe, total_bars)

    datasets = []
    for sym in symbols:
        try:
            d = fetch_extended_ohlcv(exchange_id, sym, timeframe, total_bars)
            daily = fetch_extended_ohlcv(exchange_id, sym, HTF_TIMEFRAME, _htf_bars_needed(total_bars))
            m = min(len(d), len(btc_full))
            datasets.append((sym, d.tail(m).reset_index(drop=True),
                             btc_full.tail(m).reset_index(drop=True), daily, {}))
        except Exception as e:
            print(f"[warn] {sym} 수집 실패: {e}")

    def _pool(part: str, w: tuple) -> List[Dict]:
        pool: List[Dict] = []
        for sym, d, b, daily, ctx in datasets:
            cut = int(len(d) * split_ratio)
            if part == "train":
                dd, bb = d.iloc[:cut].reset_index(drop=True), b.iloc[:cut].reset_index(drop=True)
                recs = evaluate_lean_predictions(dd, bb, daily, weights=w, forward_bars=forward_bars, ctx=ctx)
            else:
                # 검증 구간: 앞선 이력은 지표 계산에만 쓰고, 기록은 cut 이후 시점만 채택
                recs = evaluate_lean_predictions(d, b, daily, weights=w, forward_bars=forward_bars,
                                                 min_lookback=max(80, cut), ctx=ctx)
            pool += recs
        return pool

    rows = []
    for w in grid:
        tr, te = summarize_lean(_pool("train", w)), summarize_lean(_pool("test", w))
        rows.append({"weights": w, "train_n": tr["n"], "train_hit": tr["hit_rate"],
                     "test_n": te["n"], "test_hit": te["hit_rate"], "test_z": te["z"],
                     "test_avg_net_ret_pct": te["avg_net_ret_pct"]})
    out = pd.DataFrame(rows)
    # 학습 적중률 기준 선택(검증 성과로 고르면 안 됨 — 검증 데이터 오염)
    if out["train_hit"].notna().any():
        out["selected_on_train"] = out["train_hit"] == out["train_hit"].max()
    return out


# --------------------------------------------------------------------------
# 5.2 (참고용) 단순 예시 전략 WFO — 실제 추천 로직과 무관한 EMA크로스 뼈대.
#     새로 만든 backtest_wfo_real()을 실전 검증에 사용하세요. 이건 WFO 매커니즘
#     자체를 이해하기 위한 최소 예시로만 남겨둡니다.
# --------------------------------------------------------------------------

def backtest_wfo(df: pd.DataFrame, train_window: int = 500, test_window: int = 100,
                  param_grid: Optional[List[Dict]] = None) -> pd.DataFrame:
    """워크포워드 최적화 뼈대.
    - train_window 구간에서 파라미터(예: EMA fast/slow, ADX threshold)를 그리드서치로 최적화
    - 바로 다음 test_window 구간에서 '한 번도 본 적 없는 데이터'로 검증
    - 이 과정을 데이터 끝까지 롤링 반복 → 구간별 성과를 모아야 '진짜 엣지'인지 판단 가능

    ⚠️ 이 함수는 뼈대만 제공합니다. 실제 전략 로직(진입/청산 규칙)을 채워 넣고,
    거래비용·슬리피지·펀딩비를 반드시 반영해야 현실적인 결과가 나옵니다.
    과최적화 방지를 위해 파라미터 그리드는 최소한으로 유지하세요.
    """
    if param_grid is None:
        param_grid = [
            {"ema_fast": 20, "ema_slow": 50, "adx_th": 15},
            {"ema_fast": 50, "ema_slow": 200, "adx_th": 20},
        ]

    results = []
    start = 0
    while start + train_window + test_window <= len(df):
        train = df.iloc[start:start + train_window]
        test = df.iloc[start + train_window:start + train_window + test_window]

        best_param, best_score = None, -math.inf
        for params in param_grid:
            score = _evaluate_strategy(train, params)
            if score > best_score:
                best_score, best_param = score, params

        oos_score = _evaluate_strategy(test, best_param)
        results.append({
            "train_start": train["ts"].iloc[0], "train_end": train["ts"].iloc[-1],
            "test_start": test["ts"].iloc[0], "test_end": test["ts"].iloc[-1],
            "best_param": best_param, "in_sample_score": best_score, "out_of_sample_score": oos_score,
        })
        start += test_window  # 롤링

    return pd.DataFrame(results)


def _evaluate_strategy(df: pd.DataFrame, params: Dict,
                        fee_pct: float = 0.05, slippage_pct: float = 0.03,
                        funding_pct_per_8h: float = 0.01) -> float:
    """예시 전략 평가 함수: EMA 골든/데드크로스 + ADX 필터.
    ⚠️ 실전에서는 여기를 본인의 실제 전략 로직으로 교체하세요.

    비용을 반드시 반영합니다 (기본값은 대략적인 예시이며 실제 거래소 수수료로 교체하세요):
    - fee_pct: 편도 거래 수수료 (%) — 왕복이면 2번 발생
    - slippage_pct: 체결 슬리피지 (%) — 시장가 진입/청산 시 발생
    - funding_pct_per_8h: 무기한 선물 보유 시 8시간마다 발생하는 펀딩비 (%)
      → 포지션을 오래 들고 있을수록 비용이 누적되므로, 봉 하나(TIMEFRAME)당
        경과 시간에 비례해 비용을 차감합니다."""
    fast = ema(df["close"], params["ema_fast"])
    slow = ema(df["close"], params["ema_slow"])
    strength = adx(df) if len(df) > params["ema_slow"] else 0

    bars_per_8h = BARS_PER_8H  # 8시간 동안의 봉 개수 (4시간봉 2개, 1시간봉 8개)
    per_bar_funding_cost = funding_pct_per_8h / bars_per_8h / 100

    position = 0
    pnl = 0.0
    for i in range(1, len(df)):
        if strength < params["adx_th"]:
            continue
        prev_position = position
        if fast.iloc[i] > slow.iloc[i] and position <= 0:
            position = 1
        elif fast.iloc[i] < slow.iloc[i] and position >= 0:
            position = -1

        # 포지션이 바뀌는 시점(=진입/청산 발생)에는 수수료+슬리피지를 왕복 비용으로 차감
        if position != prev_position:
            pnl -= (fee_pct + slippage_pct) / 100

        ret = (df["close"].iloc[i] / df["close"].iloc[i - 1] - 1) * position
        pnl += ret

        # 포지션을 들고 있는 매 봉마다 펀딩비 차감 (방향 무관하게 비용으로만 근사 반영;
        # 실제로는 펀딩비 부호가 시장 상황에 따라 바뀌므로 이건 보수적 근사치입니다)
        if position != 0:
            pnl -= per_bar_funding_cost

    return pnl


# --------------------------------------------------------------------------
# 6. 메인 파이프라인
# --------------------------------------------------------------------------

def main(risk_cfg: Optional[RiskConfig] = None):
    if risk_cfg is None:
        # 기본값: 계좌 예시 1,000 USDT, 트레이드당 1% 리스크, 동일방향 최대 3개
        # 실전에서는 반드시 본인 실제 잔고로 바꿔서 호출하세요: main(RiskConfig(account_balance=..., ...))
        risk_cfg = RiskConfig(account_balance=1000, risk_per_trade_pct=1.0, max_concurrent_setups=10)

    # 서킷브레이커: 오늘/이번 주 실현 손실이 한도를 넘었으면 신규 신호 자체를 생성하지 않음
    breaker_msg = circuit_breaker_triggered(risk_cfg)
    if breaker_msg:
        print("=" * 60)
        print(f"🛑 서킷브레이커 작동: {breaker_msg}")
        print("   손실을 만회하려는 추가 진입이 계좌를 가장 크게 망가뜨립니다.")
        print("   내일(또는 다음 주) 한도가 초기화될 때까지 신규 진입을 쉬세요.")
        print("=" * 60)
        return

    print("=" * 60)
    print("1) 거시 국면 판단 중...")
    regime = determine_overall_regime()
    print(f"  BTC 가격추세      : {regime.btc_trend}")
    print(f"  BTC.D 추세        : {regime.btc_d_trend}")
    print(f"  USDT.D 추세       : {regime.usdt_d_trend}")
    print(f"  TOTAL2 추세       : {regime.total2_trend}")
    print(f"  TOTAL3 추세       : {regime.total3_trend}")
    print(f"  ▶ 종합 국면       : {regime.overall.upper()}")
    print("=" * 60)

    print("2) 코인 스크리닝 중 (Binance/OKX/Bitget, 거래량 상위)...")
    setups = screen_market(regime.overall)

    if not setups:
        print("  조건을 만족하는 셋업이 없습니다. (거래량 급증 + RR 1.5 이상 기준)")
        return

    # 상관관계(BTC 동조화) 노출 제한 — 같은 방향 신호가 아무리 많아도 RR 상위 N개만 실전 후보로 남김
    setups = cap_correlated_exposure(setups, risk_cfg)

    print(f"  총 {len(setups)}개 후보 발견 (상관관계 제한 적용 후)\n")

    ready = [s for s in setups if not s.is_chase]
    waiting = [s for s in setups if s.is_chase]

    def _print_setup(s: CoinSetup):
        sizing = calculate_position_size(s.entry_price, s.sl, risk_cfg)
        poc_tag = " 🎯POC컨플루언스" if s.poc_confluence else ""
        print(f"[{s.exchange}] {s.symbol} | {s.bias}{poc_tag}")
        print(f"   근거     : {s.entry_note}")
        asym_str = f"{s.asymmetry:+.2f}" if s.asymmetry is not None else "표본부족"
        print(f"   상대강도 : {s.rs:+.2f}% (지수 대비)   비대칭점수: {asym_str} (상승↑/하락↓ 비대칭)")
        print(f"   현재가   : {s.current_price:.6f}   지정가 진입가: {s.entry_price:.6f}")
        print(f"   TP1/TP2  : {s.tp1:.6f} / {s.tp2:.6f}   SL: {s.sl:.6f}   RR: {s.rr_ratio:.2f}")
        print(f"   포지션   : 수량 {sizing['size']:.4f} (명목가치 {sizing['notional']:.2f} USDT, "
              f"리스크 {sizing['risk_amount']:.2f} USDT = 계좌의 {risk_cfg.risk_per_trade_pct}%)")
        print("-" * 50)

    def _rs_sort_key(s: CoinSetup):
        asym = s.asymmetry if s.asymmetry is not None else 0.0
        if s.bias in ("long", "wait_breakout_long", "range_fade_long"):
            return (s.rs, asym)
        return (-s.rs, -asym)

    print("=" * 60)
    print(f"✅ 되돌림 진입가 도달 — 지금 진입 검토 가능 ({len(ready)}개, 상대강도 순)")
    print("=" * 60)
    if not ready:
        print("  (현재 없음 — 전부 추격 구간이거나 신호 자체가 없음)")
    for s in sorted(ready, key=_rs_sort_key, reverse=True):
        _print_setup(s)

    print()
    print("=" * 60)
    print(f"⏳ 추격 구간 — 아직 진입 대기, 지정가만 걸어두고 관망 ({len(waiting)}개, 상대강도 순)")
    print("=" * 60)
    if not waiting:
        print("  (현재 없음)")
    for s in sorted(waiting, key=_rs_sort_key, reverse=True):
        _print_setup(s)


def validate_lean_auto(exchange_id: str = "binance", top_n: int = 25) -> None:
    """코인 목록을 직접 넣을 필요 없이, 거래량 상위 top_n개를 자동으로 골라
    횡보 기울기 점수의 예측력을 검증하고 결과를 해석까지 붙여 출력합니다.
    (추천용이 아니라 '이 기울기 점수를 믿어도 되는지' 점검용 — 한 번만 돌려보면 됩니다)"""
    symbols = [s for s in get_top_volume_symbols(exchange_id, top_n + 5)
               if s != f"BTC/{QUOTE}"][:top_n]
    print(f"검증 대상 {len(symbols)}개 코인 데이터 수집·분석 중... (수 분 걸릴 수 있음)")
    res = pooled_lean_evaluation(exchange_id, symbols)
    print(res.to_string())

    chosen = res[res.get("selected_on_train", False) == True]  # noqa: E712
    row = chosen.iloc[0] if len(chosen) else res.iloc[0]
    n, hit, z, net = row["test_n"], row["test_hit"], row["test_z"], row["test_avg_net_ret_pct"]
    print("\n[해석]")
    if not n or n < 100 or hit is None or z is None:
        print(f"- 검증 신호 {n}건으로 표본이 부족해 결론을 낼 수 없습니다. 기울기 점수를 방향 선택 근거로 쓰지 마세요.")
    elif hit >= 0.55 and z >= 2 and net > 0:
        print(f"- 적중률 {hit:.1%}, z={z:.1f}, 비용 후 평균 {net:+.2f}% → 쓸 만한 엣지가 있어 보입니다(과거 기준, 미래 보장 아님).")
    else:
        print(f"- 적중률 {hit:.1%}, z={z:.1f}, 비용 후 평균 {net:+.2f}% → 우연과 구분되는 엣지가 확인되지 않았습니다.")
        print("  횡보에서는 방향을 고르지 말고 양쪽 신호를 다 열어두는 쪽이 안전합니다.")


if __name__ == "__main__":
    import sys
    if "--validate" in sys.argv:
        validate_lean_auto()
    else:
        main()
