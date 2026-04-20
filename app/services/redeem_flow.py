"""
兑换流程服务 (Redeem Flow Service)
协调兑换码验证, Team 选择和加入 Team 的完整流程
"""
import logging
import asyncio
import traceback
from collections import defaultdict
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from sqlalchemy import select, update, delete, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import AsyncSessionLocal

from app.models import RedemptionCode, RedemptionRecord, Team
from app.services.redemption import RedemptionService
from app.services.team import TeamService
from app.services.warranty import warranty_service
from app.services.notification import notification_service
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

# 全局兑换锁: 针对 code 进行加锁，防止同一个码并发请求
_code_locks = defaultdict(asyncio.Lock)
# 全局 Team 锁: 针对 Team 进行加锁，防止并发拉人导致的人数状态不同步
_team_locks = defaultdict(asyncio.Lock)


_background_tasks: set = set()


def _schedule_background_task(coro):
    """
    在正常运行环境下注册后台任务；在测试里若 create_task 被替换为 None，安全降级跳过。
    """
    task = asyncio.create_task(coro)
    if task is None:
        return None
    _background_tasks.add(task)
    if hasattr(task, "add_done_callback"):
        task.add_done_callback(_background_tasks.discard)
    return task


class RedeemFlowService:
    """兑换流程场景服务类"""

    def __init__(self):
        """初始化兑换流程服务"""
        from app.services.chatgpt import chatgpt_service
        self.redemption_service = RedemptionService()
        self.warranty_service = warranty_service
        self.team_service = TeamService()
        self.chatgpt_service = chatgpt_service

    async def verify_code_and_get_teams(
        self,
        code: str,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        验证兑换码并返回可用 Team 列表
        针对 aiosqlite 进行优化，避免 greenlet_spawn 报错
        """
        try:
            # 1. 验证兑换码
            validate_result = await self.redemption_service.validate_code(code, db_session)

            if not validate_result["success"]:
                return {
                    "success": False,
                    "valid": False,
                    "reason": None,
                    "teams": [],
                    "error": validate_result["error"]
                }
            
            # 如果是已经标记为过期了
            if not validate_result["valid"] and validate_result.get("reason") == "兑换码已过期 (超过首次兑换截止时间)":
                try:
                    await db_session.commit()
                except:
                    pass

            if not validate_result["valid"]:
                return {
                    "success": True,
                    "valid": False,
                    "reason": validate_result["reason"],
                    "teams": [],
                    "error": None
                }

            # 2. 获取可用 Team 列表
            teams_result = await self.team_service.get_available_teams(db_session)

            if not teams_result["success"]:
                return {
                    "success": False,
                    "valid": True,
                    "reason": "兑换码有效",
                    "teams": [],
                    "error": teams_result["error"]
                }

            logger.info(f"验证兑换码成功: {code}, 可用 Team 数量: {len(teams_result['teams'])}")

            return {
                "success": True,
                "valid": True,
                "reason": "兑换码有效",
                "teams": teams_result["teams"],
                "error": None
            }

        except Exception as e:
            logger.error(f"验证兑换码并获取 Team 列表失败: {e}")
            logger.error(traceback.format_exc())
            return {
                "success": False,
                "valid": False,
                "reason": None,
                "teams": [],
                "error": f"验证失败: {str(e)}"
            }

    async def select_team_auto(
        self,
        db_session: AsyncSession,
        exclude_team_ids: Optional[List[int]] = None
    ) -> Dict[str, Any]:
        """
        自动选择一个可用的 Team
        """
        try:
            # 查找所有 active 且未满的 Team
            stmt = select(Team).where(Team.status == "active")
            
            if exclude_team_ids:
                stmt = stmt.where(~Team.id.in_(exclude_team_ids))

            result = await db_session.execute(stmt)
            teams = [team for team in result.scalars().all() if self.team_service._remaining_slots(team) > 0]

            # 优先选择已占用席位最少的 Team (负载均衡)，同占用量时优先较新的 Team
            teams.sort(
                key=lambda team: (
                    self.team_service._occupied_slots(team),
                    -(team.created_at.timestamp() if team.created_at else 0),
                )
            )
            team = teams[0] if teams else None

            if not team:
                reason = "没有可用的 Team"
                if exclude_team_ids:
                    reason = "您已加入所有可用 Team"
                return {
                    "success": False,
                    "team_id": None,
                    "error": reason
                }

            logger.info(f"自动选择 Team: {team.id}")

            return {
                "success": True,
                "team_id": team.id,
                "error": None
            }

        except Exception as e:
            logger.error(f"自动选择 Team 失败: {e}")
            return {
                "success": False,
                "team_id": None,
                "error": f"自动选择 Team 失败: {str(e)}"
            }

    async def _resolve_warranty_expires_at(
        self,
        db_session: AsyncSession,
        redemption_code: RedemptionCode
    ) -> Optional[datetime]:
        """
        计算/获取质保到期时间（用于重兑自动选 Team 和返回展示）。
        """
        if not redemption_code.has_warranty:
            return None

        if redemption_code.warranty_expires_at:
            return redemption_code.warranty_expires_at

        stmt = select(func.min(RedemptionRecord.redeemed_at)).where(
            RedemptionRecord.code == redemption_code.code
        )
        result = await db_session.execute(stmt)
        first_redeemed_at = result.scalar_one_or_none() or redemption_code.used_at
        if not first_redeemed_at:
            return None

        return first_redeemed_at + timedelta(days=redemption_code.warranty_days or 30)

    async def _select_team_for_warranty(
        self,
        db_session: AsyncSession,
        warranty_expires_at: datetime,
        exclude_team_ids: Optional[List[int]] = None
    ) -> Dict[str, Any]:
        """
        质保自动选 Team（首次兑换与重兑共用）：
        1) 优先选取 expires_at >= warranty_expires_at 且正差值最小的可用 Team。
        2) 若不存在，则回退为 expires_at 与 warranty_expires_at 最接近的可用 Team（同差值优先更晚到期）。
        """
        stmt = select(Team).where(Team.status == "active")
        if exclude_team_ids:
            stmt = stmt.where(~Team.id.in_(exclude_team_ids))

        result = await db_session.execute(stmt)
        teams = [team for team in result.scalars().all() if self.team_service._remaining_slots(team) > 0]

        # 单趟扫描：同时计算"严格优先候选"和"回退最近候选"，避免多次构造中间列表。
        best_after_team = None
        best_after_key = None
        best_closest_team = None
        best_closest_key = None

        for team in teams:
            if not team.expires_at:
                continue

            created_ts = team.created_at.timestamp() if team.created_at else 0
            occupied_slots = self.team_service._occupied_slots(team)

            if team.expires_at >= warranty_expires_at:
                after_key = (
                    (team.expires_at - warranty_expires_at).total_seconds(),
                    occupied_slots,
                    -created_ts,
                )
                if best_after_key is None or after_key < best_after_key:
                    best_after_key = after_key
                    best_after_team = team

            closest_key = (
                abs((team.expires_at - warranty_expires_at).total_seconds()),
                -team.expires_at.timestamp(),
                occupied_slots,
                -created_ts,
            )
            if best_closest_key is None or closest_key < best_closest_key:
                best_closest_key = closest_key
                best_closest_team = team

        if best_after_team:
            logger.info(
                "质保自动选 Team(严格优先): code_expiry=%s, selected_team=%s, team_expiry=%s",
                warranty_expires_at.isoformat(),
                best_after_team.id,
                best_after_team.expires_at.isoformat(),
            )
            return {"success": True, "team_id": best_after_team.id, "error": None}

        if best_closest_team:
            logger.info(
                "质保自动选 Team(回退最近): code_expiry=%s, selected_team=%s, team_expiry=%s",
                warranty_expires_at.isoformat(),
                best_closest_team.id,
                best_closest_team.expires_at.isoformat(),
            )
            return {"success": True, "team_id": best_closest_team.id, "error": None}

        return {
            "success": False,
            "team_id": None,
            "error": "暂无可用 Team（无法匹配质保重兑条件）"
        }

    async def _auto_select_team_for_redemption(
        self,
        db_session: AsyncSession,
        code: str,
        email: str,
        exclude_team_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """
        根据兑换上下文自动选 Team。
        - 有质保的兑换码（首次或重兑）：优先按 Team 到期 >= 质保到期且最接近 选取，
          若无满足条件者，回退为最接近质保到期的 Team。
        - 无质保常规兑换：沿用默认负载均衡选取。
        """
        stmt = select(RedemptionCode).where(RedemptionCode.code == code)
        result = await db_session.execute(stmt)
        redemption_code = result.scalar_one_or_none()
        if not redemption_code:
            return {"success": False, "team_id": None, "error": "兑换码不存在"}

        if redemption_code.has_warranty and redemption_code.status == "used":
            warranty_check = await self.warranty_service.validate_warranty_reuse(db_session, code, email)
            if not warranty_check.get("can_reuse"):
                return {
                    "success": False,
                    "team_id": None,
                    "error": warranty_check.get("reason") or "质保不可重兑",
                }

            warranty_expires_at = await self._resolve_warranty_expires_at(db_session, redemption_code)
            if not warranty_expires_at:
                return {"success": False, "team_id": None, "error": "无法计算质保到期时间，暂不可自动分配 Team"}

            return await self._select_team_for_warranty(
                db_session=db_session,
                warranty_expires_at=warranty_expires_at,
                exclude_team_ids=exclude_team_ids,
            )

        # 首次兑换有质保码：也按质保到期时间选 Team
        if redemption_code.has_warranty and redemption_code.warranty_days:
            warranty_expires_at = get_now() + timedelta(days=redemption_code.warranty_days)
            return await self._select_team_for_warranty(
                db_session=db_session,
                warranty_expires_at=warranty_expires_at,
                exclude_team_ids=exclude_team_ids,
            )

        return await self.select_team_auto(db_session=db_session, exclude_team_ids=exclude_team_ids)

    async def redeem_and_join_team(
        self,
        email: str,
        code: str,
        team_id: Optional[int],
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        完整的兑换流程 (带事务和并发控制)
        """
        last_error = "未知错误"
        max_retries = 3
        current_target_team_id = team_id
        core_success = False
        success_result = None
        team_id_final = None
        tried_team_ids: List[int] = []

        # 针对 code 加锁，防止同一个码并发进入兑换
        async with _code_locks[code]:
            for attempt in range(max_retries):
                logger.info(f"兑换尝试 {attempt + 1}/{max_retries} (Code: {code}, Email: {email})")
                
                try:
                    # 确定目标 Team (初选)
                    team_id_final = current_target_team_id
                    if not team_id_final:
                        select_res = await self._auto_select_team_for_redemption(
                            db_session=db_session,
                            code=code,
                            email=email,
                            exclude_team_ids=tried_team_ids
                        )
                        if not select_res["success"]:
                            return {"success": False, "error": select_res["error"]}
                        team_id_final = select_res["team_id"]
                        current_target_team_id = team_id_final
                    if team_id_final not in tried_team_ids:
                        tried_team_ids.append(team_id_final)
                    
                    # 使用 Team 锁序列化对该账户的操作，防止并发冲突
                    async with _team_locks[team_id_final]:
                        logger.info(f"锁定 Team {team_id_final} 执行核心兑换步骤 (尝试 {attempt+1})")
                        
                        # 重置 Session 状态，确保没有残留事务（应对上一轮迭代可能的失败）
                        if db_session.in_transaction():
                            await db_session.rollback()
                        elif db_session.is_active:
                            await db_session.rollback()

                        # 1. 前置同步：拉人前确保人数状态绝对实时 (耗时操作)
                        await self.team_service.sync_team_info(team_id_final, db_session)
                        
                        # 2. 核心校验 (开启短事务)
                        if not db_session.in_transaction():
                            await db_session.begin()
                        
                        try:
                            # 1. 验证和锁定码
                            stmt = select(RedemptionCode).where(RedemptionCode.code == code).with_for_update()
                            res = await db_session.execute(stmt)
                            rc = res.scalar_one_or_none()

                            if not rc:
                                await db_session.rollback()
                                return {"success": False, "error": "兑换码不存在"}
                            
                            if rc.status not in ["unused", "warranty_active"]:
                                if rc.status == "used":
                                    warranty_check = await self.warranty_service.validate_warranty_reuse(
                                        db_session, code, email
                                    )
                                    if not warranty_check.get("can_reuse"):
                                        await db_session.rollback()
                                        return {"success": False, "error": warranty_check.get("reason") or "兑换码已使用"}
                                else:
                                    await db_session.rollback()
                                    return {"success": False, "error": f"兑换码状态无效: {rc.status}"}

                            # 2. 锁定并校验 Team
                            stmt = select(Team).where(Team.id == team_id_final).with_for_update()
                            res = await db_session.execute(stmt)
                            target_team = res.scalar_one_or_none()
                            
                            if not target_team or target_team.status != "active":
                                raise Exception(f"目标 Team {team_id_final} 不可用 ({target_team.status if target_team else 'None'})")
                            
                            if self.team_service._occupied_slots(target_team) >= target_team.max_members:
                                target_team.status = "full"
                                raise Exception("该 Team 已满, 请选择其他 Team 尝试")

                            # 提取必要信息后立即提交，释放 DB 锁以进行耗时的 API 调用
                            account_id_to_use = target_team.account_id
                            team_email_to_use = target_team.email
                            await db_session.commit()
                        except Exception as e:
                            if db_session.in_transaction():
                                await db_session.rollback()
                            raise e
                        
                        # 3. 执行 API 邀请 (耗时操作，放事务外)
                        # 必须重新加载 target_team
                        res = await db_session.execute(select(Team).where(Team.id == team_id_final))
                        target_team = res.scalar_one_or_none()
                        if not target_team:
                            raise Exception(f"Team {team_id_final} 不存在或已被删除")

                        access_token = await self.team_service.ensure_access_token(target_team, db_session)
                        if not access_token:
                            raise Exception("获取 Team 访问权限失败，账户状态异常")

                        invite_res = await self.chatgpt_service.send_invite(
                            access_token, account_id_to_use, email, db_session,
                            identifier=team_email_to_use
                        )
                        
                        # 4. 后置处理与状态持久化 (第二次短事务)
                        if not db_session.in_transaction():
                            await db_session.begin()
                        
                        try:
                            # 重新载入，确保状态最新
                            res = await db_session.execute(select(RedemptionCode).where(RedemptionCode.code == code).with_for_update())
                            rc = res.scalar_one_or_none()
                            res = await db_session.execute(select(Team).where(Team.id == team_id_final).with_for_update())
                            target_team = res.scalar_one_or_none()

                            if not invite_res["success"]:
                                err = invite_res.get("error", "邀请失败")
                                err_str = str(err).lower()
                                if any(kw in err_str for kw in ["already in workspace", "already in team", "already a member"]):
                                    logger.info(f"用户 {email} 已经在 Team {team_id_final} 中，视为兑换成功")
                                else:
                                    if any(kw in err_str for kw in ["maximum number of seats", "full", "no seats"]):
                                        target_team.status = "full"
                                        await db_session.commit()
                                        raise Exception(f"该 Team 席位已满 (API Error: {err})")
                                    await db_session.rollback()
                                    raise Exception(err)

                            invite_data = invite_res.get("data", {})
                            if "account_invites" in invite_data and not invite_data.get("account_invites"):
                                await db_session.rollback()
                                raise Exception("Team账号受限: 官方拦截下发(响应空列表)，请检查账单/风控状态")

                            # 成功逻辑
                            redemption_time = get_now()
                            is_first_redemption = rc.used_at is None

                            rc.status = "used"
                            if is_first_redemption:
                                rc.used_by_email = email
                                rc.used_team_id = team_id_final
                                rc.used_at = redemption_time
                                if rc.has_warranty:
                                    days = rc.warranty_days or 30
                                    rc.warranty_expires_at = redemption_time + timedelta(days=days)
                            effective_warranty_expires_at = await self._resolve_warranty_expires_at(db_session, rc)

                            record = RedemptionRecord(
                                email=email,
                                code=code,
                                team_id=team_id_final,
                                account_id=target_team.account_id,
                                is_warranty_redemption=rc.has_warranty,
                                redeemed_at=redemption_time
                            )
                            db_session.add(record)
                            target_team.pending_invites = (target_team.pending_invites or 0) + 1
                            if self.team_service._occupied_slots(target_team) >= target_team.max_members:
                                target_team.status = "full"
                            
                            await db_session.commit()
                            
                            # 核心步骤成功，准备返回结果
                            success_result = {
                                "success": True,
                                "message": "兑换成功！邀请链接已发送至您的邮箱，请及时查收。",
                                "team_info": {
                                    "id": team_id_final,
                                    "team_name": target_team.team_name,
                                    "email": target_team.email,
                                    "expires_at": target_team.expires_at.isoformat() if target_team.expires_at else None,
                                    "warranty_expires_at": effective_warranty_expires_at.isoformat() if effective_warranty_expires_at else None,
                                    "redeemed_at": redemption_time.isoformat(),
                                }
                            }
                            core_success = True
                        except Exception as e:
                            if db_session.in_transaction():
                                await db_session.rollback()
                            raise e

                    # 如果核心步骤成功，跳出重试循环
                    if core_success:
                        break

                except Exception as e:
                    last_error = str(e)
                    logger.error(f"兑换迭代失败 ({attempt+1}): {last_error}")
                    
                    try:
                        if db_session.in_transaction():
                            await db_session.rollback()
                    except:
                        pass
                    
                    # 判读是否中断重试
                    if any(kw in last_error for kw in ["不存在", "已使用", "已有正在使用", "质保已过期"]):
                        return {"success": False, "error": last_error}

                    # 判定是否需要永久标记为"满员"
                    if any(kw in last_error.lower() for kw in ["已满", "seats", "full"]):
                        try:
                            if not team_id:
                                from sqlalchemy import update as sqlalchemy_update
                                await db_session.execute(
                                    sqlalchemy_update(Team).where(Team.id == team_id_final).values(status="full")
                                )
                                await db_session.commit()
                            current_target_team_id = None
                        except:
                            pass
                    
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
            
            if core_success:
                # 后台异步验证任务 (循环检测 3 次，确保 API 数据同步) - 移至后台以极大提高并发并防止 HTTP 超时
                _schedule_background_task(self._background_verify_sync(team_id_final, email))

                # 补货通知任务 (异步)
                _schedule_background_task(notification_service.check_and_notify_low_stock())
                    
                return success_result
            else:
                return {
                    "success": False,
                    "error": f"兑换失败次数过多。最后报错: {last_error}"
                }

    async def _background_verify_sync(self, team_id: int, email: str):
        """
        后台异步验证并同步 (不阻塞 HTTP 请求)
        """
        async with AsyncSessionLocal() as db_session:
            try:
                is_verified = False
                for i in range(3):
                    await asyncio.sleep(5)
                    # 每次同步前确保 session 是最新的
                    sync_res = await self.team_service.sync_team_info(team_id, db_session)
                    member_emails = [m.lower() for m in sync_res.get("member_emails", [])]
                    if email.lower() in member_emails:
                        is_verified = True
                        logger.info(f"Team {team_id} [Background] 同步确认成功 (尝试第 {i+1} 次)")
                        break
                    
                    if i < 2:
                        logger.warning(f"Team {team_id} [Background] 尚未见到成员 {email}，准备第 {i+2} 次重试...")
                
                if not is_verified:
                    logger.error(f"检测到'虚假成功': Team {team_id} 兑换成功但 15s 后仍查不到成员 {email}")
                    # 在后台标记异常
                    stmt = select(Team).where(Team.id == team_id)
                    t_res = await db_session.execute(stmt)
                    target_t = t_res.scalar_one_or_none()
                    if target_t:
                        await self.team_service._handle_api_error(
                            {"success": False, "error": "兑换成功但 3 次同步均未见成员", "error_code": "ghost_success"},
                            target_t, db_session
                        )
            except Exception as e:
                logger.error(f"后台同步校验发生异常: {e}")

# 创建全局实例
redeem_flow_service = RedeemFlowService()
