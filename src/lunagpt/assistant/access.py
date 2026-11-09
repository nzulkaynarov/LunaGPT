"""Membership for Luna AI Assistant: everyone applies, and an adult decides."""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from lunagpt.models import AssistantApplication, AssistantMember, AssistantTimeRequest

ROLES = ("owner", "adult", "kid", "guest")
ADULT_ROLES = ("owner", "adult")
FAMILY_ROLES = ("owner", "adult", "kid")
DAILY_REQUEST_LIMITS = {"guest": 20}
PERSON_ROLE_TO_MEMBER_ROLE = {"adult": "adult", "kid": "kid", "guest": "guest"}
PENDING = "pending"
REJECTED = "rejected"
PENDING_TTL = timedelta(days=30)
MAX_PENDING_APPLICATIONS = 100


async def get_member(db: AsyncSession, telegram_id: int) -> AssistantMember | None:
    return await db.get(AssistantMember, telegram_id)


async def get_application(db: AsyncSession, telegram_id: int) -> AssistantApplication | None:
    return await db.get(AssistantApplication, telegram_id)


async def submit_application(db: AsyncSession, telegram_id: int, name: str, username: str | None, phone: str) -> bool:
    """Record a new application; False when one exists or the waiting list is full."""
    pending = await db.scalar(
        select(func.count()).select_from(AssistantApplication).where(AssistantApplication.status == PENDING)
    )
    if (pending or 0) >= MAX_PENDING_APPLICATIONS:
        return False
    statement = (
        insert(AssistantApplication)
        .values(telegram_id=telegram_id, name=name[:100], username=username, phone=phone[:32], status=PENDING)
        .on_conflict_do_nothing(index_elements=["telegram_id"])
    )
    result = await db.execute(statement)
    await db.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def lock_pending(db: AsyncSession, telegram_id: int) -> AssistantApplication | None:
    """Lock an undecided application, so two adults pressing at once cannot both decide it."""
    statement = (
        select(AssistantApplication)
        .where(AssistantApplication.telegram_id == telegram_id, AssistantApplication.status == PENDING)
        .with_for_update()
    )
    return await db.scalar(statement)


async def admit(
    db: AsyncSession, application: AssistantApplication, name: str, role: str, person_id: int | None
) -> AssistantMember:
    """Turn a locked application into membership; the phone number goes with the application."""
    if role not in ROLES:
        raise ValueError(f"Unknown role: {role}")
    member = AssistantMember(
        telegram_id=application.telegram_id, name=name[:100], role=role, requests_today=0, homesec_person_id=person_id
    )
    db.add(member)
    await db.delete(application)
    await db.commit()
    return member


async def reject(db: AsyncSession, application: AssistantApplication, decided_by: int) -> None:
    application.status = REJECTED
    application.phone = None
    application.decided_by = decided_by
    application.decided_at = datetime.now(UTC)
    await db.commit()


async def reopen(db: AsyncSession, telegram_id: int) -> AssistantApplication | None:
    """Give a rejected person another chance; only an adult's button leads here."""
    application = await db.get(AssistantApplication, telegram_id, with_for_update=True)
    if application is None or application.status != REJECTED:
        await db.rollback()
        return None
    application.status = PENDING
    application.decided_by = None
    application.decided_at = None
    application.created_at = datetime.now(UTC)
    await db.commit()
    return application


async def exclude(db: AsyncSession, member: AssistantMember, decided_by: int) -> None:
    """Remove a member, erase their memory by cascade, and stop them from applying again."""
    db.add(
        AssistantApplication(
            telegram_id=member.telegram_id,
            name=member.name,
            status=REJECTED,
            decided_by=decided_by,
            decided_at=datetime.now(UTC),
        )
    )
    await db.delete(member)
    await db.commit()


async def list_applications(db: AsyncSession, status: str) -> Sequence[AssistantApplication]:
    statement = (
        select(AssistantApplication)
        .where(AssistantApplication.status == status)
        .order_by(AssistantApplication.created_at.desc())
    )
    return (await db.scalars(statement)).all()


async def expire_applications(db: AsyncSession) -> int:
    """Nobody decided within the window, so the request and its phone number are dropped."""
    statement = delete(AssistantApplication).where(
        AssistantApplication.status == PENDING, AssistantApplication.created_at < datetime.now(UTC) - PENDING_TTL
    )
    result = await db.execute(statement)
    await db.commit()
    return int(result.rowcount)  # type: ignore[attr-defined]


async def consume_daily_request(db: AsyncSession, member: AssistantMember, today: date) -> bool:
    """Count one request; False when the member's daily allowance is spent."""
    if member.usage_day != today:
        member.usage_day = today
        member.requests_today = 0
    limit = DAILY_REQUEST_LIMITS.get(member.role)
    if limit is not None and member.requests_today >= limit:
        await db.commit()
        return False
    member.requests_today += 1
    await db.commit()
    return True


async def list_members(db: AsyncSession) -> Sequence[AssistantMember]:
    return (await db.execute(select(AssistantMember).order_by(AssistantMember.created_at))).scalars().all()


async def reconcile_family(db: AsyncSession, people: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """Apply the Pi's family to linked members; return (person_id, telegram_id) pairs the Pi lacks."""
    by_id = {int(p["id"]): p for p in people}
    missing_links: list[tuple[int, int]] = []
    for member in await list_members(db):
        if member.homesec_person_id is None:
            continue
        person = by_id.get(member.homesec_person_id)
        if person is None:
            if member.role != "owner":
                await db.delete(member)
            continue
        member.name = person["name"]
        if member.role != "owner":
            member.role = PERSON_ROLE_TO_MEMBER_ROLE.get(person["role"], "guest")
        if person.get("telegram_id") != member.telegram_id:
            missing_links.append((member.homesec_person_id, member.telegram_id))
    await db.commit()
    return missing_links


async def track_time_request(db: AsyncSession, request_id: int, telegram_id: int) -> None:
    db.add(AssistantTimeRequest(request_id=request_id, telegram_id=telegram_id))
    await db.commit()


async def pending_time_requests(db: AsyncSession) -> Sequence[AssistantTimeRequest]:
    return (await db.execute(select(AssistantTimeRequest))).scalars().all()


async def finish_time_request(db: AsyncSession, request_id: int) -> None:
    await db.execute(delete(AssistantTimeRequest).where(AssistantTimeRequest.request_id == request_id))
    await db.commit()


async def adult_chat_ids(db: AsyncSession) -> list[int]:
    stmt = select(AssistantMember.telegram_id).where(AssistantMember.role.in_(ADULT_ROLES))
    return list((await db.execute(stmt)).scalars().all())
