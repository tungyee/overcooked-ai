# Rapids_2_1_2P 关卡地图（2-1）

实测帧：`trace-20261028-234917.jsonl`，入库 fixture `tests/fixtures/rapids/rapids21-20261028.ndjson.gz`（5 帧）。

## 布局

两块**互不相连**的地面，只能靠投掷传料：

- **厨房**（主网格 `GridManager`，静止，z≈-2.6~3.9）：料箱 ×2（`DispenserCrate 3 (2)/(3)`，土豆/鸡块）、炸锅 ×2（`FryingStation (1)/(2)`，`FrierBasket`）、盘 ×4、盘回收、垃圾桶、出餐口（`workstation_plate_station`）、灭火器。
- **木筏**（`Raft_Top`，漂流，z≈6~14，x/z 都在动）：砧板 ×2 + 一圈台面。木筏 x 实测在 13~20 间来回漂，速度约 1 m/s。

开局 P2 在木筏、P1 在厨房，两人可达场交集为空。

## 配方

`ChickenNuggetsAndChips_*`：薯条（Potato → 砧板 Chopped_Potato → 炸锅 FriedChips）、鸡块（NuggetChicken → 砧板 NuggetChoppedChicken → 炸锅 FriedChickenNuggets），单点或两样都要。

## 投掷

- 射程固定 ≈7.5m（P1 4 次实测 7.3~7.8m，横偏 <0.2m，12~15 拍落地），见 `ai/space/throw.py`。
- 木筏南缘到炸锅约 9.6m，P2 从木筏投回厨房落在炸锅前 ≈2m 的地上，由 P1 捡起下锅。
- 落在木筏上的料随木筏漂移，砧板能直接接住投来的料（实测 `on_station=attach:chopping_board`）。

## 已知坑

- 木筏站点 15 个 > 厨房 13 个：旧版按站点众数把木筏当主网格，原点每帧漂。现在按插件 `grids[].main`（挂 `GridNavSpace` 的网格），旧帧按名字 `GridManager`。
- 厨房与木筏有 4 对同名台面（`countertop_01_standard_old (4)/(5)/(6)/(11)`）。按 `on_station` 反查站点必须按位置挑，插件给的 `item.grid` 也是按名字反查的，同名时同样报错。
- 木筏格投影到主网格：木筏偏移不是整格时，靠砧板那排格中心离台面 collider 不到 `CHEF_CLEARANCE`，被判不可走（fixture t=657），这几拍砧板与板上的料判不可达。随木筏漂回会恢复；根治要按附属网格本地坐标寻路。
