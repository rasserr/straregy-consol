"""
Strategy Supervision Monitor — 전략 검증 감독 스크립트

실전 매매 진입 전 전략 상태를 점검하고 리포트를 출력한다.

사용법:
    python scripts/supervise.py           # 콘솔 출력
    python scripts/supervise.py --save    # data/supervision/ 에 리포트 저장
    python scripts/supervise.py --gate    # LIVE 진입 가능 전략 목록만 출력

검증 기준 (ValidationTracker와 동일):
    SHADOW 진입: 점수 >= 50
    LIVE   진입: 점수 >= 75
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------- #
# 설정
# --------------------------------------------------------------------------- #

DB_PATH   = Path(__file__).parent.parent / "data" / "trading.db"
SAVE_DIR  = Path(__file__).parent.parent / "data" / "supervision"

SHADOW_SCORE = 50
LIVE_SCORE   = 75

# 전략별 최소 paper 거래 수 (LIVE 승격 하드 게이트)
MIN_TRADES_SHADOW = 30
MIN_TRADES_LIVE   = 50

# --------------------------------------------------------------------------- #
# Score (validation_tracker.py 와 동일 로직)
# --------------------------------------------------------------------------- #

def compute_score(stats: dict) -> int:
    score = 0
    tc  = stats.get("trade_count", 0) or 0
    pf  = stats.get("recent_10_pf") or 0.0
    wr  = stats.get("win_rate") or 0.0
    mdd = stats.get("mdd") or 0.0
    exp = stats.get("expectancy") or 0.0
    hs  = stats.get("health_status", "UNKNOWN")

    if tc >= 50:   score += 25
    elif tc >= 30: score += 15
    elif tc >= 10: score += 5

    if pf >= 1.5:  score += 20
    elif pf >= 1.2: score += 12
    elif pf >= 1.0: score += 5

    if wr >= 0.45:  score += 15
    elif wr >= 0.40: score += 10
    elif wr >= 0.35: score += 5

    if mdd >= -8.0:   score += 15
    elif mdd >= -10.0: score += 10
    elif mdd >= -15.0: score += 5

    if exp > 0:     score += 10
    if hs == "OK":  score += 15
    elif hs == "WARN": score += 5

    return min(score, 100)


# --------------------------------------------------------------------------- #
# DB 조회
# --------------------------------------------------------------------------- #

def load_data(db_path: Path) -> dict:
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # 전략 상태
    strategies = {
        r["name"]: dict(r)
        for r in conn.execute("SELECT * FROM strategy_state").fetchall()
    }

    # paper 포지션 통계 (전략별)
    rows = conn.execute("""
        SELECT strategy,
               COUNT(CASE WHEN status='CLOSED' THEN 1 END)     AS closed,
               COUNT(CASE WHEN status='OPEN'   THEN 1 END)     AS open_cnt,
               AVG(CASE WHEN status='CLOSED' THEN pnl_pct END) AS avg_pnl,
               SUM(CASE WHEN status='CLOSED' AND pnl_pct > 0 THEN 1 ELSE 0 END) AS wins
        FROM paper_positions
        GROUP BY strategy
    """).fetchall()
    paper_stats: dict = {}
    for r in rows:
        d = dict(r)
        closed = d["closed"] or 0
        wins   = d["wins"] or 0
        paper_stats[d["strategy"]] = {
            "trade_count": closed,
            "open_cnt":    d["open_cnt"] or 0,
            "avg_pnl":     round(float(d["avg_pnl"]), 3) if d["avg_pnl"] is not None else None,
            "win_rate":    round(wins / closed, 3) if closed > 0 else None,
        }

    # strategy_state 에서 health 지표 추가 보완
    for name, ps in paper_stats.items():
        ss = strategies.get(name, {})
        ps["recent_10_pf"]  = ss.get("recent_10_pf")
        ps["recent_mdd"]    = ss.get("recent_mdd")
        ps["mdd"]           = ss.get("recent_mdd")
        ps["expectancy"]    = ss.get("recent_expectancy")
        ps["health_status"] = ss.get("health_status", "UNKNOWN")

    # validation_log 최신 스냅샷
    validation: dict = {}
    try:
        vrows = conn.execute("""
            SELECT v.*
            FROM strategy_validation_log v
            INNER JOIN (
                SELECT strategy, MAX(ts) AS max_ts
                FROM strategy_validation_log GROUP BY strategy
            ) latest ON v.strategy = latest.strategy AND v.ts = latest.max_ts
        """).fetchall()
        validation = {r["strategy"]: dict(r) for r in vrows}
    except Exception:
        pass  # 테이블 없으면 skip

    # 레짐
    regime_row = conn.execute(
        "SELECT regime, ts FROM regimes ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    regime = dict(regime_row) if regime_row else {"regime": "UNKNOWN"}

    # 라이브 주문
    live_order_count = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE status NOT IN ('CANCELLED','REJECTED','CLOSED')"
    ).fetchone()[0]

    conn.close()
    return {
        "strategies":   strategies,
        "paper_stats":  paper_stats,
        "validation":   validation,
        "regime":       regime,
        "live_orders":  live_order_count,
    }


# --------------------------------------------------------------------------- #
# 리포트 생성
# --------------------------------------------------------------------------- #

def build_report(data: dict, gate_only: bool = False) -> str:
    strategies  = data["strategies"]
    paper_stats = data["paper_stats"]
    validation  = data["validation"]
    regime      = data["regime"]
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = []
    lines.append("=" * 65)
    lines.append(f"  22B Strategy Engine — 검증 감독 리포트")
    lines.append(f"  {now_str}  |  레짐: {regime.get('regime', 'UNKNOWN')}")
    lines.append("=" * 65)

    # 전략별 평가
    all_strategies = sorted(strategies.keys())
    live_ready = []
    shadow_ready = []
    need_more = []
    critical = []

    for name in all_strategies:
        ss = strategies[name]
        mode = ss.get("mode", "PAPER")
        if mode == "PAUSED":
            continue

        ps = paper_stats.get(name, {})
        tc = ps.get("trade_count", 0)
        wr = ps.get("win_rate")
        avg_pnl = ps.get("avg_pnl")
        pf  = ps.get("recent_10_pf") or ss.get("recent_10_pf")
        mdd = ps.get("mdd") or ss.get("recent_mdd")
        exp = ps.get("expectancy") or ss.get("recent_expectancy")
        hs  = ss.get("health_status", "UNKNOWN")

        # validation_log에 점수가 있으면 그걸 사용
        vsnap = validation.get(name, {})
        if vsnap:
            score = vsnap.get("validation_score", 0)
        else:
            stats_for_score = {
                "trade_count": tc, "recent_10_pf": pf, "win_rate": wr or 0,
                "mdd": mdd or 0, "expectancy": exp or 0, "health_status": hs,
            }
            score = compute_score(stats_for_score)

        meets_live   = score >= LIVE_SCORE   and tc >= MIN_TRADES_LIVE
        meets_shadow = score >= SHADOW_SCORE and tc >= MIN_TRADES_SHADOW

        entry = {
            "name": name, "mode": mode, "score": score,
            "trade_count": tc, "win_rate": wr, "pf10": pf,
            "mdd": mdd, "expectancy": exp, "health": hs,
            "meets_live": meets_live, "meets_shadow": meets_shadow,
        }

        if mode == "LIVE" and tc < MIN_TRADES_SHADOW:
            critical.append(entry)
        elif meets_live:
            live_ready.append(entry)
        elif meets_shadow:
            shadow_ready.append(entry)
        else:
            need_more.append(entry)

    def fmt_strategy(e: dict, indent: str = "  ") -> str:
        bar_filled = e["score"] // 5
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        wr_str  = f"{e['win_rate']*100:.0f}%" if e.get("win_rate") else "N/A"
        pf_str  = f"{e['pf10']:.2f}" if e.get("pf10") else "N/A"
        mdd_str = f"{e['mdd']:.1f}%" if e.get("mdd") else "N/A"
        exp_str = f"{e['expectancy']:.4f}" if e.get("expectancy") else "N/A"
        return (
            f"{indent}[{e['mode']:6s}] {e['name']}\n"
            f"{indent}         점수: {e['score']:3d}/100  [{bar}]\n"
            f"{indent}         거래:{e['trade_count']}회  WR:{wr_str}  PF10:{pf_str}  MDD:{mdd_str}  Exp:{exp_str}  Health:{e['health']}"
        )

    if critical:
        lines.append("\n🚨 즉시 조치 필요 (검증 없이 LIVE)")
        lines.append("-" * 65)
        for e in critical:
            lines.append(fmt_strategy(e))
            needed = MIN_TRADES_SHADOW - e["trade_count"]
            lines.append(f"   ▶ 해결: PAPER로 강등 후 최소 {needed}회 추가 거래 필요")

    if not gate_only or live_ready:
        if live_ready:
            lines.append("\n✅ LIVE 진입 가능 (점수 ≥75, 거래 ≥50)")
            lines.append("-" * 65)
            for e in live_ready:
                lines.append(fmt_strategy(e))
        else:
            if not gate_only:
                lines.append("\n✅ LIVE 진입 가능: 없음")

    if not gate_only:
        if shadow_ready:
            lines.append("\n🟡 SHADOW 진입 가능 (점수 ≥50, 거래 ≥30)")
            lines.append("-" * 65)
            for e in shadow_ready:
                lines.append(fmt_strategy(e))

        if need_more:
            lines.append("\n⏳ 데이터 축적 중 (PAPER)")
            lines.append("-" * 65)
            for e in need_more:
                lines.append(fmt_strategy(e))
                needed_shadow = max(0, MIN_TRADES_SHADOW - e["trade_count"])
                needed_live   = max(0, MIN_TRADES_LIVE   - e["trade_count"])
                score_gap_shadow = max(0, SHADOW_SCORE - e["score"])
                score_gap_live   = max(0, LIVE_SCORE   - e["score"])
                if needed_shadow > 0 or score_gap_shadow > 0:
                    lines.append(
                        f"   ▶ SHADOW까지: 거래 +{needed_shadow}회 더 필요, 점수 +{score_gap_shadow}점 필요"
                    )
                if needed_live > 0 or score_gap_live > 0:
                    lines.append(
                        f"   ▶ LIVE까지:   거래 +{needed_live}회 더 필요, 점수 +{score_gap_live}점 필요"
                    )

        lines.append("\n" + "=" * 65)
        lines.append("진입 기준 요약")
        lines.append("-" * 65)
        lines.append(f"  SHADOW: 준비도 점수 ≥{SHADOW_SCORE}  +  paper 거래 ≥{MIN_TRADES_SHADOW}회")
        lines.append(f"  LIVE:   준비도 점수 ≥{LIVE_SCORE}  +  paper 거래 ≥{MIN_TRADES_LIVE}회")
        lines.append(f"  현재 라이브 주문: {data['live_orders']}건")

    lines.append("=" * 65)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 저장
# --------------------------------------------------------------------------- #

def save_report(report: str) -> Path:
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    ts_str = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = SAVE_DIR / f"report_{ts_str}.txt"
    path.write_text(report, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# JSON export (대시보드/외부 연동용)
# --------------------------------------------------------------------------- #

def export_json(data: dict) -> dict:
    strategies = data["strategies"]
    paper_stats = data["paper_stats"]
    validation = data["validation"]

    result = {}
    for name, ss in strategies.items():
        ps = paper_stats.get(name, {})
        vsnap = validation.get(name, {})
        tc = ps.get("trade_count", 0)
        pf = ps.get("recent_10_pf") or ss.get("recent_10_pf")
        wr = ps.get("win_rate")
        mdd = ps.get("mdd") or ss.get("recent_mdd")
        exp = ps.get("expectancy") or ss.get("recent_expectancy")
        hs = ss.get("health_status", "UNKNOWN")

        score = vsnap.get("validation_score") or compute_score({
            "trade_count": tc, "recent_10_pf": pf, "win_rate": wr or 0,
            "mdd": mdd or 0, "expectancy": exp or 0, "health_status": hs,
        })
        result[name] = {
            "mode": ss.get("mode"),
            "validation_score": score,
            "trade_count": tc,
            "win_rate": wr,
            "pf10": pf,
            "mdd": mdd,
            "expectancy": exp,
            "health_status": hs,
            "live_ready":   score >= LIVE_SCORE   and tc >= MIN_TRADES_LIVE,
            "shadow_ready": score >= SHADOW_SCORE and tc >= MIN_TRADES_SHADOW,
        }
    return {
        "ts": int(time.time() * 1000),
        "regime": data["regime"].get("regime"),
        "strategies": result,
    }


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #

def main() -> None:
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Strategy supervision monitor")
    parser.add_argument("--save", action="store_true", help="Save report to data/supervision/")
    parser.add_argument("--gate", action="store_true", help="Show only LIVE-ready strategies")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--db",   default=str(DB_PATH),  help="DB path override")
    args = parser.parse_args()

    data = load_data(Path(args.db))

    if args.json:
        print(json.dumps(export_json(data), ensure_ascii=False, indent=2))
        return

    report = build_report(data, gate_only=args.gate)
    print(report)

    if args.save:
        path = save_report(report)
        print(f"\n리포트 저장됨: {path}")


if __name__ == "__main__":
    main()
