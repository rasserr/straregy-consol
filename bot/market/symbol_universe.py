"""
Symbol Universe — v1.3 PART 9

종목 유니버스를 3계층으로 관리:
  Tier 1 (Core):               항상 스캔, LIVE 허용
  Tier 2 (Active Expansion):   스캔, LIVE 제한 허용 (min_score 높임)
  Tier 3 (Opportunistic):      스캔, 우선 PAPER (실행 문턱 높음)

각 계층별 실행 임계값:
  Tier 1: min_score=8,  live_allowed=True
  Tier 2: min_score=9,  live_allowed=True   (더 엄격한 기준)
  Tier 3: min_score=10, live_allowed=False  (PAPER만)

런타임에 계층 변경 가능.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# 계층 정의
TIER_1 = 1   # Core
TIER_2 = 2   # Active Expansion
TIER_3 = 3   # Opportunistic

# 계층별 실행 파라미터
TIER_CONFIG: Dict[int, dict] = {
    TIER_1: {"min_score": 8,  "live_allowed": True,  "label": "Core"},
    TIER_2: {"min_score": 9,  "live_allowed": True,  "label": "Active Expansion"},
    TIER_3: {"min_score": 10, "live_allowed": False, "label": "Opportunistic"},
}

# 기본 종목 분류
DEFAULT_UNIVERSE: Dict[str, int] = {
    # Tier 1 — Core
    "BTCUSDT":   TIER_1,
    "ETHUSDT":   TIER_1,
    "SOLUSDT":   TIER_1,
    # Tier 2 — Active Expansion
    "BNBUSDT":   TIER_2,
    "XRPUSDT":   TIER_2,
    "DOGEUSDT":  TIER_2,
    "ADAUSDT":   TIER_2,
    "AVAXUSDT":  TIER_2,
    # Tier 3 — Opportunistic
    "SUIUSDT":   TIER_3,
    "PEPEUSDT":  TIER_3,
    "WIFUSDT":   TIER_3,
}


class SymbolUniverse:
    """
    종목 유니버스 계층화 관리자.

    Usage
    -----
    universe = SymbolUniverse()
    universe.initialize_from_config(config.tracked_symbols)

    tier = universe.get_tier("BTCUSDT")    # 1
    cfg  = universe.get_tier_config(tier)  # {"min_score": 8, "live_allowed": True}
    all_symbols = universe.get_all()       # ["BTCUSDT", ...]
    core = universe.get_by_tier(TIER_1)    # ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    """

    def __init__(self) -> None:
        # symbol → tier 매핑
        self._universe: Dict[str, int] = dict(DEFAULT_UNIVERSE)

    def initialize_from_config(self, tracked_symbols: List[str]) -> None:
        """
        config.tracked_symbols에 있는 심볼들을 유니버스에 등록.
        DEFAULT_UNIVERSE에 없는 심볼은 Tier 2로 기본 설정.
        """
        for sym in tracked_symbols:
            if sym not in self._universe:
                self._universe[sym] = TIER_2
                logger.info("[Universe] '%s' registered as Tier 2 (Active Expansion)", sym)

        # tracked_symbols에 없는 심볼 제거 (설정 기준 동기화)
        to_remove = [s for s in self._universe if s not in tracked_symbols]
        for sym in to_remove:
            del self._universe[sym]

        logger.info(
            "[Universe] Initialized: %d symbols  T1=%d T2=%d T3=%d",
            len(self._universe),
            self.count_by_tier(TIER_1),
            self.count_by_tier(TIER_2),
            self.count_by_tier(TIER_3),
        )

    def get_tier(self, symbol: str) -> int:
        """심볼의 계층 반환. 미등록 시 Tier 2 기본값."""
        return self._universe.get(symbol, TIER_2)

    def get_tier_config(self, tier: int) -> dict:
        """계층별 실행 설정."""
        return TIER_CONFIG.get(tier, TIER_CONFIG[TIER_2])

    def get_symbol_config(self, symbol: str) -> dict:
        """심볼의 실행 설정 (tier + min_score + live_allowed)."""
        tier = self.get_tier(symbol)
        cfg  = self.get_tier_config(tier)
        return {"symbol": symbol, "tier": tier, **cfg}

    def get_all(self) -> List[str]:
        return list(self._universe.keys())

    def get_by_tier(self, tier: int) -> List[str]:
        return [s for s, t in self._universe.items() if t == tier]

    def count_by_tier(self, tier: int) -> int:
        return sum(1 for t in self._universe.values() if t == tier)

    def set_tier(self, symbol: str, tier: int) -> bool:
        """런타임 계층 변경."""
        if tier not in TIER_CONFIG:
            return False
        old = self._universe.get(symbol, "N/A")
        self._universe[symbol] = tier
        logger.info("[Universe] '%s' tier: %s → %d", symbol, old, tier)
        return True

    def add_symbol(self, symbol: str, tier: int = TIER_2) -> None:
        """새 심볼 추가."""
        self._universe[symbol] = tier
        logger.info("[Universe] Added '%s' as Tier %d", symbol, tier)

    def remove_symbol(self, symbol: str) -> bool:
        """심볼 제거."""
        if symbol in self._universe:
            del self._universe[symbol]
            logger.info("[Universe] Removed '%s'", symbol)
            return True
        return False

    def build_summary_text(self) -> str:
        """Telegram용 유니버스 요약."""
        lines = ["*📊 Symbol Universe*\n"]
        for tier in (TIER_1, TIER_2, TIER_3):
            cfg     = TIER_CONFIG[tier]
            symbols = self.get_by_tier(tier)
            live    = "LIVE가능" if cfg["live_allowed"] else "PAPERonly"
            lines.append(
                f"*Tier {tier} — {cfg['label']}* ({live}, min_score≥{cfg['min_score']})\n"
                f"  {', '.join(f'`{s}`' for s in symbols) or '없음'}"
            )
        return "\n\n".join(lines)

    def to_dict(self) -> List[dict]:
        """대시보드 API용 직렬화."""
        return [
            {
                "symbol": sym,
                "tier":   tier,
                "label":  TIER_CONFIG[tier]["label"],
                "min_score":    TIER_CONFIG[tier]["min_score"],
                "live_allowed": TIER_CONFIG[tier]["live_allowed"],
            }
            for sym, tier in sorted(self._universe.items(), key=lambda x: x[1])
        ]
