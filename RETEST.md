# 单拍替代复测

质控室为一份封记录制中的一个原拍指定复测范围。站端在交班前固定复测件，质控室可在站端移开后提出候选及复查条件，并凭报告号领取签发结果。V1 计划、录制、普通交班件和整卷报告见 `README.md`。

## 固定与交班

- `evidence.publish.publish_retest(recordings, store, recording_id, evidence_id, tick)` 从 `RecordingLedger` 读取指定封记录制，在 `SegmentStore` 固定复测件并返回件号。`tick` 为覆盖内的非布尔整数原拍号，可以为首拍或尾拍。
- `evidence.export.export_retest(store, evidence_id, destination)` 按号导出已固定件，并返回写入目标的 `bytes`。沿用 V1 导出错误和目标替换语义。重复导出同一固定件产生同一字节。复测件号对应不可改写的内容。

固定件绑定卷、完整计划修订、录制号、含首尾的完整覆盖及替换拍。它保存来自同一录制、足以核定任意有理替代偏差的检测行为。逐拍偏差明细留在站端。复测件须可与普通 V1 件区分，并可独立解析和校验。内部行为表示由实现决定。质控端核对绑定、全覆盖、替换接缝及告警原拍范围。替换拍两侧的空段对应首拍或尾拍，单拍录制的两侧均为空。

未知录制号或导出件号报 `KeyError`。非法原拍、无效请求及无法接纳的件报 `ValueError`。固定失败不留下新件，SQLite 写入失败向调用方传播。

## 离线签发与领取

`reports.issue.issue_retest(archive, assignment, coverage, recording_id, tick, payload, replacement, required_alarms, forbidden_alarms, final_amount)` 接收独立报告端的 `ArchiveStore`、预先固定的 `RollAssignment`、`TickCoverage`、录制号、替换原拍以及唯一指定复测件的字节。卷、完整计划、录制、覆盖及替换拍须与件内绑定一致，结构错误及混源接缝整次拒绝。

`replacement` 是候选替代偏差闭区间，`final_amount` 是目标末量闭区间。两者各传入一个含两个端点的列表或元组，端点沿用 V1 精确有理输入规则，低端须小于或等于高端。`required_alarms` 与 `forbidden_alarms` 可传原拍列表、元组或集合，按集合解释。两集合互不相交，每项须为覆盖内的非布尔整数。

每个候选值是该拍的完整偏差。整卷从零开始，其余各拍保留固定录制行为。各拍加偏差后负值截零，达到或超过阈值时在该原拍告警并清零。候选可行须同时满足全部必报、全部禁报和末量闭区间。签出全部有理可行值。

签发返回含 `report_id`、`result`、`receipt` 的记录。`receipt` 提供 `receipt_id`、`report_id`。`result` 保留 `assignment`、`coverage`、`recording_id`、`tick`、`replacement`、`required_alarms`、`forbidden_alarms`、`final_amount`、`reference` 和 `pieces`。两闭区间以一对 `Fraction` 端点读回，告警条件按集合语义保存。`reference` 提供 `evidence_id` 及 `digest`，摘要为指定导出字节的 SHA-256 小写十六进制值。

`pieces` 为按候选值升序排列的互不相交区间序列。每段提供 `lo`、`hi`、`lo_closed`、`hi_closed`、`alarms`、`slope` 和 `intercept`。端点及截距为 `Fraction`，开闭标记为布尔值，告警为递增原拍元组，斜率为精确整数或 `Fraction`。该段每个候选值 `x` 的末量为 `slope * x + intercept`。单点两端都闭合，可使用在该点给出正确值的仿射式。按区间及规则的语义验收，允许等价分段。空可行集照常签发，`pieces` 为空。

报告与回执同次提交，签发拒绝或写入、提交失败时均无新增报告或回执，解除故障后可重试。`archive.read.read_retest(archive, report_id)` 按号直接领取已存请求、结果及配对回执，重开报告库后结果一致。返回对象提供 `requested_id`、`found` 和 `report`，其中 `report` 与签发结果形状相同。未知报告号返回 `found=False`、`report=None`。保留 V1 的入口、数据格式及原回归。

## 样例与核验

`retest_examples.py` 提供阈值 5、从第 1 拍起的四份录制。直接运行会核对 `short-a` 与 `short-b` 的 V1 整段响应相同，并展示两个普通结果。以下是新增能力的手算结果，告警用原拍号表示。

| 固定录制及替换拍 | 候选闭区间 | 末量闭区间 | 告警条件 | 可行区间及规则 |
| --- | --- | --- | --- | --- |
| `short-a` (5, −1)，第 1 拍 | [3,3] | [2,2] | 无 | [3,3]，无告警，末量 2 |
| `short-b` (5, −2)，第 1 拍 | [3,3] | [2,2] | 无 | 空集 |
| `middle` (4, 0, 2)，第 2 拍 | [−6,3] | [2,2] | 无 | [−6,−4] 无告警，[1,3] 仅第 2 拍告警，两段末量均为 2 |
| `middle`，第 2 拍 | [−6,3] | [0,0] | 必报第 3 拍，禁报第 2 拍 | [−1,1)，仅第 3 拍告警，末量 0 |
| `middle`，第 2 拍 | [−6,3] | [5/2,7/2] | 无 | [−7/2,−5/2]，无告警，末量为替代值加 6 |
| `tail` (0, 2)，第 2 拍 | [0,4] | [3,3] | 无 | [3,3]，无告警，末量 3 |

下面是新增入口接通后的调用示例。`Station` 是已有回归夹具。

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from archive.commit import ArchiveStore
from archive.read import read_retest
from evidence.export import export_retest
from evidence.publish import publish_retest
from reports.issue import issue_retest
from retest_examples import DEVIATIONS
from tests.support import Station

with TemporaryDirectory() as directory:
    root = Path(directory)
    station = Station(root / 'station', 'roll-retest', 'r1', 5)
    coverage = station.seal('middle', 1, DEVIATIONS['middle']).coverage
    assignment = station.assignment
    publish_retest(station.recordings, station.segments, 'middle', 'check-2', 2)
    payload = export_retest(station.segments, 'check-2', root / 'check-2.retest')
    station.detach()

    archive = ArchiveStore(root / 'quality.sqlite')
    archive.initialize()
    issued = issue_retest(archive, assignment, coverage, 'middle', 2, payload,
                          (-6, 3), (), (), (2, 2))
    lookup = read_retest(ArchiveStore(archive.path), issued.report_id)
    assert lookup.found and lookup.report == issued
    print(lookup.report.result.pieces)
```

复用原 `.pointwise-runtime.json` 清单及其 `setup`、`verify`、`demo` 命令约定。扩展 `verify` 接通全部原回归和复测核验，核验失败以非零退出码退出。核验应能发现同卷异录制误领和提交失败留下半份成果。`demo` 展示真实的断源签发和重开领取，输出实际区间、告警、末量规则和回执。原样例命令继续可用。
