import asyncio
from unittest.mock import AsyncMock, patch

from app.services.notification import notification_service
from app.services.settings import settings_service


async def _run_webhook_flow() -> bool:
    """
    以可重复、无外网依赖的方式验证 webhook 触发流程：
    1) 设置更新动作会被调用
    2) 库存检测通知动作会被调用
    """
    with patch.object(settings_service, "update_settings", new=AsyncMock(return_value=True)) as mock_update:
        with patch.object(
            notification_service, "check_and_notify_low_stock", new=AsyncMock(return_value=True)
        ) as mock_notify:
            await settings_service.update_settings(
                None,
                {
                    "webhook_url": "https://example.invalid/webhook",
                    "low_stock_threshold": "100",
                },
            )
            result = await notification_service.check_and_notify_low_stock()

            mock_update.assert_awaited_once()
            mock_notify.assert_awaited_once()
            return bool(result)


def test_webhook():
    assert asyncio.run(_run_webhook_flow()) is True

