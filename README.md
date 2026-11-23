# 电竞直播赞助的去重触达与保量履约

本地可运行的项目，帮助商务团队回答四类问题：

1. 某个赞助合同到底完成了多少合格触达/观看分钟，与原始回传差在哪里；
2. 哪些播放属于同一家庭、哪些联系仍未裁定、哪些根本无法关联；
3. 已认定交付和已开票金额中，有多少能被当前证据支持；
4. 用未来场次、受众调查和可用广告段补量时，尚有多少缺口无法保证；
5. 迟到关联、可见性更正或素材裁决到达后，重核缺口、待议价与账款差额，
   同时保留实际播放与客户已接受的事实。

全部资料为本地合成数据，参与方均使用虚构标识。项目只依赖 Python 标准库；
`requirements.txt` 保留资料阅读所需的确定性版本；应用本身只使用标准库。

## 安装与运行

需要 Python 3.11 至 3.14。建议使用独立虚拟环境：

```sh
python3 -m venv .venv
. .venv/bin/activate            # Windows PowerShell: .venv\Scripts\Activate.ps1
python3 -m pip install -r requirements.txt   # 可选
python3 tools/check_materials.py             # 校验资料指纹、字段与直接外键
python3 -m unittest discover -s tests -v
```

各命令首次运行会自动把 `data/` 接收进 `state/ledger.sqlite3`，之后重复运行
不会重复接收原始资料。`state/` 与 `outputs/` 已从 Git 排除。

```sh
python3 -m sponsor_ops init                 # 首次接收并建账本
python3 -m sponsor_ops status               # 批次、表行数与当前截止日
python3 -m sponsor_ops contract C-drink-a   # 合同交付核对
python3 -m sponsor_ops trace C-drink-a      # 逐条计入的合格曝光
python3 -m sponsor_ops reach C-drink-a      # 触达/分钟的口径与不确定性说明
python3 -m sponsor_ops break B135           # 广告段已交付或占用给谁
python3 -m sponsor_ops invoice INV-A        # 票面金额、可支持金额与差额
python3 -m sponsor_ops plan C-drink-a --level conservative
python3 -m sponsor_ops plan                 # 全部合同的补量组合，互不抢同一广告段
python3 -m sponsor_ops accept Q-A B135 --on 2026-08-30
python3 -m sponsor_ops adjust C-drink-a --on 2026-08-31 \
    --reason "迟到家庭合并后重核" --amount-minor -96000
python3 -m sponsor_ops export --out outputs
```

所有查询命令接受 `--as-of YYYY-MM-DD`，只使用该日及以前到达的资料。
`--db` 可指定另一份账本，便于并行核对不同批次。

## 口径

### 原始播放 → 合格交付 → 已认定交付

- **原始播放**：平台收到的每一行回传。`dedupe.mirror_rows_collapsed` 是同一
  `origin_event` 的镜像副本，`repeated_callback_units_collapsed` 是同一设备、
  同一场次、同一广告段的不同回传。二者都只形成一次播放，不能按行数或事件数
  直接相加。
- **合格交付**：先通过合同明确条件（同意计量、地区、受众层、渠道、合同期、
  素材版本、可见至少 30 秒、有监测、非后台），再通过广告段排他与竞品归属，
  最后按合同期频次上限结算。每一类剔除都单独报告原因：后台播放、缺监测、
  可见不足、地区/受众层/渠道不符、合同期外、素材版本未认可、上架失败。
- **已认定交付**：`recognized_delivery` 是商务当时签发的认定。项目逐条检查它
  今天是否仍有证据支持；不支持的认定单列原因，但不修改历史认定本身。

### 家庭与不确定性

- `confirmed` 联系按 `effective_from` 生效，同一家庭在电视和手机上的播放只计
  一户。`possible` 联系不并入明确家庭，只在 `upper` 中作为"可能同一户"披露。
- 没有任何联系证据的设备单列为 `unlinked`，不强行分配家庭。
- 迟到合并证据只在 `effective_from` 之后生效；`received_on` 决定商务当时是否
  已经知道。签约前不知道的受众结构，不会因为事后收到证据而"追溯已知"。
- 触达与分钟都给出区间：`lower` 只含明确联系家庭，`upper` 含 `possible` 家庭；
  两者之间是资料无法唯一归属的范围。

### 广告段与竞品

- 排他广告段只能归属一个合同。若已有客户接受的占用，其他合同不能主张；
  若没有明确占用但多个合同都有合格播放，该段保留"待认定"，不直接算给任何一方。
- 同品类竞品出现在同一排他段时，标记为 `competitor_conflict`，需商务先裁定。
- 非排他段只有在所有主张合同都允许并计（`allow_parallel_credit`）时才并计。
- 同一片段可以同时满足品牌露出与观看分钟两项约定（两个合同各自核对），
  但明确排他的曝光只交付给一个合同。

### 补量计划

- 调查按 `panel_household` 去重，同一调查家庭跨场次只算一次；已有明确联系的
  调查行与已认定触达精确去重。没有联系身份的行按纳入概率折算为"可能新增"
  上界，不当作确定家庭。
- 同一场次追加广告段不会新增触达家庭；对观看分钟只按剩余频次上限增加重复曝光。
- 每选一段都更新剩余缺口、预算与广告段占用，不能把每场预测数直接相加。
- 事后调查只用于回测校准（`coverage_ratio`），不把未来意愿当成实绩。场次
  主市场与调查地区不一致、没有同地区/同渠道历史、或调查尚未到达时，保留
  缺证范围并给出提示。
- 三种保守程度：`conservative` 只计明确新增；`balanced` 按校准覆盖率折算；
  `aggressive` 使用按纳入概率还原的上界。商务可以横向比较，计划里明确标出
  仍无法保证的部分（`remaining_lower` / `remaining_upper` / `unguaranteed`）。
- 组合计划按缺口从大到小分配广告段，已经被客户接受的广告段优先保留，避免
  为了补一个大客户挤掉其他已确认的赞助。

### 补量接受与账款

- 只有客户接受补量后才形成新的场次承诺（`makegood_requests.accepted_breaks`
  或 `accept` 命令）。`all_or_none` 或不可拆分合同的整场权益必须整体落实；
  可拆分合同允许部分成立。
- 平台上架回执（`asset_online`）决定素材实际展示起点；早于上架回执的场次
  不能被当作已交付。上架失败（`asset_failed`）的广告段不能算已交付。
- 账单差额 = 当前证据可支持金额 − 票面金额；已开票、已收款分别保留，不被
  新的去重结果覆盖。`adjust` 只登记调整建议，供商务议价或开票时使用。

## 典型核对流程

以 `C-drink-a` 为例（数字会随资料更新变化）：

```sh
python3 -m sponsor_ops contract C-drink-a
```

- `delivery.lower` 与 `delivery.upper` 给出确定触达与可能触达的范围；
  `delivery.unlinked` 是无联系证据的设备，不并入家庭数。
- `delivery.qualified_units` 是通过合同条件与排他归属的播放单元，
  `counted_units` 是再经频次上限结算后的数量；两者不同说明频次上限生效。
- `dedupe` 显示原始回传如何收敛到可计量播放单元。
- `excluded_units` 按后台播放、缺监测、可见不足、地区/受众层等分别计数；
  `excluded_unit_counts` 给出中文原因。
- `recognitions.supported_units` 与 `variance_minor` 说明已认定交付中仍有
  多少不被支持，但不消除已开票与已收款事实。每条认定还带有
  `supported_value` 与 `recognized_value`，便于逐条议价。

```sh
python3 -m sponsor_ops plan C-drink-a --level conservative
```

计划逐段列出 `break`、`known_new_households`、`unknown_weighted_households`、
`repeat_known_households`、`capped_households`、`evidence_gap` 与 `level_value`，
最后给出剩余缺口和无法保证的部分。若某段已由其他合同接受，会在 `skipped`
中标为 `break_committed_to_other_contract`。

以当前合成资料为例，`C-drink-a` 的保守计划把 120,000 美分预算用掉约一半，
仍留下确定的触达缺口；平衡与进取口径能看到调查受众上界带来的额外可能范围，
但这些范围不会被写成"已保证"。这正是商务需要和客户谈的部分。

## 从合同追到曝光，从广告段追到归属

- `trace <合同>`：每条计入的播放单元、所在场次/广告段、设备、家庭实体、
  可见秒数区间与回传数量。
- `break <广告段>`：该段的库存承诺、客户接受、上架失败与各合同的合格/待定/
  剔除播放。
- `invoice <账单>`：票面、可支持认定、差额、已收款与未收余额。

## 资料接收

`data/catalog.json` 规定表、字段类型、主键、直接外键与 SHA-256 指纹。
`tools/check_materials.py` 只做资料完整性检查，不产生业务结论。

外部批次（同一契约、新的回传或后续证据）可以用：

```sh
python3 -m sponsor_ops ingest /path/to/batch
```

批次目录需要 `data/catalog.json` 和同名 JSONL。接收前会把增量与既有账本按
主键合并，并校验合并后的跨表引用；校验失败时整批拒绝，不会留下半批数据。
后续场次回传进入对应合同期后，`contract` / `plan` 会自动重算缺口。

迟到证据也可以直接写入 `later_evidence` 表后重新接收，支持的类别：

- `household_confirm`：把若干设备确认到同一家庭，带 `effective_from`；
- `visibility_revision`：更正被点名播放事件的可见秒数或监测状态；
- `asset_online`：某合同素材在某广告段实际上架；
- `asset_failed`：某合同素材在某广告段上架失败。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

- `tests/test_materials.py`：资料指纹、字段与外键损坏检测。
- `tests/test_app.py`：合成小世界中的迟到家庭关联、重复回传、后台/缺监测/
  未关联的分别解释、竞品排他争用、已认定重复家庭、补量拆分接受。
- `tests/test_increment.py`：后续回传进入合同期、期外回传不计入、可见性更正
  削减已计入曝光、坏批次整批拒绝。测试中的小世界同时包含迟到家庭关联、
  已认定交付与竞品补量争用。
- `tests/test_real_data.py`：在 `data/` 上核对原始/合格/认定三层不混同、迟到
  证据改变缺口但不改写已开票事实、组合计划不重复占用广告段。

## 目录

```
data/               合成资料（只读，catalog.json 登记指纹）
docs/资料契约.md     字段与口径说明
sponsor_ops/        应用
  materials.py      资料读取与校验
  store.py          SQLite 账本与增量接收
  identity.py       家庭联系与去重
  playback.py       回传去重与播放单元
  delivery.py       合同条件、排他、频次与认定对账
  planning.py       补量预测、校准与组合计划
  service.py        查询与人工决定
  __main__.py       命令行
tests/              合成资料与测试
tools/              资料检查入口
state/              本地账本（Git 排除）
outputs/            导出目录（Git 排除）
```

## 已知边界

- 项目不推测未知设备的家庭归属；`unlinked` 是未关联而非"估计户数"。
- 调查加权只使用 `inclusion_probability`，没有额外的人口结构校准。
- 校准覆盖率来自历史调查与计量触达的总体比例，不针对单个地区或渠道；没有
  可比历史的场次会明确标注缺证。
- 竞品争用只按合同的 `category` 判断，未使用真实商标或品类层级。
- 账单调整只登记建议金额，不自动改写发票或收款记录。
