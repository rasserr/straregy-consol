"""
KillSwitch — Emergency stop for the 22B Strategy Engine (Part 3.5 / Part 10.4).

SOFT Kill: block new entries only. Existing positions protected by SL/TP.
HARD Kill: block entries + cancel open orders + optional reduce-only close all.

Auto-triggers on:
  - Daily loss limit exceeded
  - 3 consecutive API failures
  - Reconciliation discrepancy
  - Unexpected position found

Manual triggers:
  - Dashboard KILL SWITCH button  (POST /api/kill-switch)
  - Telegram /kill or /killhard command
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from bot.data.store import DataStore
    from bot.notifications.telegram import TelegramNotifier

logger = logging.getLogger(__name__)

# Kill modes
KILL_SOFT = "SOFT"  # block new entries only
KILL_HARD = "HARD"  # block entries + cancel orders (+ optional close all)


class KillSwitch:
    """
    Synchronous flag-based kill switch with async action execution.

    SOFT mode: is_active=True, mode=SOFT → new entries blocked, positions kept.
    HARD mode: is_active=True, mode=HARD → entries blocked + orders cancelled.

    The `is_active` property is checked synchronously before every order
    submission so there is zero latency overhead.
    """

    def __init__(
        self,
        store: "DataStore",
        telegram: Optional["TelegramNotifier"] = None,
    ) -> None:
        self._store = store
        self._telegram = telegram
        self._active: bool = False
        self._kill_mode: str = KILL_SOFT  # SOFT | HARD
        self._reason: str = ""
        self._triggered_at: Optional[int] = None
        self._triggered_by: Optional[str] = None
        self._reset_by: Optional[str] = None
        self._reset_at: Optional[int] = None

        # Reference to executor is injected after construction to avoid circular deps
        self._executor = None

    def set_executor(self, executor) -> None:
        """Inject Executor reference (called by Engine after both are created)."""
        self._executor = executor

    # ---------------------------------------------------------------------- #
    # Core flag
    # ---------------------------------------------------------------------- #

    @property
    def is_active(self) -> bool:
        """Synchronous check — called before EVERY order submission."""
        return self._active

    @property
    def kill_mode(self) -> str:
        return self._kill_mode

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def triggered_at(self) -> Optional[int]:
        return self._triggered_at

    # ---------------------------------------------------------------------- #
    # Trigger — Soft Kill (block new entries only)
    # ---------------------------------------------------------------------- #

    async def trigger(
        self,
        reason: str,
        triggered_by: str = "system",
        mode: str = KILL_SOFT,
    ) -> None:
        """
        Activate the kill switch.

        SOFT mode (default):
          1. Set BLOCKED flag immediately
          2. Update system mode → BLOCKED
          3. Send Telegram alert
          (positions kept — SL/TP protect them)

        HARD mode (mode=KILL_HARD):
          1. SOFT steps above
          2. Cancel all open orders on Binance
        """
        if self._active and self._kill_mode == KILL_HARD:
            # Already in hardest mode — ignore
            logger.warning("[KillSwitch] Already HARD active. Ignoring new trigger.")
            return

        was_active = self._active
        escalating = was_active and mode == KILL_HARD and self._kill_mode == KILL_SOFT

        # Step 1: Block immediately (synchronous)
        self._active = True
        self._kill_mode = mode
        self._reason = reason
        self._triggered_at = int(time.time() * 1000)
        self._triggered_by = triggered_by

        log_msg = "ESCALATED to HARD" if escalating else f"TRIGGERED ({mode})"
        logger.critical(
            "[KillSwitch] %s — reason='%s' by='%s'",
            log_msg, reason, triggered_by,
        )

        # Step 2 (HARD): Cancel all open orders
        if mode == KILL_HARD and self._executor is not None:
            try:
                cancelled = await self._executor.cancel_all_orders()
                logger.warning("[KillSwitch] Cancelled %d open orders.", len(cancelled))
            except Exception as exc:
                logger.error("[KillSwitch] Failed to cancel orders during HARD kill: %s", exc)

        # Step 3: Update system mode → BLOCKED
        self._store.set_system_mode("BLOCKED")
        self._store._broadcast("kill_switch", {
            "active":       True,
            "kill_mode":    mode,
            "reason":       reason,
            "triggered_by": triggered_by,
            "ts":           self._triggered_at,
        })

        # Step 4: Telegram alert
        if self._telegram is not None:
            mode_label = "⛔ SOFT KILL" if mode == KILL_SOFT else "🚨 HARD KILL"
            self._telegram._enqueue(
                f"*{mode_label} ACTIVATED*\n"
                f"사유: {reason}\n"
                f"트리거: `{triggered_by}`\n"
                f"시각: {_ts()}"
            )

    async def trigger_soft(self, reason: str, triggered_by: str = "system") -> None:
        """신규 진입만 차단. 포지션은 SL/TP로 보호."""
        await self.trigger(reason, triggered_by, mode=KILL_SOFT)

    async def trigger_hard(self, reason: str, triggered_by: str = "system") -> None:
        """신규 진입 차단 + 미체결 주문 전체 취소."""
        await self.trigger(reason, triggered_by, mode=KILL_HARD)

    # ---------------------------------------------------------------------- #
    # Reset
    # ---------------------------------------------------------------------- #

    def reset(self, authorized_by: str) -> None:
        """
        Reset the kill switch — allows new entries again.

        System mode is set back to OBSERVE (safest default).
        Operator must manually promote to ACTIVE/LIMITED.
        """
        if not self._active:
            logger.info("[KillSwitch] Reset called but switch is not active.")
            return

        prev_mode   = self._kill_mode
        prev_reason = self._reason

        logger.warning(
            "[KillSwitch] RESET by='%s' (previous mode=%s reason='%s')",
            authorized_by, prev_mode, prev_reason,
        )

        self._active = False
        self._kill_mode = KILL_SOFT
        self._reset_by = authorized_by
        self._reset_at = int(time.time() * 1000)

        self._store.set_system_mode("OBSERVE")
        self._store._broadcast("kill_switch", {
            "active":   False,
            "reset_by": authorized_by,
            "ts":       self._reset_at,
        })

        if self._telegram is not None:
            self._telegram._enqueue(
                f"*Kill Switch RESET*\n"
                f"이전 모드: `{prev_mode}`\n"
                f"사유: {prev_reason}\n"
                f"해제자: `{authorized_by}`\n"
                f"시스템 모드 → OBSERVE\n"
                f"시각: {_ts()}"
            )

    # ---------------------------------------------------------------------- #
    # Status dict (for dashboard)
    # ---------------------------------------------------------------------- #

    def get_status(self) -> dict:
        return {
            "active":       self._active,
            "kill_mode":    self._kill_mode,
            "reason":       self._reason,
            "triggered_at": self._triggered_at,
            "triggered_by": self._triggered_by,
            "reset_by":     self._reset_by,
            "reset_at":     self._reset_at,
        }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _ts() -> str:
    import datetime
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
