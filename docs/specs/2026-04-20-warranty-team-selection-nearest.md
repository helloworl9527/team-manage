# Spec: 质保重兑 Team 选择策略（最接近质保期）

## Objective
在质保重兑场景下，提升 Team 自动分配成功率与可解释性。

用户故事：
- 当用户当前工作空间为 `banned` 或 `expired` 且仍在质保期内，系统自动选 Team。
- 优先选 `team.expires_at > warranty_expires_at` 且差值最小的 Team。
- 若不存在满足上述条件的 Team，则回退为“到期时间最接近质保截止时间”的 Team（例如质保 6 月 1 日，Team 到期为 5 月 13 日和 5 月 16 日时选 5 月 16 日）。

## Assumptions
1. 当前项目已有“优先选择 `expires_at > warranty_expires_at`”逻辑，需保持兼容。
2. “最接近质保期”在回退分支下定义为：`expires_at` 与 `warranty_expires_at` 绝对时间差最小；若同差值，优先更晚的 `expires_at`。
3. 仅考虑 `status=active` 且有剩余席位的 Team。

## Commands
- 定向测试：
  - `PYTHONPATH=. ./.venv/bin/pytest -q tests/test_warranty_issue20.py -k "warranty_auto_select"`
- 全量测试：
  - `PYTHONPATH=. ./.venv/bin/pytest -q`
- 语法检查：
  - `PYTHONPATH=. ./.venv/bin/python -m compileall -q app tests`

## Project Structure
- `app/services/redeem_flow.py`：质保重兑自动选 Team 核心逻辑
- `app/services/warranty.py`：质保有效期与重兑资格判断
- `tests/test_warranty_issue20.py`：质保回归测试

## Code Style
- 保持现有服务层风格：返回结构化 dict（`success/team_id/error`），避免抛出到路由层。
- 复杂分支以可读的单趟扫描 + 明确优先级键实现，避免多次排序和冗余中间状态。

## Testing Strategy
- 先红后绿（TDD）：
  - 先写/改失败用例复现“仅有早于质保截止的 Team 时应选最接近者”。
  - 保留并回归“存在晚于质保截止 Team 时仍选差值最小正差值”的既有行为。
  - 增加边界：`warranty_expires_at == now` 视为过期不可重兑。

## Boundaries
- Always:
  - 先测试复现再改实现
  - 每个增量改动后运行定向测试
  - 最终跑全量测试
- Ask first:
  - 改数据库结构
  - 修改兑换码状态机语义（如 `used/warranty_active`）
- Never:
  - 删除已有回归测试来“让测试通过”
  - 降低质保有效期校验标准

## Success Criteria
1. 存在 `expires_at > warranty_expires_at` Team 时：选正差值最小者。
2. 不存在 `expires_at > warranty_expires_at` Team 时：选“最接近质保截止”的 Team（满足 6/1 vs 5/13,5/16 选 5/16）。
3. `warranty_expires_at <= now` 时：自动选 Team 失败并返回质保已过期。
4. 全量测试通过。

## 任务拆解与里程碑
### Milestone 1: 规格冻结
- 输出冲突需求的统一规则（严格优先 + 最近回退）。
- 明确验收标准与边界条件。

### Milestone 2: 测试先行（TDD）
- 用例矩阵落地为自动化测试。
- 至少包含：
  - `>` 优先场景
  - 全部早于质保截止场景（示例 6/1 vs 5/13、5/16）
  - 质保到期边界 `== now`
  - expired/banned 可重兑资格

### Milestone 3: 增量实现
- 先通过失败测试确认复现，再修改实现。
- 每次改动后执行定向回归。

### Milestone 4: 性能优化
- 在保证行为不变的前提下优化选择算法复杂度/内存分配。

### Milestone 5: 质量审查与全量回归
- 多维审查（正确性/可维护性/性能/安全）
- 全量测试通过后给出风险与下一步建议。

## 用例矩阵
| ID | 场景 | 期望结果 |
|---|---|---|
| T1 | 有 `expires_at > warranty_expires_at` 候选 | 选正差值最小 Team |
| T2 | 无 `>` 候选，所有 Team 早于质保截止 | 选最接近质保截止 Team（同差值优先更晚到期） |
| T3 | `warranty_expires_at == now` | 判定质保已过期，不自动分配 |
| T4 | 用户历史 Team 为 `expired`/`banned` | 质保期内允许重兑并走自动选 Team |

## Open Questions
- 回退分支是否仅允许选择 `expires_at <= warranty_expires_at` 的 Team（当前按“最接近”定义会自然落在该集合）。
