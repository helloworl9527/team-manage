import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine as real_create_async_engine
import sqlalchemy.ext.asyncio as sqlalchemy_asyncio

_original_create_async_engine = sqlalchemy_asyncio.create_async_engine
sqlalchemy_asyncio.create_async_engine = lambda *args, **kwargs: None

from app.database import Base
from app.models import RedemptionCode, RedemptionRecord, Team
from app.routes.redeem import RedeemRequest, confirm_redeem

sqlalchemy_asyncio.create_async_engine = _original_create_async_engine


class RedeemErrorRecoveryTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_confirm_redeem_recovers_when_service_raises_but_record_exists(self):
        async with self.session_factory() as session:
            team = Team(
                email="owner@example.com",
                access_token_encrypted="enc",
                account_id="acct-recover-1",
                team_name="Recover Team",
                current_members=1,
                max_members=5,
                status="active",
            )
            code = RedemptionCode(code="RECOVER-RAISE", status="used", used_by_email="user@example.com")
            session.add_all([team, code])
            await session.flush()

            record = RedemptionRecord(
                email="user@example.com",
                code="RECOVER-RAISE",
                team_id=team.id,
                account_id=team.account_id,
                redeemed_at=team.created_at,
            )
            session.add(record)
            await session.commit()

            with patch(
                "app.routes.redeem.redeem_flow_service.redeem_and_join_team",
                new=AsyncMock(side_effect=RuntimeError("上游接口超时")),
            ):
                response = await confirm_redeem(
                    RedeemRequest(email="user@example.com", code="RECOVER-RAISE", team_id=team.id),
                    db=session,
                )

            self.assertTrue(response.success)
            self.assertIn("自动修正失败提示", response.message or "")
            self.assertEqual((response.team_info or {}).get("team_name"), "Recover Team")

    async def test_confirm_redeem_recovers_with_case_insensitive_email_match(self):
        async with self.session_factory() as session:
            team = Team(
                email="owner@example.com",
                access_token_encrypted="enc",
                account_id="acct-recover-2",
                team_name="Case Team",
                current_members=1,
                max_members=5,
                status="active",
            )
            code = RedemptionCode(code="RECOVER-CASE", status="unused")
            session.add_all([team, code])
            await session.flush()

            record = RedemptionRecord(
                email="user@example.com",
                code="RECOVER-CASE",
                team_id=team.id,
                account_id=team.account_id,
                redeemed_at=team.created_at,
            )
            session.add(record)
            await session.commit()

            with patch(
                "app.routes.redeem.redeem_flow_service.redeem_and_join_team",
                new=AsyncMock(return_value={"success": False, "error": "网络超时"}),
            ):
                response = await confirm_redeem(
                    RedeemRequest(email="User@Example.com", code="RECOVER-CASE", team_id=team.id),
                    db=session,
                )

            self.assertTrue(response.success)
            self.assertEqual((response.team_info or {}).get("team_name"), "Case Team")

    async def test_confirm_redeem_failure_returns_root_cause_feedback(self):
        async with self.session_factory() as session:
            with patch(
                "app.routes.redeem.redeem_flow_service.redeem_and_join_team",
                new=AsyncMock(
                    return_value={
                        "success": False,
                        "error": "兑换失败次数过多。最后报错: maximum number of seats",
                    }
                ),
            ):
                with self.assertRaises(HTTPException) as ctx:
                    await confirm_redeem(
                        RedeemRequest(email="user@example.com", code="ERR-FEEDBACK", team_id=1),
                        db=session,
                    )

            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn("根因:", ctx.exception.detail)
            self.assertIn("可能原因:", ctx.exception.detail)
            self.assertIn("maximum number of seats", ctx.exception.detail)

    async def test_confirm_redeem_exception_returns_root_cause_feedback(self):
        async with self.session_factory() as session:
            with patch(
                "app.routes.redeem.redeem_flow_service.redeem_and_join_team",
                new=AsyncMock(side_effect=RuntimeError("database is locked")),
            ):
                with self.assertRaises(HTTPException) as ctx:
                    await confirm_redeem(
                        RedeemRequest(email="user@example.com", code="ERR-EXCEPTION", team_id=1),
                        db=session,
                    )

            self.assertEqual(ctx.exception.status_code, 500)
            self.assertIn("根因:", ctx.exception.detail)
            self.assertIn("database is locked", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
