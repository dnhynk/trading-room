"""Pure marked inventory accounting, including unsold sub-minimum residuals."""
from decimal import Decimal as D


def residual_mark(row):
    # v3 residuals written before the audit have basis but no market mark.
    # Preserve that historical estimate until a current book updates it.
    qty = D(row['qty'])
    return D(row['mark']) if row.get('mark') is not None else D(row['cost']) / qty if qty else D(0)


def residual_value(row):
    return D(row['qty']) * residual_mark(row)


def inventory_rows(state):
    campaigns = list(state['campaigns'].values()) if state.get('version') == 3 else [state['campaign']] if state.get('campaign') else []
    return campaigns + list(state.get('residuals', {}).values())


def marked_equity(state):
    return D(state['cash_krw']) + sum((residual_value(r) for r in inventory_rows(state)), D(0))


def unrealized_loss(state):
    return sum((min(D(0), residual_value(r) - D(r['cost'])) for r in inventory_rows(state)), D(0))
