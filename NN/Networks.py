import os
import cv2
import time
import pickle
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
from tensorflow.python.framework import graph_io
from tensorflow.python.tools import freeze_graph

from NN.Layers import *

from Util.ProgressBar import ProgressBar


class Util:
    @staticmethod
    def get_and_pop(dic, key, default):
        try:
            val = dic[key]
            dic.pop(key)
        except KeyError:
            val = default
        return val

    @staticmethod
    def callable(obj):
        _str_obj = str(obj)
        if callable(obj):
            return True
        if "<" not in _str_obj and ">" not in _str_obj:
            return False
        if _str_obj.find("function") >= 0 or _str_obj.find("staticmethod") >= 0:
            return True


class NNVerbose:
    NONE = 0
    EPOCH = 1
    METRICS = 2
    METRICS_DETAIL = 3
    DETAIL = 4
    DEBUG = 5


class NNConfig:
    BOOST_LESS_SAMPLES = False
    TRAINING_SCALE = 0.8


# Neural Network

class NNBase:
    NNTiming = Timing()

    def __init__(self):
        self._layers = []
        self._layer_names, self._layer_params = [], []
        self._lr = 0
        self._w_stds, self._b_inits = [], []
        self._optimizer = None
        self._data_size = 0
        self.verbose = 1

        self._current_dimension = 0

        self._logs = {}
        self._timings = {}
        self._metrics, self._metric_names = [], []

        self._x = self._y = None
        self._x_min = self._x_max = self._y_min = self._y_max = 0
        self._transferred_flags = {"train": False, "test": False}

        self._tfx = self._tfy = None
        self._tf_weights, self._tf_bias = [], []
        self._loss = self._y_pred = self._activations = None

        self._loaded = False
        self._train_step = None

        self._layer_factory = LayerFactory()

    def __getitem__(self, item):
        if isinstance(item, int):
            if item < 0 or item >= len(self._layers):
                return
            bias = self._tf_bias[item]
            return {
                "name": self._layers[item].name,
                "weight": self._tf_weights[item],
                "bias": bias
            }
        if isinstance(item, str):
            return getattr(self, "_" + item)
        return

    def __str__(self):
        return "Neural Network"

    __repr__ = __str__

    @NNTiming.timeit(level=4, prefix="[API] ")
    def feed_timing(self, timing):
        if isinstance(timing, Timing):
            self.NNTiming = timing
            for layer in self._layers:
                layer.feed_timing(timing)

    @property
    def name(self):
        return (
            "-".join([str(_layer.shape[1]) for _layer in self._layers]) +
            " at {}".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))
        )

    @NNTiming.timeit(level=4)
    def _get_w(self, shape):
        initial = tf.truncated_normal(shape, stddev=self._w_stds[-1])
        return tf.Variable(initial, name="w")

    @NNTiming.timeit(level=4)
    def _get_b(self, shape):
        return tf.Variable(np.zeros(shape, dtype=np.float32) + self._b_inits[-1], name="b")

    @NNTiming.timeit(level=4)
    def _add_params(self, shape, conv_channel=None, fc_shape=None, apply_bias=True):
        if fc_shape is not None:
            w_shape = (fc_shape, shape[1])
            b_shape = shape[1],
        elif conv_channel is not None:
            if len(shape[1]) <= 2:
                w_shape = shape[1][0], shape[1][1], conv_channel, conv_channel
            else:
                w_shape = (shape[1][1], shape[1][2], conv_channel, shape[1][0])
            b_shape = shape[1][0],
        else:
            w_shape = shape
            b_shape = shape[1],
        self._tf_weights.append(self._get_w(w_shape))
        if apply_bias:
            self._tf_bias.append(self._get_b(b_shape))
        else:
            self._tf_bias.append(None)

    @NNTiming.timeit(level=4)
    def _add_param_placeholder(self):
        self._tf_weights.append(tf.constant([.0]))
        self._tf_bias.append(tf.constant([.0]))

    @NNTiming.timeit(level=4)
    def _add_layer(self, layer, *args, **kwargs):
        if not self._layers and isinstance(layer, str):
            if layer.lower() == "pipe":
                self._layers.append(NNPipe(args[0]))
                self._add_param_placeholder()
                return
            _layer = self._layer_factory.handle_str_main_layers(layer, *args, **kwargs)
            if _layer:
                self.add(_layer, pop_last_init=True)
                return
        _parent = self._layers[-1]
        if isinstance(_parent, CostLayer):
            raise BuildLayerError("Adding layer after CostLayer is not permitted")
        if isinstance(layer, str):
            if layer.lower() == "pipe":
                self._layers.append(NNPipe(args[0]))
                self._add_param_placeholder()
                return
            layer, shape = self._layer_factory.get_layer_by_name(
                layer, _parent, self._current_dimension, *args, **kwargs
            )
            if shape is None:
                self.add(layer, pop_last_init=True)
                return
            _current, _next = shape
        else:
            _current, _next = args
        if isinstance(layer, SubLayer):
            if not isinstance(layer, CostLayer) and _current != _parent.shape[1]:
                raise BuildLayerError("Output shape should be identical with input shape "
                                      "if chosen SubLayer is not a CostLayer")
            layer.is_sub_layer = True
            self.parent = _parent
            self._layers.append(layer)
            self._add_param_placeholder()
            self._current_dimension = _next
        else:
            fc_shape, conv_channel, last_layer = None, None, self._layers[-1]
            if NNBase._is_conv(last_layer):
                if NNBase._is_conv(layer):
                    conv_channel = last_layer.n_filters
                    _current = (conv_channel, last_layer.out_h, last_layer.out_w)
                    layer.feed_shape((_current, _next))
                else:
                    layer.is_fc = True
                    last_layer.is_fc_base = True
                    fc_shape = last_layer.out_h * last_layer.out_w * last_layer.n_filters
            self._layers.append(layer)
            self._add_params((_current, _next), conv_channel, fc_shape, layer.apply_bias)
            self._current_dimension = _next
        self._update_layer_information(layer)

    @NNTiming.timeit(level=4)
    def _update_layer_information(self, layer):
        self._layer_params.append(layer.params)
        if len(self._layer_params) > 1 and not layer.is_sub_layer:
            self._layer_params[-1] = ((self._layer_params[-1][0][1],), *self._layer_params[-1][1:])

    @staticmethod
    @NNTiming.timeit(level=4)
    def _is_conv(layer):
        return isinstance(layer, ConvLayer) or isinstance(layer, NNPipe)

    @NNTiming.timeit(level=1, prefix="[API] ")
    def get_rs(self, x, y=None, predict=False, pipe=False, idx=-1):
        if y is None:
            predict = True
        if isinstance(self._layers[0], NNPipe):
            _cache = self._layers[0].get_rs(x, predict)
        else:
            _cache = self._layers[0].activate(x, self._tf_weights[0], self._tf_bias[0], predict)
        idx += 1
        _layers = self._layers[1:idx] if idx != 0 else self._layers[1:]
        for i, layer in enumerate(_layers):
            if i == len(self._layers) - 2:
                if y is None:
                    if not pipe:
                        if NNDist._is_conv(self._layers[i]):
                            _cache = tf.reshape(_cache, [-1, int(np.prod(_cache.get_shape()[1:]))])
                        if self._tf_bias[-1] is not None:
                            return tf.matmul(_cache, self._tf_weights[-1]) + self._tf_bias[-1]
                        return tf.matmul(_cache, self._tf_weights[-1])
                    else:
                        if not isinstance(layer, NNPipe):
                            return layer.activate(_cache, self._tf_weights[i + 1], self._tf_bias[i + 1], predict)
                        return layer.get_rs(_cache, predict)
                predict = y
            if not isinstance(layer, NNPipe):
                _cache = layer.activate(_cache, self._tf_weights[i + 1], self._tf_bias[i + 1], predict)
            else:
                _cache = layer.get_rs(_cache, predict)
        return _cache

    @NNTiming.timeit(level=4, prefix="[API] ")
    def add(self, layer, *args, **kwargs):

        # Init kwargs
        kwargs["apply_bias"] = kwargs.get("apply_bias", True)
        kwargs["position"] = kwargs.get("position", len(self._layers) + 1)

        self._w_stds.append(Util.get_and_pop(kwargs, "std", 0.1))
        self._b_inits.append(Util.get_and_pop(kwargs, "init", 0.1))
        if Util.get_and_pop(kwargs, "pop_last_init", False):
            self._w_stds.pop()
            self._b_inits.pop()
        if isinstance(layer, str):
            # noinspection PyTypeChecker
            self._add_layer(layer, *args, **kwargs)
        else:
            if not isinstance(layer, Layer):
                raise BuildLayerError("Invalid Layer provided (should be subclass of Layer)")
            if not self._layers:
                if isinstance(layer, SubLayer):
                    raise BuildLayerError("Invalid Layer provided (first layer should not be subclass of SubLayer)")
                if len(layer.shape) != 2:
                    raise BuildLayerError("Invalid input Layer provided (shape should be {}, {} found)".format(
                        2, len(layer.shape)
                    ))
                self._layers, self._current_dimension = [layer], layer.shape[1]
                self._update_layer_information(layer)
                if isinstance(layer, ConvLayer):
                    self._add_params(layer.shape, layer.n_channels, apply_bias=layer.apply_bias)
                else:
                    self._add_params(layer.shape, apply_bias=layer.apply_bias)
            else:
                if len(layer.shape) > 2:
                    raise BuildLayerError("Invalid Layer provided (shape should be {}, {} found)".format(
                        2, len(layer.shape)
                    ))
                if len(layer.shape) == 2:
                    _current, _next = layer.shape
                    if isinstance(layer, SubLayer):
                        if _next != self._current_dimension:
                            raise BuildLayerError("Invalid SubLayer provided (shape[1] should be {}, {} found)".format(
                                self._current_dimension, _next
                            ))
                    elif not NNDist._is_conv(layer) and _current != self._current_dimension:
                        raise BuildLayerError("Invalid Layer provided (shape[0] should be {}, {} found)".format(
                            self._current_dimension, _current
                        ))
                    self._add_layer(layer, _current, _next)
                elif len(layer.shape) == 1:
                    _next = layer.shape[0]
                    layer.shape = (self._current_dimension, _next)
                    self._add_layer(layer, self._current_dimension, _next)
                else:
                    raise LayerError("Invalid Layer provided (invalid shape '{}' found)".format(layer.shape))

    @NNTiming.timeit(level=4, prefix="[API] ")
    def add_pipe_layer(self, idx, layer, shape=None, *args, **kwargs):
        _last_layer = self._layers[-1]
        if len(self._layers) == 1:
            _last_parent = None
        else:
            _last_parent = self._layers[-2]
        if not isinstance(_last_layer, NNPipe):
            raise BuildLayerError("Adding pipe layers to a non-NNPipe object is not allowed")
        if not _last_layer.initialized[idx] and len(shape) == 1:
            if _last_parent is None:
                raise BuildLayerError("Adding invalid pipe layer, please check the 'shape' parameter")
            _dim = (_last_parent.n_filters, _last_parent.out_h, _last_parent.out_w)
            shape = (_dim, shape[0])
        _last_layer.add(idx, layer, shape, *args, **kwargs)

    @NNTiming.timeit(level=4, prefix="[API] ")
    def preview(self, verbose=0):
        if not self._layers:
            rs = "None"
        else:
            rs = (
                "Input  :  {:<10s} - {}\n".format("Dimension", self._layers[0].shape[0]) +
                "\n".join([_layer.info for _layer in self._layers]))
        print("=" * 30 + "\n" + "Structure\n" + "-" * 30 + "\n" + rs + "\n" + "-" * 30)
        if verbose >= 1:
            print("Initial Values\n" + "-" * 30)
            print("\n".join(["({:^16s}) w_std: {:8.6} ; b_init: {:8.6}".format(
                _batch[0].name, float(_batch[1]), float(_batch[2])) if not isinstance(
                _batch[0], NNPipe) else "({:^16s}) ({:^3d})".format(
                "Pipe", len(_batch[0]["nn_lst"])
            ) for _batch in zip(self._layers, self._w_stds, self._b_inits) if not isinstance(
                _batch[0], SubLayer) and not isinstance(
                _batch[0], CostLayer) and not isinstance(
                _batch[0], ConvPoolLayer)])
            )
        if verbose >= 2:
            for _layer in self._layers:
                if isinstance(_layer, NNPipe):
                    _layer.preview()
        print("-" * 30)


class NNDist(NNBase):
    NNTiming = Timing()

    def __init__(self):
        NNBase.__init__(self)
        gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=0.2)
        self._sess = tf.Session(config=tf.ConfigProto(gpu_options=gpu_options))
        # self._sess = tf.Session()
        self._optimizer_factory = OptFactory()

        self._available_metrics = {
            "acc": NNDist._acc, "_acc": NNDist._acc,
            "f1": NNDist._f1_score, "_f1_score": NNDist._f1_score
        }

    @NNTiming.timeit(level=4, prefix="[Initialize] ")
    def initialize(self):
        self._layers = []
        self._layer_names, self._layer_params = [], []
        self._lr = 0
        self._w_stds, self._b_inits = [], []
        self._optimizer = None
        self._data_size = 0
        self.verbose = 0

        self._current_dimension = 0

        self._logs = {}
        self._timings = {}
        self._metrics, self._metric_names = [], []

        self._x = self._y = None
        self._x_min = self._x_max = self._y_min = self._y_max = 0
        self._transferred_flags = {"train": False, "test": False}

        self._tfx = self._tfy = None
        self._tf_weights, self._tf_bias = [], []
        self._loss = self._y_pred = self._activations = None

        self._loaded = False
        self._train_step = None

        self._sess = tf.Session()

    # Property

    @property
    def layer_names(self):
        return [layer.name for layer in self._layers]

    @layer_names.setter
    def layer_names(self, value):
        self._layer_names = value

    @property
    def layer_special_params(self):
        return [layer.get_special_params(self._sess) for layer in self._layers]

    @layer_special_params.setter
    def layer_special_params(self, value):
        for layer, sp_param in zip(self._layers, value):
            if sp_param is not None:
                layer.set_special_params(sp_param)

    @property
    def optimizer(self):
        return self._optimizer.name

    @optimizer.setter
    def optimizer(self, value):
        self._optimizer = value

    # Utils

    @staticmethod
    @NNTiming.timeit(level=4, prefix="[Private StaticMethod] ")
    def _transfer_x(x):
        if x is None:
            return
        if len(x.shape) == 1:
            x = x.reshape(1, -1)
        if len(x.shape) == 4:
            x = x.transpose(0, 2, 3, 1)
        return x.astype(np.float32)

    @NNTiming.timeit(level=4)
    def _feed_data(self, x, y):
        if x is None:
            if self._x is None:
                raise BuildNetworkError("Please provide input matrix")
            x = self._x
        else:
            if not self._transferred_flags["train"]:
                x = NNDist._transfer_x(x)
                self._transferred_flags["train"] = True
        if y is None:
            if self._y is None:
                raise BuildNetworkError("Please provide input matrix")
            y = self._y
        else:
            y = np.array(y, dtype=np.float32)
        if len(x) != len(y):
            raise BuildNetworkError("Data fed to network should be identical in length, x: {} and y: {} found".format(
                len(x), len(y)
            ))
        self._x, self._y = x, y
        self._x_min, self._x_max = np.min(x), np.max(x)
        self._y_min, self._y_max = np.min(y), np.max(y)
        self._data_size = len(x)
        return x, y

    @NNTiming.timeit(level=2)
    def _get_prediction(self, x, name=None, batch_size=1e6, verbose=None, out_of_sess=False, idx=-1):
        if verbose is None:
            verbose = self.verbose
        single_batch = int(batch_size / np.prod(x.shape[1:]))
        if not single_batch:
            single_batch = 1
        _y_pred = self._y_pred if idx == -1 else self.get_rs(self._tfx, idx=idx)
        if single_batch >= len(x):
            if not out_of_sess:
                return _y_pred.eval(feed_dict={self._tfx: x})
            with self._sess.as_default():
                return self.get_rs(x, idx=idx).eval(feed_dict={self._tfx: x})
        epoch = int(len(x) / single_batch)
        if not len(x) % single_batch:
            epoch += 1
        name = "Prediction" if name is None else "Prediction ({})".format(name)
        sub_bar = ProgressBar(min_value=0, max_value=epoch, name=name)
        if verbose >= NNVerbose.METRICS:
            sub_bar.start()
        if not out_of_sess:
            rs = [_y_pred.eval(feed_dict={self._tfx: x[:single_batch]})]
        else:
            rs = [self.get_rs(x[:single_batch], idx=idx)]
        count = single_batch
        if verbose >= NNVerbose.METRICS:
            sub_bar.update()
        while count < len(x):
            count += single_batch
            if count >= len(x):
                if not out_of_sess:
                    rs.append(_y_pred.eval(feed_dict={self._tfx: x[count - single_batch:]}))
                else:
                    rs.append(self.get_rs(x[count - single_batch:], idx=idx))
            else:
                if not out_of_sess:
                    rs.append(_y_pred.eval(feed_dict={self._tfx: x[count - single_batch:count]}))
                else:
                    rs.append(self.get_rs(x[count - single_batch:count], idx=idx))
            if verbose >= NNVerbose.METRICS:
                sub_bar.update()
        if out_of_sess:
            with self._sess.as_default():
                rs = [_rs.eval() for _rs in rs]
        return np.vstack(rs)

    @NNTiming.timeit(level=4)
    def _get_activations(self, x, predict=False):
        if not isinstance(self._layers[0], NNPipe):
            _activations = [self._layers[0].activate(x, self._tf_weights[0], self._tf_bias[0], predict)]
        else:
            _activations = [self._layers[0].get_rs(x, predict)]
        for i, layer in enumerate(self._layers[1:]):
            if i == len(self._layers) - 2:
                if NNDist._is_conv(self._layers[i]):
                    _activations[-1] = tf.reshape(
                        _activations[-1], [-1, int(np.prod(_activations[-1].get_shape()[1:]))])
                if self._tf_bias[-1] is not None:
                    _activations.append(tf.matmul(_activations[-1], self._tf_weights[-1]) + self._tf_bias[-1])
                else:
                    _activations.append(tf.matmul(_activations[-1], self._tf_weights[-1]))
            else:
                if not isinstance(layer, NNPipe):
                    _activations.append(layer.activate(
                        _activations[-1], self._tf_weights[i + 1], self._tf_bias[i + 1], predict))
                else:
                    _activations.append(layer.get_rs(_activations[-1], predict))
        return _activations

    @NNTiming.timeit(level=1)
    def _get_l2_loss(self, lb):
        if lb <= 0:
            return 0
        _l2_loss = lb * tf.reduce_sum([tf.nn.l2_loss(_w) for i, _w in enumerate(self._tf_weights)
                                       if not isinstance(self._layers[i], SubLayer)])
        with tf.name_scope("loss"):
            tf.summary.scalar("l2 loss", _l2_loss)
        return _l2_loss

    @NNTiming.timeit(level=3)
    def _append_log(self, x, y, name, get_loss=True, out_of_sess=False):
        y_pred = self._get_prediction(x, name, out_of_sess=out_of_sess)
        for i, metric in enumerate(self._metrics):
            self._logs[name][i].append(metric(y, y_pred))
        if get_loss:
            if not out_of_sess:
                self._logs[name][-1].append(self._layers[-1].calculate(y, y_pred).eval())
            else:
                with self._sess.as_default():
                    self._logs[name][-1].append(self._layers[-1].calculate(y, y_pred).eval())

    @NNTiming.timeit(level=3)
    def _print_metric_logs(self, show_loss, data_type):
        print()
        print("=" * 47)
        for i, name in enumerate(self._metric_names):
            print("{:<16s} {:<16s}: {:12.8}".format(
                data_type, name, self._logs[data_type][i][-1]))
        if show_loss:
            print("{:<16s} {:<16s}: {:12.8}".format(
                data_type, "loss", self._logs[data_type][-1][-1]))
        print("=" * 47)

    # Metrics

    @staticmethod
    @NNTiming.timeit(level=2, prefix="[Private StaticMethod] ")
    def _acc(y, y_pred):
        y_arg, y_pred_arg = np.argmax(y, axis=1), np.argmax(y_pred, axis=1)
        return np.sum(y_arg == y_pred_arg) / len(y_arg)

    @staticmethod
    @NNTiming.timeit(level=2, prefix="[Private StaticMethod] ")
    def _f1_score(y, y_pred):
        y_true, y_pred = np.argmax(y, axis=1), np.argmax(y_pred, axis=1)
        tp = np.sum(y_true * y_pred)
        if tp == 0:
            return .0
        fp = np.sum((1 - y_true) * y_pred)
        fn = np.sum(y_true * (1 - y_pred))
        return 2 * tp / (2 * tp + fn + fp)

    # Init

    @NNTiming.timeit(level=4)
    def _init_optimizer(self, optimizer=None):
        if optimizer is None:
            if isinstance(self._optimizer, str):
                optimizer = self._optimizer
            else:
                if self._optimizer is None:
                    self._optimizer = Adam(self._lr)
                if isinstance(self._optimizer, Optimizer):
                    return
                raise BuildNetworkError("Invalid optimizer '{}' provided".format(self._optimizer))
        if isinstance(optimizer, str):
            self._optimizer = self._optimizer_factory.get_optimizer_by_name(
                optimizer, self.NNTiming, self._lr)
        elif isinstance(optimizer, Optimizer):
            self._optimizer = optimizer
        else:
            raise BuildNetworkError("Invalid optimizer '{}' provided".format(optimizer))

    @NNTiming.timeit(level=4)
    def _init_layers(self):
        for _layer in self._layers:
            _layer.init()

    @NNTiming.timeit(level=4)
    def _init_structure(self, verbose):
        x_shape = self._layers[0].shape[0]
        if isinstance(x_shape, int):
            x_shape = x_shape,
        y_shape = self._layers[-1].shape[1]
        x_placeholder, y_placeholder = np.zeros((1, *x_shape)), np.zeros((1, y_shape))
        self.fit(x_placeholder, y_placeholder, x_placeholder, y_placeholder, epoch=0, verbose=verbose)
        self._transferred_flags["train"] = False

    @NNTiming.timeit(level=4)
    def _init_train_step(self, sess):
        if not self._loaded:
            self._train_step = self._optimizer.minimize(self._loss)
            sess.run(tf.global_variables_initializer())
        else:
            _var_cache = set(tf.global_variables())
            self._train_step = self._optimizer.minimize(self._loss)
            sess.run(tf.variables_initializer(set(tf.global_variables()) - _var_cache))

    # API

    @NNTiming.timeit(level=4, prefix="[API] ")
    def get_current_pipe(self, idx):
        _last_layer = self._layers[-1]
        if not isinstance(_last_layer, NNPipe):
            return
        return _last_layer["nn_lst"][idx]

    @NNTiming.timeit(level=4, prefix="[API] ")
    def feed(self, x, y):
        self._feed_data(x, y)

    @NNTiming.timeit(level=4, prefix="[API] ")
    def build(self, units="load"):
        if isinstance(units, str):
            if units == "load":
                for name, param in zip(self._layer_names, self._layer_params):
                    self.add(name, *param)
            else:
                raise NotImplementedError("Invalid param '{}' provided to 'build' method".format(units))
        else:
            try:
                units = np.array(units).flatten().astype(np.int)
            except ValueError as err:
                raise BuildLayerError(err)
            if len(units) < 2:
                raise BuildLayerError("At least 2 layers are needed")
            _input_shape = (units[0], units[1])
            self.initialize()
            self.add(Sigmoid(_input_shape))
            for unit_num in units[2:]:
                self.add(Sigmoid((unit_num,)))
            self.add(CrossEntropy((units[-1],)))
        self._init_layers()

    @NNTiming.timeit(level=4, prefix="[API] ")
    def split_data(self, x, y, x_test, y_test,
                   train_only, training_scale=NNConfig.TRAINING_SCALE):
        if train_only:
            if x_test is not None and y_test is not None:
                if not self._transferred_flags["test"]:
                    x, y = np.vstack((x, NNDist._transfer_x(np.array(x_test)))), np.vstack((y, y_test))
                    self._transferred_flags["test"] = True
            x_train = x_test = x.astype(np.float32)
            y_train = y_test = y.astype(np.float32)
        else:
            shuffle_suffix = np.random.permutation(len(x))
            x, y = x[shuffle_suffix], y[shuffle_suffix]
            if x_test is None or y_test is None:
                train_len = int(len(x) * training_scale)
                x_train, y_train = x[:train_len], y[:train_len]
                x_test, y_test = x[train_len:], y[train_len:]
            elif x_test is None or y_test is None:
                raise BuildNetworkError("Please provide test sets if you want to split data on your own")
            else:
                x_train, y_train = x, y
                if not self._transferred_flags["test"]:
                    x_test, y_test = NNDist._transfer_x(np.array(x_test)), np.array(y_test, dtype=np.float32)
                    self._transferred_flags["test"] = True
        if NNConfig.BOOST_LESS_SAMPLES:
            if y_train.shape[1] != 2:
                raise BuildNetworkError("It is not permitted to boost less samples in multiple classification")
            y_train_arg = np.argmax(y_train, axis=1)
            y0 = y_train_arg == 0
            y1 = ~y0
            y_len, y0_len = len(y_train), int(np.sum(y0))
            if y0_len > 0.5 * y_len:
                y0, y1 = y1, y0
                y0_len = y_len - y0_len
            boost_suffix = np.random.randint(y0_len, size=y_len - y0_len)
            x_train = np.vstack((x_train[y1], x_train[y0][boost_suffix]))
            y_train = np.vstack((y_train[y1], y_train[y0][boost_suffix]))
            shuffle_suffix = np.random.permutation(len(x_train))
            x_train, y_train = x_train[shuffle_suffix], y_train[shuffle_suffix]
        return (x_train, x_test), (y_train, y_test)

    @NNTiming.timeit(level=1, prefix="[API] ")
    def fit(self,
            x=None, y=None, x_test=None, y_test=None,
            lr=0.01, lb=0.01, epoch=20, weight_scale=1,
            batch_size=256, record_period=1, optimizer=None,
            show_loss=True, metrics=None, do_log=False, verbose=None):

        x, y = self._feed_data(x, y)
        self._lr = lr
        self._init_optimizer(optimizer)

        if not self._layers:
            raise BuildNetworkError("Please provide layers before fitting data")

        if y.shape[1] != self._current_dimension:
            raise BuildNetworkError("Output layer's shape should be {}, {} found".format(
                self._current_dimension, y.shape[1]))

        x_train, y_train, x_test = x, y, NNDist._transfer_x(x_test)
        train_len = len(x_train)
        batch_size = min(batch_size, train_len)
        do_random_batch = train_len >= batch_size
        train_repeat = int(train_len / batch_size) + 1

        with tf.name_scope("Entry"):
            self._tfx = tf.placeholder(tf.float32, shape=[None, *x.shape[1:]])
        self._tfy = tf.placeholder(tf.float32, shape=[None, y.shape[1]])
        if epoch <= 0:
            return

        self._metrics = ["acc"] if metrics is None else metrics
        for i, metric in enumerate(self._metrics):
            if isinstance(metric, str):
                if metric not in self._available_metrics:
                    raise BuildNetworkError("Metric '{}' is not implemented".format(metric))
                self._metrics[i] = self._available_metrics[metric]
        self._metric_names = [_m.__name__ for _m in self._metrics]

        self._logs = {
            name: [[] for _ in range(len(self._metrics) + 1)] for name in ("train", "test")
            }
        if verbose is not None:
            self.verbose = verbose

        bar = ProgressBar(min_value=0, max_value=max(1, epoch // record_period), name="Epoch")
        if self.verbose >= NNVerbose.EPOCH and epoch > 0:
            bar.start()
        img = None

        with self._sess.as_default() as sess:
            # Session
            self._y_pred = self.get_rs(self._tfx)
            self._loss = self.get_rs(self._tfx, self._tfy) + self._get_l2_loss(lb)
            self._activations = self._get_activations(self._tfx)
            self._init_train_step(sess)
            for weight in self._tf_weights:
                weight *= weight_scale

            sub_bar = ProgressBar(min_value=0, max_value=train_repeat * record_period - 1, name="Iteration")
            for counter in range(epoch):
                if self.verbose >= NNVerbose.EPOCH and counter % record_period == 0:
                    sub_bar.start()
                for i in range(train_repeat):
                    if do_random_batch:
                        batch = np.random.choice(train_len, batch_size)
                        x_batch, y_batch = x_train[batch], y_train[batch]
                    else:
                        x_batch, y_batch = x_train, y_train
                    feed_dict = {self._tfx: x_batch, self._tfy: y_batch}
                    self._train_step.run(feed_dict=feed_dict)
                    if self.verbose >= NNVerbose.DEBUG:
                        pass
                    if self.verbose >= NNVerbose.EPOCH:
                        if sub_bar.update() and self.verbose >= NNVerbose.METRICS_DETAIL:
                            self._append_log(x, y, "train", get_loss=show_loss)
                            self._append_log(x_test, y_test, "test", get_loss=show_loss)
                            self._print_metric_logs(show_loss, "train")
                            self._print_metric_logs(show_loss, "test")
                if self.verbose >= NNVerbose.EPOCH:
                    sub_bar.update()

                if (counter + 1) % record_period == 0:
                    if do_log:
                        self._append_log(x, y, "train", get_loss=show_loss)
                        if x_test is not None:
                            self._append_log(x_test, y_test, "test", get_loss=show_loss)
                        if self.verbose >= NNVerbose.METRICS:
                            self._print_metric_logs(show_loss, "train")
                            if x_test is not None:
                                self._print_metric_logs(show_loss, "test")
                    if self.verbose >= NNVerbose.EPOCH:
                        bar.update(counter // record_period + 1)
                        sub_bar = ProgressBar(min_value=0, max_value=train_repeat * record_period - 1, name="Iteration")

        if img is not None:
            cv2.waitKey(0)
            cv2.destroyAllWindows()

        return self._logs

    @NNTiming.timeit(level=2, prefix="[API] ")
    def save(self, path=None, name=None, overwrite=True):
        path = "Models" if path is None else path
        name = "Cache" if name is None else name
        if not os.path.exists(os.path.join(path, name)):
            os.makedirs(os.path.join(path, name))
        _dir = os.path.join(path, name, "Model")
        if os.path.isfile(_dir):
            if not overwrite:
                _count = 1
                _new_dir = _dir + "({})".format(_count)
                while os.path.isfile(_new_dir):
                    _count += 1
                    _new_dir = _dir + "({})".format(_count)
                _dir = _new_dir
            else:
                os.remove(_dir)

        with open(_dir + ".nn", "wb") as file:
            _dic = {
                "structures": {
                    "_lr": self._lr,
                    "_layer_names": self.layer_names,
                    "_layer_params": self._layer_params,
                    "_next_dimension": self._current_dimension
                },
                "params": {
                    "_logs": self._logs,
                    "_metric_names": self._metric_names,
                    "_optimizer": self._optimizer.name,
                    "layer_special_params": self.layer_special_params
                }
            }
            pickle.dump(_dic, file)
        _saver = tf.train.Saver()
        _saver.save(self._sess, _dir)
        graph_io.write_graph(self._sess.graph, os.path.join(path, name), "Model.pb", False)
        with tf.name_scope("OutputFlow"):
            self.get_rs(self._tfx)
        _output = ""
        for op in self._sess.graph.get_operations()[::-1]:
            if "OutputFlow" in op.name:
                _output = op.name
                break
        with open(os.path.join(path, name, "IO.txt"), "w") as file:
            file.write("\n".join([
                "Input  : Entry:Placeholder:0",
                "Output : {}:0".format(_output)
            ]))
        graph_io.write_graph(self._sess.graph, os.path.join(path, name), "Cache.pb", False)
        freeze_graph.freeze_graph(
            os.path.join(path, name, "Cache.pb"),
            "", True, os.path.join(path, name, "Model"),
            _output, "save/restore_all", "save/Const:0",
            os.path.join(path, name, "Frozen.pb"), True, ""
        )
        os.remove(os.path.join(path, name, "Cache.pb"))

        print()
        print("=" * 30)
        print("Model saved in folder: ", os.path.join(path, name))
        print("=" * 30)

    @NNTiming.timeit(level=2, prefix="[API] ")
    def load(self, path=None, verbose=2):

        # Reset Graph
        tf.reset_default_graph()

        self.initialize()
        if path is None:
            path = os.path.join("Models", "Cache", "Model")
        else:
            path = os.path.join(path, "Model")
        try:
            with open(path + ".nn", "rb") as file:
                _dic = pickle.load(file)
                for key, value in _dic["structures"].items():
                    setattr(self, key, value)
                self.build()
                for key, value in _dic["params"].items():
                    setattr(self, key, value)
                self._init_optimizer()
                for i in range(len(self._metric_names) - 1, -1, -1):
                    name = self._metric_names[i]
                    if name not in self._available_metrics:
                        self._metric_names.pop(i)
                    else:
                        self._metrics.insert(0, self._available_metrics[name])
        except Exception as err:
            raise BuildNetworkError("Failed to load Network ({}), structure initialized".format(err))
        self._init_layers()
        self._loaded = True

        _saver = tf.train.Saver()
        _saver.restore(self._sess, path)
        self._init_structure(verbose)

        print()
        print("=" * 30)
        print("Model restored")
        print("=" * 30)

    @NNTiming.timeit(level=4, prefix="[API] ")
    def predict(self, x):
        x = NNDist._transfer_x(np.array(x))
        return self._get_prediction(x, out_of_sess=True)

    @NNTiming.timeit(level=4, prefix="[API] ")
    def predict_classes(self, x, flatten=True):
        x = NNDist._transfer_x(np.array(x))
        if flatten:
            return np.argmax(self._get_prediction(x, out_of_sess=True), axis=1)
        return np.argmax([self._get_prediction(x, out_of_sess=True)], axis=2).T

    @NNTiming.timeit(level=4, prefix="[API] ")
    def evaluate(self, x, y, metrics=None):
        x = NNDist._transfer_x(np.array(x))
        if metrics is None:
            metrics = self._metrics
        else:
            for i in range(len(metrics) - 1, -1, -1):
                metric = metrics[i]
                if isinstance(metric, str):
                    if metric not in self._available_metrics:
                        metrics.pop(i)
                    else:
                        metrics[i] = self._available_metrics[metric]
        logs, y_pred = [], self._get_prediction(x, verbose=2, out_of_sess=True)
        for metric in metrics:
            logs.append(metric(y, y_pred))
        return logs

    @NNTiming.timeit(level=1)
    def get_activation(self, x, idx=-1):
        with self._sess.as_default():
            return self._get_prediction(x.transpose((0, 2, 3, 1)), idx=idx)

    def draw_results(self):
        metrics_log, cost_log = {}, {}
        for key, value in sorted(self._logs.items()):
            metrics_log[key], cost_log[key] = value[:-1], value[-1]

        for i, name in enumerate(sorted(self._metric_names)):
            plt.figure()
            plt.title("Metric Type: {}".format(name))
            for key, log in sorted(metrics_log.items()):
                xs = np.arange(len(log[i])) + 1
                plt.plot(xs, log[i], label="Data Type: {}".format(key))
            plt.legend(loc=4)
            plt.show()
            plt.close()

        plt.figure()
        plt.title("Cost")
        for key, loss in sorted(cost_log.items()):
            xs = np.arange(len(loss)) + 1
            plt.plot(xs, loss, label="Data Type: {}".format(key))
        plt.legend()
        plt.show()

    @staticmethod
    def fuck_pycharm_warning():
        print(Axes3D.acorr)


class NNPipe:
    NNTiming = Timing()

    def __init__(self, num):
        self._nn_lst = [NNBase() for _ in range(num)]
        for _nn in self._nn_lst:
            _nn.verbose = 0
        self._initialized = [False] * num
        self.is_sub_layer = False

    def __getitem__(self, item):
        if isinstance(item, str):
            return getattr(self, "_" + item)
        return

    def __str__(self):
        return "NNPipe"

    __repr__ = __str__

    @property
    def name(self):
        return "NNPipe"

    @property
    def parent(self):
        return

    @property
    def n_filters(self):
        return sum([_nn["layers"][-1].n_filters for _nn in self._nn_lst])

    @property
    def out_h(self):
        return self._nn_lst[0]["layers"][-1].out_h

    @property
    def out_w(self):
        return self._nn_lst[0]["layers"][-1].out_w

    @property
    def shape(self):
        # TODO: Modify shape[0] to correct one
        return (self.n_filters, self.out_h, self.out_w), (self.n_filters, self.out_h, self.out_w)

    @property
    def info(self):
        return "Pipe ({:^3d})".format(len(self._nn_lst)) + " " * 65 + "- out: {}".format(
            self.shape[1])

    @property
    def initialized(self):
        return self._initialized

    @NNTiming.timeit(level=4, prefix="[API] ")
    def preview(self):
        print("=" * 90)
        print("Pipe Structure")
        for i, _nn in enumerate(self._nn_lst):
            print("-" * 60 + "\n" + str(i) + "\n" + "-" * 60)
            _nn.preview()

    @NNTiming.timeit(level=4, prefix="[API] ")
    def feed_timing(self, timing):
        self.NNTiming = timing

    @NNTiming.timeit(level=4, prefix="[API] ")
    def add(self, idx, layer, shape, *args, **kwargs):
        if shape is None:
            self._nn_lst[idx].add(layer, *args, **kwargs)
        else:
            self._nn_lst[idx].add(layer, shape, *args, **kwargs)
        self._initialized[idx] = True

    @NNTiming.timeit(level=1, prefix="[API] ")
    def get_rs(self, x, predict):
        return tf.concat([_nn.get_rs(x, predict=predict, pipe=True) for _nn in self._nn_lst], 3)
