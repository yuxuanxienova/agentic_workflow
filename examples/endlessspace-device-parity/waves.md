# 跨域并行波次（W1–W8）

> 来源：`Doc/godot/plans/2026-10-04-device-parity-remediation-plan.md` §4.3。
> 核心约束：**同一波次内的任务互不改同一文件**，因此无需合并、不会分叉。波次划分依据 `ownership-matrix.md` 的串行链。

## 波次清单

- **W1（峰值 17 并发）**：T:`P1-1` ｜ S:`P2-1` ｜ S′:`P2-8` ｜ E1:`P3-1` ｜ E2:`P3-3` ｜ E3:`P3-2` ｜ E4:`P3-6` ｜ B1:`P4-7` ｜ B2:`P4-6` ｜ B3:`P4-1` ｜ B4:`P4-3` ｜ B5:`P4-4` ｜ B6:`P4-5` ｜ B7:`P4-8` ｜ B8:`P4-11` ｜ B9:`P4-10` ｜ B10:`P4-2`
- **W2（5 并发）**：T:`P1-3` ｜ S:`P2-2` ｜ E1:`P3-7` ｜ E2:`P3-4` ｜ B1:`P4-9`
- **W3（4 并发）**：T:`P1-4` ｜ S:`P2-3` ｜ E1:`P3-11` ｜ E2:`P3-5`
- **W4（4 并发）**：T:`P1-5` ｜ S:`P2-4` ｜ E1:`P3-12` ｜ E2:`P3-8`
- **W5（2 并发）**：T:`P1-6` ｜ S:`P2-5`
- **W6（2 并发）**：T:`P1-8` ｜ S:`P2-6`
- **W7（1）**：S:`P2-7`
- **W8（收口·串行链 C + 各域证据归档）**：C:`P1-11`→`P2-10`→`P3-9`→`P4-12`（同改 `ui_parity_cases_v1.json` 与 `parity-matrix.md`，必须串行）；随后归档 `P1-12`、`P2-11`、`P2-9`、`P2-12`、`P3-10` 证据。

## 逐波「波内无文件重叠」验证

以下为人工核对 `ownership-matrix.md` 后逐波的结论（每个任务取其在矩阵中的归属文件）。

**W1 ✅ 无重叠**——17 个任务分别取自 17 条不同链，每条链在本波只出 1 个任务，故两两文件不相交：
`P1-1`→`tech_view.gd`；`P2-1`→`shipyard_view.gd`+`shipyard_layout.gd`；`P2-8`→`ui_root.gd`；`P3-1`→`armory_bootstrap.gd`；`P3-3`→`equip_view.gd`；`P3-2`→`legacy_armory_importer.gd`；`P3-6`→`equip_service.gd`；`P4-7`→`battle_radar_view.gd`；`P4-6`→`tactical_3d_layer.gd`；`P4-1`→`tactical_camera_mapper.gd`；`P4-3`→`tactical_node_shell.gd`；`P4-4`→`tactical_backdrop.gd`；`P4-5`→`tactical_combat_controller.gd`+`hostile_engagement_planner.gd`；`P4-8`→`battle_group_rail.gd`+`battle_group_slot_visual.gd`；`P4-11`→`fleet_status_plate.gd`；`P4-10`→`system_status_plate.gd`；`P4-2`→`world_feedback_layer.gd`。无任何文件重复。

**W2 ✅ 无重叠**——5 条不同链各取 1：
`tech_view.gd` / `shipyard_layout.gd`+`shipyard_view.gd` / `armory_bootstrap.gd` / `equip_view.gd`+`blueprint_grid.gd`+`slot_guides.gd` / `battle_radar_view.gd`。互不相交。

**W3 ✅ 无重叠**——`tech_view.gd` / `shipyard_view.gd` / `armory_bootstrap.gd` / `equip_view.gd`，分属 T/S/E1/E2 四条链。

**W4 ✅ 无重叠**——`tech_weapon_icon.gd`+`tech_view.gd` / `shipyard_layout.gd`+`shipyard_view.gd` / `armory_bootstrap.gd` / `equip_view.gd`，分属 T/S/E1/E2。

**W5 ✅ 无重叠**——`ship_path_silhouette.gd`+`tech_view.gd` / `shipyard_view.gd`+`shipyard_p2_5_tabs_center.json`，分属 T/S。

**W6 ✅ 无重叠**——`tech_view.gd`+`tech_p1_8_domain_cycle.json` / `shipyard_view.gd`+`shipyard_l2_upgrade.json`，分属 T/S。

**W7 ✅ 无重叠**——单任务 `P2-7`→`shipyard_view.gd`。

**W8 ✅ 无重叠（但为强制串行）**——链 C 四个任务同改 `ui_parity_cases_v1.json` 与 `parity-matrix.md`，**必须顺序执行** `P1-11`→`P2-10`→`P3-9`→`P4-12`；其后的证据归档（`P2-11` 与 `P2-9` 同在 `out/godot/parity/shipyard/` 树下、`P1-12`/`P2-11`/`P3-10`/`P4-12` 又同改 `parity-matrix.md`）同样串行。

## 特别说明：W1 峰值 17 = 独立链数

W1 之所以能到 17 并发，是因为它**每条链只取第一个任务**：T、S、S′、E1–E4、B1–B10 共 **1+1+1+4+10 = 17 条独立链**，恰好等于 §4.2 推算的并行度上限（T=1、S=1、S′=1、E=4、B=10 → 峰值 ≈17）。换言之，**峰值并发数不是任务的多少决定的，而是独立文件链的条数决定的**；链一旦在前一波被占用，后续波次只能依次收缩（17→5→4→4→2→2→1→串行）。

## 其它约束

- **P0 必须先做完**（≥1 天前半天），否则四波会各造一套坐标系继续错。
- **同链内的子任务严格顺序执行**（例：`tech_view.gd` 上的 P1-1→P1-3→P1-4→P1-5→P1-6→P1-8 绝不能并行）。
- 若并发太高难协调，可先把 W1 压到「每域取 1~2 条链」，再逐波放开；**但绝不可把同域的多个任务放进同一波**。
- P5 收口依赖 P1–P4 全部 L2 通过。
