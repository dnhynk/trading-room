from __future__ import annotations
from decimal import Decimal
from typing import Mapping, Any
def _d(v: Any) -> str: return f"{Decimal(str(v)):,.1f}"
def daily_report(data: Mapping[str,Any]) -> str:
    return "\n".join(("[ARX 일일 보고]",f"상태: {data.get('risk_state','UNKNOWN')}",f"실현 손익(USDT): {_d(data.get('realized_pnl_usdt',0))}",f"미실현 손익(USDT): {_d(data.get('unrealized_pnl_usdt',0))}",f"포지션 수량(ARX): {_d(data.get('position_quantity',0))}",f"보호 상태: {data.get('protection','UNVERIFIED')}","주의: paper/replay 결과는 실체결 검증이나 손익 하한이 아닙니다."))
def severe_report(code: str, detail: str) -> str:
    return f"[ARX 긴급] {code}\n{detail}\n신규 진입은 중지하고 대사 및 보호 범위를 확인하십시오."
