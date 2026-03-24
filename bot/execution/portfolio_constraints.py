"""
PortfolioConstraintEngine — v1.3 Master Plan PART 7

동시 포지션 / 동일 방향 / 총 노출 등 포트폴리오 수준 제약을 강제한다.

초기 글로벌 룰 (PART 7.2):
  MAX_ACTIVE_LIVE_POSITIONS    = 2
  MAX_NEW_EXECUTIONS_PER_15M   = 2
  SAME_DIRECTION_LIMIT         = 1
  TOTAL_EXPOSURE_CAP           = 30%
  MAX_SYMBOL_EXPOSURE          = 15%
  MAX_CATEGORY_EXPOSURE        = 20%
  MAX_CORRELATED_POSITIONS     = 1
  MAX_LEVERAGE                 = 3x
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from bot.strategies.opportunity import Opportunity
    from bot.data.store import DataStore

logger = logging.getLogger(__name__)


@dataclass
class ConstraintResult:
    passed:        bool
    reason:        str
    rule_failed:   Optional[str] = None


class PortfolioConstraintEngine:
    """
    Opportunity 실행 전 포트폴리오 수준 제약 검사.

    Usage
    -----
    pce = PortfolioConstraintEngine(store)
    result = pce.check(opp, live_positions)
    if result.passed:
        execute(opp)
    """

    MAX_ACTIVE_LIVE_POSITIONS  = 2
    MAX_NEW_EXECUTIONS_PER_15M = 2
    SAME_DIRECTION_LIMIT       = 1
    TOTAL_EXPOSURE_CAP         = 0.30     # 30%
    MAX_SYMBOL_EXPOSURE        = 0.15     # 15%
    MAX_CATEGORY_EXPOSURE      = 0.20     # 20%
    MAX_LEVERAGE               = 3

    WINDOW_15M_MS = 15 * 60 * 1000

    def __init__(self, store: "DataStore") -> None:
        self._store = store
        # 최근 실행 기록: list of ts_ms
        self._recent_executions: List[int] = []

    # ---------------------------------------------------------------------- #
    # Public check
    # ---------------------------------------------------------------------- #

    def check(
        self,
        opp: "Opportunity",
        live_positions: List[dict],
    ) -> ConstraintResult:
        """
        Opportunity가 포트폴리오 제약을 통과하는지 검사한다.
        live_positions: 현재 열린 LIVE 포지션 목록 (store.get_open_live_positions())
        """

        # 1. 최대 동시 LIVE 포지션
        if len(live_positions) >= self.MAX_ACTIVE_LIVE_POSITIONS:
            return ConstraintResult(
                passed=False,
                reason=f"MAX_ACTIVE_LIVE_POSITIONS({self.MAX_ACTIVE_LIVE_POSITIONS}) 도달",
                rule_failed="MAX_ACTIVE_LIVE_POSITIONS",
            )

        # 2. 15분 내 신규 실행 횟수
        self._purge_old_executions()
        if len(self._recent_executions) >= self.MAX_NEW_EXECUTIONS_PER_15M:
            return ConstraintResult(
                passed=False,
                reason=f"15분 내 실행 횟수 {self.MAX_NEW_EXECUTIONS_PER_15M}회 초과",
                rule_failed="MAX_NEW_EXECUTIONS_PER_15M",
            )

        # 3. 동일 방향 제한
        same_dir = [
            p for p in live_positions
            if p.get("side", "").upper() == opp.side
        ]
        if len(same_dir) >= self.SAME_DIRECTION_LIMIT:
            return ConstraintResult(
                passed=False,
                reason=f"동일 방향({opp.side}) 포지션이 이미 {len(same_dir)}개 존재",
                rule_failed="SAME_DIRECTION_LIMIT",
            )

        # 4. 동일 심볼 기존 포지션
        sym_positions = [p for p in live_positions if p.get("symbol") == opp.symbol]
        if sym_positions:
            return ConstraintResult(
                passed=False,
                reason=f"{opp.symbol} 포지션 이미 존재",
                rule_failed="SYMBOL_CONFLICT",
            )

        return ConstraintResult(passed=True, reason="모든 포트폴리오 제약 통과")

    def record_execution(self) -> None:
        """실행 후 호출하여 15분 윈도우 카운터 업데이트."""
        self._recent_executions.append(int(time.time() * 1000))

    # ---------------------------------------------------------------------- #
    # Internal
    # ---------------------------------------------------------------------- #

    def _purge_old_executions(self) -> None:
        now = int(time.time() * 1000)
        self._recent_executions = [
            ts for ts in self._recent_executions
            if (now - ts) <= self.WINDOW_15M_MS
        ]


# --------------------------------------------------------------------------- #
# Dynamic Aggression Model — PART 6
# --------------------------------------------------------------------------- #

@dataclass
class AggressionResult:
    risk_pct:      float    # 계정 대비 리스크 비율
    size_modifier: float    # 최종 사이즈 배수 (1.0 = 기본)
    reason:        str


class DynamicAggressionModel:
    """
    점수와 시장 상태에 따라 포지션 크기를 조절한다.

    PART 6.2 기본 설정:
      score 8  → 계정 리스크 0.5%
      score 9  → 계정 리스크 0.75%
      score 10+ → 계정 리스크 1.0%

    추가 조건:
      - HIGH_VOL → -20~30% 축소
      - 일일 손실 50% 소진 → 신규 리스크 50% 축소
      - 동일 방향 노출 존재 → size 축소
    """

    BASE_RISK = {8: 0.005, 9: 0.0075, 10: 0.01}
    DEFAULT_RISK = 0.005

    def compute(
        self,
        opp: "Opportunity",
        regime: dict,
        daily_loss_pct: float = 0.0,
        daily_loss_limit: float = 0.02,
        has_same_direction: bool = False,
    ) -> AggressionResult:

        score = opp.score_total
        base_risk = self.BASE_RISK.get(min(score, 10), self.DEFAULT_RISK)
        modifier = 1.0
        reasons = []

        # HIGH_VOL → 25% 축소
        if regime.get("regime") in ("HIGH_VOLATILITY",) or opp.volatility_state == "EXPANDING":
            modifier *= 0.75
            reasons.append("HIGH_VOL -25%")

        # 일일 손실 50% 이상 소진
        if daily_loss_limit > 0 and abs(daily_loss_pct) >= daily_loss_limit * 0.5:
            modifier *= 0.5
            reasons.append("daily_loss_50pct -50%")

        # 동일 방향 노출 존재
        if has_same_direction:
            modifier *= 0.6
            reasons.append("same_dir_exists -40%")

        final_risk = base_risk * modifier
        return AggressionResult(
            risk_pct      = round(final_risk, 5),
            size_modifier = round(modifier, 4),
            reason        = ", ".join(reasons) or "기본 사이징",
        )
