"""Offline unit tests for the commission wallet models — enum wire
values + ledger row shape. No DB."""

from app.models.commission_ledger import CommissionLedger, CommissionLedgerKind
from app.models.withdrawal_request import (
    WithdrawalPayoutChannel,
    WithdrawalStatus,
)


def test_ledger_kind_values_are_uppercase() -> None:
    values = [m.value for m in CommissionLedgerKind]
    assert values == [
        "COMMISSION", "WITHDRAW", "WITHDRAW_REFUND", "CONVERT", "CLAWBACK", "ADJUST",
    ]
    assert all(v == v.upper() for v in values)


def test_withdrawal_enums_values() -> None:
    assert [m.value for m in WithdrawalStatus] == ["PENDING", "PAID", "REJECTED"]
    assert [m.value for m in WithdrawalPayoutChannel] == ["ALIPAY", "BANK"]


def test_ledger_row_carries_signed_fen() -> None:
    earn = CommissionLedger(
        user_id=1, amount_fen=1000, kind=CommissionLedgerKind.COMMISSION,
    )
    debit = CommissionLedger(
        user_id=1, amount_fen=-1000, kind=CommissionLedgerKind.WITHDRAW,
    )
    assert earn.amount_fen == 1000
    assert debit.amount_fen == -1000
    assert "COMMISSION" in repr(earn)
