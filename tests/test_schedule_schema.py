"""Request validation for the /api/schedules surface.

The rule "each frequency owns exactly one optional field" is written down
three times on purpose — here, in ``ck_schedule_frequency_fields``, and in
the frontend's ``lib/schedules.ts#isComplete`` — because each layer refuses
at a different cost: a disabled Save button, a 422 naming the field, a
database that will not store nonsense whatever reaches it.

Three copies can drift, and this one is the likeliest to move. The matrix
below is the shared rule stated once as data; the model test pins the
database's copy against the same shape.
"""

from datetime import date, time

import pytest
from pydantic import ValidationError

from app.models.schedule import ScheduleFrequency
from app.schemas.schedule import (
    RecurrenceSpec,
    ScheduleCreate,
    ScheduleUpdate,
)
from app.schemas.session import CONNECTORS_MAX, SKILLS_MAX

# frequency → the one optional field it requires (None = requires none).
OWNED_FIELD = {
    ScheduleFrequency.ONCE: "run_date",
    ScheduleFrequency.DAILY: None,
    ScheduleFrequency.WEEKLY: "weekday",
    ScheduleFrequency.MONTHLY: "day_of_month",
}
SAMPLE = {"run_date": date(2030, 1, 1), "weekday": 3, "day_of_month": 12}


def _spec(frequency, **fields):
    return RecurrenceSpec(
        frequency=frequency, at_time=time(9, 0), timezone="Asia/Shanghai", **fields
    )


@pytest.mark.parametrize("frequency", list(ScheduleFrequency))
def test_each_frequency_accepts_exactly_its_own_field(frequency):
    owned = OWNED_FIELD[frequency]
    spec = _spec(frequency, **({owned: SAMPLE[owned]} if owned else {}))
    assert spec.frequency is frequency


@pytest.mark.parametrize(
    "frequency", [frequency for frequency in ScheduleFrequency if OWNED_FIELD[frequency]]
)
def test_a_frequency_missing_its_field_is_refused(frequency):
    with pytest.raises(ValidationError, match=OWNED_FIELD[frequency]):
        _spec(frequency)


@pytest.mark.parametrize(
    ("frequency", "intruder"),
    [
        (frequency, field)
        for frequency in ScheduleFrequency
        for field in SAMPLE
        if field != OWNED_FIELD[frequency]
    ],
)
def test_a_frequency_carrying_someone_elses_field_is_refused(frequency, intruder):
    # Not merely tidiness: the recurrence math asserts on the field its
    # frequency owns, so a WEEKLY row that also carries a day_of_month is a
    # spec with two readings and no way to choose.
    owned = OWNED_FIELD[frequency]
    fields = {intruder: SAMPLE[intruder]}
    if owned:
        fields[owned] = SAMPLE[owned]
    with pytest.raises(ValidationError, match=intruder):
        _spec(frequency, **fields)


class TestTimeAndZone:
    def test_an_unknown_timezone_is_refused(self):
        with pytest.raises(ValidationError, match="unknown timezone"):
            RecurrenceSpec(
                frequency=ScheduleFrequency.DAILY,
                at_time=time(9, 0),
                timezone="Mars/Olympus",
            )

    def test_an_offset_on_the_time_itself_is_refused(self):
        # ``datetime.combine(day, at_time, tzinfo=<schedule zone>)`` would
        # override it in silence, firing hours from what was asked.
        with pytest.raises(ValidationError, match="wall-clock"):
            RecurrenceSpec(
                frequency=ScheduleFrequency.DAILY,
                at_time="09:00+00:00",
                timezone="Asia/Shanghai",
            )

    def test_day_of_month_spans_the_month_and_stops_there(self):
        # 29-31 are accepted — a month without that day simply does not
        # fire — but 32 is not a day of any month.
        for day in (28, 29, 30, 31):
            assert _spec(ScheduleFrequency.MONTHLY, day_of_month=day).day_of_month == day
        for day in (0, 32):
            with pytest.raises(ValidationError):
                _spec(ScheduleFrequency.MONTHLY, day_of_month=day)


class TestCreateBody:
    def test_a_blank_prompt_is_refused(self):
        with pytest.raises(ValidationError):
            ScheduleCreate(prompt="", recurrence=_spec(ScheduleFrequency.DAILY))

    def test_a_null_character_is_refused_rather_than_500ing(self):
        # ``schedule.prompt`` is a ``text`` column and Postgres cannot store
        # U+0000, so without this the INSERT fails and the caller gets an
        # opaque 500. Refused rather than stripped: silently rewriting what
        # someone typed is worse than telling them.
        for body in (
            {"prompt": "总结\x00新闻", "recurrence": _spec(ScheduleFrequency.DAILY)},
            {"prompt": "a\x00b"},
        ):
            model = ScheduleCreate if "recurrence" in body else ScheduleUpdate
            with pytest.raises(ValidationError, match="null character"):
                model(**body)

    def test_a_prompt_with_nothing_visible_is_refused(self):
        # U+200B passes ``min_length`` and ``str.strip`` alike, and would be
        # delivered as a turn with no instruction — while the dispatcher,
        # which strips format characters to title the session, found nothing
        # to title it with.
        for prompt in ("\u200b", "\u200b\n\u200d", " \ufeff "):
            with pytest.raises(ValidationError, match="visible text"):
                ScheduleCreate(prompt=prompt, recurrence=_spec(ScheduleFrequency.DAILY))
            with pytest.raises(ValidationError, match="visible text"):
                ScheduleUpdate(prompt=prompt)

    def test_a_prompt_with_invisible_characters_around_text_is_kept_verbatim(self):
        body = ScheduleCreate(prompt="\u200b总结新闻", recurrence=_spec(ScheduleFrequency.DAILY))
        assert body.prompt == "\u200b总结新闻"

    def test_a_null_prompt_on_patch_means_leave_it_alone(self):
        # Shared with the create body, where the field is required; here
        # ``None`` is the documented "unchanged", not a value to inspect.
        assert ScheduleUpdate(prompt=None).prompt is None
        assert ScheduleUpdate().prompt is None

    def test_no_title_is_accepted_or_stored(self):
        # A schedule has no name of its own: the prompt identifies it, and
        # asking for a title was asking the user to summarise what they had
        # just written. ``extra`` is not forbidden here, so the point is that
        # nothing reads one — not that sending one 422s.
        body = ScheduleCreate(prompt="p", recurrence=_spec(ScheduleFrequency.DAILY))
        assert not hasattr(body, "title")

    def test_next_run_at_is_not_accepted_from_a_caller(self):
        # It is derived at create, edit and resume, so a client cannot put
        # the dispatcher's scan column out of step with the spec it shows.
        body = ScheduleCreate(prompt="p", recurrence=_spec(ScheduleFrequency.DAILY))
        assert not hasattr(body, "next_run_at")


class TestCarriedContext:
    """Skill picks and Connector bindings on the create/update bodies."""

    def test_both_default_to_empty(self):
        # Every schedule created before this feature, and every one whose
        # form leaves them alone, must keep behaving exactly as it did.
        body = ScheduleCreate(prompt="x", recurrence=_spec(ScheduleFrequency.DAILY))
        assert body.skills == []
        assert body.connectors == []

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("skills", ["s"] * (SKILLS_MAX + 1)),
            ("connectors", list(range(CONNECTORS_MAX + 1))),
        ],
    )
    def test_the_per_turn_cost_is_bounded(self, field, value):
        # Both enter the model's context on every step of every firing, and
        # a scheduled task pays that cost unattended. Same bounds the
        # composer has.
        with pytest.raises(ValidationError):
            ScheduleCreate(prompt="x", recurrence=_spec(ScheduleFrequency.DAILY), **{field: value})

    def test_update_tells_leave_alone_apart_from_clear(self):
        # ``None`` means the form did not touch it; ``[]`` means the owner
        # removed the last one. A patch that conflated them could never
        # clear a selection.
        assert ScheduleUpdate().skills is None
        assert ScheduleUpdate(skills=[]).skills == []
        assert ScheduleUpdate().connectors is None
        assert ScheduleUpdate(connectors=[]).connectors == []
