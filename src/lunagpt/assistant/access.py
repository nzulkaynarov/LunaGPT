"""Membership for Luna AI Assistant: everyone applies, and an adult decides."""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from lunagpt.models import AssistantApplication, AssistantMember, AssistantTimeRequest

ROLES = ("owner", "adult", "kid", "guest")
ADULT_ROLES = ("owner", "adult")
FAMILY_ROLES = ("owner", "adult", "kid")
PERSON_ROLE_TO_MEMBER_ROLE = {"adult": "adult", "kid": "kid", "guest": "guest"}
PENDING = "pending"
REJECTED = "rejected"
EXPIRED = "expired"
PENDING_TTL = timedelta(days=30)
MAX_PENDING_APPLICATIONS = 100


async def get_member(db: AsyncSession, telegram_id: int) -> AssistantMember | None:
    return await db.get(AssistantMember, telegram_id)


async def get_application(db: AsyncSession, telegram_id: int) -> AssistantApplication | None:
    return await db.get(AssistantApplication, telegram_id)


async def submit_application(db: AsyncSession, telegram_id: int, name: str, username: str | None, phone: str) -> bool:
    """Record a new application; False when one is open or the waiting list is full."""
    pending = await db.scalar(
        select(func.count()).select_from(AssistantApplication).where(AssistantApplication.status == PENDING)
    )
    if (pending or 0) >= MAX_PENDING_APPLICATIONS:
        return False
    result = await db.execute(_replace_expired(telegram_id, name, username, phone, PENDING))
    await db.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


def _replace_expired(telegram_id: int, name: str, username: str | None, phone: str, status: str) -> Any:
    """Insert an application; only one that nobody ever answered may be overwritten."""
    values = {"name": name[:100], "username": username, "phone": phone[:32], "status": status}
    statement = insert(AssistantApplication).values(telegram_id=telegram_id, **values)
    return statement.on_conflict_do_update(
        index_elements=["telegram_id"],
        set_={**values, "created_at": func.now()},
        where=AssistantApplication.status == EXPIRED,
    )


async def refuse_rejected_number(
    db: AsyncSession, telegram_id: int, name: str, username: str | None, phone: str
) -> bool:
    """A rejected number stays rejected under a new account, and that account joins the rejected list."""
    rejected = await db.scalar(
        select(AssistantApplication.telegram_id)
        .where(AssistantApplication.phone == phone, AssistantApplication.status == REJECTED)
        .limit(1)
    )
    if rejected is None:
        return False
    await db.execute(_replace_expired(telegram_id, name, username, phone, REJECTED))
    await db.commit()
    return True


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
    """Turn a locked application into membership; the phone number moves with the person."""
    if role not in ROLES:
        raise ValueError(f"Unknown role: {role}")
    member = AssistantMember(
        telegram_id=application.telegram_id,
        name=name[:100],
        role=role,
        phone=application.phone,
        homesec_person_id=person_id,
    )
    db.add(member)
    await db.delete(application)
    await db.commit()
    return member


async def reject(db: AsyncSession, application: AssistantApplication, decided_by: int) -> None:
    application.status = REJECTED
    application.decided_by = decided_by
    application.decided_at = datetime.now(UTC)
    await db.commit()


async def reopen(db: AsyncSession, telegram_id: int) -> AssistantApplication | None:
    """Give a person another chance; the number is forgiven on every account it was refused under."""
    application = await db.get(AssistantApplication, telegram_id, with_for_update=True)
    if application is None or application.status not in (REJECTED, EXPIRED):
        await db.rollback()
        return None
    if application.phone is not None:
        await db.execute(
            delete(AssistantApplication).where(
                AssistantApplication.phone == application.phone,
                AssistantApplication.status == REJECTED,
                AssistantApplication.telegram_id != telegram_id,
            )
        )
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
            phone=member.phone,
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


async def find_by_phone(
    db: AsyncSession, phone: str
) -> tuple[Sequence[AssistantMember], Sequence[AssistantApplication]]:
    members = await db.scalars(select(AssistantMember).where(AssistantMember.phone == phone))
    applications = await db.scalars(
        select(AssistantApplication)
        .where(AssistantApplication.phone == phone)
        .order_by(AssistantApplication.created_at.desc())
    )
    return members.all(), applications.all()


async def expire_applications(db: AsyncSession) -> int:
    """Nobody decided within the window: the request closes, the person may apply again, the number stays."""
    statement = (
        update(AssistantApplication)
        .where(
            AssistantApplication.status == PENDING, AssistantApplication.created_at < datetime.now(UTC) - PENDING_TTL
        )
        .values(status=EXPIRED)
    )
    result = await db.execute(statement)
    await db.commit()
    return int(result.rowcount)  # type: ignore[attr-defined]


def access_ended(member: AssistantMember) -> bool:
    return member.access_until is not None and member.access_until <= datetime.now(UTC)


def age_of(member: AssistantMember, today: date) -> int | None:
    return None if member.birth_year is None else today.year - member.birth_year


def spent_this_month(member: AssistantMember, today: date) -> Decimal:
    return member.spent_usd if member.spend_month == today.replace(day=1) else Decimal(0)


def money_limit_reached(member: AssistantMember, today: date) -> bool:
    return member.money_limit_usd is not None and spent_this_month(member, today) >= member.money_limit_usd


async def record_spend(db: AsyncSession, telegram_id: int, cost: float, today: date) -> None:
    member = await db.get(AssistantMember, telegram_id, with_for_update=True)
    if member is None:
        await db.rollback()
        return
    member.spent_usd = spent_this_month(member, today) + Decimal(str(cost))
    member.spend_month = today.replace(day=1)
    await db.commit()


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
