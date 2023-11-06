# GT4Py - GridTools Framework
#
# Copyright (c) 2014-2023, ETH Zurich
# All rights reserved.
#
# This file is part of the GT4Py project and the GridTools framework.
# GT4Py is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or any later
# version. See the LICENSE.txt file at the top-level directory of this
# distribution for a copy of the license or check <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later
from dataclasses import dataclass
from typing import Optional

from gt4py import eve
from gt4py.next import type_inference
from gt4py.next.iterator import ir, type_inference as it_type_inference


def _get_tuple_size(elem_or_type: type_inference.Type | ir.Node) -> int:
    if isinstance(elem, ir.Node):
        type_ = it_type_inference.infer(elem)  # use local type inference
    else:
        type_ = elem_or_type
    assert isinstance(type_, it_type_inference.Val) and isinstance(
        type_.dtype, it_type_inference.Tuple
    )
    return len(type_.dtype)
    infered_type = it_type_inference.infer(elem) if isinstance(elem, ir.Node) else elem
    assert isinstance(infered_type, it_type_inference.Val) and isinstance(
        infered_type.dtype, it_type_inference.Tuple
    )
    return len(infered_type.dtype)


@dataclass(frozen=True)
class CollapseTuple(eve.NodeTranslator):
    """
    Simplifies `make_tuple`, `tuple_get` calls.

      - `make_tuple(tuple_get(0, t), tuple_get(1, t), ..., tuple_get(N-1,t))` -> `t`
      - `tuple_get(i, make_tuple(e_0, e_1, ..., e_i, ..., e_N))` -> `e_i`
    """

    ignore_tuple_size: bool
    collapse_make_tuple_tuple_get: bool
    collapse_tuple_get_make_tuple: bool
    collapse_tuple_inference: bool
    _node_types: Optional[dict[int, type_inference.Type]] = None

    @classmethod
    def apply(
        cls,
        node: ir.Node,
        *,
        ignore_tuple_size: bool = False,
        # the following options are mostly for allowing separate testing of the modes
        collapse_make_tuple_tuple_get: bool = True,
        collapse_tuple_get_make_tuple: bool = True,
        use_global_type_inferrence: bool = False,
    ) -> ir.Node:
        """
        Simplifies `make_tuple`, `tuple_get` calls.

        If `ignore_tuple_size`, apply the transformation even if length of the inner tuple
        is greater than the length of the outer tuple.
        """
        node_types = it_type_inference.infer_all(node) if use_global_type_inferrence else None
        return cls(
            ignore_tuple_size,
            collapse_make_tuple_tuple_get,
            collapse_tuple_get_make_tuple,
            collapse_tuple_inference,
            node_types
        ).visit(node)

        return cls(
            ignore_tuple_size,
            collapse_make_tuple_tuple_get,
            collapse_tuple_get_make_tuple,
            collapse_tuple_inference,
        ).visit(node)

    def visit_FunCall(self, node: ir.FunCall, **kwargs) -> ir.Node:
        if (
            self.collapse_make_tuple_tuple_get
            and node.fun == ir.SymRef(id="make_tuple")
            and all(
                isinstance(arg, ir.FunCall) and arg.fun == ir.SymRef(id="tuple_get")
                for arg in node.args
            )
        ):
            # `make_tuple(tuple_get(0, t), tuple_get(1, t), ..., tuple_get(N-1,t))` -> `t`
            assert isinstance(node.args[0], ir.FunCall)
            first_expr = node.args[0].args[1]

            for i, v in enumerate(node.args):
                assert isinstance(v, ir.FunCall)
                assert isinstance(v.args[0], ir.Literal)
                if not (int(v.args[0].value) == i and v.args[1] == first_expr):
                    # tuple argument differs, just continue with the rest of the tree
                    return self.generic_visit(node)

            if self.ignore_tuple_size or _get_tuple_size(first_expr, self.node_types) == len(node.args):
                return first_expr
        if (
            self.collapse_tuple_get_make_tuple
            and node.fun == ir.SymRef(id="tuple_get")
            and isinstance(node.args[1], ir.FunCall)
            and node.args[1].fun == ir.SymRef(id="make_tuple")
            and isinstance(node.args[0], ir.Literal)
        ):
            # `tuple_get(i, make_tuple(e_0, e_1, ..., e_i, ..., e_N))` -> `e_i`
            assert node.args[0].type in ir.INTEGER_BUILTINS
            make_tuple_call = node.args[1]
            idx = int(node.args[0].value)
            assert idx < len(
                make_tuple_call.args
            ), f"Index {idx} is out of bounds for tuple of size {len(make_tuple_call.args)}"
            return node.args[1].args[idx]
        return self.generic_visit(node)
