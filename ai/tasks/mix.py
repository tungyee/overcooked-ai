"""拌合 task：Mix（MixedCompositeOrderNode 的生产步骤）。

拌合 = 把多种成分（各取进炉形态）装进一个拌合容器。当前关卡无 mixing 类
站点，本 task 按「站点 t=="mixing"」定位；拌合容器的世界表示（内容字段）
尚无帧可验证，属假设性实现——遇不到 mixing 站点时 feasible 恒 False，惰性
不触发。这是阶段 4 的接口位：compile/emit 已接好，执行细节待真实帧补。
"""
from typing import Any, Dict, List, Tuple

from ai.query import predicates as P
from ai import primitives as PR
from ai.exec.bt import Node, Sequence
from ai.tasks.base import (TASK_TIMEOUT_TICKS, Task, _grid_matches, _register,
                           _unpack_resource_id, ingredient_available,
                           ingredient_source)


def _mix_station(ctx, station_id):
    # type: (Dict, Any) -> Any
    """按资源 id 取拌合站点（spec §5.2：id 可能是 (grid,name) 元组，
    过渡期也接受裸字符串——见 _unpack_resource_id）。"""
    grid, name = _unpack_resource_id(station_id)
    for st in ctx["w"].stations:
        if st.get("t") != "mixing":
            continue
        if str(st.get("n")) != name:
            continue
        if not _grid_matches(st.get("grid"), grid):
            continue
        return st
    return None


@_register
class Mix(Task):
    """把多种成分（各取进炉形态）装进拌合容器。

    item 是拌合节点名（如 MixedFlourEggHoneycombCarrot）。成分由
    RecipeBook.components 反查；resources 第二项可显式带成分表。
    """

    TYPE = "Mix"

    @property
    def station_id(self):
        return self._resources[0]

    @property
    def comps(self):
        # type: () -> Tuple[str, ...]
        if len(self._resources) > 1 and self._resources[1]:
            return tuple(self._resources[1])
        return ()

    def claims(self):
        return ((self.station_id, "mix"),)

    def _comps(self, ctx):
        # type: (Dict) -> Tuple[str, ...]
        return self.comps or ctx["book"].components(self.item)

    def done(self, ctx):
        # type: (Dict) -> bool
        """拌合完成 = 拌合容器已含全部成分的进炉形态。

        假设容器内容记在 stations[] 的 item 字段（与砧板同款）；真实字段待帧确认。
        """
        st = _mix_station(ctx, self.station_id)
        if st is None:
            return False
        forms = {str(ctx["book"].cook_input_form(c)).lower()
                 for c in self._comps(ctx)}
        contents = st.get("item") or []
        if not isinstance(contents, (list, tuple)):
            contents = [contents]
        have = {str(x).lower() for x in contents}
        return forms <= have

    def feasible(self, ctx):
        # type: (Dict) -> bool
        st = _mix_station(ctx, self.station_id)
        if st is None:
            return False
        comps = self._comps(ctx)
        if not comps:
            return False
        return all(ingredient_available(ctx, ctx["book"].cook_input_form(c))
                   for c in comps)

    def build(self):
        # type: () -> Node
        station_id = self.station_id
        comps = self.comps

        def src(c, comp):
            return ingredient_source(c, c["book"].cook_input_form(comp))

        def holding(c, comp):
            form = c["book"].cook_input_form(comp)
            return P.held_matches(c["chef"], form)

        steps = []  # type: List[Any]
        if comps:
            first = comps[0]
            steps.append(PR.EnsureHandsFree(keep=lambda c: holding(c, first)))
            for comp in comps:
                steps.append(PR.InteractWith(
                    lambda c, comp=comp: src(c, comp), "pickup",
                    lambda c, comp=comp: holding(c, comp)))
                steps.append(PR.InteractWith(
                    lambda c: _mix_station(c, station_id), "placement",
                    lambda c: _mix_station(c, station_id) is not None))
        return PR.Timeout(Sequence(steps), TASK_TIMEOUT_TICKS)
