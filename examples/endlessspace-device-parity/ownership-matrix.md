# 文件所有权矩阵（P1–P4）

> 来源：`Doc/godot/plans/2026-10-04-device-parity-remediation-plan.md` §4.2。
> 矩阵按「文件」分组——**同一组的子任务 = 一条串行链**，只能顺序执行。这是 `waves.md` 波次划分的唯一依据（并行度上限 = 独立链数）。

## 视图一：按文件分组的串行链表

### TECH（T 链几乎吞并全部 TECH）

| 文件 | 子任务 | 性质 |
| --- | --- | --- |
| `presentation/tech/tech_view.gd` | P1-1, P1-2✅, P1-3, P1-4, P1-5, P1-6, P1-8, P1-9✅, P1-10✅ | **串行链 T** |
| `presentation/tech/tech_weapon_icon.gd` | P1-5 | 随 T |
| `presentation/tech/ship_path_silhouette.gd` | P1-6 | 随 T |
| `tools/flows/tech_*.json` + `tests/tools/test_tech_parity_flow_contract.py` + `tools/tech_parity_cases_v1.json` | P1-11 | 独立 |
| `out/godot/parity/tech/` + `parity-matrix.md` | P1-12 | 收口热点 |

### SHIPYARD

| 文件 | 子任务 | 性质 |
| --- | --- | --- |
| `presentation/shipyard/shipyard_view.gd` | P2-1, P2-2, P2-3, P2-4, P2-5, P2-6, P2-7 | **串行链 S** |
| `presentation/shipyard/shipyard_layout.gd`（P2-1 抽出的新文件） | P2-1 | 随 S |
| `presentation/root/ui_root.gd` | P2-8 | 独立链 S′（安全区，亦属全局热点） |
| `tools/flows/shipyard_*.json` | P2-10 | 独立 |
| `tests/presentation/test_shipyard_view.gd` | P2-12 | 独立 |
| `out/godot/parity/shipyard/` | P2-9, P2-11 | 证据 |

### EQUIP（本域最可并行）

| 文件 | 子任务 | 性质 |
| --- | --- | --- |
| `domain/equip/armory_bootstrap.gd` | P3-1, P3-7, P3-11, P3-12 | **串行链 E1** |
| `presentation/equip/equip_view.gd`（及 `presentation/equip/*`） | P3-3, P3-4, P3-5, P3-8 | **串行链 E2** |
| `domain/equip/legacy_armory_importer.gd` | P3-2 | 独立链 E3 |
| `domain/equip/equip_service.gd` | P3-6 | 独立链 E4 |
| `tools/flows/equip_*.json` | P3-9 | 独立 |
| `out/godot/parity/equip/` | P3-10 | 证据 |

### BATTLE（按真实文件重切，10 条链）

| 文件 | 子任务 | 性质 |
| --- | --- | --- |
| `presentation/tactical_hud/battle_radar_view.gd` | P4-7, P4-9 | **串行链 B1**（雷达本体与五按钮 affordance 同一文件） |
| `presentation/helios/tactical_3d_layer.gd` | P4-6 | 独立链 B2 |
| `presentation/helios/tactical_camera_mapper.gd` | P4-1 | 独立链 B3 |
| `app/tactical/tactical_node_shell.gd` | P4-3 | 独立链 B4 |
| `presentation/environment/tactical_backdrop.gd` | P4-4 | 独立链 B5 |
| `app/tactical/tactical_combat_controller.gd` + `domain/tactical/hostile_engagement_planner.gd` | P4-5 | 独立链 B6 |
| `presentation/tactical_hud/battle_group_rail.gd` + `battle_group_slot_visual.gd` | P4-8 | 独立链 B7 |
| `presentation/tactical_hud/fleet_status_plate.gd` | P4-11 | 独立链 B8 |
| `presentation/tactical_hud/system_status_plate.gd` | P4-10 | 独立链 B9 |
| `presentation/tactical_hud/world_feedback_layer.gd` | P4-2 | 独立链 B10（IFF 环实际绘制处） |
| `out/godot/parity/battle/` | P4-12 | 证据 |

## BATTLE 勘误：初版 7 条链 → 校正为 10 条链

初版把 `presentation/tactical_hud/*` 当成一条链（B1）、把雷达单列 B7，且路径写成 `presentation/tactical/*`。回到真实文件核对后修正：

1. **路径勘误**：`tactical_3d_layer.gd` / `tactical_camera_mapper.gd` 位于 `presentation/helios/`，**不是** `presentation/tactical/`（仓库中根本不存在 `presentation/tactical/` 目录）。
2. **五按钮归属**：右上五按钮（SHIPS/FLEET/TECH/RETREAT/MAP）是 `battle_radar_view.gd :: _ensure_affordances` 里的 affordance，与 P4-9（雷达尺寸/红点）**同文件**，故合并为同一条链 B1，而非拆成两条。
3. **IFF 真实落点**：IFF 环实际画在 `presentation/tactical_hud/world_feedback_layer.gd :: _render_iff`，**不是** `tactical_3d_layer.gd`，故 B10。
4. **子视图拆分**：P4-8 / P4-10 / P4-11 各落在不同子视图（`battle_group_rail.gd` / `system_status_plate.gd` / `fleet_status_plate.gd`），互不重叠，各自成链 B7 / B9 / B8。

结论：B1 收窄为雷达单文件，BATTLE 由初版误判的 **7 条链细化为 10 条链**（细粒度切分把真实并行度从误判的 4 提升到 10）。**矩阵必须回到真实文件核对，不能凭目录名想象。**

## 视图二：全局共享热点

（任何波次里都只允许一个持有者，且尽量留到收口。）

| 热点文件 | 说明 / 处置 |
| --- | --- |
| `Doc/godot/parity-matrix.md` | 复刻矩阵状态真源；P1-12 / P2-11 / P3-10 / P4-12 都改，收口串行 |
| `Godot/tools/ui_parity_cases_v1.json` | parity catalog 冻结计数；P1-11 · P2-10 · P3-9 · P4-12 都改它 → 合并为一条**收口串行链 C** |
| `Godot/tests/test_runner.gd` | 测试套件注册；只在收尾集成时改 |
| `Godot/project.godot` | 工程配置；串行 |
| `Godot/app/main.gd` / `Godot/app/main.tscn` | 主入口；串行，只在收尾集成时改 |

## 并行度上限 = 独立链数

T=1、S=1、S′=1、E=4、B=10 → 理论峰值 ≈ **17 并发**（收口链 C 见 `waves.md` W8）。这才是「≥10 并行」的真正来源，且**必须跨域取，不能同域取**。
