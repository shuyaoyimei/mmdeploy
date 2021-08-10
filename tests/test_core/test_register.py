import os

import torch


def test_function_rewriter():
    from mmdeploy.core import FUNCTION_REWRITER, RewriterContext

    x = torch.tensor([1, 2, 3, 4, 5])
    y = torch.tensor([2, 4, 6, 8, 10])

    @FUNCTION_REWRITER.register_rewriter(
        func_name='torch.mul', backend='tensorrt')
    @FUNCTION_REWRITER.register_rewriter(
        func_name='torch.add', backend='tensorrt')
    def sub_func(rewriter, x, y):
        assert hasattr(rewriter, 'cfg')
        assert hasattr(rewriter, 'origin_func')
        return x - y

    cfg = dict()
    with RewriterContext(cfg, backend='tensorrt'):
        result = torch.add(x, y)
        # replace add with sub
        torch.testing.assert_allclose(result, x - y)
        result = torch.mul(x, y)
        # replace add with sub
        torch.testing.assert_allclose(result, x - y)

    result = torch.add(x, y)
    # recovery origin function
    torch.testing.assert_allclose(result, x + y)

    with RewriterContext(cfg):
        result = torch.add(x, y)
        # replace should not happen with wrong backend
        torch.testing.assert_allclose(result, x + y)

    # test different config
    @FUNCTION_REWRITER.register_rewriter(
        func_name='torch.Tensor.add', backend='default')
    def mul_func_class(rewriter, x, y):
        return x * y

    with RewriterContext(cfg, backend='tensorrt'):
        result = x.add(y)
        # replace add with multi
        torch.testing.assert_allclose(result, x * y)

    result = x.add(y)
    # recovery origin function
    torch.testing.assert_allclose(result, x + y)

    with RewriterContext(cfg):
        result = x.add(y)
        # replace add with multi
        torch.testing.assert_allclose(result, x * y)

    # test origin_func
    @FUNCTION_REWRITER.register_rewriter(
        func_name='torch.add', backend='default')
    def origin_add_func(rewriter, x, y):
        return rewriter.origin_func(x, y) + 1

    with RewriterContext(cfg):
        result = torch.add(x, y)
        # replace with origin + 1
        torch.testing.assert_allclose(result, x + y + 1)


def test_module_rewriter():
    from torchvision.models.resnet import resnet50

    from mmdeploy.core import MODULE_REWRITER, patch_model

    @MODULE_REWRITER.register_rewrite_module(
        module_type='torchvision.models.resnet.Bottleneck', backend='tensorrt')
    class BottleneckWrapper(torch.nn.Module):

        def __init__(self, module, cfg, **kwargs):
            super().__init__()
            self.module = module

        def forward(self, *args, **kwargs):
            return self.module(*args, **kwargs) * 2

    x = torch.rand(1, 64, 32, 32)
    model = resnet50().eval()
    bottle_neck = model.layer1[0]
    result = bottle_neck(x)

    # rewrite module
    cfg = dict()

    rewrited_model = patch_model(model, cfg=cfg, backend='tensorrt')
    rewrited_bottle_nect = rewrited_model.layer1[0]
    rewrited_result = rewrited_bottle_nect(x)
    torch.testing.assert_allclose(rewrited_result, result * 2)

    # wrong backend should not be rewrited

    rewrited_model = patch_model(model, cfg=cfg)
    rewrited_bottle_nect = rewrited_model.layer1[0]
    rewrited_result = rewrited_bottle_nect(x)
    torch.testing.assert_allclose(rewrited_result, result)


def test_symbolic_register():
    import onnx
    from torch.autograd import Function

    import mmdeploy
    from mmdeploy.core import SYMBOLIC_REGISTER, register_extra_symbolics

    class TestFunc(Function):

        @staticmethod
        def symbolic(g, x, val):
            return g.op('mmcv::symbolic_old', x, val_i=val)

        @staticmethod
        def forward(ctx, x, val):
            return x + val

    # put TestFunc in an module so we can found it
    # could be any module
    mmdeploy.TestFunc = TestFunc
    test_func = mmdeploy.TestFunc.apply

    @SYMBOLIC_REGISTER.register_symbolic('mmdeploy.TestFunc', backend='ncnn')
    @SYMBOLIC_REGISTER.register_symbolic('mmdeploy.TestFunc')
    def symbolic_testfunc_default(symbolic_wrapper, g, x, val):
        assert hasattr(symbolic_wrapper, 'cfg')
        return g.op('mmcv::symbolic_testfunc_default', x, val_i=val)

    @SYMBOLIC_REGISTER.register_symbolic(
        'mmdeploy.TestFunc', backend='tensorrt')
    def symbolic_testfunc_tensorrt(symbolic_wrapper, g, x, val):
        return g.op('mmcv::symbolic_testfunc_tensorrt', x, val_i=val)

    @SYMBOLIC_REGISTER.register_symbolic(
        'cummax', is_pytorch=True, arg_descriptors=['v', 'i'])
    def symbolic_cummax(symbolic_wrapper, g, input, dim):
        return g.op('mmcv::cummax_default', input, dim_i=dim, outputs=2)

    class TestModel(torch.nn.Module):

        def __init__(self):
            super().__init__()

        def forward(self, x):
            return torch.cummax(test_func(x, 5), dim=1)

    model = TestModel().eval()

    # dummy input
    x = torch.rand(2, 3, 4)
    output_file = 'demo.onnx'

    # default
    cfg = dict()
    register_extra_symbolics(cfg=cfg, opset=11)
    torch.onnx.export(model, x, output_file, opset_version=11)
    onnx_model = onnx.load(output_file)
    os.remove(output_file)
    nodes = onnx_model.graph.node
    assert nodes[0].op_type == 'symbolic_testfunc_default'
    assert nodes[0].domain == 'mmcv'
    assert nodes[1].op_type == 'cummax_default'
    assert nodes[1].domain == 'mmcv'

    # ncnn
    register_extra_symbolics(cfg=cfg, backend='ncnn', opset=11)
    torch.onnx.export(model, x, output_file, opset_version=11)
    onnx_model = onnx.load(output_file)
    os.remove(output_file)
    nodes = onnx_model.graph.node
    assert nodes[0].op_type == 'symbolic_testfunc_default'
    assert nodes[0].domain == 'mmcv'
    assert nodes[1].op_type == 'cummax_default'
    assert nodes[1].domain == 'mmcv'

    # default
    register_extra_symbolics(cfg=cfg, backend='tensorrt', opset=11)
    torch.onnx.export(model, x, output_file, opset_version=11)
    onnx_model = onnx.load(output_file)
    os.remove(output_file)
    nodes = onnx_model.graph.node
    assert nodes[0].op_type == 'symbolic_testfunc_tensorrt'
    assert nodes[0].domain == 'mmcv'
    assert nodes[1].op_type == 'cummax_default'
    assert nodes[1].domain == 'mmcv'