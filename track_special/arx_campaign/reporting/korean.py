from __future__ import annotations
from decimal import Decimal
from typing import Mapping, Any
def _d(v: Any) -> str:
 try: return f"{Decimal(str(v)):,.1f}"
 except Exception: return "UNKNOWN"
def _v(data: Mapping[str,Any], key: str, default: str="UNKNOWN") -> str: return str(data[key]) if data.get(key) is not None else default
def daily_report(data: Mapping[str,Any]) -> str:
 """Korean operations report: missing inputs remain explicit unknowns, never zeroed."""
 rows=[("상태",_v(data,"risk_state")),("시각",_v(data,"observed_at")),("실현 손익(USDT)",_d(data["realized_pnl_usdt"]) if "realized_pnl_usdt" in data else "UNKNOWN"),("미실현 손익(USDT)",_d(data["unrealized_pnl_usdt"]) if "unrealized_pnl_usdt" in data else "UNKNOWN"),("펀딩/수수료(USDT)",_d(data.get("funding_and_fees_usdt")) if data.get("funding_and_fees_usdt") is not None else "UNKNOWN"),("포지션 수량(ARX)",_d(data["position_quantity"]) if "position_quantity" in data else "UNKNOWN"),("평균 진입/마크/최종가",f"{_v(data,'average_entry')} / {_v(data,'mark_price')} / {_v(data,'last_price')}"),("가용/격리 증거금(USDT)",f"{_v(data,'available_margin_usdt')} / {_v(data,'isolated_margin_usdt')}"),("청산가/완충",f"{_v(data,'liquidation_price')} / {_v(data,'liquidation_buffer')}"),("보호 상태",_v(data,"protection")),("보호 주문/트리거/커버",f"{_v(data,'protection_order_id')} / {_v(data,'trigger_reference')} / {_v(data,'protected_quantity') }"),("미보호 노출",_d(data["unprotected_exposure"]) if "unprotected_exposure" in data else "UNKNOWN"),("예약/미확정 주문",_v(data,"reservations")),("대사 상태",_v(data,"reconciliation")),("데이터 결측/지연",_v(data,"data_quality")),("다음 조치",_v(data,"next_action"))]
 return "\n".join(["[ARX 일일 보고]"]+[f"{k}: {v}" for k,v in rows]+["주의: paper/replay 결과는 실체결 검증이나 손익 하한이 아닙니다."])
def severe_report(code: str, detail: str, data: Mapping[str,Any]|None=None) -> str:
 data=data or {}
 return "\n".join((f"[ARX 긴급] {code}",f"상세: {detail}",f"위험 상태: {_v(data,'risk_state')}",f"포지션/미보호 노출: {_v(data,'position_quantity')} / {_v(data,'unprotected_exposure')}",f"보호/대사: {_v(data,'protection')} / {_v(data,'reconciliation')}","신규 진입은 중지하고 대사·보호 범위·감축 우선순위를 확인하십시오."))
