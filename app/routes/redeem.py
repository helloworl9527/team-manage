"""
兑换路由
处理用户兑换码验证和加入 Team 的请求
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List, Dict, Any
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import RedemptionCode, RedemptionRecord, Team
from app.services.redeem_flow import redeem_flow_service

logger = logging.getLogger(__name__)

# 创建路由器
router = APIRouter(
    prefix="/redeem",
    tags=["redeem"]
)


# 请求模型
class VerifyCodeRequest(BaseModel):
    """验证兑换码请求"""
    code: str = Field(..., description="兑换码", min_length=1)


class RedeemRequest(BaseModel):
    """兑换请求"""
    email: EmailStr = Field(..., description="用户邮箱")
    code: str = Field(..., description="兑换码", min_length=1)
    team_id: Optional[int] = Field(None, description="Team ID (可选，不提供则自动选择)")


# 响应模型
class TeamInfo(BaseModel):
    """Team 信息"""
    id: int
    team_name: str
    current_members: int
    max_members: int
    expires_at: Optional[str]
    subscription_plan: Optional[str]


class VerifyCodeResponse(BaseModel):
    """验证兑换码响应"""
    success: bool
    valid: bool
    reason: Optional[str] = None
    teams: List[TeamInfo] = []
    error: Optional[str] = None


class RedeemResponse(BaseModel):
    """兑换响应"""
    success: bool
    message: Optional[str] = None
    team_info: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


def _extract_root_cause(error_msg: str) -> str:
    marker = "最后报错:"
    if marker in error_msg:
        return error_msg.split(marker, 1)[1].strip() or error_msg
    return error_msg


def _build_error_feedback(error_msg: str) -> str:
    root_cause = _extract_root_cause(error_msg)
    root_lower = root_cause.lower()

    hint = None
    if any(kw in root_lower for kw in ["maximum number of seats", "已满", "席位", "no seats"]):
        hint = "目标 Team 当前席位不足，请稍后重试或更换 Team。"
    elif any(kw in root_lower for kw in ["token 已过期且无法刷新", "token_expired", "token is expired"]):
        hint = "目标 Team 管理员 Token 已失效，系统刷新失败。"
    elif any(kw in root_lower for kw in ["account_deactivated", "token_invalidated", "账号已封禁", "失效"]):
        hint = "目标 Team 管理员账号可能被封禁或令牌失效。"
    elif any(kw in root_lower for kw in ["网络", "timeout", "超时", "连接"]):
        hint = "网络或上游接口波动导致回执异常。"
    elif any(kw in root_lower for kw in ["身份", "不符", "session"]):
        hint = "账号登录态可能被污染，请清理会话后重试。"
    elif any(kw in root_lower for kw in ["质保已过期", "截止时间", "已过期"]):
        hint = "兑换码质保/有效期已到期。"

    if hint:
        return f"{error_msg}；根因: {root_cause}；可能原因: {hint}"
    return f"{error_msg}；根因: {root_cause}"


async def _recover_if_already_redeemed(
    db: AsyncSession,
    email: str,
    code: str,
    original_error: str
) -> Optional[Dict[str, Any]]:
    """
    失败后的只读兜底：
    如果数据库已显示该邮箱该兑换码已生效，则返回成功，避免“假失败”提示。
    """
    normalized_email = (email or "").strip().lower()
    normalized_code = (code or "").strip()
    normalized_code_lower = normalized_code.lower()

    # 1) 优先看 redemption_records（最可靠）
    stmt = (
        select(RedemptionRecord, Team)
        .join(Team, RedemptionRecord.team_id == Team.id)
        .where(
            func.lower(RedemptionRecord.email) == normalized_email,
            func.lower(RedemptionRecord.code) == normalized_code_lower,
        )
        .order_by(desc(RedemptionRecord.redeemed_at))
    )
    result = await db.execute(stmt)
    row = result.first()
    if row:
        record, team = row
        return {
            "success": True,
            "message": "系统检测到该兑换码已成功生效，已自动修正失败提示。请查收邀请邮件。",
            "team_info": {
                "id": team.id,
                "team_name": team.team_name,
                "email": team.email,
                "expires_at": team.expires_at.isoformat() if team.expires_at else None,
                "warranty_expires_at": None,
                "redeemed_at": record.redeemed_at.isoformat() if record.redeemed_at else None,
                "recovered_from_error": original_error,
            },
            "error": None,
        }

    # 2) 次优看 redemption_codes（处理历史“已使用但未写记录”数据）
    code_stmt = select(RedemptionCode).where(func.lower(RedemptionCode.code) == normalized_code_lower)
    code_result = await db.execute(code_stmt)
    redemption_code = code_result.scalar_one_or_none()

    if (
        redemption_code
        and redemption_code.status in ["used", "warranty_active"]
        and redemption_code.used_by_email
        and redemption_code.used_by_email.lower() == normalized_email
        and redemption_code.used_at
    ):
        team_info = None
        if redemption_code.used_team_id:
            team_stmt = select(Team).where(Team.id == redemption_code.used_team_id)
            team_result = await db.execute(team_stmt)
            team = team_result.scalar_one_or_none()
            if team:
                team_info = {
                    "id": team.id,
                    "team_name": team.team_name,
                    "email": team.email,
                    "expires_at": team.expires_at.isoformat() if team.expires_at else None,
                }

        payload = {
            "warranty_expires_at": redemption_code.warranty_expires_at.isoformat()
            if redemption_code.warranty_expires_at
            else None,
            "redeemed_at": redemption_code.used_at.isoformat() if redemption_code.used_at else None,
            "recovered_from_error": original_error,
        }
        if team_info:
            payload.update(team_info)

        return {
            "success": True,
            "message": "系统检测到兑换码状态已生效，已自动修正失败提示。请查收邀请邮件。",
            "team_info": payload,
            "error": None,
        }

    return None


def _status_code_for_error(error_msg: str) -> int:
    error_msg = error_msg or ""
    error_lower = error_msg.lower()

    if any(kw in error_lower for kw in ["已满", "席位", "maximum number of seats", "full", "no seats"]):
        return status.HTTP_409_CONFLICT

    if any(kw in error_lower for kw in ["不存在", "已使用", "已过期", "截止时间", "质保", "无效", "失效"]):
        return status.HTTP_400_BAD_REQUEST

    return status.HTTP_500_INTERNAL_SERVER_ERROR


@router.post("/verify", response_model=VerifyCodeResponse)
async def verify_code(
    request: VerifyCodeRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    验证兑换码并返回可用 Team 列表

    Args:
        request: 验证请求
        db: 数据库会话

    Returns:
        验证结果和可用 Team 列表
    """
    try:
        logger.info(f"验证兑换码请求: {request.code}")

        result = await redeem_flow_service.verify_code_and_get_teams(
            request.code,
            db
        )

        if not result["success"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=result["error"]
            )

        return VerifyCodeResponse(
            success=result.get("success", False),
            valid=result.get("valid", False),
            reason=result.get("reason"),
            teams=[TeamInfo(**team) for team in result.get("teams", [])],
            error=result.get("error")
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"验证兑换码失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"验证失败: {str(e)}"
        )


@router.post("/confirm", response_model=RedeemResponse)
async def confirm_redeem(
    request: RedeemRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    确认兑换并加入 Team

    Args:
        request: 兑换请求
        db: 数据库会话

    Returns:
        兑换结果
    """
    try:
        logger.info(f"兑换请求: {request.email} -> Team {request.team_id} (兑换码: {request.code})")

        result = await redeem_flow_service.redeem_and_join_team(
            request.email,
            request.code,
            request.team_id,
            db
        )

        if not result["success"]:
            error_msg = str(result.get("error") or "未知原因")

            # 失败后兜底校验：若已生效则回写成功响应，避免“显示失败但实际成功”
            recovered = await _recover_if_already_redeemed(
                db=db,
                email=request.email,
                code=request.code,
                original_error=error_msg,
            )
            if recovered:
                return RedeemResponse(
                    success=recovered.get("success", True),
                    message=recovered.get("message"),
                    team_info=recovered.get("team_info"),
                    error=None,
                )

            detailed_error = _build_error_feedback(error_msg)
            raise HTTPException(
                status_code=_status_code_for_error(error_msg),
                detail=detailed_error
            )

        return RedeemResponse(
            success=result.get("success", False),
            message=result.get("message"),
            team_info=result.get("team_info"),
            error=result.get("error")
        )

    except HTTPException:
        raise
    except Exception as e:
        error_msg = str(e) or "未知异常"
        exception_reason = f"{e.__class__.__name__}: {error_msg}"
        logger.exception(f"兑换失败: {exception_reason}")

        recovered = await _recover_if_already_redeemed(
            db=db,
            email=request.email,
            code=request.code,
            original_error=exception_reason,
        )
        if recovered:
            return RedeemResponse(
                success=recovered.get("success", True),
                message=recovered.get("message"),
                team_info=recovered.get("team_info"),
                error=None,
            )

        raise HTTPException(
            status_code=_status_code_for_error(error_msg),
            detail=_build_error_feedback(f"兑换失败: {exception_reason}")
        )
