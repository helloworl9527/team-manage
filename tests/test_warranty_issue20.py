import sqlite3
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine as real_create_async_engine
import sqlalchemy.ext.asyncio as sqlalchemy_asyncio

_original_create_async_engine = sqlalchemy_asyncio.create_async_engine
sqlalchemy_asyncio.create_async_engine = lambda *args, **kwargs: None

from app.database import Base
from app.db_migrations import repair_warranty_timestamps
from app.models import RedemptionCode, Team
from app.services.redeem_flow import RedeemFlowService
from app.services.warranty import WarrantyService
from app.utils.time_utils import get_now

sqlalchemy_asyncio.create_async_engine = _original_create_async_engine


def _discard_task(coro):
    coro.close()
    return None


class WarrantyIssue20Tests(unittest.IsolatedAsyncioTestCase):
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

    async def test_repeated_warranty_redemption_preserves_original_timestamps(self):
        async with self.session_factory() as session:
            first_team = Team(
                email="owner1@example.com",
                access_token_encrypted="enc",
                account_id="acct-1",
                team_name="Team One",
                current_members=0,
                max_members=6,
                status="active",
            )
            second_team = Team(
                email="owner2@example.com",
                access_token_encrypted="enc",
                account_id="acct-2",
                team_name="Team Two",
                current_members=0,
                max_members=6,
                status="active",
            )
            code = RedemptionCode(
                code="WARRANTY20",
                status="unused",
                has_warranty=True,
                warranty_days=30,
            )
            session.add_all([first_team, second_team, code])
            await session.commit()

            service = RedeemFlowService()
            service.select_team_auto = AsyncMock(return_value={"success": True, "team_id": first_team.id, "error": None})
            service.team_service.sync_team_info = AsyncMock(return_value={"success": True, "member_emails": []})
            service.team_service.ensure_access_token = AsyncMock(return_value="token")
            service.chatgpt_service.send_invite = AsyncMock(
                return_value={"success": True, "data": {"account_invites": [{"email": "user@example.com"}]}}
            )

            with patch("app.services.redeem_flow.asyncio.create_task", new=_discard_task):
                first_result = await service.redeem_and_join_team(
                    email="user@example.com",
                    code="WARRANTY20",
                    team_id=first_team.id,
                    db_session=session,
                )

            self.assertTrue(first_result["success"])
            self.assertIn("team_info", first_result)
            self.assertIsNotNone(first_result["team_info"].get("redeemed_at"))
            self.assertIsNotNone(first_result["team_info"].get("warranty_expires_at"))

            first_code = await session.scalar(
                select(RedemptionCode).where(RedemptionCode.code == "WARRANTY20")
            )
            original_used_at = first_code.used_at
            original_expiry = first_code.warranty_expires_at

            self.assertIsNotNone(original_used_at)
            self.assertEqual(original_expiry, original_used_at + timedelta(days=30))

            first_team.status = "banned"
            second_team.status = "active"
            await session.commit()

            with patch("app.services.redeem_flow.asyncio.create_task", new=_discard_task):
                second_result = await service.redeem_and_join_team(
                    email="user@example.com",
                    code="WARRANTY20",
                    team_id=second_team.id,
                    db_session=session,
                )

            self.assertTrue(second_result["success"])

            updated_code = await session.scalar(
                select(RedemptionCode).where(RedemptionCode.code == "WARRANTY20")
            )
            self.assertEqual(updated_code.used_at, original_used_at)
            self.assertEqual(updated_code.warranty_expires_at, original_expiry)

    async def test_validate_warranty_reuse_allows_expired_team(self):
        async with self.session_factory() as session:
            expired_team = Team(
                email="expired-owner@example.com",
                access_token_encrypted="enc",
                account_id="acct-expired",
                team_name="Expired Team",
                current_members=0,
                max_members=6,
                status="expired",
            )
            code = RedemptionCode(
                code="WARRANTY-EXPIRED",
                status="used",
                has_warranty=True,
                warranty_days=30,
                warranty_expires_at=get_now() + timedelta(days=7),
            )
            session.add_all([expired_team, code])
            await session.flush()

            from app.models import RedemptionRecord
            session.add(
                RedemptionRecord(
                    email="user@example.com",
                    code="WARRANTY-EXPIRED",
                    team_id=expired_team.id,
                    account_id=expired_team.account_id,
                    is_warranty_redemption=True,
                    redeemed_at=get_now() - timedelta(days=3),
                )
            )
            await session.commit()

            result = await WarrantyService().validate_warranty_reuse(
                session,
                code="WARRANTY-EXPIRED",
                email="user@example.com",
            )

            self.assertTrue(result["success"])
            self.assertTrue(result["can_reuse"])
            self.assertIn("封禁或过期", result["reason"])

    async def test_warranty_auto_selects_team_closest_after_warranty_expiry(self):
        async with self.session_factory() as session:
            old_team = Team(
                email="old-owner@example.com",
                access_token_encrypted="enc",
                account_id="acct-old",
                team_name="Old Team",
                current_members=0,
                max_members=6,
                status="banned",
            )
            candidate_far = Team(
                email="far@example.com",
                access_token_encrypted="enc",
                account_id="acct-far",
                team_name="Far Team",
                current_members=0,
                max_members=6,
                status="active",
                expires_at=get_now() + timedelta(days=20),
            )
            candidate_best = Team(
                email="best@example.com",
                access_token_encrypted="enc",
                account_id="acct-best",
                team_name="Best Team",
                current_members=1,
                pending_invites=0,
                max_members=6,
                status="active",
                expires_at=get_now() + timedelta(days=11),
            )
            candidate_too_early = Team(
                email="early@example.com",
                access_token_encrypted="enc",
                account_id="acct-early",
                team_name="Early Team",
                current_members=0,
                max_members=6,
                status="active",
                expires_at=get_now() + timedelta(days=8),
            )
            code = RedemptionCode(
                code="WARRANTY-AUTO",
                status="used",
                has_warranty=True,
                warranty_days=30,
                warranty_expires_at=get_now() + timedelta(days=10),
            )
            session.add_all([old_team, candidate_far, candidate_best, candidate_too_early, code])
            await session.flush()

            from app.models import RedemptionRecord
            session.add(
                RedemptionRecord(
                    email="user@example.com",
                    code="WARRANTY-AUTO",
                    team_id=old_team.id,
                    account_id=old_team.account_id,
                    is_warranty_redemption=True,
                    redeemed_at=get_now() - timedelta(days=5),
                )
            )
            await session.commit()

            service = RedeemFlowService()
            result = await service._auto_select_team_for_redemption(
                session,
                code="WARRANTY-AUTO",
                email="user@example.com",
            )

            self.assertTrue(result["success"])
            self.assertEqual(result["team_id"], candidate_best.id)

    async def test_warranty_auto_selects_closest_team_for_dominicross_real_like_data(self):
        async with self.session_factory() as session:
            user_email = "dominicross004@gmail.com"

            old_team = Team(
                email="vvaekn90072x@outlook.com",
                access_token_encrypted="enc",
                account_id="acct-old-real-like",
                team_name="vvaekn90072x",
                current_members=3,
                pending_invites=1,
                max_members=6,
                status="expired",
            )
            team_20260518 = Team(
                email="yannickdenis@bahlil.tech",
                access_token_encrypted="enc",
                account_id="acct-20260518",
                team_name="yannickdenis",
                current_members=3,
                pending_invites=1,
                max_members=6,
                status="active",
                expires_at=get_now() + timedelta(days=28),
            )
            team_20260515 = Team(
                email="jheckys@otpmu.web.id",
                access_token_encrypted="enc",
                account_id="acct-20260515",
                team_name="Otpmu",
                current_members=2,
                pending_invites=0,
                max_members=6,
                status="active",
                expires_at=get_now() + timedelta(days=25),
            )
            team_20260507 = Team(
                email="vvaekn90072x@outlook.com",
                access_token_encrypted="enc",
                account_id="acct-20260507",
                team_name="vvaekn90072x",
                current_members=3,
                pending_invites=1,
                max_members=6,
                status="active",
                expires_at=get_now() + timedelta(days=17),
            )
            code = RedemptionCode(
                code="WARRANTY-DOMINI-AUTO",
                status="used",
                has_warranty=True,
                warranty_days=30,
                warranty_expires_at=get_now() + timedelta(days=16),
            )
            session.add_all([old_team, team_20260518, team_20260515, team_20260507, code])
            await session.flush()

            from app.models import RedemptionRecord
            session.add(
                RedemptionRecord(
                    email=user_email,
                    code=code.code,
                    team_id=old_team.id,
                    account_id=old_team.account_id,
                    is_warranty_redemption=True,
                    redeemed_at=get_now() - timedelta(days=5),
                )
            )
            await session.commit()

            service = RedeemFlowService()
            result = await service._auto_select_team_for_redemption(
                session,
                code=code.code,
                email=user_email,
            )

            self.assertTrue(result["success"])
            self.assertEqual(result["team_id"], team_20260507.id)

    async def test_warranty_auto_selects_closest_team_when_all_expire_before_warranty(self):
        async with self.session_factory() as session:
            user_email = "dominicross004@gmail.com"
            warranty_deadline = datetime(2026, 6, 1, 0, 0, 0)

            old_team = Team(
                email="old-owner@example.com",
                access_token_encrypted="enc",
                account_id="acct-old-none",
                team_name="Old Team",
                current_members=0,
                max_members=6,
                status="expired",
            )
            # 示例场景：质保 6/1，Team 到期 5/13 与 5/16，应选 5/16
            team_1 = Team(
                email="yannickdenis@bahlil.tech",
                access_token_encrypted="enc",
                account_id="acct-none-1",
                team_name="Team 1",
                current_members=3,
                pending_invites=1,
                max_members=6,
                status="active",
                expires_at=datetime(2026, 5, 13, 0, 0, 0),
            )
            team_2 = Team(
                email="jheckys@otpmu.web.id",
                access_token_encrypted="enc",
                account_id="acct-none-2",
                team_name="Team 2",
                current_members=2,
                pending_invites=0,
                max_members=6,
                status="active",
                expires_at=datetime(2026, 5, 16, 0, 0, 0),
            )
            team_3 = Team(
                email="vvaekn90072x@outlook.com",
                access_token_encrypted="enc",
                account_id="acct-none-3",
                team_name="Team 3",
                current_members=3,
                pending_invites=1,
                max_members=6,
                status="active",
                expires_at=datetime(2026, 4, 28, 0, 0, 0),
            )
            code = RedemptionCode(
                code="WARRANTY-NO-CANDIDATE",
                status="used",
                has_warranty=True,
                warranty_days=30,
                warranty_expires_at=warranty_deadline,
            )
            session.add_all([old_team, team_1, team_2, team_3, code])
            await session.flush()

            from app.models import RedemptionRecord
            session.add(
                RedemptionRecord(
                    email=user_email,
                    code=code.code,
                    team_id=old_team.id,
                    account_id=old_team.account_id,
                    is_warranty_redemption=True,
                    redeemed_at=datetime(2026, 5, 1, 0, 0, 0),
                )
            )
            await session.commit()

            service = RedeemFlowService()
            result = await service._auto_select_team_for_redemption(
                session,
                code=code.code,
                email=user_email,
            )

            self.assertTrue(result["success"])
            self.assertEqual(result["team_id"], team_2.id)

    async def test_warranty_auto_select_rejects_when_warranty_expiry_hits_now(self):
        async with self.session_factory() as session:
            boundary_now = get_now()
            user_email = "dominicross004@gmail.com"

            old_team = Team(
                email="old-owner@example.com",
                access_token_encrypted="enc",
                account_id="acct-boundary-old",
                team_name="Boundary Old Team",
                current_members=0,
                max_members=6,
                status="banned",
            )
            candidate = Team(
                email="candidate@example.com",
                access_token_encrypted="enc",
                account_id="acct-boundary-candidate",
                team_name="Boundary Candidate",
                current_members=0,
                max_members=6,
                status="active",
                expires_at=boundary_now + timedelta(days=1),
            )
            code = RedemptionCode(
                code="WARRANTY-BOUNDARY-NOW",
                status="used",
                has_warranty=True,
                warranty_days=30,
                warranty_expires_at=boundary_now,
            )
            session.add_all([old_team, candidate, code])
            await session.flush()

            from app.models import RedemptionRecord
            session.add(
                RedemptionRecord(
                    email=user_email,
                    code=code.code,
                    team_id=old_team.id,
                    account_id=old_team.account_id,
                    is_warranty_redemption=True,
                    redeemed_at=boundary_now - timedelta(days=10),
                )
            )
            await session.commit()

            service = RedeemFlowService()
            with patch("app.services.warranty.get_now", return_value=boundary_now):
                result = await service._auto_select_team_for_redemption(
                    session,
                    code=code.code,
                    email=user_email,
                )

            self.assertFalse(result["success"])
            self.assertIn("质保已过期", result["error"])

    async def test_warranty_auto_select_prioritizes_after_deadline_when_available(self):
        async with self.session_factory() as session:
            user_email = "dominicross004@gmail.com"
            warranty_deadline = datetime(2026, 6, 1, 0, 0, 0)

            old_team = Team(
                email="old-owner@example.com",
                access_token_encrypted="enc",
                account_id="acct-priority-old",
                team_name="Old Team",
                current_members=0,
                max_members=6,
                status="banned",
            )
            # 更接近但早于质保截止（不应被优先）
            before_team = Team(
                email="before@example.com",
                access_token_encrypted="enc",
                account_id="acct-priority-before",
                team_name="Before Team",
                current_members=0,
                max_members=6,
                status="active",
                expires_at=datetime(2026, 5, 31, 0, 0, 0),
            )
            # 满足“>质保截止”且正差值最小（应被优先）
            after_team = Team(
                email="after@example.com",
                access_token_encrypted="enc",
                account_id="acct-priority-after",
                team_name="After Team",
                current_members=0,
                max_members=6,
                status="active",
                expires_at=datetime(2026, 6, 2, 0, 0, 0),
            )
            code = RedemptionCode(
                code="WARRANTY-PRIORITY-AFTER",
                status="used",
                has_warranty=True,
                warranty_days=30,
                warranty_expires_at=warranty_deadline,
            )
            session.add_all([old_team, before_team, after_team, code])
            await session.flush()

            from app.models import RedemptionRecord
            session.add(
                RedemptionRecord(
                    email=user_email,
                    code=code.code,
                    team_id=old_team.id,
                    account_id=old_team.account_id,
                    is_warranty_redemption=True,
                    redeemed_at=datetime(2026, 5, 10, 0, 0, 0),
                )
            )
            await session.commit()

            service = RedeemFlowService()
            result = await service._auto_select_team_for_redemption(
                session,
                code=code.code,
                email=user_email,
            )

            self.assertTrue(result["success"])
            self.assertEqual(result["team_id"], after_team.id)

    async def test_warranty_status_falls_back_to_first_redemption_record(self):
        async with self.session_factory() as session:
            team = Team(
                email="owner@example.com",
                access_token_encrypted="enc",
                account_id="acct-main",
                team_name="Main Team",
                current_members=0,
                max_members=6,
                status="banned",
            )
            code = RedemptionCode(
                code="WARRANTY-CHECK",
                status="used",
                has_warranty=True,
                warranty_days=30,
                used_at=None,
                warranty_expires_at=None,
            )
            session.add_all([team, code])
            await session.flush()

            first_redeem_at = get_now() - timedelta(days=12)
            second_redeem_at = get_now() - timedelta(days=2)
            from app.models import RedemptionRecord

            session.add_all(
                [
                    RedemptionRecord(
                        email="user@example.com",
                        code="WARRANTY-CHECK",
                        team_id=team.id,
                        account_id=team.account_id,
                        redeemed_at=first_redeem_at,
                        is_warranty_redemption=True,
                    ),
                    RedemptionRecord(
                        email="user@example.com",
                        code="WARRANTY-CHECK",
                        team_id=team.id,
                        account_id=team.account_id,
                        redeemed_at=second_redeem_at,
                        is_warranty_redemption=True,
                    ),
                ]
            )
            await session.commit()

            service = WarrantyService()
            result = await service.check_warranty_status(session, code="WARRANTY-CHECK")

            self.assertTrue(result["success"])
            self.assertEqual(
                result["warranty_expires_at"],
                (first_redeem_at + timedelta(days=30)).isoformat(),
            )

    async def test_redeem_success_returns_team_warranty_and_redeem_timestamps(self):
        async with self.session_factory() as session:
            team = Team(
                email="owner3@example.com",
                access_token_encrypted="enc",
                account_id="acct-3",
                team_name="Team Three",
                current_members=0,
                max_members=6,
                status="active",
                expires_at=get_now() + timedelta(days=90),
            )
            code = RedemptionCode(
                code="WARRANTY-TIME-FIELDS",
                status="unused",
                has_warranty=True,
                warranty_days=30,
            )
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
                    code=code.code,
                    team_id=team.id,
                    db_session=session,
                )

            self.assertTrue(result["success"])
            team_info = result["team_info"]
            self.assertIsNotNone(team_info.get("expires_at"))
            self.assertIsNotNone(team_info.get("warranty_expires_at"))
            self.assertIsNotNone(team_info.get("redeemed_at"))

    def test_repair_warranty_timestamps_uses_first_redemption_record(self):
        conn = sqlite3.connect(":memory:")
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE redemption_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                has_warranty BOOLEAN DEFAULT 0,
                warranty_days INTEGER DEFAULT 30,
                used_at DATETIME,
                warranty_expires_at DATETIME
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE redemption_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                redeemed_at DATETIME
            )
            """
        )

        cursor.execute(
            """
            INSERT INTO redemption_codes (code, has_warranty, warranty_days, used_at, warranty_expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("FIXME20", 1, 30, "2026-03-10 10:00:00", "2026-04-09 10:00:00"),
        )
        cursor.executemany(
            """
            INSERT INTO redemption_records (code, redeemed_at)
            VALUES (?, ?)
            """,
            [
                ("FIXME20", "2026-03-01 08:00:00"),
                ("FIXME20", "2026-03-10 10:00:00"),
            ],
        )

        repaired = repair_warranty_timestamps(cursor)
        conn.commit()

        self.assertEqual(repaired, 1)

        cursor.execute(
            "SELECT used_at, warranty_expires_at FROM redemption_codes WHERE code = ?",
            ("FIXME20",),
        )
        used_at, warranty_expires_at = cursor.fetchone()
        self.assertEqual(used_at, "2026-03-01 08:00:00")
        self.assertEqual(warranty_expires_at, "2026-03-31 08:00:00")
        conn.close()


if __name__ == "__main__":
    unittest.main()
