# 任务清单（P1–P4 子任务表摘录）

> 来源：`Doc/godot/plans/2026-10-04-device-parity-remediation-plan.md` §6–§9。
> 本文件保留六列 `ID | 子任务 | 真源 | 落点 | 验收 | 改动文件`；**改动文件列与计划文档逐字一致**，是 §4.2 文件所有权矩阵与 `waves.md` 波次划分的唯一数据源。
> 描述文字为便于阅读做了轻度精简（真源/落点以计划文档链接为准）。

## 任务解读：从模糊目标到四个域

原始的模糊目标是**「把 Swift 版一比一迁移成 Godot 版」**——表述上只有一个「迁移」，既没说清范围，也没给出可并行的切法。框架要求把它**树状搜索成影响域**，再落到可执行的任务：

1. **候选影响域（一级域树）**：对着 Swift 真源目录（`EndlessSpace/UI`、`Factories`、`Managers`）与 Godot 复刻目录（`Godot/presentation`、`domain`、`app`）逐页比对，穷举出所有需要迁移的产品面。结果见 `domains.json`：`shell / homebase / tech / shipyard / equip / market / spaceport / store / battle / strategic_map / fleet / war / economy / progression / persistence / devtools` 共 16 个一级域。
2. **收窄到「真机实测有差异」的域**：用户 iPhone 真机实测一次性报告了 **TECH / SHIPYARD / EQUIP / BATTLE** 四个域的问题（船厂错位、装备页空白、战斗无开火且比例全错、科技图标错误）。这四个域即落在本计划的 P1–P4 四批。
3. **从「功能域」切到「文件」**：这是本示例最关键的一步。初版错误地把批次轴定成「功能域」（P1=TECH…），并要求每批 ≥10 并行。但一个功能域在文件层面往往只有 1 个文件（TECH 12 个任务里 10 个改 `tech_view.gd`），同域真实并行度≈1。正确做法是**按文件聚合出串行链**，并行只能跨域取——详见 `ownership-matrix.md` 与 `waves.md`。

## P1 批次：TECH

| ID | 子任务 | 真源 | 落点 | 验收 | 改动文件（仓库相对路径） |
|---|---|---|---|---|---|
| P1-1 | research 栏滚动高度抖动修复（内容重建后再算 `custom_minimum_size`，去竞态） | — | `tech_view.gd#L363-L378` | L1 反复滚动 3 次截图一致 | `Godot/presentation/tech/tech_view.gd` |
| P1-2 ✅ | `ladder_scroll` reflow 时机修正（`_rebuild_tracks` 后 `queue_sort`/下一帧再量高） | — | `tech_view.gd#L703-L722` | L1 域切换后滚动无跳变 | `Godot/presentation/tech/tech_view.gd` |
| P1-3 | 节点图标规则对齐：effect 舰名需经注册表校验，未命中回退武器/类目 | `HomebaseTechTreeUI#L2109-L2126` | `tech_view.gd#L516-L533` | L2 defense 域并排一致 | `Godot/presentation/tech/tech_view.gd` |
| P1-4 | defense 类目 glyph 复刻（替换 `⬡`） | `buildTechCategoryIcon(.defense)` | `tech_view.gd#L923-L929` | L2 并排 | `Godot/presentation/tech/tech_view.gd` |
| P1-5 | TECH 武器图标坐标/造型复刻（SpriteKit y-up 映射、精确 FNV-1a、core 混色及炮/激光/导弹/PD/Lance 分型；详情标题类别图标换 Swift 矢量瞄准 glyph；EQUIP/MARKET 不变） | `WeaponIconFactory.makeIcon` | `tech_weapon_icon.gd`；`tech_view.gd` | TECH runner + 真输入 `tech_l2.json` 主场景流程通过 | `Godot/presentation/tech/tech_weapon_icon.gd`；`Godot/presentation/tech/tech_view.gd` |
| P1-6 | ship silhouette 复刻 | `ShipFactory.createShipPath` | `_add_ship_portrait` | L2 并排 | `Godot/presentation/tech/ship_path_silhouette.gd`；`Godot/presentation/tech/tech_view.gd` |
| P1-7 ✅ | 右侧 details ScrollContainer 稳定性：保留容器引用、只允许纵向滚动；详情内容重建后延迟归顶 | `detailScrollArea` | `tech_view.gd`；`tech_detail_scroll_p1_7.json` | L1 overflow + 真实 viewport wheel 滚动断言通过 | `Godot/presentation/tech/tech_view.gd`；`Godot/tools/flows/tech_detail_scroll_p1_7.json` |
| P1-8 | 域切换时滚动位置与内容重建一致性 | `loadDomain()` | `_rebuild_tracks()` | L1 域循环断言 | `Godot/presentation/tech/tech_view.gd`；`Godot/tools/flows/tech_p1_8_domain_cycle.json` |
| P1-9 ✅ | 船影 39×39、武器 27×27，中心对齐 Swift `(26pt,27pt)` 经 1.5× 换算的 `(39,40.5)`；仅研究船影取消头像默认留白 | `nodeIcon` | `tech_view.gd`；尺寸/中心断言见 `test_tech_view_runner.gd` | Godot TECH 视图 runner 通过 | `Godot/presentation/tech/tech_view.gd`；`Godot/tests/progression/test_tech_view_runner.gd` |
| P1-10 ✅ | DEFENSE 生效项图标逐节点静态核对：93 节点（42 船影 / 51 防御盾牌 / 0 武器），目录与 Godot 资源路由无缺项 | `nodeIcon` | `_research_art` | 逐节点对照表；静态审计完成，非 L2 | `Godot/presentation/tech/tech_view.gd` |
| P1-11 | tech parity catalog + 动作流（域切换/滚动/图标断言） | — | `Godot/tools/flows/tech_*.json` | L1 三连过 | `Godot/tools/flows/tech_*.json`；`Godot/tools/tech_parity_cases_v1.json`；`Godot/tests/tools/test_tech_parity_flow_contract.py`；`Godot/tools/ui_parity_cases_v1.json`；`Doc/godot/parity-matrix.md` |
| P1-12 | TECH L2 双端并排（≥4 case） | — | `out/godot/parity/tech/` | review.json accepted | `out/godot/parity/tech/`；`Doc/godot/parity-matrix.md` |

## P2 批次：SHIPYARD

> 核心：**用 Swift 比例模型重写布局，而非修补常量**。

| ID | 子任务 | 真源 | 落点 | 验收 | 改动文件（仓库相对路径） |
|---|---|---|---|---|---|
| P2-1 | 布局模型重构：常量 → `panelWidth/contentHeight` 比例推导函数 | `HomebaseShipyardUI#L371-L443` | `shipyard_view.gd#L51-L89` | L1 比例矩阵不错位 | `Godot/presentation/shipyard/shipyard_view.gd`；`Godot/presentation/shipyard/shipyard_layout.gd` |
| P2-2 | 三栏宽度比例 0.28/0.30/0.38 对齐 | 同上 | `DEPLOYED_RECT`/`PREVIEW_RECT`/`RIGHT_RECT` | L2 并排 | `Godot/presentation/shipyard/shipyard_layout.gd`；`Godot/presentation/shipyard/shipyard_view.gd` |
| P2-3 | 右栏 STATISTICS/ENERGY 垂直布局（修"energy 压橙色线"） | `buildRightPanel` | `shipyard_view.gd#L578-L625` | L2 图1 复现修复 | `Godot/presentation/shipyard/shipyard_view.gd` |
| P2-4 | 左栏 DEPLOYED/RESERVE 高度比例 0.42/0.52（修 CL/DD 越界） | `HomebaseShipyardUI#L386-L414` | `shipyard_view.gd#L52-L53` | L2 | `Godot/presentation/shipyard/shipyard_layout.gd`；`Godot/presentation/shipyard/shipyard_view.gd` |
| P2-5 | INFO/UPGRADE 页签在右栏内居中（修"没在按钮中间"） | `buildRightPanel` | `shipyard_view.gd#L630-L643` | L2 居中度量 | `Godot/presentation/shipyard/shipyard_view.gd`；`Godot/tools/flows/shipyard_p2_5_tabs_center.json` |
| P2-6 | UPGRADE 5 行布局比例化（Swift 36:4 行距比例，随右栏内容区高度均分） | `buildUpgradePage` | `shipyard_view.gd`；`shipyard_l2_upgrade.json` | L1 几何边界断言；L2 对照待 review | `Godot/presentation/shipyard/shipyard_view.gd`；`Godot/tools/flows/shipyard_l2_upgrade.json` |
| P2-7 | 关闭按钮 / 预览区按比例定位 | `ShipyardUI` | `CLOSE_RECT`/`PREVIEW_RECT` | L2 | `Godot/presentation/shipyard/shipyard_view.gd` |
| P2-8 | 安全区 + 宽高比 letterbox 策略统一 | — | `ui_root.gd#L79-L115` | L1 多比例 | `Godot/presentation/root/ui_root.gd`；`Godot/tests/presentation/test_ui_root.gd` |
| P2-9 | 宽高比回归（1398×645 / 2556×1179 / 1:1）矩阵截图 | — | `out/godot/parity/shipyard/ratio/` | 三比例全不越界 | `out/godot/parity/shipyard/ratio/`；`Godot/tools/flows/shipyard_ratio_*.json` |
| P2-10 | shipyard parity catalog + 动作流（INFO/UPGRADE/滚动/选舰） | — | `tools/flows/shipyard_*.json` | L1 三连过 | `Godot/tools/flows/shipyard_*.json`；`Godot/tools/ui_parity_cases_v1.json`；`Doc/godot/parity-matrix.md` |
| P2-11 | SHIPYARD L2 双端并排（INFO + UPGRADE ≥4 case） | — | `out/godot/parity/shipyard/` | review.json accepted | `out/godot/parity/shipyard/`；`Doc/godot/parity-matrix.md` |
| P2-12 | 回归：确认 R0-3 卡片横拖修复在新布局下仍生效 | — | 既有测试 | L0 回归绿 | `Godot/tests/presentation/test_shipyard_view.gd` |

## P3 批次：EQUIP

| ID | 子任务 | 真源 | 落点 | 验收 | 改动文件（仓库相对路径） |
|---|---|---|---|---|---|
| P3-1 | **核心修复**：legacy 导入失败时回退 starter armory，不阻塞整页 | `PlayerArmory.shared` 初始化 | `armory_bootstrap.gd#L10-L48` | L1 三栏非空 | `Godot/domain/equip/armory_bootstrap.gd` |
| P3-2 | `invalid_slot` 容错（非法槽键跳过而非整包失败） | — | `legacy_armory_importer.gd#L58-L60` | 单测 + L1 | `Godot/domain/equip/legacy_armory_importer.gd` |
| P3-3 | 错误态 UI 与真源对齐（仅在真无数据时提示） | — | `equip_view.gd#L311-L327` | L2 | `Godot/presentation/equip/equip_view.gd` |
| P3-4 | 三栏 LOADOUT/MODULES/WEAPONS 复刻 | `ShipsMenuUI.swift` | `presentation/equip/*` | L2 并排 | `Godot/presentation/equip/equip_view.gd`；`Godot/presentation/equip/blueprint_grid.gd`；`Godot/presentation/equip/slot_guides.gd` |
| P3-5 | WEAPONS/MODULES 双 tab 行为对齐 | `equipModeTab_*` | equip tabs | L1 tab 断言 | `Godot/presentation/equip/equip_view.gd` |
| P3-6 | 能量计算统一（装填增删的能量校验） | `armory.installModule` 能量守卫 | `equip_service.gd` | 单测 | `Godot/domain/equip/equip_service.gd` |
| P3-7 | starter armory 生成内容对齐 Swift 初始舰 | `PlayerArmory` starter | `armory_bootstrap` | L1 | `Godot/domain/equip/armory_bootstrap.gd` |
| P3-8 | 槽位/武器安装-卸载交互 | `equip(weapon:toSlotIndex:)` | equip 交互 | L1 动作流 | `Godot/presentation/equip/equip_view.gd` |
| P3-9 | equip parity catalog + 动作流 | — | `tools/flows/equip_*.json` | L1 三连过 | `Godot/tools/flows/equip_*.json`；`Godot/tools/ui_parity_cases_v1.json`；`Doc/godot/parity-matrix.md` |
| P3-10 | EQUIP L2 双端并排（真机同款操作） | — | `out/godot/parity/equip/` | review.json accepted | `out/godot/parity/equip/`；`Doc/godot/parity-matrix.md` |
| P3-11 | legacy bridge 状态读取健壮性（iOS 非 iOS 分支） | `LegacyArmoryStage` | `armory_bootstrap.gd#L33-L48` | 单测 | `Godot/domain/equip/armory_bootstrap.gd` |
| P3-12 | 存档一致性：Godot 存档与 legacy 并存时的优先级 | — | `armory_bootstrap` | 单测 | `Godot/domain/equip/armory_bootstrap.gd` |

## P4 批次：BATTLE

| ID | 子任务 | 真源 | 落点 | 验收 | 改动文件（仓库相对路径） |
|---|---|---|---|---|---|
| P4-1 | 舰体尺寸统一为 `50*config.size`（设计 ×1.5） | `ShipFactory#L157-L171` | `tactical_camera_mapper.gd` | L2 舰体大小并排 | `Godot/presentation/helios/tactical_camera_mapper.gd` |
| P4-2 | IFF 环半径 `28*size`（修"绿圈蓝圈太小"） | `IFFVisuals#L96-L106` | `world_feedback_layer.gd`（`_render_iff`） | L2 环/舰比例 0.56 | `Godot/presentation/tactical_hud/world_feedback_layer.gd` |
| P4-3 | 相机 zoom 基线（默认取景对齐 Swift） | GameScene 相机 | `tactical_node_shell.gd` | L2 | `Godot/app/tactical/tactical_node_shell.gd` |
| P4-4 | 星系背景替换为视差贴图（nebula/starfield） | `ParallaxBackground.swift` | `tactical_backdrop.gd` | L2 背景并排 | `Godot/presentation/environment/tactical_backdrop.gd` |
| P4-5 | **开火链路修复**：有敌舰却零开火（按 P0-12 取证结论） | `WeaponFireEngine.swift` | `tactical_combat_controller.gd` | L1 接敌→开火→命中 | `Godot/app/tactical/tactical_combat_controller.gd`；`Godot/domain/tactical/hostile_engagement_planner.gd` |
| P4-6 | 舰船移动/转向运动学对齐（朝向约定） | SpriteKit 朝向 | `tactical_3d_layer.gd` `_sync_hull_positions()` | L1 转向序列 | `Godot/presentation/helios/tactical_3d_layer.gd` |
| P4-7 | 右上 HUD 五按钮尺寸（SHIPS/FLEET/TECH/RETREAT/MAP） | `ZoomControlsHUD`/HUD 真源 | `battle_radar_view.gd`（`_ensure_affordances`） | L2 | `Godot/presentation/tactical_hud/battle_radar_view.gd` |
| P4-8 | 右缘编组槽 1-5 + +/- 缩放尺寸 | `BattleGroupMenuUI` | `battle_group_rail.gd` | L2 | `Godot/presentation/tactical_hud/battle_group_rail.gd`；`Godot/presentation/tactical_hud/battle_group_slot_visual.gd` |
| P4-9 | 雷达尺寸/红点位置 | `BattleRadarHUD.swift` | `battle_radar_view.gd` | L2 | `Godot/presentation/tactical_hud/battle_radar_view.gd` |
| P4-10 | 节点卡（SOL/HUMAN SPACE…）尺寸与信息 | `SystemStatusPlate` | `system_status_plate.gd` | L2 | `Godot/presentation/tactical_hud/system_status_plate.gd` |
| P4-11 | 左下 GROUP 1 面板（4/4）尺寸 | `BattleGroupMenuUI` | `fleet_status_plate.gd` | L2 | `Godot/presentation/tactical_hud/fleet_status_plate.gd` |
| P4-12 | BATTLE parity catalog + 动作流 + L2（≥6 case：到达/尺寸/IFF/HUD/开火/背景） | — | `out/godot/parity/battle/` | review.json accepted | `out/godot/parity/battle/`；`Godot/tools/flows/battle_*.json`；`Godot/tools/ui_parity_cases_v1.json`；`Doc/godot/parity-matrix.md` |
