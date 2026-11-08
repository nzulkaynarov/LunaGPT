"""Membership for Luna AI Assistant: nobody gets in without a single-use invitation."""

import hashlib
import secrets
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from lunagpt.models import AssistantInvite, AssistantMember, AssistantTimeRequest

ROLES = ("owner", "adult", "kid", "guest")
ADULT_ROLES = ("owner", "adult")
DAILY_REQUEST_LIMITS = {"guest": 20}
INVITE_VALID_HOURS = 72
DEFAULT_GUEST_ACCESS_HOURS = 24
PERSON_ROLE_TO_MEMBER_ROLE = {"adult": "adult", "kid": "kid", "guest": "guest"}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def can_invite(inviter_role: str, role: str) -> bool:
    """Adults may invite guests; only the owner adds family members."""
    if inviter_role == "owner":
        return role in ROLES
    return inviter_role == "adult" and role == "guest"


async def create_invite(
    db: AsyncSession, name: str, role: str, access_hours: int | None = None, person_id: int | None = None
) -> str:
    """Create an invitation and return its token, which is not stored."""
    if role not in ROLES:
        raise ValueError(f"Unknown role: {role}")
    if role == "guest" and access_hours is None and person_id is None:
        access_hours = DEFAULT_GUEST_ACCESS_HOURS
    token = secrets.token_urlsafe(16)
    db.add(
        AssistantInvite(
            token_hash=hash_token(token),
            name=name,
            role=role,
            invite_expires_at=datetime.now(UTC) + timedelta(hours=INVITE_VALID_HOURS),
            access_hours=access_hours if role == "guest" else None,
            homesec_person_id=person_id,
        )
    )
    await db.commit()
    return token


async def redeem_invite(db: AsyncSession, token: str, telegram_id: int) -> AssistantMember | None:
    """Turn a valid unused invitation into membership."""
    now = datetime.now(UTC)
    stmt = select(AssistantInvite).where(AssistantInvite.token_hash == hash_token(token)).with_for_update()
    invite = (await db.execute(stmt)).scalar_one_or_none()
    if invite is None or invite.used_at is not None or invite.invite_expires_at <= now:
        await db.rollback()
        return None
    invite.used_at = now
    expires_at = now + timedelta(hours=invite.access_hours) if invite.access_hours else None
    member = await db.get(AssistantMember, telegram_id)
    if member is None:
        member = AssistantMember(
            telegram_id=telegram_id,
            name=invite.name,
            role=invite.role,
            expires_at=expires_at,
            requests_today=0,
            homesec_person_id=invite.homesec_person_id,
        )
        db.add(member)
    else:
        member.name, member.expires_at = invite.name, expires_at
        member.homesec_person_id = invite.homesec_person_id
        if member.role != "owner":  # linking the owner's own Pi profile must not demote them
            member.role = invite.role
    await db.commit()
    return member


async def get_member(db: AsyncSession, telegram_id: int) -> AssistantMember | None:
    """Return the member unless they are unknown or their access has expired."""
    member = await db.get(AssistantMember, telegram_id)
    if member is None or (member.expires_at is not None and member.expires_at <= datetime.now(UTC)):
        return None
    return member


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


async def remove_member(db: AsyncSession, telegram_id: int) -> bool:
    result = await db.execute(delete(AssistantMember).where(AssistantMember.telegram_id == telegram_id))
    await db.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


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
