# GT4Py - GridTools Framework
#
# Copyright (c) 2014-2024, ETH Zurich
# All rights reserved.
#
# Please, refer to the LICENSE file in the root directory.
# SPDX-License-Identifier: BSD-3-Clause

from gt4py import eve
from gt4py.next.iterator import ir as itir
from gt4py.next.iterator.ir_utils import ir_makers as im


class FencilToProgram(eve.NodeTranslator):
    @classmethod
    def apply(cls, node: itir.FencilDefinition | itir.Program) -> itir.Program:
        return cls().visit(node)

    def visit_StencilClosure(self, node: itir.StencilClosure) -> itir.SetAt:
        as_fieldop = im.call(im.call("as_fieldop")(node.stencil, node.domain))(*node.inputs)
        return itir.SetAt(expr=as_fieldop, domain=node.domain, target=node.output)

    def visit_FencilDefinition(self, node: itir.FencilDefinition) -> itir.Program:
        return itir.Program(
            id=node.id,
            function_definitions=node.function_definitions,
            params=node.params,
            declarations=[],
            body=self.visit(node.closures),
            implicit_domain=node.implicit_domain,
        )
