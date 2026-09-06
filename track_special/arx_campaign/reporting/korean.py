"""Korean operator-facing reports with explicit unknown values."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping


def _decimal(value: Any) -> str:
    try:
        return f"{Decimal(str(value)):,.1f}"
    except Exception:
        return "UNKNOWN"


def _value(data: Mapping[str, Any], key: str, default: str = "UNKNOWN") -> str:
    return str(data[key]) if data.get(key) is not None else default


def daily_report(data: Mapping[str, Any]) -> str:
    """Never render an unavailable account value as zero."""

    rows = [
        ("전략/위험 상태", f"{_value(data, 'campaign_state')} / {_value(data, 'risk_state')}"),
        ("기준 시각", _value(data, "observed_at")),
        ("승인 시작자본 E0(USDT)", _decimal(data.get("e0_usdt"))),
        ("전략 순자산(USDT)", _decimal(data.get("strategy_equity_usdt"))),
        ("캠페인 순손익(USDT)", _decimal(data.get("campaign_net_pnl_usdt"))),
        (
            "일/주 손실(입출금 보정, USDT)",
            f"{_decimal(data.get('daily_loss_usdt'))} / "
            f"{_decimal(data.get('weekly_loss_usdt'))}",
        ),
        ("고점 대비 하락(USDT)", _decimal(data.get("equity_drawdown_usdt"))),
        ("포지션 수량(ARX)", _decimal(data.get("position_quantity"))),
        ("평균 진입가", _value(data, "average_entry")),
        ("명목금액(USDT)", _decimal(data.get("gross_notional_usdt"))),
        (
            "설정 배율/실효 노출",
            f"{_value(data, 'configured_leverage')} / {_value(data, 'effective_leverage')}",
        ),
        ("가용 담보(USDT)", _decimal(data.get("available_margin_usdt"))),
        ("격리증거금(USDT)", _decimal(data.get("isolated_margin_usdt"))),
        (
            "mark/last 가격",
            f"{_value(data, 'mark_price')} / {_value(data, 'last_price')}",
        ),
        (
            "추정 청산가/완충",
            f"{_value(data, 'liquidation_price')} / {_value(data, 'liquidation_buffer')}",
        ),
        ("보호선", _value(data, "protective_stop")),
        (
            "서버 보호 주문/기준/커버",
            f"{_value(data, 'protection_order_id')} / "
            f"{_value(data, 'trigger_reference')} / "
            f"{_value(data, 'protected_quantity')}",
        ),
        ("비용 포함 손절 예상손익(USDT)", _decimal(data.get("pnl_at_stop_usdt"))),
        ("원금 손실 위험(USDT)", _decimal(data.get("principal_loss_at_stop_usdt"))),
        ("평가이익 반납 위험(USDT)", _decimal(data.get("giveback_at_stop_usdt"))),
        (
            "현재/예상 펀딩(USDT)",
            f"{_value(data, 'current_funding_rate')} / "
            f"{_decimal(data.get('projected_funding_cost_usdt'))}",
        ),
        ("다음 펀딩 시각", _value(data, "next_funding_at")),
        ("순실현손익(USDT)", _decimal(data.get("realized_net_pnl_usdt"))),
        ("재사용 가능 이익(USDT)", _decimal(data.get("reusable_profit_usdt"))),
        ("보호 reserve(USDT)", _decimal(data.get("reserve_profit_usdt"))),
        ("미보호 노출(ARX)", _decimal(data.get("unprotected_exposure"))),
        ("예약/결과 미확정 주문", _value(data, "reservations")),
        ("대사 상태", _value(data, "reconciliation")),
        ("데이터 결측/지연", _value(data, "data_quality")),
        ("다음 증액 조건", _value(data, "next_add_condition")),
        ("다음 축소 조건", _value(data, "next_reduce_condition")),
    ]
    return "\n".join(
        ["[ARX 일일 보고]"]
        + [f"{name}: {value}" for name, value in rows]
        + [
            "주의: E0와 전체 전략 순자산 기준이며, paper/replay는 실체결·수익성 증거가 아닙니다."
        ]
    )


def severe_report(
    code: str, detail: str, data: Mapping[str, Any] | None = None
) -> str:
    data = data or {}
    return "\n".join(
        (
            f"[ARX 긴급] {code}",
            f"상세: {detail}",
            f"위험 상태: {_value(data, 'risk_state')}",
            f"포지션/미보호 노출: {_value(data, 'position_quantity')} / "
            f"{_value(data, 'unprotected_exposure')}",
            f"보호/대사: {_value(data, 'protection')} / {_value(data, 'reconciliation')}",
            "신규·증액을 중지하고 진입 주문 취소, 보호 확인, 승인된 감축 순서로 대응하십시오.",
        )
    )
