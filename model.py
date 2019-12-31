# Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import

import inspect
from collections import OrderedDict

import numpy as np

from paddle import fluid
from paddle.fluid.framework import in_dygraph_mode
from paddle.fluid.dygraph.base import to_variable

__all__ = ['Model', 'shape_hints']

LOSS_DTYPE_MAP = {
    'cross_entropy': 'int64'
}


def to_list(value):
    if isinstance(value, (list, tuple)):
        return value
    return [value]


def extract_args(func):
    if hasattr(inspect, 'getfullargspec'):
        return inspect.getfullargspec(func)[0]
    else:
        return inspect.getargspec(func)[0]


def shape_hints(**hints):
    assert hints, "hints can not be empty"
    assert all(isinstance(h, (list, tuple)) for h in hints.values()), \
        "shape hint must be a list or tuple"

    def wrapper(func):
        args = extract_args(func)
        invalid = set(hints.keys()) - set(args)
        assert not invalid, \
            "shape hint for arguments that are not present in forward method" \
            + ": ({})".format(", ".join(invalid))
        func.shape_hints = hints
        return func
    return wrapper


class StaticGraphAdapter(object):
    def __init__(self, model):
        super(StaticGraphAdapter, self).__init__()
        self.model = model
        # with `_build_once` gone, parameters are now created in `__init__`
        # so we need to keep track of the parameters already created
        self._startup_prog = fluid.default_startup_program()
        self._main_prog = fluid.default_main_program()

        # HACK separate models by cleanup global scope
        self._scope = fluid.executor.global_scope()
        fluid.executor.g_scope = fluid.core.Scope()

        self._label_vars = None  # label variables
        self._endpoints = {}
        self._loss_endpoint = None
        self._executor = None
        self._progs = {}
        self._compiled_progs = {}

        # parse shape hints
        self._input_desc = OrderedDict([
            (n, None) for n in extract_args(self.model.forward) if n != 'self'
        ])
        if hasattr(self.model.forward, 'shape_hints'):
            self._input_desc.update(self.model.forward.shape_hints)

    @property
    def mode(self):
        return self.model.mode

    @mode.setter
    def mode(self, value):
        self.model.mode = value

    def train(self, inputs, labels, device='CPU', device_ids=None):
        assert self.model._optimizer and self.model._loss_functions, \
            "model not ready, please call `model.prepare()` first"
        self.mode = 'train'
        return self._run(inputs, labels, device, device_ids)

    def eval(self, inputs, labels, device='CPU', device_ids=None):
        assert self.model._loss_functions, \
            "model not ready, please call `model.prepare()` first"
        self.mode = 'eval'
        return self._run(inputs, labels, device, device_ids)

    def test(self, inputs, device='CPU', device_ids=None):
        self.mode = 'test'
        return self._run(inputs, None, device, device_ids)

    def save(self, path):
        prog = self._progs.get('train', None)
        if prog is None or self.model._optimizer is None:
            print("optimizer not initialized, save parameters only")
            prog = self._main_prog
        with fluid.executor.scope_guard(self._scope):
            fluid.save(prog, path)

    def load(self, path):
        prog = self._main_prog
        with fluid.executor.scope_guard(self._scope):
            fluid.load(prog, path, self._executor)

    def _run(self, inputs, labels=None, device='CPU', device_ids=None):
        inputs = to_list(inputs)
        if labels is not None:
            labels = to_list(labels)
        assert len(inputs) == len(self._input_desc), "number of inputs" \
            + " does not match number of arguments of `forward` method"

        with fluid.executor.scope_guard(self._scope):
            if self._progs.get(self.mode, None) is None:
                self._make_program(self._infer_input_vars(inputs))

            ids = [str(i) for i in device_ids]
            ids.sort()
            prog_hash = '_'.join([self.mode] + ids)
            compiled_prog = self._compiled_progs.get(prog_hash, None)
            if compiled_prog is None:
                compiled_prog = self._compile_and_initialize(
                    self._progs[self.mode], device, device_ids)
                self._compiled_progs[prog_hash] = compiled_prog

        feed = {}
        input_names = [name for name in self._input_desc.keys()]
        for idx, n in enumerate(input_names):
            # train and test may take different arguments
            if inputs[idx] is not None:
                feed[n] = inputs[idx]
        if labels is not None:
            for idx, v in enumerate(self._label_vars):
                feed[v.name] = labels[idx]

        outputs = self._executor.run(
            compiled_prog, scope=self._scope, feed=feed,
            fetch_list=self._endpoints[self.mode])
        return outputs

    def _make_program(self, inputs):
        prog = self._main_prog.clone(self.mode != 'train')
        with fluid.program_guard(prog, self._startup_prog):
            outputs = to_list(self.model.forward(*inputs))
            label_vars = []
            if self.mode != 'test':
                losses = []
                for o, l in zip(outputs, self.model._loss_functions):
                    if l is None:
                        continue
                    label_var = self._infer_label_var(o, l)
                    label_vars.append(label_var)
                    loss_fn = getattr(fluid.layers, l)
                    loss = loss_fn(o, label_var)
                    losses.append(fluid.layers.reduce_mean(loss))
                outputs = losses
                if self.mode == 'train':
                    self._label_vars = label_vars
                    self._loss_endpoint = fluid.layers.sum(losses)
                    self.model._optimizer.minimize(self._loss_endpoint)
        self._progs[self.mode] = prog
        self._endpoints[self.mode] = outputs

    def _infer_input_vars(self, inputs):
        input_vars = []
        for idx, i in enumerate(inputs):
            if i is None:  # train and test may take different arguments
                input_vars.append(None)
                continue
            ndarray = np.array(i)
            name = list(self._input_desc.keys())[idx]
            shape = list(self._input_desc.values())[idx]
            if shape is None:
                shape = (None, ) + ndarray.shape[1:]
            input_vars.append(fluid.data(name, shape, ndarray.dtype))
        return input_vars

    # TODO wrap loss in callable classes
    # - same call signaure
    # - infer_shape method? or same shape as y_pred (e.g., one hot)
    # - split multiple dtype loss functions (e.g., soft label)
    def _infer_label_var(self, output, loss):
        name = output.name + '.label'
        shape = output.shape
        # XXX could get ugly very quickly
        if loss == 'cross_entropy':
            shape = shape[:-1] + (1, )
        dtype = LOSS_DTYPE_MAP.get(loss, output.dtype)
        return fluid.data(name, shape, dtype)

    def _compile_and_initialize(self, prog, device='CPU', device_ids=None):
        if device.lower() == 'cpu':
            place = fluid.CPUPlace()
        elif device.lower() == 'gpu' and isinstance(device_ids, (list, tuple)):
            place = fluid.CUDAPlace(device_ids[0])
        else:
            raise "device not supported"

        compiled_prog = fluid.CompiledProgram(prog)
        if device.lower() == 'gpu' and len(device_ids) > 0:
            places = [fluid.CUDAPlace(i) for i in device_ids]
            loss_name = None
            if self._loss_endpoint is not None:
                loss_name = self._loss_endpoint.name
            compiled_prog = compiled_prog.with_data_parallel(
                loss_name=loss_name, places=places)

        if self._executor is None:
            self._executor = fluid.Executor(place)
            # XXX only run startup once as *ALL* weights should be initialized
            # upon construction of the model
            # XXX incremental initialization, lifted from GuoSheng code
            uninitialized = []
            for var_py in self._startup_prog.list_vars():
                var = fluid.global_scope().find_var(var_py.name)
                if var and var.get_tensor()._is_initialized():
                    continue
                uninitialized.append(var_py)
            if uninitialized:
                startup_prog = self._startup_prog._prune(uninitialized)
                self._executor.run(startup_prog)

        return compiled_prog


class DynamicGraphAdapter(object):
    def __init__(self, model):
        super(DynamicGraphAdapter, self).__init__()
        self.model = model

    @property
    def mode(self):
        return self.model.mode

    @mode.setter
    def mode(self, value):
        self.model.mode = value

    def train(self, inputs, labels, device='CPU', device_ids=None):
        assert self.model._optimizer and self.model._loss_functions, \
            "model not ready, please call `model.prepare()` first"
        super(Model, self.model).train()
        self.mode = 'train'
        inputs = to_list(inputs)
        labels = to_list(labels)
        outputs = self.model.forward(*[to_variable(x) for x in inputs])
        losses = self._loss(outputs, labels)
        final_loss = fluid.layers.sum(losses)
        final_loss.backward()
        self.model._optimizer.minimize(final_loss)
        self.model.clear_gradients()
        return losses

    def eval(self, inputs, labels, device='CPU', device_ids=None):
        assert self.model._loss_functions, \
            "model not ready, please call `model.prepare()` first"
        super(Model, self.model).train()
        self.mode = 'eval'
        inputs = to_list(inputs)
        labels = to_list(labels)
        outputs = self.model.forward(*[to_variable(x) for x in inputs])
        return self._loss(outputs, labels)

    def test(self, inputs, device='CPU', device_ids=None):
        super(Model, self.model).train()
        self.mode = 'test'
        inputs = to_list(inputs)
        return self.model.forward(*[to_variable(x) for x in inputs])

    def save(self, path):
        params = self.model.state_dict()
        fluid.save_dygraph(params, path)

        if self.model._optimizer is None:
            print("model does not have an optimizer, save parameters only")
            return
        if self.model._optimizer.state_dict():
            optim = self.model._optimizer.state_dict()
            fluid.save_dygraph(optim, path)

    def load(self, path):
        params, optim = fluid.load_dygraph(path)
        self.model.set_dict(params)
        if optim is None:
            print("optimizer state file not found, load parameters only")
            return
        self.model._optimizer.set_dict(optim)

    def _loss(self, pred, labels):
        losses = []
        for o, l, t in zip(to_list(pred), self.model._loss_functions, labels):
            if l is None:
                continue
            loss_fn = getattr(fluid.layers, l)
            loss = loss_fn(o, to_variable(t))
            losses.append(fluid.layers.reduce_mean(loss))
        return losses


class Model(fluid.dygraph.Layer):
    def __init__(self):
        super(Model, self).__init__(self.__class__.__name__)
        self.mode = 'train'
        self._loss_functions = []
        self._optimizer = None
        if in_dygraph_mode():
            self._adapter = DynamicGraphAdapter(self)
        else:
            self._adapter = StaticGraphAdapter(self)

    def train(self, *args, **kwargs):
        return self._adapter.train(*args, **kwargs)

    def eval(self, *args, **kwargs):
        return self._adapter.eval(*args, **kwargs)

    def test(self, *args, **kwargs):
        return self._adapter.test(*args, **kwargs)

    def save(self, *args, **kwargs):
        return self._adapter.save(*args, **kwargs)

    def load(self, *args, **kwargs):
        return self._adapter.load(*args, **kwargs)

    def prepare(self, optimizer, loss_functions):
        self._optimizer = optimizer
        self._loss_functions = to_list(loss_functions)