# Spec: Team Manage Deployment + Warranty Redeem Enhancements

## Assumptions
1. “一键刷新”范围为全部 Team 管理员账号，刷新后由系统自动同步并更新可用状态。
2. “当前工作空间被封禁、过期” 指用户最近一次兑换记录关联 Team 状态为 `banned` 或 `expired`，或 Team 已过期。
3. 当满足质保重兑条件时，若未手动指定 Team，系统自动选取新 Team。
4. 自动选取规则优先满足：`team.expires_at > code.warranty_expires_at`，并按差值最小优先。

## Objective
在保持现有兑换流程兼容的前提下，完成以下目标：
- 项目部署并可通过网页访问。
- 增加“一键刷新所有 Team 管理员账号并更新可用状态”操作。
- 质保重兑在原 Team 被封禁/过期时，自动分配最接近质保到期时间的可用 Team。
- 兑换成功页与质保查询页都展示：Team 到期时间、质保到期时间、兑换时间。

## Commands
- Install deps: `python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt`
- Init DB: `./.venv/bin/python init_db.py`
- Run app: `./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8008`
- Tests: `./.venv/bin/python -m pytest -q`

## Project Structure
- `app/services/redeem_flow.py`: 兑换与自动选 Team逻辑
- `app/services/warranty.py`: 质保复用判定与查询
- `app/routes/admin.py`: 管理员批量/一键刷新路由
- `app/templates/admin/index.html`: 管理员页按钮与前端交互
- `app/static/js/redeem.js`: 兑换成功展示与质保记录展示
- `tests/`: 行为回归测试

## Testing Strategy
- 单元/集成测试使用 `pytest`。
- 覆盖重点：
  1) 质保重兑在 `expired` 情况可复用。
  2) 自动选 Team 规则满足 “大于且最接近质保到期”。
  3) 兑换成功返回包含三个关键时间字段。

## Boundaries
- Always: 保持现有 API 路由兼容；新增逻辑优先向后兼容。
- Ask first: 数据库结构变更（本次不改表结构）。
- Never: 删除现有质保记录或放宽安全校验。

## Success Criteria
1. `http://<host>:8008/` 可访问页面。
2. 管理员界面可一键刷新所有 Team，并返回当前可用账号数量。
3. 当质保有效且原 Team `banned/expired` 时，重兑自动选择符合规则的 Team。
4. 兑换成功响应和页面显示包含：`team_expires_at`、`warranty_expires_at`、`redeemed_at`。
5. 质保查询（按兑换码）显示相同三项信息。

## Task Breakdown
1. 新增管理员“一键刷新可用账号”后端路由与前端按钮。
2. 在质保服务放宽复用条件：`banned` 或 `expired` 均可触发。
3. 在兑换流程新增“按质保到期匹配 Team”的自动选取函数并接入重兑路径。
4. 扩展兑换成功返回字段并更新前端展示。
5. 补充/更新测试并执行回归测试。
