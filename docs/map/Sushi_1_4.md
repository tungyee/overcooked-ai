# 代码实际读到的地图快照（captured map）

> 来源：`tests/fixtures/full_level.ndjson`（21:27 录制帧），与实机 trace 同一套解析路径。
> 本图的 `.`/`#` 用 `w.walkable()`（nodeMap + 站点占用 + 传送带 + 边界）绘制，即 agent 实际使用的可走性。

- **插件上报 level**: `Sushi_1_4_2P`
- **phase**: `level`, **paused**: `False`
- **grid.origin**: `(2.4, 0.0, 0.0)`  **mapOffset**: `8,6`
- **订单**: Sushi_Cucumber(Seaweed,BoiledSushiRice,Cucumber), Sushi_Fish(Seaweed,BoiledSushiRice,SushiFish)

## 图例

- `.` = 可走  `#` = 不可走
- `C`=食材箱 crate  `X`=柜台  `H`=砧板  `K`=灶台  `W`=洗碗池  `B`=垃圾桶  `P`=盘站  `R`=盘回收
- `A`=出餐口

## 网格图（行=gz 6→-6，列=gx -8→8）

```
     gx: -8 -7 -6 -5 -4 -3 -2 -1  0  1  2  3  4  5  6  7  8
gz  6    #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #
gz  5    #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #
gz  4    #  .  .  C  .  .  .  C  .  X  .  .  .  X  .  .  #
gz  3    #  X  X  C  .  .  .  C  X  H  .  .  .  H  X  X  #
gz  2    #  B  .  .  .  .  .  .  .  .  .  .  .  .  .  X  #
gz  1    #  X  .  .  X  X  X  X  .  X  X  X  X  .  .  K  #
gz  0    #  R  .  .  X  .  .  X  .  X  .  .  X  .  .  X  #
gz -1    #  W  .  .  X  .  .  X  .  X  .  .  X  .  .  X  #
gz -2    #  X  .  .  X  X  X  X  .  X  X  X  X  .  .  K  #
gz -3    #  X  .  .  .  .  .  .  .  .  .  .  .  .  .  X  #
gz -4    #  X  X  X  X  X  X  .  .  .  X  X  X  K  X  X  #
gz -5    #  .  .  .  .  .  .  R  A  A  .  .  .  .  .  .  #
gz -6    #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #
```

## 食材箱（crate）

| 实例名 | 食材 ing | gx,gz | 世界 x,z |
|---|---|---|---|
| `DispenserCrate 3 (1)` | Seaweed | (-5,4) | (8.4,-1.2) |
| `DispenserCrate 3 (5)` | SushiFish | (-1,4) | (13.2,-1.2) |
| `DispenserCrate 3 (3)` | SushiRice | (-5,3) | (8.4,-2.4) |
| `DispenserCrate 3 (6)` | Cucumber | (-1,3) | (13.2,-2.4) |

## 厨师位置

- `Player 1`: 世界 (10.9,-1.6) → 网格 (-3, 4), held=None
- `Player 2`: 世界 (17.9,-1.7) → 网格 (3, 4), held=None

## 鱼箱可达性（本帧）

- P2 网格 (3, 4)，鱼箱 `DispenserCrate 3 (5)` 在 (-1, 4)
- `interact_cells` = [(-2, 4), (0, 4)]，`nearest_cost` = [((-2, 4), 29), ((0, 4), None)]

## 站点清单（全部带坐标）

| t | 实例名 | gx,gz |
|---|---|---|
| counter | `countertop_01_standard_wood_corner` | (-7,-4) |
| counter | `countertop_01_standard_wood (43)` | (-7,-3) |
| counter | `countertop_01_standard_wood (44)` | (-7,-2) |
| washing | `WashingPart` | (-7,-1) |
| plate_return | `DryingPart` | (-7,0) |
| counter | `countertop_01_standard_wood (45)` | (-7,1) |
| bin | `Bin 1` | (-7,2) |
| counter | `countertop_01_standard_wood_corner (1)` | (-7,3) |
| counter | `countertop_01_standard_wood (41)` | (-6,-4) |
| counter | `countertop_01_standard_wood (47)` | (-6,3) |
| counter | `countertop_01_standard_wood (40)` | (-5,-4) |
| crate | `DispenserCrate 3 (3)` | (-5,3) |
| crate | `DispenserCrate 3 (1)` | (-5,4) |
| counter | `countertop_01_standard_wood (39)` | (-4,-4) |
| counter | `countertop_01_standard_wood (6)` | (-4,-2) |
| counter | `countertop_01_standard_wood (9)` | (-4,-1) |
| counter | `countertop_01_standard_wood (8)` | (-4,0) |
| counter | `countertop_01_standard_wood (7)` | (-4,1) |
| counter | `countertop_01_standard_wood (38)` | (-3,-4) |
| counter | `countertop_01_standard_wood (5)` | (-3,-2) |
| counter | `countertop_01_standard_wood (10)` | (-3,1) |
| counter | `countertop_01_standard_wood (37)` | (-2,-4) |
| counter | `countertop_01_standard_wood (4)` | (-2,-2) |
| counter | `countertop_01_standard_wood (11)` | (-2,1) |
| plate_return | `workstation_plate_return` | (-1,-5) |
| counter | `countertop_01_standard_wood (3)` | (-1,-2) |
| counter | `countertop_01_standard_wood` | (-1,-1) |
| counter | `countertop_01_standard_wood (1)` | (-1,0) |
| counter | `countertop_01_standard_wood (2)` | (-1,1) |
| crate | `DispenserCrate 3 (6)` | (-1,3) |
| crate | `DispenserCrate 3 (5)` | (-1,4) |
| plate_station | `workstation_plate_station` | (0,-5) |
| counter | `countertop_01_standard_wood (27)` | (0,3) |
| counter | `countertop_01_standard_wood (17)` | (1,-2) |
| counter | `countertop_01_standard_wood (14)` | (1,-1) |
| counter | `countertop_01_standard_wood (15)` | (1,0) |
| counter | `countertop_01_standard_wood (16)` | (1,1) |
| chopping | `countertop_01_chopping_board_wood_no_edge (2)` | (1,3) |
| counter | `countertop_01_standard_wood (25)` | (1,4) |
| counter | `countertop_01_standard_wood (36)` | (2,-4) |
| counter | `countertop_01_standard_wood (18)` | (2,-2) |
| counter | `countertop_01_standard_wood (13)` | (2,1) |
| counter | `countertop_01_standard_wood (34)` | (3,-4) |
| counter | `countertop_01_standard_wood (19)` | (3,-2) |
| counter | `countertop_01_standard_wood (12)` | (3,1) |
| counter | `countertop_01_standard_wood (35)` | (4,-4) |
| counter | `countertop_01_standard_wood (20)` | (4,-2) |
| counter | `countertop_01_standard_wood (23)` | (4,-1) |
| counter | `countertop_01_standard_wood (22)` | (4,0) |
| counter | `countertop_01_standard_wood (21)` | (4,1) |
| cooking | `workstation_cooker_01` | (5,-4) |
| chopping | `countertop_01_chopping_board_wood_no_edge` | (5,3) |
| counter | `countertop_01_standard_wood (32)` | (5,4) |
| counter | `countertop_01_standard_wood (33)` | (6,-4) |
| counter | `countertop_01_standard_wood (26)` | (6,3) |
| counter | `countertop_01_standard_wood_corner (3)` | (7,-4) |
| counter | `countertop_01_standard_wood (31)` | (7,-3) |
| cooking | `workstation_cooker_01 (3)` | (7,-2) |
| counter | `countertop_01_standard_wood (30)` | (7,-1) |
| counter | `countertop_01_standard_wood (29)` | (7,0) |
| cooking | `workstation_cooker_01 (2)` | (7,1) |
| counter | `countertop_01_standard_wood (28)` | (7,2) |
| counter | `countertop_01_standard_wood_corner (2)` | (7,3) |
| plate_stack | `equipment_plate_01 (3)` | (None,None) |
| plate_stack | `equipment_plate_01 (2)` | (None,None) |
| plate_stack | `equipment_plate_01 (1)` | (None,None) |
| plate_stack | `equipment_plate_01` | (None,None) |
| plate_stack | `equipment_plate_01 (2)` | (None,None) |
| plate_stack | `equipment_plate_01` | (None,None) |
| plate_stack | `equipment_plate_01 (3)` | (None,None) |
| plate_stack | `equipment_plate_01 (1)` | (None,None) |
