import json
import unittest
import asyncio
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine as real_create_async_engine
import sqlalchemy.ext.asyncio as sqlalchemy_asyncio

_original_create_async_engine = sqlalchemy_asyncio.create_async_engine
sqlalchemy_asyncio.create_async_engine = lambda *args, **kwargs: None

from app.database import Base
from app.models import Team
from app.services.team import TeamService
from app.routes import admin as admin_routes

sqlalchemy_asyncio.create_async_engine = _original_create_async_engine


class AdminImportRefreshRegressionTests(unittest.IsolatedAsyncioTestCase):
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

        if hasattr(admin_routes, "_refresh_all_jobs"):
            admin_routes._refresh_all_jobs.clear()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_import_single_treats_existing_account_as_idempotent_success(self):
        async with self.session_factory() as session:
            existing = Team(
                email="owner@example.com",
                access_token_encrypted="enc",
                account_id="acct-existing",
                team_name="Existing Team",
                status="active",
            )
            session.add(existing)
            await session.commit()

            service = TeamService()
            service.jwt_parser.is_token_expired = AsyncMock(return_value=False)
            service.jwt_parser.extract_email = AsyncMock(return_value="owner@example.com")
            service.chatgpt_service.get_account_info = AsyncMock(
                return_value={
                    "success": True,
                    "accounts": [
                        {
                            "account_id": "acct-existing",
                            "name": "Existing Team",
                            "plan_type": "team",
                            "subscription_plan": "chatgptteamplan",
                            "expires_at": None,
                            "has_active_subscription": True,
                        }
                    ],
                }
            )

            # AsyncMock above is wrong for sync methods; reset to plain lambdas for parser methods.
            service.jwt_parser.is_token_expired = lambda _: False
            service.jwt_parser.extract_email = lambda _: "owner@example.com"

            result = await service.import_team_single(
                access_token="fake-token",
                db_session=session,
                email="owner@example.com",
            )

            self.assertTrue(result["success"])
            self.assertIn("已在系统中", result["message"])
            self.assertIsNone(result["error"])

            count_result = await session.execute(select(Team))
            self.assertEqual(len(count_result.scalars().all()), 1)

    async def test_refresh_all_returns_failure_when_all_accounts_fail(self):
        async with self.session_factory() as session:
            session.add(
                Team(
                    email="owner@example.com",
                    access_token_encrypted="enc",
                    account_id="acct-1",
                    team_name="Team One",
                    status="active",
                )
            )
            await session.commit()

            with patch.object(admin_routes, "AsyncSessionLocal", self.session_factory), patch.object(
                admin_routes.team_service,
                "sync_team_info",
                new=AsyncMock(return_value={"success": False, "message": None, "error": "sync failed"}),
            ) as mock_sync:
                response = await admin_routes.refresh_all_teams(
                    db=session,
                    current_user={"username": "admin", "is_admin": True},
                )

            mock_sync.assert_awaited_once()
            _, kwargs = mock_sync.await_args
            self.assertIn("force_refresh", kwargs)
            self.assertFalse(kwargs["force_refresh"])

            payload = json.loads(response.body.decode("utf-8"))
            self.assertFalse(payload["success"])
            self.assertEqual(payload["success_count"], 0)
            self.assertEqual(payload["failed_count"], 1)
            self.assertEqual(len(payload["results"]), 1)
            self.assertIn("全部失败", payload["message"])

    async def test_refresh_all_persists_status_changes(self):
        async with self.session_factory() as session:
            team = Team(
                email="owner@example.com",
                access_token_encrypted="enc",
                account_id="acct-1",
                team_name="Team One",
                status="active",
            )
            session.add(team)
            await session.commit()

            async def fake_sync(team_id, db, force_refresh=False):
                row = await db.scalar(select(Team).where(Team.id == team_id))
                row.status = "expired"
                await db.flush()
                return {"success": True, "message": "ok", "error": None}

            with patch.object(admin_routes, "AsyncSessionLocal", self.session_factory), patch.object(
                admin_routes.team_service,
                "sync_team_info",
                side_effect=fake_sync,
            ):
                response = await admin_routes.refresh_all_teams(
                    db=session,
                    current_user={"username": "admin", "is_admin": True},
                )

            payload = json.loads(response.body.decode("utf-8"))
            self.assertTrue(payload["success"])
            self.assertEqual(payload["success_count"], 1)

        async with self.session_factory() as verify_session:
            persisted = await verify_session.scalar(select(Team).where(Team.email == "owner@example.com"))
            self.assertEqual(persisted.status, "expired")

    async def test_refresh_all_runs_with_bounded_latency_for_multiple_accounts(self):
        async with self.session_factory() as session:
            session.add_all(
                [
                    Team(
                        email=f"owner{i}@example.com",
                        access_token_encrypted="enc",
                        account_id=f"acct-{i}",
                        team_name=f"Team {i}",
                        status="active",
                    )
                    for i in range(4)
                ]
            )
            await session.commit()

            async def slow_sync(team_id, db, force_refresh=False):
                await asyncio.sleep(0.2)
                return {"success": True, "message": "ok", "error": None}

            with patch.object(admin_routes, "AsyncSessionLocal", self.session_factory), patch.object(
                admin_routes.team_service,
                "sync_team_info",
                side_effect=slow_sync,
            ):
                started = asyncio.get_running_loop().time()
                response = await admin_routes.refresh_all_teams(
                    db=session,
                    current_user={"username": "admin", "is_admin": True},
                )
                elapsed = asyncio.get_running_loop().time() - started

            payload = json.loads(response.body.decode("utf-8"))
            self.assertTrue(payload["success"])
            self.assertLess(
                elapsed,
                0.6,
                msg=f"refresh-all elapsed={elapsed:.3f}s, expected concurrent execution to avoid timeout-like delay",
            )

    async def test_refresh_all_background_job_start_and_status(self):
        async with self.session_factory() as session:
            session.add_all(
                [
                    Team(
                        email=f"owner-bg-{i}@example.com",
                        access_token_encrypted="enc",
                        account_id=f"acct-bg-{i}",
                        team_name=f"BG Team {i}",
                        status="active",
                    )
                    for i in range(4)
                ]
            )
            await session.commit()

            async def slow_sync(team_id, db, force_refresh=False):
                await asyncio.sleep(0.2)
                return {"success": True, "message": "ok", "error": None}

            with patch.object(admin_routes, "AsyncSessionLocal", self.session_factory), patch.object(
                admin_routes.team_service,
                "sync_team_info",
                side_effect=slow_sync,
            ):
                started = asyncio.get_running_loop().time()
                start_response = await admin_routes.start_refresh_all_teams(
                    db=session,
                    current_user={"username": "admin", "is_admin": True},
                )
                start_elapsed = asyncio.get_running_loop().time() - started

                start_payload = json.loads(start_response.body.decode("utf-8"))
                self.assertTrue(start_payload["success"])
                self.assertEqual(start_payload["status"], "running")
                self.assertIsNotNone(start_payload.get("job_id"))
                self.assertLess(start_elapsed, 0.2)

                job_id = start_payload["job_id"]
                final_payload = None
                for _ in range(40):
                    status_response = await admin_routes.get_refresh_all_teams_status(
                        job_id=job_id,
                        current_user={"username": "admin", "is_admin": True},
                    )
                    payload = json.loads(status_response.body.decode("utf-8"))
                    if payload.get("status") == "completed":
                        final_payload = payload
                        break
                    await asyncio.sleep(0.05)

                self.assertIsNotNone(final_payload, "后台刷新任务未在预期时间内完成")
                self.assertTrue(final_payload["success"])
                self.assertEqual(final_payload["total"], 4)
                self.assertEqual(final_payload["success_count"], 4)
                self.assertEqual(final_payload["failed_count"], 0)

    async def test_refresh_all_background_status_returns_not_found_for_unknown_job(self):
        response = await admin_routes.get_refresh_all_teams_status(
            job_id="missing-job-id",
            current_user={"username": "admin", "is_admin": True},
        )
        payload = json.loads(response.body.decode("utf-8"))
        self.assertFalse(payload["success"])
        self.assertIn("不存在", payload["error"])


if __name__ == "__main__":
    unittest.main()
