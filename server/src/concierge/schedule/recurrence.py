"""Recurrence for scheduled jobs.

Specs are structured (not raw RRULE strings) so the agent tool schema and the
UI can work with named fields. Occurrences are computed as wall-clock times
in the family timezone via a dateutil rruleset over *naive local* datetimes,
then converted to naive UTC (the ORM convention). Naive datetimes inside the
spec are treated as family-local; datetimes coming from the DB are naive UTC.
"""

import itertools
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from dateutil.rrule import (
    DAILY,
    HOURLY,
    MINUTELY,
    MONTHLY,
    WEEKLY,
    YEARLY,
    rrule,
    rruleset,
)
from pydantic import BaseModel, Field, model_validator

from concierge.core.config import get_settings

MAX_GAP_SAMPLE = 50
DAILY_GAP = timedelta(hours=24)

_WEEKDAYS = ("mo", "tu", "we", "th", "fr", "sa", "su")
_WEEKDAY_RU = {
    "mo": "пн",
    "tu": "вт",
    "we": "ср",  # noqa: RUF001
    "th": "чт",
    "fr": "пт",
    "sa": "сб",  # noqa: RUF001
    "su": "вс",
}
_FREQ_MAP = {
    "minutely": MINUTELY,
    "hourly": HOURLY,
    "daily": DAILY,
    "weekly": WEEKLY,
    "monthly": MONTHLY,
    "yearly": YEARLY,
}
# Frequencies anchored solely by dtstart — `times` doesn't apply.
_FINE = {"minutely", "hourly"}
_FREQ_RU = {
    "minutely": ("минута", "минуты", "минут"),
    "hourly": ("час", "часа", "часов"),
    "daily": ("день", "дня", "дней"),
    "weekly": ("неделя", "недели", "недель"),
    "monthly": ("месяц", "месяца", "месяцев"),
    "yearly": ("год", "года", "лет"),
}
_FREQ_SINGLE = {
    "minutely": "каждую минуту",
    "hourly": "каждый час",
    "daily": "каждый день",
    "weekly": "каждую неделю",
    "monthly": "каждый месяц",
    "yearly": "каждый год",
}


class ScheduleSpec(BaseModel):
    """Schedule for a job: one-shot (at/delay_minutes) and/or a recurrence.

    All datetimes are in the family timezone; `dates` may carry a time.
    `times` are "HH:MM" local wall clock for `daily` and slower frequencies.
    """

    at: datetime | None = None
    delay_minutes: int | None = None
    freq: (
        Literal["minutely", "hourly", "daily", "weekly", "monthly", "yearly"]
        | None
    ) = None
    interval: int = Field(default=1, ge=1)
    weekdays: (
        list[Literal["mo", "tu", "we", "th", "fr", "sa", "su"]] | None
    ) = None
    times: list[str] | None = None
    month_days: list[int] | None = None
    start: datetime | None = None
    until: datetime | None = None
    count: int | None = Field(default=None, ge=1)
    dates: list[datetime] | None = None
    exclude_dates: list[date] | None = None

    @model_validator(mode="after")
    def _check(self) -> "ScheduleSpec":
        once = (self.at is not None) + (self.delay_minutes is not None)
        if once > 1:
            raise ValueError("at and delay_minutes are mutually exclusive")
        if once == 1 and (self.freq is not None or self.dates):
            raise ValueError("one-shot cannot be combined with freq/dates")
        if not once and self.freq is None and not self.dates:
            raise ValueError("need at/delay_minutes, freq, or dates")
        if self.freq is None and self.interval != 1:
            raise ValueError("interval requires freq")
        for t in self.times or []:
            try:
                h, m = (int(x) for x in t.split(":"))
            except ValueError:
                raise ValueError(f"bad time {t!r}, want HH:MM") from None
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError(f"bad time {t!r}, want HH:MM")
        for d in self.month_days or []:
            if d != -1 and not 1 <= d <= 31:
                raise ValueError(f"bad month day {d}")
        return self


def family_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def spec_to_local(dt: datetime, tz: ZoneInfo) -> datetime:
    """Spec datetime -> naive local wall clock (naive is already local)."""
    if dt.tzinfo is not None:
        return dt.astimezone(tz).replace(tzinfo=None)
    return dt.replace(tzinfo=None)


def utc_to_local(dt: datetime, tz: ZoneInfo) -> datetime:
    """DB datetime (naive UTC) -> naive local wall clock."""
    return dt.replace(tzinfo=UTC).astimezone(tz).replace(tzinfo=None)


def local_to_utc(local: datetime, tz: ZoneInfo) -> datetime:
    return local.replace(tzinfo=tz).astimezone(UTC).replace(tzinfo=None)


def _parse_time(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def _rruleset(spec: ScheduleSpec, tz: ZoneInfo) -> rruleset:
    """Occurrences as naive local datetimes."""
    rs = rruleset()
    # dtstart is the anchor of the recurrence; create_job persists `start`
    # (or `at`) so it never drifts between fires.
    start = spec_to_local(spec.start, tz) if spec.start else spec_to_local(
        spec.at, tz
    ) if spec.at else datetime.now()
    until = spec_to_local(spec.until, tz) if spec.until else None
    if spec.freq is not None:
        slots: list[tuple[int, int] | None] = (
            [None]
            if spec.freq in _FINE
            else [_parse_time(t) for t in spec.times]
            if spec.times
            else [(start.hour, start.minute)]
        )
        for slot in slots:
            kwargs: dict[str, object] = {
                "dtstart": start,
                "interval": spec.interval,
            }
            if until is not None:
                kwargs["until"] = until
            if spec.count is not None:
                kwargs["count"] = spec.count
            if spec.weekdays:
                kwargs["byweekday"] = [
                    _WEEKDAYS.index(w) for w in spec.weekdays
                ]
            if spec.month_days:
                kwargs["bymonthday"] = spec.month_days
            if slot is not None:
                kwargs["byhour"], kwargs["byminute"] = slot
                kwargs["bysecond"] = 0
            rs.rrule(rrule(_FREQ_MAP[spec.freq], **kwargs))  # type: ignore[arg-type]
    for extra in [spec.at, *(spec.dates or [])]:
        if extra is not None:
            rs.rdate(spec_to_local(extra, tz))
    return rs


def _occurrences_after(
    spec: ScheduleSpec, tz: ZoneInfo, after_local: datetime, limit: int
) -> list[datetime]:
    rs = _rruleset(spec, tz)
    excluded = set(spec.exclude_dates or [])
    out: list[datetime] = []
    cursor = after_local
    while len(out) < limit:
        occ = rs.after(cursor, inc=False)
        if occ is None:
            break
        cursor = occ
        if occ.date() not in excluded:
            out.append(occ)
    return out


def next_after(
    spec: ScheduleSpec,
    tz: ZoneInfo,
    after_utc: datetime,
    paused_until: datetime | None = None,
) -> datetime | None:
    """Next fire time (naive UTC) strictly after `after_utc` (naive UTC).

    `paused_until` (naive UTC) skips occurrences inside the pause window.
    """
    after_local = utc_to_local(after_utc, tz)
    if paused_until is not None:
        pause_local = utc_to_local(paused_until, tz)
        if pause_local > after_local:
            after_local = pause_local
    occ = _occurrences_after(spec, tz, after_local, 1)
    return local_to_utc(occ[0], tz) if occ else None


def normalize_oneshot(
    spec: ScheduleSpec, now_local: datetime
) -> ScheduleSpec:
    """Fold delay_minutes into `at` relative to `now_local`."""
    if spec.delay_minutes is None:
        return spec
    return spec.model_copy(
        update={
            "at": now_local + timedelta(minutes=spec.delay_minutes),
            "delay_minutes": None,
        }
    )


def is_daily_or_rarer(
    spec: ScheduleSpec, tz: ZoneInfo, now_local: datetime
) -> bool:
    """True for one-shot jobs and patterns whose smallest gap between the
    first upcoming occurrences is >= 24h (=> a fresh session per fire)."""
    occ = _occurrences_after(spec, tz, now_local, MAX_GAP_SAMPLE)
    if len(occ) <= 1:
        return True
    gaps = (b - a for a, b in itertools.pairwise(occ))
    return min(gaps) >= DAILY_GAP


def upcoming(
    spec: ScheduleSpec, tz: ZoneInfo, now_local: datetime, n: int = 3
) -> list[datetime]:
    return _occurrences_after(spec, tz, now_local, n)


def _plural(n: int, forms: tuple[str, str, str]) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return forms[2]
    n %= 10
    if n == 1:
        return forms[0]
    if 2 <= n <= 4:
        return forms[1]
    return forms[2]


def describe(spec: ScheduleSpec) -> str:
    """Short Russian description of the pattern for the UI and tool replies."""
    if spec.at is not None:
        base = f"однократно {spec.at:%d.%m.%Y %H:%M}"
    elif spec.freq is not None:
        forms = _FREQ_RU[spec.freq]
        if spec.freq == "weekly" and spec.weekdays:
            base = "по " + ", ".join(_WEEKDAY_RU[w] for w in spec.weekdays)
            if spec.interval > 1:
                base += (
                    f" каждые {spec.interval} {_plural(spec.interval, forms)}"
                )
        elif spec.freq == "monthly" and spec.month_days:
            days = ", ".join(
                "последнему числу" if d == -1 else str(d)
                for d in spec.month_days
            )
            base = f"по {days} числам месяца"
            if spec.interval > 1:
                base += (
                    f" каждые {spec.interval} {_plural(spec.interval, forms)}"
                )
        elif spec.interval == 1:
            base = _FREQ_SINGLE[spec.freq]
        else:
            base = f"каждые {spec.interval} {_plural(spec.interval, forms)}"
        if spec.times and spec.freq not in _FINE:
            base += " в " + ", ".join(spec.times)
    else:
        base = "по датам: " + ", ".join(
            f"{d:%d.%m %H:%M}" for d in spec.dates or []
        )
    extras: list[str] = []
    if spec.exclude_dates:
        extras.append(
            "кроме " + ", ".join(f"{d:%d.%m}" for d in spec.exclude_dates)
        )
    if spec.count:
        extras.append(
            f"всего {spec.count} {_plural(spec.count, ('раз', 'раза', 'раз'))}"
        )
    if spec.until:
        extras.append(f"до {spec.until:%d.%m.%Y}")
    return base + ((" (" + "; ".join(extras) + ")") if extras else "")
