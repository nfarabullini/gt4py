# GT4Py - GridTools Framework
#
# Copyright (c) 2014-2024, ETH Zurich
# All rights reserved.
#
# Please, refer to the LICENSE file in the root directory.
# SPDX-License-Identifier: BSD-3-Clause

from __future__ import annotations

import dataclasses
from typing import Any, Dict, Final, List, Optional, Set, Tuple, TypeAlias, Union

import dace
import dace.subsets as sbs

from gt4py import eve
from gt4py.next import common as gtx_common
from gt4py.next.iterator import ir as gtir
from gt4py.next.iterator.ir_utils import common_pattern_matcher as cpm
from gt4py.next.iterator.type_system import type_specifications as gtir_ts
from gt4py.next.program_processors.runners.dace_common import utility as dace_common_util
from gt4py.next.program_processors.runners.dace_fieldview import (
    gtir_python_codegen,
    gtir_to_sdfg,
    utility as dace_fieldview_util,
)
from gt4py.next.type_system import type_specifications as ts


@dataclasses.dataclass(frozen=True)
class MemletExpr:
    """Scalar or array data access thorugh a memlet."""

    node: dace.nodes.AccessNode
    subset: sbs.Indices | sbs.Range


@dataclasses.dataclass(frozen=True)
class SymbolExpr:
    """Any symbolic expression that is constant in the context of current SDFG."""

    value: dace.symbolic.SymExpr
    dtype: dace.typeclass


@dataclasses.dataclass(frozen=True)
class ValueExpr:
    """Result of the computation implemented by a tasklet node."""

    node: dace.nodes.AccessNode
    dtype: gtir_ts.ListType | ts.ScalarType


# Define alias for the elements needed to setup input connections to a map scope
InputConnection: TypeAlias = tuple[
    dace.nodes.AccessNode,
    sbs.Range,
    dace.nodes.Node,
    Optional[str],
]

IteratorIndexExpr: TypeAlias = MemletExpr | SymbolExpr | ValueExpr


@dataclasses.dataclass(frozen=True)
class IteratorExpr:
    """
    Iterator for field access to be consumed by `deref` or `shift` builtin functions.

    Args:
        field: The field this iterator operates on.
        dimensions: Field domain represented as a sorted list of dimensions.
                    In order to dereference an element in the field, we need index values
                    for all the dimensions in the right order.
        indices: Maps each dimension to an index value, which could be either a symbolic value
                 or the result of a tasklet computation like neighbors connectivity or dynamic offset.

    """

    field: dace.nodes.AccessNode
    dimensions: list[gtx_common.Dimension]
    indices: dict[gtx_common.Dimension, IteratorIndexExpr]


DACE_REDUCTION_MAPPING: dict[str, dace.dtypes.ReductionType] = {
    "minimum": dace.dtypes.ReductionType.Min,
    "maximum": dace.dtypes.ReductionType.Max,
    "plus": dace.dtypes.ReductionType.Sum,
    "multiplies": dace.dtypes.ReductionType.Product,
    "and_": dace.dtypes.ReductionType.Logical_And,
    "or_": dace.dtypes.ReductionType.Logical_Or,
    "xor_": dace.dtypes.ReductionType.Logical_Xor,
    "minus": dace.dtypes.ReductionType.Sub,
    "divides": dace.dtypes.ReductionType.Div,
}


def get_reduce_params(node: gtir.FunCall) -> tuple[str, SymbolExpr, SymbolExpr]:
    assert node.type
    dtype = dace_fieldview_util.as_dace_type(node.type)

    assert isinstance(node.fun, gtir.FunCall)
    assert len(node.fun.args) == 2
    assert isinstance(node.fun.args[0], gtir.SymRef)
    op_name = str(node.fun.args[0])
    assert isinstance(node.fun.args[1], gtir.Literal)
    assert node.fun.args[1].type == node.type
    reduce_init = SymbolExpr(node.fun.args[1].value, dtype)

    if op_name not in DACE_REDUCTION_MAPPING:
        raise RuntimeError(f"Reduction operation '{op_name}' not supported.")
    identity_value = dace.dtypes.reduction_identity(dtype, DACE_REDUCTION_MAPPING[op_name])
    reduce_identity = SymbolExpr(identity_value, dtype)

    return op_name, reduce_init, reduce_identity


class LambdaToTasklet(eve.NodeVisitor):
    """Translates an `ir.Lambda` expression to a dataflow graph.

    Lambda functions should only be encountered as argument to the `as_fieldop`
    builtin function, therefore the dataflow graph generated here typically
    represents the stencil function of a field operator.
    """

    sdfg: dace.SDFG
    state: dace.SDFGState
    subgraph_builder: gtir_to_sdfg.DataflowBuilder
    reduce_identity: Optional[SymbolExpr]
    input_connections: list[InputConnection]
    symbol_map: dict[str, IteratorExpr | MemletExpr | SymbolExpr]

    def __init__(
        self,
        sdfg: dace.SDFG,
        state: dace.SDFGState,
        subgraph_builder: gtir_to_sdfg.DataflowBuilder,
        reduce_identity: Optional[SymbolExpr],
    ):
        self.sdfg = sdfg
        self.state = state
        self.subgraph_builder = subgraph_builder
        self.reduce_identity = reduce_identity
        self.input_connections = []
        self.symbol_map = {}

    def _add_entry_memlet_path(
        self,
        src: dace.nodes.AccessNode,
        src_subset: sbs.Range,
        dst_node: dace.nodes.Node,
        dst_conn: Optional[str] = None,
    ) -> None:
        self.input_connections.append((src, src_subset, dst_node, dst_conn))

    def _add_edge(
        self,
        src_node: dace.Node,
        src_node_connector: Optional[str],
        dst_node: dace.Node,
        dst_node_connector: Optional[str],
        memlet: dace.Memlet,
    ) -> None:
        """Helper method to add an edge in current state."""
        self.state.add_edge(src_node, src_node_connector, dst_node, dst_node_connector, memlet)

    def _add_map(
        self,
        name: str,
        ndrange: Union[
            Dict[str, Union[str, dace.subsets.Subset]],
            List[Tuple[str, Union[str, dace.subsets.Subset]]],
        ],
        **kwargs: Any,
    ) -> Tuple[dace.nodes.MapEntry, dace.nodes.MapExit]:
        """Helper method to add a map with unique name in current state."""
        return self.subgraph_builder.add_map(name, self.state, ndrange, **kwargs)

    def _add_tasklet(
        self,
        name: str,
        inputs: Union[Set[str], Dict[str, dace.dtypes.typeclass]],
        outputs: Union[Set[str], Dict[str, dace.dtypes.typeclass]],
        code: str,
        **kwargs: Any,
    ) -> dace.nodes.Tasklet:
        """Helper method to add a tasklet with unique name in current state."""
        return self.subgraph_builder.add_tasklet(name, self.state, inputs, outputs, code, **kwargs)

    def _get_tasklet_result(
        self,
        dtype: dace.typeclass,
        src_node: dace.nodes.Tasklet,
        src_connector: str,
    ) -> ValueExpr:
        temp_name = self.sdfg.temp_data_name()
        self.sdfg.add_scalar(temp_name, dtype, transient=True)
        data_type = dace_common_util.as_scalar_type(str(dtype.as_numpy_dtype()))
        temp_node = self.state.add_access(temp_name)
        self._add_edge(
            src_node,
            src_connector,
            temp_node,
            None,
            dace.Memlet(data=temp_name, subset="0"),
        )
        return ValueExpr(temp_node, data_type)

    def _visit_deref(self, node: gtir.FunCall) -> MemletExpr | ValueExpr:
        """
        Visit a `deref` node, which represents dereferencing of an iterator.
        The iterator is the argument of this node.

        The iterator contains the information for accessing a field, that is the
        sorted list of dimensions in the field domain and the index values for
        each dimension. The index values can be either symbol values, that is
        literal values or scalar arguments which are constant in the SDFG scope;
        or they can be the result of some expression, that computes a dynamic
        index offset or gets an neighbor index from a connectivity table.
        In case all indexes are symbol values, the `deref` node is lowered to a
        memlet; otherwise dereferencing is a runtime operation represented in
        the SDFG as a tasklet node.
        """
        # format used for field index tasklet connector
        IndexConnectorFmt: Final = "__index_{dim}"

        assert len(node.args) == 1
        it = self.visit(node.args[0])

        if isinstance(it, IteratorExpr):
            field_desc = it.field.desc(self.sdfg)
            assert len(field_desc.shape) == len(it.dimensions)
            if all(isinstance(index, SymbolExpr) for index in it.indices.values()):
                # when all indices are symblic expressions, we can perform direct field access through a memlet
                field_subset = sbs.Range(
                    (it.indices[dim].value, it.indices[dim].value, 1)  # type: ignore[union-attr]
                    if dim in it.indices
                    else (0, size - 1, 1)
                    for dim, size in zip(it.dimensions, field_desc.shape)
                )
                return MemletExpr(it.field, field_subset)

            else:
                # we use a tasklet to dereference an iterator when one or more indices are the result of some computation,
                # either indirection through connectivity table or dynamic cartesian offset.
                assert all(dim in it.indices for dim in it.dimensions)
                field_indices = [(dim, it.indices[dim]) for dim in it.dimensions]
                index_connectors = [
                    IndexConnectorFmt.format(dim=dim.value)
                    for dim, index in field_indices
                    if not isinstance(index, SymbolExpr)
                ]
                # here `internals` refer to the names used as index in the tasklet code string:
                # an index can be either a connector name (for dynamic/indirect indices)
                # or a symbol value (for literal values and scalar arguments).
                index_internals = ",".join(
                    str(index.value)
                    if isinstance(index, SymbolExpr)
                    else IndexConnectorFmt.format(dim=dim.value)
                    for dim, index in field_indices
                )
                deref_node = self._add_tasklet(
                    "runtime_deref",
                    {"field"} | set(index_connectors),
                    {"val"},
                    code=f"val = field[{index_internals}]",
                )
                # add new termination point for the field parameter
                self._add_entry_memlet_path(
                    it.field,
                    sbs.Range.from_array(field_desc),
                    deref_node,
                    "field",
                )

                for dim, index_expr in field_indices:
                    # add termination points for the dynamic iterator indices
                    deref_connector = IndexConnectorFmt.format(dim=dim.value)
                    if isinstance(index_expr, MemletExpr):
                        self._add_entry_memlet_path(
                            index_expr.node,
                            index_expr.subset,
                            deref_node,
                            deref_connector,
                        )

                    elif isinstance(index_expr, ValueExpr):
                        self._add_edge(
                            index_expr.node,
                            None,
                            deref_node,
                            deref_connector,
                            dace.Memlet(data=index_expr.node.data, subset="0"),
                        )
                    else:
                        assert isinstance(index_expr, SymbolExpr)

                dtype = it.field.desc(self.sdfg).dtype
                return self._get_tasklet_result(dtype, deref_node, "val")

        else:
            assert isinstance(it, MemletExpr)
            return it

    def _visit_neighbors(self, node: gtir.FunCall) -> ValueExpr:
        assert len(node.args) == 2

        assert isinstance(node.args[0], gtir.OffsetLiteral)
        offset = node.args[0].value
        assert isinstance(offset, str)
        offset_provider = self.subgraph_builder.get_offset_provider(offset)
        assert isinstance(offset_provider, gtx_common.Connectivity)

        it = self.visit(node.args[1])
        assert isinstance(it, IteratorExpr)
        assert offset_provider.neighbor_axis in it.dimensions
        neighbor_dim_index = it.dimensions.index(offset_provider.neighbor_axis)
        assert offset_provider.neighbor_axis not in it.indices
        assert offset_provider.origin_axis not in it.dimensions
        assert offset_provider.origin_axis in it.indices
        origin_index = it.indices[offset_provider.origin_axis]
        assert isinstance(origin_index, SymbolExpr)
        assert all(isinstance(index, SymbolExpr) for index in it.indices.values())

        field_desc = it.field.desc(self.sdfg)
        connectivity = dace_common_util.connectivity_identifier(offset)
        # initially, the storage for the connectivty tables is created as transient;
        # when the tables are used, the storage is changed to non-transient,
        # as the corresponding arrays are supposed to be allocated by the SDFG caller
        connectivity_desc = self.sdfg.arrays[connectivity]
        connectivity_desc.transient = False

        # The visitor is constructing a list of input connections that will be handled
        # by `translate_as_fieldop` (the primitive translator), that is responsible
        # of creating the map for the field domain. For each input connection, it will
        # create a memlet that will write to a node specified by the third attribute
        # in the `InputConnection` tuple (either a tasklet, or a view node, or a library
        # node). For the specific case of `neighbors` we need to nest the neighbors map
        # inside the field map and the memlets will traverse the external map and write
        # to the view nodes. The simplify pass will remove the redundant access nodes.
        field_slice_view, field_slice_desc = self.sdfg.add_view(
            f"{offset_provider.neighbor_axis.value}_view",
            (field_desc.shape[neighbor_dim_index],),
            field_desc.dtype,
            strides=(field_desc.strides[neighbor_dim_index],),
            find_new_name=True,
        )
        field_slice_node = self.state.add_access(field_slice_view)
        field_subset = ",".join(
            it.indices[dim].value  # type: ignore[union-attr]
            if dim != offset_provider.neighbor_axis
            else f"0:{size}"
            for dim, size in zip(it.dimensions, field_desc.shape, strict=True)
        )
        self._add_entry_memlet_path(
            it.field,
            sbs.Range.from_string(field_subset),
            field_slice_node,
        )

        connectivity_slice_view, _ = self.sdfg.add_view(
            "neighbors_view",
            (offset_provider.max_neighbors,),
            connectivity_desc.dtype,
            strides=(connectivity_desc.strides[1],),
            find_new_name=True,
        )
        connectivity_slice_node = self.state.add_access(connectivity_slice_view)
        self._add_entry_memlet_path(
            self.state.add_access(connectivity),
            sbs.Range.from_string(f"{origin_index.value}, 0:{offset_provider.max_neighbors}"),
            connectivity_slice_node,
        )

        neighbors_temp, _ = self.sdfg.add_temp_transient(
            (offset_provider.max_neighbors,), field_desc.dtype
        )
        neighbors_node = self.state.add_access(neighbors_temp)

        offset_dim = gtx_common.Dimension(offset)
        neighbor_idx = dace_fieldview_util.get_map_variable(offset_dim)
        me, mx = self._add_map(
            f"{offset}_neighbors",
            {
                neighbor_idx: f"0:{offset_provider.max_neighbors}",
            },
        )
        index_connector = "__index"
        if offset_provider.has_skip_values:
            assert self.reduce_identity is not None
            assert self.reduce_identity.dtype == field_desc.dtype
            # TODO: Investigate if a NestedSDFG brings benefits
            tasklet_node = self._add_tasklet(
                "gather_neighbors_with_skip_values",
                {"__field", index_connector},
                {"__val"},
                f"__val = __field[{index_connector}] if {index_connector} != {gtx_common._DEFAULT_SKIP_VALUE} else {self.reduce_identity.dtype}({self.reduce_identity.value})",
            )

        else:
            tasklet_node = self._add_tasklet(
                "gather_neighbors",
                {"__field", index_connector},
                {"__val"},
                f"__val = __field[{index_connector}]",
            )

        self.state.add_memlet_path(
            field_slice_node,
            me,
            tasklet_node,
            dst_conn="__field",
            memlet=dace.Memlet.from_array(field_slice_view, field_slice_desc),
        )
        self.state.add_memlet_path(
            connectivity_slice_node,
            me,
            tasklet_node,
            dst_conn=index_connector,
            memlet=dace.Memlet(data=connectivity_slice_view, subset=neighbor_idx),
        )
        self.state.add_memlet_path(
            tasklet_node,
            mx,
            neighbors_node,
            src_conn="__val",
            memlet=dace.Memlet(data=neighbors_temp, subset=neighbor_idx),
        )

        assert isinstance(node.type, gtir_ts.ListType)
        return ValueExpr(neighbors_node, node.type)

    def _visit_reduce(self, node: gtir.FunCall) -> ValueExpr:
        op_name, reduce_init, reduce_identity = get_reduce_params(node)
        dtype = reduce_identity.dtype

        # We store the value of reduce identity in the visitor context while visiting
        # the input to reduction; this value will be use by the `neighbors` visitor
        # to fill the skip values in the neighbors list.
        prev_reduce_identity = self.reduce_identity
        self.reduce_identity = reduce_identity

        try:
            input_expr = self.visit(node.args[0])
        finally:
            # ensure that we leave the visitor in the same state as we entered
            self.reduce_identity = prev_reduce_identity

        assert isinstance(input_expr, MemletExpr | ValueExpr)
        input_desc = input_expr.node.desc(self.sdfg)
        assert isinstance(input_desc, dace.data.Array)

        if len(input_desc.shape) > 1:
            assert isinstance(input_expr, MemletExpr)
            ndims = len(input_desc.shape) - 1
            # the axis to be reduced is always the last one, because `reduce` is supposed
            # to operate on `ListType`
            assert set(input_expr.subset.size()[0:ndims]) == {1}
            reduce_axes = [ndims]
        else:
            reduce_axes = None

        reduce_wcr = "lambda x, y: " + gtir_python_codegen.format_builtin(op_name, "x", "y")
        reduce_node = self.state.add_reduce(reduce_wcr, reduce_axes, reduce_init.value)

        if isinstance(input_expr, MemletExpr):
            self._add_entry_memlet_path(
                input_expr.node,
                input_expr.subset,
                reduce_node,
            )
        else:
            self.state.add_nedge(
                input_expr.node,
                reduce_node,
                dace.Memlet.from_array(input_expr.node.data, input_desc),
            )

        temp_name = self.sdfg.temp_data_name()
        self.sdfg.add_scalar(temp_name, dtype, transient=True)
        temp_node = self.state.add_access(temp_name)

        self.state.add_nedge(
            reduce_node,
            temp_node,
            dace.Memlet(data=temp_name, subset="0"),
        )
        assert isinstance(node.type, ts.ScalarType)
        return ValueExpr(temp_node, node.type)

    def _split_shift_args(
        self, args: list[gtir.Expr]
    ) -> tuple[tuple[gtir.Expr, gtir.Expr], Optional[list[gtir.Expr]]]:
        """
        Splits the arguments to `shift` builtin function as pairs, each pair containing
        the offset provider and the offset expression in one dimension.
        """
        nargs = len(args)
        assert nargs >= 2 and nargs % 2 == 0
        return (args[-2], args[-1]), args[: nargs - 2] if nargs > 2 else None

    def _visit_shift_multidim(
        self, iterator: gtir.Expr, shift_args: list[gtir.Expr]
    ) -> tuple[gtir.Expr, gtir.Expr, IteratorExpr]:
        """Transforms a multi-dimensional shift into recursive shift calls, each in a single dimension."""
        (offset_provider_arg, offset_value_arg), tail = self._split_shift_args(shift_args)
        if tail:
            node = gtir.FunCall(
                fun=gtir.FunCall(fun=gtir.SymRef(id="shift"), args=tail),
                args=[iterator],
            )
            it = self.visit(node)
        else:
            it = self.visit(iterator)

        assert isinstance(it, IteratorExpr)
        return offset_provider_arg, offset_value_arg, it

    def _make_cartesian_shift(
        self, it: IteratorExpr, offset_dim: gtx_common.Dimension, offset_expr: IteratorIndexExpr
    ) -> IteratorExpr:
        """Implements cartesian shift along one dimension."""
        assert offset_dim in it.dimensions
        new_index: SymbolExpr | ValueExpr
        assert offset_dim in it.indices
        index_expr = it.indices[offset_dim]
        if isinstance(index_expr, SymbolExpr) and isinstance(offset_expr, SymbolExpr):
            # purely symbolic expression which can be interpreted at compile time
            new_index = SymbolExpr(
                dace.symbolic.pystr_to_symbolic(index_expr.value) + offset_expr.value,
                index_expr.dtype,
            )
        else:
            # the offset needs to be calculated by means of a tasklet (i.e. dynamic offset)
            new_index_connector = "shifted_index"
            if isinstance(index_expr, SymbolExpr):
                dynamic_offset_tasklet = self._add_tasklet(
                    "dynamic_offset",
                    {"offset"},
                    {new_index_connector},
                    f"{new_index_connector} = {index_expr.value} + offset",
                )
            elif isinstance(offset_expr, SymbolExpr):
                dynamic_offset_tasklet = self._add_tasklet(
                    "dynamic_offset",
                    {"index"},
                    {new_index_connector},
                    f"{new_index_connector} = index + {offset_expr}",
                )
            else:
                dynamic_offset_tasklet = self._add_tasklet(
                    "dynamic_offset",
                    {"index", "offset"},
                    {new_index_connector},
                    f"{new_index_connector} = index + offset",
                )
            for input_expr, input_connector in [(index_expr, "index"), (offset_expr, "offset")]:
                if isinstance(input_expr, MemletExpr):
                    self._add_entry_memlet_path(
                        input_expr.node,
                        input_expr.subset,
                        dynamic_offset_tasklet,
                        input_connector,
                    )
                elif isinstance(input_expr, ValueExpr):
                    self._add_edge(
                        input_expr.node,
                        None,
                        dynamic_offset_tasklet,
                        input_connector,
                        dace.Memlet(data=input_expr.node.data, subset="0"),
                    )

            if isinstance(index_expr, SymbolExpr):
                dtype = index_expr.dtype
            else:
                dtype = index_expr.node.desc(self.sdfg).dtype

            new_index = self._get_tasklet_result(dtype, dynamic_offset_tasklet, new_index_connector)

        # a new iterator with a shifted index along one dimension
        return IteratorExpr(
            it.field,
            it.dimensions,
            {dim: (new_index if dim == offset_dim else index) for dim, index in it.indices.items()},
        )

    def _make_dynamic_neighbor_offset(
        self,
        offset_expr: MemletExpr | ValueExpr,
        offset_table_node: dace.nodes.AccessNode,
        origin_index: SymbolExpr,
    ) -> ValueExpr:
        """
        Implements access to neighbor connectivity table by means of a tasklet node.

        It requires a dynamic offset value, either obtained from a field/scalar argument (`MemletExpr`)
        or computed by another tasklet (`ValueExpr`).
        """
        new_index_connector = "neighbor_index"
        tasklet_node = self._add_tasklet(
            "dynamic_neighbor_offset",
            {"table", "offset"},
            {new_index_connector},
            f"{new_index_connector} = table[{origin_index.value}, offset]",
        )
        self._add_entry_memlet_path(
            offset_table_node,
            sbs.Range.from_array(offset_table_node.desc(self.sdfg)),
            tasklet_node,
            "table",
        )
        if isinstance(offset_expr, MemletExpr):
            self._add_entry_memlet_path(
                offset_expr.node,
                offset_expr.subset,
                tasklet_node,
                "offset",
            )
        else:
            self._add_edge(
                offset_expr.node,
                None,
                tasklet_node,
                "offset",
                dace.Memlet(data=offset_expr.node.data, subset="0"),
            )

        dtype = offset_table_node.desc(self.sdfg).dtype
        return self._get_tasklet_result(dtype, tasklet_node, new_index_connector)

    def _make_unstructured_shift(
        self,
        it: IteratorExpr,
        connectivity: gtx_common.Connectivity,
        offset_table_node: dace.nodes.AccessNode,
        offset_expr: IteratorIndexExpr,
    ) -> IteratorExpr:
        """Implements shift in unstructured domain by means of a neighbor table."""
        assert connectivity.neighbor_axis in it.dimensions
        neighbor_dim = connectivity.neighbor_axis
        assert neighbor_dim not in it.indices

        origin_dim = connectivity.origin_axis
        assert origin_dim in it.indices
        origin_index = it.indices[origin_dim]
        assert isinstance(origin_index, SymbolExpr)

        shifted_indices = {dim: idx for dim, idx in it.indices.items() if dim != origin_dim}
        if isinstance(offset_expr, SymbolExpr):
            # use memlet to retrieve the neighbor index
            shifted_indices[neighbor_dim] = MemletExpr(
                offset_table_node,
                sbs.Indices([origin_index.value, offset_expr.value]),
            )
        else:
            # dynamic offset: we cannot use a memlet to retrieve the offset value, use a tasklet node
            shifted_indices[neighbor_dim] = self._make_dynamic_neighbor_offset(
                offset_expr, offset_table_node, origin_index
            )

        return IteratorExpr(it.field, it.dimensions, shifted_indices)

    def _visit_shift(self, node: gtir.FunCall) -> IteratorExpr:
        # convert builtin-index type to dace type
        IndexDType: Final = dace_fieldview_util.as_dace_type(
            ts.ScalarType(kind=getattr(ts.ScalarKind, gtir.INTEGER_INDEX_BUILTIN.upper()))
        )

        assert isinstance(node.fun, gtir.FunCall)
        # the iterator to be shifted is the node argument, while the shift arguments
        # are provided by the nested function call; the shift arguments consist of
        # the offset provider and the offset value in each dimension to be shifted
        offset_provider_arg, offset_value_arg, it = self._visit_shift_multidim(
            node.args[0], node.fun.args
        )

        # first argument of the shift node is the offset provider
        assert isinstance(offset_provider_arg, gtir.OffsetLiteral)
        offset = offset_provider_arg.value
        assert isinstance(offset, str)
        offset_provider = self.subgraph_builder.get_offset_provider(offset)
        # second argument should be the offset value, which could be a symbolic expression or a dynamic offset
        offset_expr = (
            SymbolExpr(offset_value_arg.value, IndexDType)
            if isinstance(offset_value_arg, gtir.OffsetLiteral)
            else self.visit(offset_value_arg)
        )

        if isinstance(offset_provider, gtx_common.Dimension):
            return self._make_cartesian_shift(it, offset_provider, offset_expr)
        else:
            # initially, the storage for the connectivity tables is created as transient;
            # when the tables are used, the storage is changed to non-transient,
            # so the corresponding arrays are supposed to be allocated by the SDFG caller
            offset_table = dace_common_util.connectivity_identifier(offset)
            self.sdfg.arrays[offset_table].transient = False
            offset_table_node = self.state.add_access(offset_table)

            return self._make_unstructured_shift(
                it, offset_provider, offset_table_node, offset_expr
            )

    def visit_FunCall(self, node: gtir.FunCall) -> IteratorExpr | MemletExpr | ValueExpr:
        if cpm.is_call_to(node, "deref"):
            return self._visit_deref(node)

        elif cpm.is_call_to(node, "neighbors"):
            return self._visit_neighbors(node)

        elif cpm.is_applied_reduce(node):
            return self._visit_reduce(node)

        elif cpm.is_applied_shift(node):
            return self._visit_shift(node)

        else:
            assert isinstance(node.fun, gtir.SymRef)

        node_internals = []
        node_connections: dict[str, MemletExpr | ValueExpr] = {}
        for i, arg in enumerate(node.args):
            arg_expr = self.visit(arg)
            if isinstance(arg_expr, MemletExpr | ValueExpr):
                # the argument value is the result of a tasklet node or direct field access
                connector = f"__inp_{i}"
                node_connections[connector] = arg_expr
                node_internals.append(connector)
            else:
                assert isinstance(arg_expr, SymbolExpr)
                # use the argument value without adding any connector
                node_internals.append(arg_expr.value)

        # use tasklet connectors as expression arguments
        builtin_name = str(node.fun.id)
        code = gtir_python_codegen.format_builtin(builtin_name, *node_internals)

        out_connector = "result"
        tasklet_node = self._add_tasklet(
            builtin_name,
            set(node_connections.keys()),
            {out_connector},
            "{} = {}".format(out_connector, code),
        )

        for connector, arg_expr in node_connections.items():
            if isinstance(arg_expr, ValueExpr):
                self._add_edge(
                    arg_expr.node,
                    None,
                    tasklet_node,
                    connector,
                    dace.Memlet(data=arg_expr.node.data, subset="0"),
                )
            else:
                self._add_entry_memlet_path(
                    arg_expr.node,
                    arg_expr.subset,
                    tasklet_node,
                    connector,
                )

        assert node.type
        dtype = dace_fieldview_util.as_dace_type(node.type)

        return self._get_tasklet_result(dtype, tasklet_node, "result")

    def visit_Lambda(
        self, node: gtir.Lambda, args: list[IteratorExpr | MemletExpr | SymbolExpr]
    ) -> tuple[list[InputConnection], ValueExpr]:
        for p, arg in zip(node.params, args, strict=True):
            self.symbol_map[str(p.id)] = arg
        output_expr: MemletExpr | SymbolExpr | ValueExpr = self.visit(node.expr)
        if isinstance(output_expr, ValueExpr):
            return self.input_connections, output_expr

        if isinstance(output_expr, MemletExpr):
            # special case where the field operator is simply copying data from source to destination node
            output_dtype = output_expr.node.desc(self.sdfg).dtype
            tasklet_node = self._add_tasklet("copy", {"__inp"}, {"__out"}, "__out = __inp")
            self._add_entry_memlet_path(
                output_expr.node,
                output_expr.subset,
                tasklet_node,
                "__inp",
            )
        else:
            # even simpler case, where a constant value is written to destination node
            output_dtype = output_expr.dtype
            tasklet_node = self._add_tasklet("write", {}, {"__out"}, f"__out = {output_expr.value}")
        return self.input_connections, self._get_tasklet_result(output_dtype, tasklet_node, "__out")

    def visit_Literal(self, node: gtir.Literal) -> SymbolExpr:
        dtype = dace_fieldview_util.as_dace_type(node.type)
        return SymbolExpr(node.value, dtype)

    def visit_SymRef(self, node: gtir.SymRef) -> IteratorExpr | MemletExpr | SymbolExpr:
        param = str(node.id)
        if param in self.symbol_map:
            return self.symbol_map[param]
        # if not in the lambda symbol map, this must be a symref to a builtin function
        assert param in gtir_python_codegen.MATH_BUILTINS_MAPPING
        return SymbolExpr(param, dace.string)
