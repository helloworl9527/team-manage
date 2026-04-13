import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine as real_create_async_engine
import sqlalchemy.ext.asyncio as sqlalchemy_asyncio

_original_create_async_engine = sqlalchemy_asyncio.create_async_engine
sqlalchemy_asyncio.create_async_engine = lambda *args, **kwargs: None

from app.database import Base
from app.models import RedemptionCode, Team
from app.services.redeem_flow import RedeemFlowService
from app.services.team import TeamService

sqlalchemy_asyncio.create_async_engine = _original_create_async_engine


def _discard_task(coro):
    coro.close()
    return None


class PendingInviteCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = real_create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_total_available_seats_subtracts_pending_invites(self):
        async with self.session_factory() as session:
            session.add_all(
                [
                    Team(
                        email="full@example.com",
                        access_token_encrypted="enc",
                        account_id="acct-full",
                        team_name="Full By Invite",
                        current_members=4,
                        pending_invites=1,
                        max_members=5,
                        status="active",
                    ),
                    Team(
                        email="available@example.com",
                        access_token_encrypted="enc",
                        account_id="acct-available",
                        team_name="Still Available",
                        current_members=2,
                        pending_invites=1,
                        max_members=5,
                        status="active",
                    ),
                    Team(
                        email="expired@example.com",
                        access_token_encrypted="enc",
                        account_id="acct-expired",
                        team_name="Expired Team",
                        current_members=0,
                        pending_invites=0,
                        max_members=5,
                        status="expired",
                    ),
                ]
            )
            await session.commit()

            seats = await TeamService().get_total_available_seats(session)

            self.assertEqual(seats, 2)

    async def test_select_team_auto_skips_team_filled_by_pending_invites(self):
        async with self.session_factory() as session:
            blocked_team = Team(
                email="blocked@example.com",
                access_token_encrypted="enc",
                account_id="acct-blocked",
                team_name="Blocked Team",
                current_members=4,
                pending_invites=1,
                max_members=5,
                status="active",
            )
            available_team = Team(
                email="available@example.com",
                access_token_encrypted="enc",
                account_id="acct-available",
                team_name="Available Team",
                current_members=3,
                pending_invites=0,
                max_members=5,
                status="active",
            )
            session.add_all([blocked_team, available_team])
            await session.commit()

            result = await RedeemFlowService().select_team_auto(session)

            self.assertTrue(result["success"])
            self.assertEqual(result["team_id"], available_team.id)

    async def test_add_team_member_rejects_full_team_when_pending_invites_fill_capacity(self):
        async with self.session_factory() as session:
            team = Team(
                email="owner@example.com",
                access_token_encrypted="enc",
                account_id="acct-owner",
                team_name="Pending Full Team",
                current_members=4,
                pending_invites=1,
                max_members=5,
                status="active",
            )
            session.add(team)
            await session.commit()

            service = TeamService()
            service.ensure_access_token = AsyncMock(return_value="token")
            service.chatgpt_service.send_invite = AsyncMock(
                return_value={"success": True, "data": {"account_invites": [{"email": "new@example.com"}]}}
            )

            result = await service.add_team_member(team.id, "new@example.com", session)

            self.assertFalse(result["success"])
            self.assertIn("已满", result["error"])
            service.chatgpt_service.send_invite.assert_not_called()

            refreshed_team = await session.scalar(select(Team).where(Team.id == team.id))
            self.assertEqual(refreshed_team.status, "full")

    async def test_redeem_flow_reserves_pending_invite_slot_after_success(self):
        async with self.session_factory() as session:
            team = Team(
                email="owner@example.com",
                access_token_encrypted="enc",
                account_id="acct-main",
                team_name="Main Team",
                current_members=3,
                pending_invites=1,
                max_members=5,
                status="active",
            )
            code = RedemptionCode(code="ISSUE10", status="unused")
            session.add_all([team, code])
            await session.commit()

            service = RedeemFlowService()
            service.team_service.sync_team_info = AsyncMock(return_value={"success": True, "member_emails": []})
            service.team_service.ensure_access_token = AsyncMock(return_value="token")
            service.chatgpt_service.send_invite = AsyncMock(
                return_value={"success": True, "data": {"account_invites": [{"email": "user@example.com"}]}}
            )

            with patch("app.services.redeem_flow.asyncio.create_task", new=_discard_task):
                result = await service.redeem_and_join_team(
                    email="user@example.com",
                    code="ISSUE10",
                    team_id=team.id,
                    db_session=session,
                )

            self.assertTrue(result["success"])

            refreshed_team = await session.scalar(select(Team).where(Team.id == team.id))
            self.assertEqual(refreshed_team.current_members, 3)
            self.assertEqual(refreshed_team.pending_invites, 2)
            self.assertEqual(refreshed_team.status, "full")


if __name__ == "__main__":
    unittest.main()
