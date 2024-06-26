# Copyright 2024 Google LLC.
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

from typing import Any, Callable, Optional, Tuple, Type
from jax.nn import silu, sigmoid
import flax.linen as nn
import jax.numpy as jnp

from vit_jax import models_resnet


Array = Any
PRNGKey = Any
Shape = Tuple[int]
Dtype = Any


class IdentityLayer(nn.Module):
  """Identity layer, convenient for giving a name to an array."""

  @nn.compact
  def __call__(self, x):
    return x


class AddPositionEmbs(nn.Module):
  """Adds learned positional embeddings to the inputs.

  Attributes:
    posemb_init: positional embedding initializer.
  """

  posemb_init: Callable[[PRNGKey, Shape, Dtype], Array]
  param_dtype: Dtype = jnp.float32

  @nn.compact
  def __call__(self, inputs):
    """Applies the AddPositionEmbs module.

    Args:
      inputs: Inputs to the layer.

    Returns:
      Output tensor with shape `(bs, timesteps, in_dim)`.
    """
    # inputs.shape is (batch_size, seq_len, emb_dim).
    assert inputs.ndim == 3, ('Number of dimensions should be 3,'
                              ' but it is: %d' % inputs.ndim)
    pos_emb_shape = (1, inputs.shape[1], inputs.shape[2])
    pe = self.param(
        'pos_embedding', self.posemb_init, pos_emb_shape, self.param_dtype)
    return inputs + pe


class MlpBlock(nn.Module):
  """Transformer MLP / feed-forward block."""

  mlp_dim: int
  dtype: Dtype = jnp.float32
  param_dtype: Dtype = jnp.float32
  out_dim: Optional[int] = None
  dropout_rate: float = 0.1
  kernel_init: Callable[[PRNGKey, Shape, Dtype],
                        Array] = nn.initializers.xavier_uniform()
  bias_init: Callable[[PRNGKey, Shape, Dtype],
                      Array] = nn.initializers.normal(stddev=1e-6)

  @nn.compact
  def __call__(self, inputs, *, deterministic):
    """Applies Transformer MlpBlock module."""
    actual_out_dim = inputs.shape[-1] if self.out_dim is None else self.out_dim
    x = nn.Dense(
        features=self.mlp_dim,
        dtype=self.dtype,
        param_dtype=self.param_dtype,
        kernel_init=self.kernel_init,
        bias_init=self.bias_init)(  # pytype: disable=wrong-arg-types
            inputs)
    x = nn.gelu(x)
    x = nn.Dropout(rate=self.dropout_rate)(x, deterministic=deterministic)
    output = nn.Dense(
        features=actual_out_dim,
        dtype=self.dtype,
        param_dtype=self.param_dtype,
        kernel_init=self.kernel_init,
        bias_init=self.bias_init)(  # pytype: disable=wrong-arg-types
            x)
    output = nn.Dropout(
        rate=self.dropout_rate)(
            output, deterministic=deterministic)
    return output


class Encoder1DBlock(nn.Module):
  """Transformer encoder layer.

  Attributes:
    inputs: input data.
    mlp_dim: dimension of the mlp on top of attention block.
    dtype: the dtype of the computation (default: float32).
    dropout_rate: dropout rate.
    attention_dropout_rate: dropout for attention heads.
    deterministic: bool, deterministic or not (to apply dropout).
    num_heads: Number of heads in nn.MultiHeadDotProductAttention
  """

  mlp_dim: int
  num_heads: int
  dtype: Dtype = jnp.float32
  dropout_rate: float = 0.1
  attention_dropout_rate: float = 0.1

  @nn.compact
  def __call__(self, inputs, *, deterministic, hidden_size):
    """Applies Encoder1DBlock module.

    Args:
      inputs: Inputs to the layer.
      deterministic: Dropout will not be applied when set to true.

    Returns:
      output after transformer encoder block.
    """

    # Attention block.
    assert inputs.ndim == 3, f'Expected (batch, seq, hidden) got {inputs.shape}'
    x = MBConv(inp=hidden_size, oup=hidden_size, stride=1, expand_ratio=1.0, use_se=True, dtype=self.dtype)(inputs)
    x = MBConv(inp=hidden_size, oup=hidden_size, stride=1, expand_ratio=1.0, use_se=True, dtype=self.dtype)(x)
    x = nn.LayerNorm(dtype=self.dtype)(x)
    x = nn.MultiHeadDotProductAttention(
        dtype=self.dtype,
        kernel_init=nn.initializers.xavier_uniform(),
        broadcast_dropout=False,
        deterministic=deterministic,
        dropout_rate=self.attention_dropout_rate,
        num_heads=self.num_heads)(
            x, x)
    x = nn.Dropout(rate=self.dropout_rate)(x, deterministic=deterministic)
    x = x + inputs

    # MLP block.
    y = nn.LayerNorm(dtype=self.dtype)(x)
    y = MBConv(inp=self.mlp_dim, oup=self.mlp_dim, stride=1, expand_ratio=1.0, use_se=True, dtype=self.dtype)(y)
    y = MBConv(inp=self.mlp_dim, oup=self.mlp_dim, stride=1, expand_ratio=1.0, use_se=True, dtype=self.dtype)(y)
    y = MlpBlock(mlp_dim=self.mlp_dim, dtype=self.dtype, dropout_rate=self.dropout_rate)(y, deterministic=deterministic)

    return x + y


class Encoder(nn.Module):
  """Transformer Model Encoder for sequence to sequence translation.

  Attributes:
    num_layers: number of layers
    mlp_dim: dimension of the mlp on top of attention block
    num_heads: Number of heads in nn.MultiHeadDotProductAttention
    dropout_rate: dropout rate.
    attention_dropout_rate: dropout rate in self attention.
  """

  num_layers: int
  mlp_dim: int
  num_heads: int
  dropout_rate: float = 0.1
  attention_dropout_rate: float = 0.1
  add_position_embedding: bool = True

  @nn.compact
  def __call__(self, x, *, train, hidden_size):
    """Applies Transformer model on the inputs.

    Args:
      x: Inputs to the layer.
      train: Set to `True` when training.

    Returns:
      output of a transformer encoder.
    """
    assert x.ndim == 3  # (batch, len, emb)

    if self.add_position_embedding:
      x = AddPositionEmbs(
          posemb_init=nn.initializers.normal(stddev=0.02),  # from BERT.
          name='posembed_input')(
              x)
      x = nn.Dropout(rate=self.dropout_rate)(x, deterministic=not train)

    # Input Encoder
    for lyr in range(self.num_layers):
      x = Encoder1DBlock(
          mlp_dim=self.mlp_dim,
          dropout_rate=self.dropout_rate,
          attention_dropout_rate=self.attention_dropout_rate,
          name=f'encoderblock_{lyr}',
          num_heads=self.num_heads)(
              x, deterministic=not train, hidden_size=hidden_size)
    encoded = nn.LayerNorm(name='encoder_norm')(x)

    return encoded


dtype = jnp.float32
conv_init = nn.initializers.variance_scaling(2., mode='fan_out', distribution="truncated_normal", dtype=dtype)
dense_init = nn.initializers.variance_scaling(1./3, mode='fan_out', distribution="truncated_normal", dtype=dtype)

def _make_divisible(v, divisor, min_value=None):
    if min_value is None:
        min_value = divisor
    new_v = max(min_value, int(v + divisor / 2) // divisor * divisor)
    # Make sure that round down does not go down by more than 10%.
    if new_v < 0.9 * v:
        new_v += divisor
    return new_v


class SELayer(nn.Module):
    inp: int
    oup: int
    dtype: Any
    reduction: int = 4

    @nn.compact
    def __call__(self, x):
        features = _make_divisible(self.inp // self.reduction, 4)
        y = silu(nn.Conv(features, kernel_size=(1, 1), param_dtype=self.dtype, dtype=self.dtype)(x))
        y = sigmoid(nn.Conv(self.oup, kernel_size=(1, 1), param_dtype=self.dtype, dtype=self.dtype)(y))
        return x * y


class ConvBlock(nn.Module):
    oup: int
    kernel: int
    dtype: Any
    stride: int = 1
    groups: int = 1
    dropout: float = 0.
    # Whether to do activation
    act: bool = True
    has_skip: bool = False

    @nn.compact
    def __call__(self, x):
        if self.has_skip:
            shortcut = x
        x = nn.Conv(self.oup, kernel_size=(self.kernel, self.kernel),
                    strides=self.stride, feature_group_count=self.groups,
                    use_bias=False, param_dtype=self.dtype, dtype=self.dtype, kernel_init=conv_init)(x)
        mutable = self.is_mutable_collection('batch_stats')
        if self.dropout != 0.:
            x = nn.Dropout(rate=self.dropout, deterministic=not mutable)(x)
        x = nn.BatchNorm(momentum=.9, use_running_average=not mutable, param_dtype=self.dtype, dtype=self.dtype, axis_name='devices')(x)
        if self.act:
            x = silu(x)
        if self.has_skip:
            return x + shortcut
        else:
            return x


class DropBlock(nn.Module):
    dropblock_rate: float = .0

    @nn.compact
    def __call__(self, x):
        mutable = self.is_mutable_collection('batch_stats')
        if mutable:
            pred = jax.random.bernoulli(self.make_rng('dropout'), p=self.dropblock_rate)
            return jax.lax.cond(pred, lambda x: 0., lambda x: 1., 0) * x
        else:
            return (1 - self.dropblock_rate) * x


class MBConv(nn.Module):
    inp: int
    oup: int
    stride: int
    expand_ratio: float
    use_se: bool
    dtype: Any
    dropout: float = 0.
    dropblock: float = 0.

    def setup(self):
        assert self.stride in [1, 2]

        hidden_dim = round(self.inp * self.expand_ratio)
        self.identity = self.stride == 1 and self.inp == self.oup
        if self.dropblock != 0:
            self.DropBlock = DropBlock(dropblock_rate=self.dropblock)
        if self.use_se:
            self.conv = nn.Sequential([
                ConvBlock(oup=hidden_dim, kernel=1, stride=1, dtype=self.dtype, dropout=self.dropout),
                ConvBlock(oup=hidden_dim, kernel=3, stride=self.stride, groups=hidden_dim, dtype=self.dtype, dropout=self.dropout),
                SELayer(inp=self.inp, oup=hidden_dim, dtype=self.dtype),
                nn.Conv(self.oup, kernel_size=(1, 1), use_bias=False, param_dtype=self.dtype, dtype=self.dtype, kernel_init=conv_init),
            ])
        else:
            self.conv = nn.Sequential([
                ConvBlock(oup=hidden_dim, kernel=3, stride=self.stride, dtype=self.dtype, dropout=self.dropout),
                nn.Conv(self.oup, kernel_size=(1, 1), use_bias=False, param_dtype=self.dtype, dtype=self.dtype, kernel_init=conv_init),
            ])
        self.bn = nn.BatchNorm(momentum=.9, param_dtype=self.dtype, dtype=self.dtype, axis_name='devices')

    # Well this .remat almost is not doing anything
    @nn.remat
    def __call__(self, x):
        mutable = self.is_mutable_collection('batch_stats')
        if self.identity:
            if self.dropblock != 0:
                return x + self.DropBlock(self.bn(self.conv(x), use_running_average=not mutable))
            else:
                return x + self.bn(self.conv(x), use_running_average=not mutable)
        else:
            return self.bn(self.conv(x), use_running_average=not mutable)

class VisionTransformer(nn.Module):
  """VisionTransformer."""

  num_classes: int
  patches: Any
  transformer: Any
  hidden_size: int
  resnet: Optional[Any] = None
  representation_size: Optional[int] = None
  classifier: str = 'token'
  head_bias_init: float = 0.
  encoder: Type[nn.Module] = Encoder
  model_name: Optional[str] = None

  @nn.compact
  def __call__(self, inputs, *, train):

    x = inputs

    # (Possibly partial) ResNet root.
    if self.resnet is not None:
      width = int(64 * self.resnet.width_factor)

      # Root block.
      x = models_resnet.StdConv(
          features=width,
          kernel_size=(7, 7),
          strides=(2, 2),
          use_bias=False,
          name='conv_root')(
              x)
      x = nn.GroupNorm(name='gn_root')(x)
      x = nn.relu(x)
      x = nn.max_pool(x, window_shape=(3, 3), strides=(2, 2), padding='SAME')

      # ResNet stages.
      if self.resnet.num_layers:
        x = models_resnet.ResNetStage(
            block_size=self.resnet.num_layers[0],
            nout=width,
            first_stride=(1, 1),
            name='block1')(
                x)
        for i, block_size in enumerate(self.resnet.num_layers[1:], 1):
          x = models_resnet.ResNetStage(
              block_size=block_size,
              nout=width * 2**i,
              first_stride=(2, 2),
              name=f'block{i + 1}')(
                  x)

    n, h, w, c = x.shape

    # We can merge s2d+emb into a single conv; it's the same.
    x = nn.Conv(
        features=self.hidden_size,
        kernel_size=self.patches.size,
        strides=self.patches.size,
        padding='VALID',
        name='embedding')(
            x)

    # Here, x is a grid of embeddings.

    # (Possibly partial) Transformer.
    if self.transformer is not None:
      n, h, w, c = x.shape
      x = jnp.reshape(x, [n, h * w, c])

      # If we want to add a class token, add it here.
      if self.classifier in ['token', 'token_unpooled']:
        cls = self.param('cls', nn.initializers.zeros, (1, 1, c))
        cls = jnp.tile(cls, [n, 1, 1])
        x = jnp.concatenate([cls, x], axis=1)

      x = self.encoder(name='Transformer', **self.transformer)(x, train=train, hidden_size=self.hidden_size)

    if self.classifier == 'token':
      x = x[:, 0]
    elif self.classifier == 'gap':
      x = jnp.mean(x, axis=list(range(1, x.ndim - 1)))  # (1,) or (1,2)
    elif self.classifier in ['unpooled', 'token_unpooled']:
      pass
    else:
      raise ValueError(f'Invalid classifier={self.classifier}')

    if self.representation_size is not None:
      x = nn.Dense(features=self.representation_size, name='pre_logits')(x)
      x = nn.tanh(x)
    else:
      x = IdentityLayer(name='pre_logits')(x)

    if self.num_classes:
      x = nn.Dense(
          features=self.num_classes,
          name='head',
          kernel_init=nn.initializers.zeros,
          bias_init=nn.initializers.constant(self.head_bias_init))(x)
    return x
