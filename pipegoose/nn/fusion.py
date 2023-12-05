import torch
from typing import Any, Type, Callable
from multimethod import overload
from torch import fx
from torch import Tensor
from torch.nn import functional as F

from torch.nn import GELU, Dropout, Module
from torch.nn.modules.dropout import _DropoutNd
from transformers.models.bloom.modeling_bloom import BloomGelu


class FusedLayer:
    # Used to match layers in Parallel.module to their fused layer counterpart
    represents: list[Type[Module]] = []
    wraps: set[Callable] = []

    # We pass the target_layer to give each fused layer the ability to copy its instantiation arguments
    def __init__(self, target_layer: Module) -> None:
        pass


def _parent_name(target: str) -> tuple[str, str]:
    *parent, name = target.rsplit(".", 1)
    return parent[0] if parent else "", name


def replace_node_module(node: fx.Node, modules: dict[str, Any], new_module: torch.nn.Module):
    assert(isinstance(node.target, str))
    parent_name, name = _parent_name(node.target)
    setattr(modules[parent_name], name, new_module)


def should_fuse_layer(
    layer: fx.Node, fusion_candidate: FusedLayer, modules: dict[str, Any]
) -> bool:
    # Output is not going anywhere, we will run into indexing issues and won't fuse
    if len(layer.args) == 0:
        return False
    # Enforce Node type
    if not isinstance(layer, fx.Node):
        return False
    if layer.op != "call_module":
        return False
    if not isinstance(layer.target, str):
        return False
    if layer.target not in modules:
        return False
    if type(modules[layer.target]) not in fusion_candidate.represents:
        return False

    return True


@torch.jit.script
def _fused_gelu_fwd(input):
    return (
        input
        * 0.5
        * (
            1.0
            + torch.tanh(
                0.7978845608028654 * (input + 0.044715 * input * input * input)
            )
        )
    )


@torch.jit.script
def _fused_gelu_bwd(g, input):
    tanh_out = torch.tanh(0.7978845608028654 * input * (1 + 0.044715 * input * input))
    ff = 0.5 * input * (
        (1 - tanh_out * tanh_out)
        * (0.7978845608028654 + 0.1070322244089 * input * input)
    ) + 0.5 * (1 + tanh_out)
    return ff * g


@torch.jit.script
def _fused_bias_gelu_fwd(input, bias):
    x = input + bias
    return _fused_gelu_fwd(x)


@torch.jit.script
def _fused_bias_gelu_bwd(g, input, bias):
    x = input + bias
    return _fused_gelu_bwd(g, x)


from torch import nn

BASE_MODEL = nn.Sequential(
    nn.Linear(10, 10),
    nn.GELU(),
    nn.Dropout(0.1),
    nn.Linear(10, 10),
    nn.GELU(),
    nn.Dropout(0.1),
    nn.Linear(10, 10)
)


@torch.jit.script
def fused_bias_dropout(x, bias, p, training, inplace):
    # type: (Tensor, Tensor, float, bool, bool) -> Tensor
    return F.dropout(x + bias, p=p, training=training, inplace=inplace)


# This is our next best bet, where we wrap the actual fused gelu in another module class
# And then call fused_gelu.apply, where we assume fusedgelu inherits from torch.autograd.Function
# It seems input is not a Tensor, but a tuple of Tensors, so we get to unpack it based on whether it has bias or not

class _FusedBiasGeluFn(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input, bias):
        ctx.save_for_backward(input, bias)
        return _fused_bias_gelu_fwd(input, bias)

    @staticmethod
    def backward(ctx, grad_output):
        input, bias = ctx.saved_tensors
        return (tmp := _fused_bias_gelu_bwd(grad_output, input, bias)), tmp
    
class _FusedGeluFn(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input):
        ctx.save_for_backward(input)
        return _fused_gelu_fwd(input)

    @staticmethod
    def backward(ctx, grad_output):
        input = ctx.saved_tensors
        return _fused_gelu_bwd(grad_output, input)
        
class FusedBiasGelu(GELU, FusedLayer):
    """Fused gelu + bias function."""

    represents = [GELU, BloomGelu]
    approximate: str
    wraps = [len]

    @overload
    def __init__(self, target_layer: GELU):
        super().__init__()
        self.approximate = target_layer.approximate

    @overload
    def __init__(self, target_layer): super().__init__()

    @staticmethod
    def forward(input):
        return _FusedBiasGeluFn.apply(input)


class FusedGelu(GELU, FusedLayer):
    represents = [GELU, BloomGelu]
    approximate: str
    wraps = [len]

    @overload
    def __init__(self, target_layer: GELU):
        super().__init__()
        self.approximate = target_layer.approximate

    @overload
    def __init__(self, target_layer): super().__init__()

    @staticmethod
    def forward(input):
        return _FusedGeluFn.apply(input)
    
@torch.jit.script
def fused_bias_dropout(
    input: Tensor,
    bias: Tensor,
    dropout_prob: float,
    training: bool,
    inplace: bool = False,
) -> Tensor:
    # type: (Tensor, Tensor, float, bool, bool) -> Tensor
    return F.dropout(input + bias, p=dropout_prob, training=training, inplace=inplace)

class _FusedBiasDropoutFn(torch.autograd.Function):

    @staticmethod
    def forward(ctx, input, bias, p, training, inplace):
        ctx.save_for_backward(input, bias)
        ctx.p = p
        ctx.training = training
        ctx.inplace = inplace
        return fused_bias_dropout(input, bias, p, training, inplace)

    @staticmethod
    def backward(ctx, grad_output):
        input, bias = ctx.saved_tensors
        return (tmp := _fused_bias_gelu_bwd(grad_output, input, bias)), tmp

class FusedBiasDropout(_DropoutNd, FusedLayer):
    """
    Fused dropout + bias function.
    See: https://pytorch.org/docs/stable/_modules/torch/nn/modules/dropout.html#Dropout
    """

    represents = [Dropout]

    def __init__(self, target_layer: Dropout):
        dropout_p = target_layer.p
        inplace = target_layer.inplace
        super().__init__(p=dropout_p, inplace=inplace)

    def forward(self, input: Tensor):
        print(input)
        return F.dropout(input, self.p, self.training, self.inplace)

import copy

fx_model = fx.symbolic_trace(BASE_MODEL)
# Maps node.target to the module it represents
modules = dict(fx_model.named_modules())
new_graph = copy.deepcopy(fx_model.graph)


for node in new_graph.nodes:
    if node.op == "call_module":
        if type(modules[node.target]) is GELU:
            if len(node.users) > 1:  # Output used by other nodes
                continue
            gelu = modules[node.target]
            fused_gelu = FusedGelu(gelu)
            replace_node_module(node, modules, fused_gelu)
            # This could be redundant overfitting to the torch example
            node.replace_all_uses_with(node.target)
            # In the example , this removes the batch once it was folded.
            # We are not folding any modules together, so this is not needed
            # new_graph.erase_node(node)
        # if type(modules[node.target]) is Dropout:
        #     if len(node.users) > 1:
        #         continue
        #     dropout = modules[node.target]
        #     fused_dropout = FusedBiasDropout(dropout)
        #     replace_node_module(node, modules, fused_dropout)
        #     node.replace_all_uses_with(node.target)

randinp = torch.randn(10)            
BASE_MODEL = fx.GraphModule(fx_model, new_graph)
BASE_MODEL(randinp)
