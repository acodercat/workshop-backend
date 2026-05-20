from sqlalchemy import Index
from sqlalchemy.dialects import postgresql

from app.models.credit_reservation import CreditReservation


def test_open_session_index_is_unique() -> None:
    indexes = {
        item.name: item
        for item in CreditReservation.__table__.indexes
        if isinstance(item, Index)
    }

    index = indexes["uq_credit_reservation_open_session"]
    where = index.dialect_options["postgresql"]["where"]
    predicate = str(
        where.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert index.unique is True
    assert [column.name for column in index.columns] == ["session_id"]
    assert predicate == "state = 'OPEN' AND session_id IS NOT NULL"
