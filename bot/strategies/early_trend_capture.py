"""
Strategy C — Early Trend Capture (v1.3 PART 3.2C)

후행 추세 추종이 아니라 초기 방향 전환 구조만 노린다.
기존 ema_cross를 강화 재구현.

진입 조건 (BUY):
  - EMA20 > EMA50 크로스 (직전 봉: EMA20 <= EMA50, 현재 봉: EMA20 > EMA50)
  - RSI(14) 40~60 구간 (추세 전환 초기, 과열 제외)
  - price > EMA50 (방향 확인)
  - 거래량 > 1.5x 20봉 평균
  - ATR이 최근 10봉 평균 이하 (과열 변동성 제외 — 초기 포착 목적)

진입 조건 (SELL):
  - EMA20 < EMA50 크로스
  - RSI(14) 40~60
  - price < EMA50
  - 거래량 조건 동일

보유 시간: 1시간~6시간
TP = +2.0%, SL = -1.0%
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List, Optional

import numpy as np
import pandas as pd

from bot.strategies._base import Signal, StrategyBase

if TYPE_CHECKING:
    from bot.data.store import DataStore

logger = logging.getLogger(__name__)

MIN_CANDLES = 70


class EarlyTrendCaptureStrategy(StrategyBase):
    """초기 추세 전환만 포착하는 EMA 크로스 강화 전략."""

    name:          str       = "early_trend_capture"
    category:      str       = "trend"
    regime_filter: List[str] = ["BTC_BULLISH", "ALT_ROTATION"]

    EMA_FAST:    int   = 20
    EMA_SLOW:    int   = 50
    RSI_PERIOD:  int   = 14
    RSI_LOW:     float = 40.0    # 과매수/과매도 제외
    RSI_HIGH:    float = 60.0
    VOL_MULT:    float = 1.5
    ATR_PERIOD:  int   = 14
    TP_PCT:      float = 0.020   # 2.0%
    SL_PCT:      float = 0.010   # 1.0%
    INTERVAL:    str   = "1h"

    def compute(self, store: "DataStore", regime: dict) -> List[Signal]:
        from bot.config import get_config
        config = get_config()
        current_regime = regime.get("regime", "UNKNOWN")
        signals: List[Signal] = []
        for symbol in config.tracked_symbols:
            sig = self._evaluate_symbol(store, symbol, current_regime)
            if sig is not None:
                signals.append(sig)
        return signals

    def _evaluate_symbol(
        self, store: "DataStore", symbol: str, regime: str
    ) -> Optional[Signal]:
        candles = store.get_candles(symbol, self.INTERVAL, limit=MIN_CANDLES + 10)
        if len(candles) < MIN_CANDLES:
            return None

        df = pd.DataFrame(candles).sort_values("ts").reset_index(drop=True)
        close  = df["c"].astype(float)
        high   = df["h"].astype(float)
        low    = df["l"].astype(float)
        volume = df["v"].astype(float)

        # EMA
        ema_fast = close.ewm(span=self.EMA_FAST, adjust=False).mean()
        ema_slow = close.ewm(span=self.EMA_SLOW, adjust=False).mean()

        ema_fast_cur  = float(ema_fast.iloc[-1])
        ema_fast_prev = float(ema_fast.iloc[-2])
        ema_slow_cur  = float(ema_slow.iloc[-1])
        ema_slow_prev = float(ema_slow.iloc[-2])

        # RSI
        delta = close.diff()
        gain  = delta.clip(lower=0).ewm(span=self.RSI_PERIOD, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(span=self.RSI_PERIOD, adjust=False).mean()
        rs    = gain / loss.replace(0, np.nan)
        rsi   = (100 - (100 / (1 + rs))).fillna(50)
        rsi_cur = float(rsi.iloc[-1])

        # ATR
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low  - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(self.ATR_PERIOD).mean()
        atr_cur   = float(atr.iloc[-1])
        atr_avg10 = float(atr.iloc[-11:-1].mean()) if len(atr) >= 11 else atr_cur

        # Volume
        vol_avg   = float(volume.rolling(20).mean().iloc[-1])
        vol_cur   = float(volume.iloc[-1])
        vol_ratio = vol_cur / vol_avg if vol_avg > 0 else 1.0

        price_cur = float(close.iloc[-1])

        # 변동성 과열 필터: ATR이 10봉 평균보다 30% 이상 높으면 제외
        if atr_avg10 > 0 and atr_cur > atr_avg10 * 1.30:
            return None

        # ─── BUY: 골든 크로스 초기 ───────────────────────────────────────── #
        golden_cross = (ema_fast_prev <= ema_slow_prev) and (ema_fast_cur > ema_slow_cur)
        if (
            golden_cross
            and self.RSI_LOW <= rsi_cur <= self.RSI_HIGH
            and price_cur > ema_slow_cur
            and vol_ratio >= self.VOL_MULT
        ):
            confidence = self._clamp(0.5 + (rsi_cur - 40) / 40 * 0.4 + (vol_ratio - 1) * 0.1, 0.5, 1.0)
            tp = round(price_cur * (1 + self.TP_PCT), 8)
            sl = round(price_cur * (1 - self.SL_PCT), 8)
            return Signal(
                strategy=self.name, symbol=symbol,
                action="BUY", mode=self._PHASE2_MODE,
                confidence=round(confidence, 4), regime=regime,
                tp=tp, sl=sl,
                reason=(
                    f"EarlyTrend BUY: golden cross EMA{self.EMA_FAST}>{self.EMA_SLOW}, "
                    f"RSI={rsi_cur:.1f}, vol={vol_ratio:.1f}x"
                ),
            )

        # ─── SELL: 데드 크로스 초기 ──────────────────────────────────────── #
        dead_cross = (ema_fast_prev >= ema_slow_prev) and (ema_fast_cur < ema_slow_cur)
        if (
            dead_cross
            and self.RSI_LOW <= rsi_cur <= self.RSI_HIGH
            and price_cur < ema_slow_cur
            and vol_ratio >= self.VOL_MULT
        ):
            confidence = self._clamp(0.5 + (60 - rsi_cur) / 40 * 0.4 + (vol_ratio - 1) * 0.1, 0.5, 1.0)
            tp = round(price_cur * (1 - self.TP_PCT), 8)
            sl = round(price_cur * (1 + self.SL_PCT), 8)
            return Signal(
                strategy=self.name, symbol=symbol,
                action="SELL", mode=self._PHASE2_MODE,
                confidence=round(confidence, 4), regime=regime,
                tp=tp, sl=sl,
                reason=(
                    f"EarlyTrend SELL: dead cross EMA{self.EMA_FAST}<{self.EMA_SLOW}, "
                    f"RSI={rsi_cur:.1f}, vol={vol_ratio:.1f}x"
                ),
            )

        return None
