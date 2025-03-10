# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
# Copyright 2021 The Fairseq Authors and The HuggingFace Inc. team.
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

import numpy as np
import paddle
import paddle.nn as nn
import paddle.nn.functional as F
from paddle.nn import Embedding, Layer

from ...utils.log import logger
from .. import PretrainedModel, register_base_model
from ..model_outputs import (
    ModelOutput,
    Seq2SeqLMOutput,
    Seq2SeqModelOutput,
    Seq2SeqQuestionAnsweringModelOutput,
    Seq2SeqSequenceClassifierOutput,
    convert_encoder_output,
)

__all__ = [
    "BartModel",
    "BartPretrainedModel",
    "BartEncoder",
    "BartDecoder",
    "BartClassificationHead",
    "BartForSequenceClassification",
    "BartForQuestionAnswering",
    "BartForConditionalGeneration",
]


def shift_tokens_right(input_ids, decoder_start_token_id):
    """
    Shift input ids one token to the right.
    """
    shifted_input_ids = paddle.zeros_like(input_ids)
    shifted_input_ids[:, 1:] = input_ids[:, :-1].clone()
    shifted_input_ids[:, 0] = decoder_start_token_id

    return shifted_input_ids


class BartPretrainedModel(PretrainedModel):
    """
    An abstract class for pretrained Bart models. It provides Bart related
    `model_config_file`, `pretrained_init_configuration`, `resource_files_names`,
    `pretrained_resource_files_map`, `base_model_prefix` for downloading and
    loading pretrained models.
    See :class:`~paddlenlp.transformers.model_utils.PretrainedModel` for more details.
    """

    pretrained_init_configuration = {
        "bart-base": {
            "vocab_size": 50265,
            "bos_token_id": 0,
            "pad_token_id": 1,
            "eos_token_id": 2,
            "forced_eos_token_id": 2,
            "decoder_start_token_id": 2,
            "d_model": 768,
            "num_encoder_layers": 6,
            "num_decoder_layers": 6,
            "encoder_attention_heads": 12,
            "decoder_attention_heads": 12,
            "encoder_ffn_dim": 3072,
            "decoder_ffn_dim": 3072,
            "dropout": 0.1,
            "activation_function": "gelu",
            "attention_dropout": 0.1,
            "activation_dropout": 0.1,
            "max_position_embeddings": 1024,
            "init_std": 0.02,
        },
        "bart-large": {
            "vocab_size": 50265,
            "bos_token_id": 0,
            "pad_token_id": 1,
            "eos_token_id": 2,
            "forced_eos_token_id": 2,
            "decoder_start_token_id": 2,
            "d_model": 1024,
            "num_encoder_layers": 12,
            "num_decoder_layers": 12,
            "encoder_attention_heads": 16,
            "decoder_attention_heads": 16,
            "encoder_ffn_dim": 4096,
            "decoder_ffn_dim": 4096,
            "dropout": 0.1,
            "activation_function": "gelu",
            "attention_dropout": 0.1,
            "activation_dropout": 0.1,
            "max_position_embeddings": 1024,
            "init_std": 0.02,
        },
    }
    pretrained_resource_files_map = {
        "model_state": {
            "bart-base": "https://bj.bcebos.com/paddlenlp/models/transformers/bart/bart-base.pdparams",
            "bart-large": "https://bj.bcebos.com/paddlenlp/models/transformers/bart/bart-large.pdparams",
        }
    }
    base_model_prefix = "bart"

    def init_weights(self, layer):
        """Initialization hook"""
        if isinstance(layer, (nn.Linear, nn.Embedding)):
            # In the dygraph mode, use the `set_value` to reset the parameter directly,
            # and reset the `state_dict` to update parameter in static mode.
            if isinstance(layer.weight, paddle.Tensor):
                layer.weight.set_value(
                    paddle.tensor.normal(
                        mean=0.0,
                        std=self.init_std if hasattr(self, "init_std") else self.bart.config["init_std"],
                        shape=layer.weight.shape,
                    )
                )


class BartLearnedPositionalEmbedding(Embedding):
    """
    This module learns positional embeddings up to a fixed maximum size.
    """

    def __init__(self, num_embeddings, embedding_dim):
        # Bart is set up so that if padding_idx is specified then offset the embedding ids by 2
        # and adjust num_embeddings appropriately. Other models dont have this hack
        self.offset = 2
        super().__init__(num_embeddings + self.offset, embedding_dim)

    def forward(self, input_ids_shape, past_key_values_length=0):
        """`input_ids_shape` is expected to be [bsz x seqlen]."""
        bsz, seq_len = input_ids_shape[:2]
        positions = paddle.arange(past_key_values_length, past_key_values_length + seq_len, dtype="int64")
        # (gongenlei) For dygraph to static graph
        return Embedding.forward(self, positions + self.offset)


class BartEncoder(BartPretrainedModel):
    """
    The Transformer Encoder of BartModel. The arguments of BartEncoder can see :class:`BartModel`.
    """

    def __init__(
        self,
        embed_tokens,
        vocab_size,
        pad_token_id=1,
        d_model=768,
        num_encoder_layers=6,
        encoder_attention_heads=12,
        encoder_ffn_dim=3072,
        dropout=0.1,
        activation_function="gelu",
        attention_dropout=0.1,
        activation_dropout=0.1,
        max_position_embeddings=1024,
        init_std=0.02,
    ):
        super().__init__()
        self.init_std = init_std
        self.pad_token_id = pad_token_id
        if embed_tokens is not None:
            self.embed_tokens = embed_tokens
        else:
            self.embed_tokens = nn.Embedding(vocab_size, d_model)

        self.encoder_embed_positions = BartLearnedPositionalEmbedding(max_position_embeddings, d_model)

        self.encoder_dropout = nn.Dropout(dropout)
        self.encoder_layernorm_embedding = nn.LayerNorm(d_model)
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=d_model,
            nhead=encoder_attention_heads,
            dim_feedforward=encoder_ffn_dim,
            dropout=dropout,
            activation=activation_function,
            attn_dropout=attention_dropout,
            act_dropout=activation_dropout,
        )
        self.encoder = nn.TransformerEncoder(encoder_layer, num_encoder_layers)
        self.apply(self.init_weights)

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        inputs_embeds=None,
        output_attentions=False,
        output_hidden_states=False,
        return_dict=False,
        **kwargs
    ):
        """
        The BartEncoder forward method, overrides the `__call__()` special method.

        Args:
            input_ids (Tensor, optional):
                See :class:`BartModel`.
            attention_mask (Tensor, optional):
                See :class:`BartModel`.
            inputs_embeds (Tensor, optional):
                See :class:`BartModel`.
            output_attentions (bool, optional):
                See :class:`BartModel`.
            output_hidden_states (bool, optional):
                See :class:`BartModel`.
            return_dict (bool, optional):
                See :class:`BartModel`.

        Returns:
            An instance of :class:`~paddlenlp.transformers.model_outputs.BaseModelOutputWithPastAndCrossAttentions` if
            `return_dict=True`. Otherwise it returns a tuple of tensors corresponding
            to ordered and not None (depending on the input arguments) fields of
            :class:`~paddlenlp.transformers.model_outputs.BaseModelOutputWithPastAndCrossAttentions`.
            Especially, When `return_dict=output_hidden_states=output_attentions=False`,
            returns tensor `encoder_outputs` which is the output at the last layer of the model.
            Its data type should be float32 and has a shape of [batch_size, sequence_length, hidden_size].

        """
        if input_ids is None and inputs_embeds is None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            inputs_shape = paddle.shape(input_ids)
            input_ids = input_ids.reshape((-1, inputs_shape[-1]))
        elif inputs_embeds is not None:
            inputs_shape = paddle.shape(inputs_embeds)[:-1]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids)

        inputs_embed_pos = self.encoder_embed_positions(inputs_shape)
        hidden_states = inputs_embeds + inputs_embed_pos
        hidden_states = self.encoder_layernorm_embedding(hidden_states)
        encoder_input = self.encoder_dropout(hidden_states)

        if attention_mask is None and input_ids is not None:
            attention_mask = (
                paddle.cast(input_ids == self.pad_token_id, dtype=paddle.get_default_dtype()).unsqueeze([1, 2]) * -1e4
            )
        # For 2D attention_mask from tokenizer
        elif attention_mask.ndim == 2:
            attention_mask = paddle.unsqueeze(attention_mask, axis=[1, 2]).astype(paddle.get_default_dtype())
            attention_mask = (1.0 - attention_mask) * -1e4
        attention_mask.stop_gradient = True

        encoder_output = self.encoder(
            encoder_input,
            src_mask=attention_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        return encoder_output


class BartDecoder(BartPretrainedModel):
    """
    The Transformer Decoder of BartModel. The arguments of BartDecoder can see :class:`BartModel`.
    """

    def __init__(
        self,
        embed_tokens,
        vocab_size,
        pad_token_id=1,
        d_model=768,
        num_decoder_layers=6,
        decoder_attention_heads=12,
        decoder_ffn_dim=3072,
        dropout=0.1,
        activation_function="gelu",
        attention_dropout=0.1,
        activation_dropout=0.1,
        max_position_embeddings=1024,
        init_std=0.02,
    ):
        super().__init__()
        self.init_std = init_std
        if embed_tokens is not None:
            self.embed_tokens = embed_tokens
        else:
            self.embed_tokens = nn.Embedding(vocab_size, d_model)

        self.decoder_embed_positions = BartLearnedPositionalEmbedding(max_position_embeddings, d_model)
        self.decoder_dropout = nn.Dropout(dropout)
        self.decoder_layernorm_embedding = nn.LayerNorm(d_model)

        decoder_layer = nn.TransformerDecoderLayer(
            d_model=d_model,
            nhead=decoder_attention_heads,
            dim_feedforward=decoder_ffn_dim,
            dropout=dropout,
            activation=activation_function,
            attn_dropout=attention_dropout,
            act_dropout=activation_dropout,
        )
        self.decoder = nn.TransformerDecoder(decoder_layer, num_decoder_layers)
        self.apply(self.init_weights)

    def forward(
        self,
        decoder_input_ids=None,
        decoder_attention_mask=None,
        encoder_output=None,
        memory_mask=None,
        decoder_inputs_embeds=None,
        cache=None,
        output_attentions=False,
        output_hidden_states=False,
        return_dict=False,
    ):
        """
        The BartDecoder forward method, overrides the `__call__()` special method.

        Args:
            decoder_input_ids (Tensor, optional):
                See :class:`BartModel`.
            decoder_attention_mask (Tensor, optional):
                See :class:`BartModel`.
            encoder_output (Tensor, optional):
                See :class:`BartModel`.
            memory_mask (Tensor, optional):
                See :class:`BartModel`.
            decoder_inputs_embeds (Tensor, optional):
                See :class:`BartModel`.
            cache (Tensor, optional):
                See :class:`BartModel`.
            output_attentions (bool, optional):
                See :class:`BartModel`.
            output_hidden_states (bool, optional):
                See :class:`BartModel`.
            return_dict (bool, optional):
                See :class:`BartModel`.

        Returns:
            An instance of :class:`~paddlenlp.transformers.model_outputs.BaseModelOutputWithPastAndCrossAttentions` if
            `return_dict=True`. Otherwise it returns a tuple of tensors corresponding
            to ordered and not None (depending on the input arguments) fields of
            :class:`~paddlenlp.transformers.model_outputs.BaseModelOutputWithPastAndCrossAttentions`.
            Especially, When `return_dict=output_hidden_states=output_attentions=False`,
            returns tensor `decoder_outputs` which is the output at the last layer of the model.
            Its data type should be float32 and has a shape of [batch_size, sequence_length, hidden_size].

        """
        # retrieve input_ids and inputs_embeds
        if decoder_input_ids is not None and decoder_inputs_embeds is not None:
            raise ValueError("You cannot specify both decoder_input_ids and decoder_inputs_embeds at the same time")
        elif decoder_input_ids is not None:
            inputs_shape = paddle.shape(decoder_input_ids)
            decoder_input_ids = decoder_input_ids.reshape((-1, inputs_shape[-1]))
        elif decoder_inputs_embeds is not None:
            inputs_shape = paddle.shape(decoder_inputs_embeds)[:-1]
        else:
            raise ValueError("You have to specify either decoder_input_ids or decoder_inputs_embeds")

        if decoder_attention_mask is None:
            decoder_length = inputs_shape[-1]
            decoder_attention_mask = paddle.tensor.triu(
                (paddle.full((decoder_length, decoder_length), -np.inf, dtype=paddle.get_default_dtype())), 1
            )

        if decoder_inputs_embeds is None:
            decoder_inputs_embeds = self.embed_tokens(decoder_input_ids)

        past_key_values_length = paddle.shape(cache[0][0].k)[2] if cache is not None else 0
        decoder_inputs_embed_pos = self.decoder_embed_positions(inputs_shape, past_key_values_length)
        hidden_states = decoder_inputs_embeds + decoder_inputs_embed_pos
        hidden_states = self.decoder_layernorm_embedding(hidden_states)
        decoder_input = self.decoder_dropout(hidden_states)

        decoder_output = self.decoder(
            tgt=decoder_input,
            memory=encoder_output if isinstance(encoder_output, type(decoder_input)) else encoder_output[0],
            tgt_mask=decoder_attention_mask,
            memory_mask=memory_mask,
            cache=cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        return decoder_output


@register_base_model
class BartModel(BartPretrainedModel):
    r"""
    The bare Bart Model transformer outputting raw hidden-states.

    This model inherits from :class:`~paddlenlp.transformers.model_utils.PretrainedModel`.
    Refer to the superclass documentation for the generic methods.

    This model is also a Paddle `paddle.nn.Layer <https://www.paddlepaddle.org.cn/documentation
    /docs/en/api/paddle/fluid/dygraph/layers/Layer_en.html>`__ subclass. Use it as a regular Paddle Layer
    and refer to the Paddle documentation for all matter related to general usage and behavior.

    Args:
        vocab_size (int):
            Vocabulary size of `inputs_ids` in `BartModel`. Also is the vocab size of token embedding matrix.
            Defines the number of different tokens that can be represented by the `inputs_ids` passed when calling `BartModel`.
        bos_token (int, optional):
            The beginning of sequence token that was used during pretraining. Can be
            used a sequence classifier token.
            Defaults to `0`.
        pad_token_id(int, optional):
            The index of padding token in the token vocabulary.
            Defaults to `1`.
        eos_token (int, optional):
            A special token representing the end of a sequence that was used during pretraining.
            Defaults to `2`.
        d_model (int, optional):
            Dimensionality of the embedding layer, encoder layer and decoder layer. Defaults to `768`.
        num_encoder_layers (int, optional):
            Number of hidden layers in the Transformer encoder. Defaults to `6`.
        num_decoder_layers (int, optional):
            Number of hidden layers in the Transformer decoder. Defaults to `6`.
        encoder_attention_heads (int, optional):
            Number of attention heads for each attention layer in the Transformer encoder.
            Defaults to `12`.
        decoder_attention_heads (int, optional):
            Number of attention heads for each attention layer in the Transformer decoder.
            Defaults to `12`.
        encoder_ffn_dim (int, optional):
            Dimensionality of the feed-forward (ff) layer in the encoder. Input tensors
            to ff layers are firstly projected from `d_model` to `encoder_ffn_dim`,
            and then projected back to `d_model`. Typically `encoder_ffn_dim` is larger than `d_model`.
            Defaults to `3072`.
        decoder_ffn_dim (int, optional):
            Dimensionality of the feed-forward (ff) layer in the encoder. Input tensors
            to ff layers are firstly projected from `d_model` to `decoder_ffn_dim`,
            and then projected back to `d_model`. Typically `decoder_ffn_dim` is larger than `d_model`.
            Defaults to `3072`.
        dropout (float, optional):
            The dropout probability used in all fully connected layers (pre-process and post-process of MHA and FFN sub-layer)
            in the encoders and decoders. Defaults to `0.1`.
        activation_function (str, optional):
            The non-linear activation function in the feed-forward layer.
            ``"gelu"``, ``"relu"`` and any other paddle supported activation functions are supported.
            Defaults to `"gelu"`.
        attention_dropout (float, optional):
            The dropout probability used in MultiHeadAttention in all encoder layers and decoder layers to drop some attention target.
            Defaults to `0.1`.
        activation_dropout (float, optional):
            The dropout probability used after FFN activation in all encoder layers and decoder layers.
            Defaults to `0.1`.
        max_position_embeddings (int, optional):
            The maximum value of the dimensionality of position encoding, which dictates the maximum supported length of an input
            sequence. Defaults to `1024`.
        init_std (float, optional):
            The standard deviation of the truncated_normal_initializer for initializing all weight matrices.
            Default to `0.02`.

    """

    def __init__(
        self,
        vocab_size,
        bos_token_id=0,
        pad_token_id=1,
        eos_token_id=2,
        forced_eos_token_id=2,
        decoder_start_token_id=2,
        d_model=768,
        num_encoder_layers=6,
        num_decoder_layers=6,
        encoder_attention_heads=12,
        decoder_attention_heads=12,
        encoder_ffn_dim=3072,
        decoder_ffn_dim=3072,
        dropout=0.1,
        activation_function="gelu",
        attention_dropout=0.1,
        activation_dropout=0.1,
        max_position_embeddings=1024,
        init_std=0.02,
    ):
        super().__init__()
        self.init_std = init_std
        self.pad_token_id = pad_token_id
        self.decoder_start_token_id = decoder_start_token_id
        self.shared = nn.Embedding(vocab_size, d_model)
        self.encoder = BartEncoder(
            self.shared,
            vocab_size,
            pad_token_id,
            d_model,
            num_encoder_layers,
            encoder_attention_heads,
            encoder_ffn_dim,
            dropout,
            activation_function,
            attention_dropout,
            activation_dropout,
            max_position_embeddings,
            init_std,
        )

        self.decoder = BartDecoder(
            self.shared,
            vocab_size,
            pad_token_id,
            d_model,
            num_decoder_layers,
            decoder_attention_heads,
            decoder_ffn_dim,
            dropout,
            activation_function,
            attention_dropout,
            activation_dropout,
            max_position_embeddings,
            init_std,
        )
        self.apply(self.init_weights)

    def get_encoder(self):
        return self.encoder

    def get_decoder(self):
        return self.decoder

    def get_input_embeddings(self):
        return self.shared

    def set_input_embeddings(self, value):
        self.shared = value

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        decoder_input_ids=None,
        decoder_attention_mask=None,
        encoder_output=None,
        inputs_embeds=None,
        decoder_inputs_embeds=None,
        use_cache=False,
        cache=None,
        output_attentions=False,
        output_hidden_states=False,
        return_dict=False,
    ):
        r"""
        The BartModel forward method, overrides the `__call__()` special method.

        Args:
            input_ids (Tensor, optional):
                Indices of input sequence tokens in the vocabulary. They are
                numerical representations of tokens that build the input sequence.
                Its data type should be `int64` and it has a shape of [batch_size, sequence_length].
            attention_mask (Tensor, optional):
                Mask used in multi-head attention to avoid performing attention to some unwanted positions,
                usually the paddings or the subsequent positions.
                Its data type can be int, float and bool.
                When the data type is bool, the `masked` tokens have `False` values and the others have `True` values.
                When the data type is int, the `masked` tokens have `0` values and the others have `1` values.
                When the data type is float, the `masked` tokens have `-INF` values and the others have `0` values.
                It is a tensor with shape broadcasted to `[batch_size, num_attention_heads, sequence_length, sequence_length]`.
                For example, its shape can be  [batch_size, sequence_length], [batch_size, sequence_length, sequence_length],
                [batch_size, num_attention_heads, sequence_length, sequence_length].
                Defaults to `None`, which means nothing needed to be prevented attention to.
            decoder_input_ids (Tensor, optional):
                Indices of decoder input sequence tokens in the vocabulary.
                Its data type should be `int64` and it has a shape of [batch_size, sequence_length].
                Defaults to `None`, which means no `decoder_input_ids` is provided, the model will create the tensor
                by shifting the `input_ids` to the right.
            decoder_attention_mask (Tensor, optional):
                Mask used in multi-head attention to avoid performing attention to some unwanted positions in `decoder_input_ids`.
                Its data type and shape is the same as `attention_mask`. Defaults to `None`.
            encoder_output (tuple, optional):
                The output of the encoder, a tuple consists `last_hidden_state`, `hidden_states`(optional), `attentions`(optional).
                The data type of `last_hidden_state` is float32 and its shape is `[batch_size, sequence_length, hidden_size]`.
                `hidden_states` is hidden_states of all layers in the Transformer encoder. The length of `hidden_states` is `num_hidden_layers + 1`.
                For all element in the tuple, its data type should be float32 and its shape is [`batch_size, sequence_length, hidden_size`].
                `attentions` is attentions of all layers of in the Transformer encoder. The length of `attentions` is `num_hidden_layers`.
                For all element in the tuple, its data type should be float32 and its shape is [`batch_size, num_attention_heads, sequence_length, sequence_length`].
            inputs_embeds (Tensor, optional):
                Optionally, instead of passing `input_ids` you can choose to directly pass an embedded representation
                of shape `(batch_size, sequence_length, hidden_size)`. This is useful if you want more control over
                how to convert `input_ids` indices into associated vectors than the model's internal embedding lookup matrix.
                Default to None.
            decoder_inputs_embeds (Tensor, optional):
                Optionally, instead of passing `decoder_input_ids` you can choose to directly pass an embedded
                representation  of shape `(batch_size, target_sequence_length, hidden_size)`. If `cache` is used,
                optionally only the last `decoder_inputs_embeds` have to be input (see `past_key_values`).
                This is useful if you want more control over how to convert `decoder_input_ids` indices
                into associated vectors than the model's internal embedding lookup matrix. Default to None.
                If `decoder_input_ids` and `decoder_inputs_embeds` are both unset, `decoder_inputs_embeds` takes the value
                of `inputs_embeds`.
            use_cache (bool, optional):
                 Whether or not to use cache. Defaults to `False`. If set to `True`, key value states will be returned and
                 can be used to speed up decoding.
            cache (list, optional):
                It is a list, and each element in the list is a tuple `(incremental_cache, static_cache)`.
                See `TransformerDecoder.gen_cache <https://github.com/PaddlePaddle/Paddle/blob/release/2.1/python/paddle/nn/layer/transformer.py#L1060>`__ for more details.
                It is only used for inference and should be None for training.
                Default to `None`.
            output_attentions (bool, optional):
                Whether or not to return the attentions tensors of all attention layers. See `attentions` under returned
                tensors for more detail. Defaults to `False`.
            output_hidden_states (bool, optional):
                Whether or not to return the hidden states of all layers. See `hidden_states` under returned tensors for
                more detail. Defaults to `False`.
            return_dict (bool, optional):
                Whether to return a :class:`~paddlenlp.transformers.model_outputs.BaseModelOutputWithPastAndCrossAttentions` object. If `False`, the output
                will be a tuple of tensors. Defaults to `False`.
        Returns:
            An instance of :class:`~paddlenlp.transformers.model_outputs.BaseModelOutputWithPastAndCrossAttentions` if
            `return_dict=True`. Otherwise it returns a tuple of tensors corresponding
            to ordered and not None (depending on the input arguments) fields of
            :class:`~paddlenlp.transformers.model_outputs.BaseModelOutputWithPastAndCrossAttentions`.
            Especially, When `return_dict=output_hidden_states=output_attentions=False`,
            returns tensor `decoder_output`, which is the output at the last layer of the model.
            Its data type should be float32 and has a shape of [batch_size, sequence_length, hidden_size].

        Example:
            .. code-block::

                import paddle
                from paddlenlp.transformers import BartModel, BartTokenizer

                tokenizer = BartTokenizer.from_pretrained('bart-base')
                model = BartModel.from_pretrained('bart-base')

                inputs = tokenizer("Welcome to use PaddlePaddle and PaddleNLP!")
                inputs = {k:paddle.to_tensor([v]) for (k, v) in inputs.items()}
                output = model(**inputs)
        """
        # different to other models, Bart automatically creates decoder_input_ids from
        # inputBartForSequenceClassification_ids if no decoder_input_ids are provided
        if input_ids is None and inputs_embeds is None and encoder_output is None:
            raise ValueError("You have to specify either input_ids or encoder_output")

        if decoder_input_ids is None and decoder_inputs_embeds is None:
            if input_ids is None:
                raise ValueError(
                    "If no `decoder_input_ids` or `decoder_inputs_embeds` are "
                    "passed, `input_ids` cannot be `None`. Please pass either "
                    "`input_ids` or `decoder_input_ids` or `decoder_inputs_embeds`."
                )
            decoder_input_ids = shift_tokens_right(input_ids, self.decoder_start_token_id)
        if attention_mask is None and input_ids is not None:
            # only generate attention_mask when input_ids is specified
            attention_mask = (
                paddle.cast(input_ids == self.pad_token_id, dtype=paddle.get_default_dtype()).unsqueeze([1, 2]) * -1e4
            )
        if inputs_embeds is not None and input_ids is None and attention_mask is None:
            logger.warning("provided inputs_embeds without attention_mask")
        # For 2D attention_mask from tokenizer
        elif attention_mask.ndim == 2:
            attention_mask = paddle.unsqueeze(attention_mask, axis=[1, 2]).astype(paddle.get_default_dtype())
            attention_mask = (1.0 - attention_mask) * -1e4
            attention_mask.stop_gradient = True

        input_type = type(decoder_input_ids) if decoder_input_ids is not None else type(decoder_inputs_embeds)
        if encoder_output is None:
            encoder_output = self.encoder(
                input_ids,
                attention_mask,
                inputs_embeds=inputs_embeds,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
            )
        # If the user passed a tuple for encoder_outputs, we wrap it in a BaseModelOutput when return_dict=True
        elif return_dict and not isinstance(encoder_output, ModelOutput):
            if isinstance(encoder_output, input_type):
                encoder_output = (encoder_output,)
            encoder_output = convert_encoder_output(encoder_output)
        if isinstance(encoder_output, input_type):
            encoder_last_hidden_state = encoder_output
        else:
            encoder_last_hidden_state = encoder_output[0]
        if use_cache:
            if cache is None:
                cache = self.decoder.decoder.gen_cache(encoder_last_hidden_state)
        else:
            cache = None
        decoder_output = self.decoder(
            decoder_input_ids,
            decoder_attention_mask,
            encoder_last_hidden_state,
            attention_mask,
            cache=cache,
            decoder_inputs_embeds=decoder_inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        if not return_dict:
            if isinstance(decoder_output, input_type):
                decoder_output = (decoder_output,)
            if isinstance(encoder_output, input_type):
                encoder_output = (encoder_output,)
            return decoder_output + encoder_output

        return Seq2SeqModelOutput(
            last_hidden_state=decoder_output.last_hidden_state,
            past_key_values=decoder_output.past_key_values,
            decoder_hidden_states=decoder_output.hidden_states,
            decoder_attentions=decoder_output.attentions,
            cross_attentions=decoder_output.cross_attentions,
            encoder_last_hidden_state=encoder_output.last_hidden_state,
            encoder_hidden_states=encoder_output.hidden_states,
            encoder_attentions=encoder_output.attentions,
        )


class BartClassificationHead(Layer):
    """
    Perform sentence-level classification tasks.
    """

    def __init__(self, input_dim: int, inner_dim: int, num_classes: int, pooler_dropout: float):
        super().__init__()
        self.dense = nn.Linear(input_dim, inner_dim)
        self.dropout = nn.Dropout(p=pooler_dropout)
        self.out_proj = nn.Linear(inner_dim, num_classes)

    def forward(self, hidden_states):
        """
        Args:
            hidden_states (Tensor):
                Hidden states of the classification model.
        """
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.dense(hidden_states)
        hidden_states = F.tanh(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.out_proj(hidden_states)
        return hidden_states


class BartForSequenceClassification(BartPretrainedModel):
    r"""
    Bart Model with a linear layer on top of the pooled output,
    designed for sequence classification/regression tasks like GLUE tasks.

    Args:
        bart (:class:`BartModel`):
            An instance of BartModel.
        num_labels (int, optional):
            The number of different labels. Defaults to `2`.
        dropout (float, optional):
            The dropout probability for output of Bart.
            If None, use the same value as `hidden_dropout_prob` of `BartModel`
            instance `bart`. Defaults to None.
    """

    def __init__(self, bart, num_labels=2, dropout=None):
        super().__init__()
        self.bart = bart
        self.num_labels = num_labels
        self.classifier = BartClassificationHead(
            self.bart.config["d_model"],
            self.bart.config["d_model"],
            num_labels,
            dropout if dropout else self.bart.config["dropout"],
        )
        self.apply(self.init_weights)

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        decoder_input_ids=None,
        decoder_attention_mask=None,
        encoder_output=None,
        inputs_embeds=None,
        decoder_inputs_embeds=None,
        use_cache=False,
        cache=None,
        labels=None,
        output_attentions=False,
        output_hidden_states=False,
        return_dict=False,
    ):
        r"""
        The BartForSequenceClassification forward method, overrides the __call__() special method.

        Args:
            input_ids (Tensor, optional):
                See :class:`BartModel`.
            attention_mask (Tensor, optional):
                See :class:`BartModel`.
            decoder_input_ids (Tensor, `optional`):
                See :class:`BartModel`.
            decoder_attention_mask (Tensor, optional):
                See :class:`BartModel`.
            encoder_output (Tensor, optonal):
                See :class:`BartModel`.
            inputs_embeds (Tensor, optional):
                See :class:`BartModel`.
            decoder_inputs_embeds (Tensor, optional):
                See :class:`BartModel`.
            use_cache (bool, optional):
                See :class:`BartModel`. Forcely set to `False` when `labels` is provided that can save memory during training.
            cache (Tensor, optional):
                See :class:`BartModel`.
            labels (Tensor, optional):
                Labels for computing the sequence classification/regression loss. Indices should be in `[0, ...,
                num_labels - 1]`. If `num_labels > 1` a classification loss is computed (Cross-Entropy).
                Default to `None`.
            output_attentions (bool, optional):
                See :class:`BartModel`.
            output_hidden_states (bool, optional):
                See :class:`BartModel`.
            return_dict (bool, optional):
                See :class:`BartModel`.


        Returns:
            An instance of :class:`~paddlenlp.transformers.model_outputs.Seq2SeqSequenceClassifierOutput` if
            `return_dict=True`. Otherwise it returns a tuple of tensors corresponding
            to ordered and not None (depending on the input arguments) fields of
            :class:`~paddlenlp.transformers.model_outputs.Seq2SeqSequenceClassifierOutput`.
            Especially, When `return_dict=output_hidden_states=output_attentions=False` and labels=None,
            returns tensor `logits`, a tensor of the input text classification logits.
            Shape as `[batch_size, num_labels]` and dtype as float32.

        Example:
            .. code-block::

                import paddle
                from paddlenlp.transformers import BartForSequenceClassification, BartTokenizer

                tokenizer = BartTokenizer.from_pretrained('bart-base')
                model = BartForSequenceClassification.from_pretrained('bart-base')

                inputs = tokenizer("Welcome to use PaddlePaddle and PaddleNLP!")
                inputs = {k:paddle.to_tensor([v]) for (k, v) in inputs.items()}
                logits = model(**inputs)
        """
        if labels is not None:
            logger.warning("The `use_cache` argument is changed to `False` since `labels` is provided.")
            use_cache = False

        if input_ids is None and inputs_embeds is not None:
            logger.warning(
                f"{self.__class__.__name__} will not detect eos tokens in `inputs_embeds`. Results may be "
                "unexpected if using eos tokens in conjunction with `inputs_embeds.`"
            )

        outputs = self.bart(
            input_ids,
            attention_mask,
            decoder_input_ids,
            decoder_attention_mask,
            encoder_output,
            inputs_embeds=inputs_embeds,
            decoder_inputs_embeds=decoder_inputs_embeds,
            use_cache=use_cache,
            cache=cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        output = outputs[0]
        output_shape = paddle.shape(output)

        if input_ids is not None:
            eos_mask = paddle.cast(input_ids == self.bart.config["eos_token_id"], dtype="int64")
            if len(paddle.unique(paddle.sum(eos_mask, axis=1))) > 1:
                raise ValueError("All examples must have the same number of <eos> tokens.")

            # TODO(gongenlei): support bool tensor index
            output = output.masked_select(eos_mask.unsqueeze(-1).astype("bool").tile([1, 1, output_shape[-1]]))

        sentence_representation = output.reshape([output_shape[0], -1, output_shape[-1]])[:, -1, :]
        logits = self.classifier(sentence_representation)

        loss = None
        if labels is not None:
            if self.num_labels == 1:
                loss_fct = nn.MSELoss()
                loss = loss_fct(logits, labels)
            elif labels.dtype == paddle.int64 or labels.dtype == paddle.int32:
                loss_fct = nn.CrossEntropyLoss()
                loss = loss_fct(logits.reshape((-1, self.num_labels)), labels.reshape((-1,)))
            else:
                loss_fct = nn.BCEWithLogitsLoss()
                loss = loss_fct(logits, labels)

        if not return_dict:
            if len(outputs) == 2:
                return (loss, logits) if loss is not None else logits
            output = (logits,) + outputs[1:]
            return ((loss,) + output) if loss is not None else output

        return Seq2SeqSequenceClassifierOutput(
            loss=loss,
            logits=logits,
            past_key_values=outputs.past_key_values,
            decoder_hidden_states=outputs.decoder_hidden_states,
            decoder_attentions=outputs.decoder_attentions,
            cross_attentions=outputs.cross_attentions,
            encoder_last_hidden_state=outputs.encoder_last_hidden_state,
            encoder_hidden_states=outputs.encoder_hidden_states,
            encoder_attentions=outputs.encoder_attentions,
        )


class BartForQuestionAnswering(BartPretrainedModel):
    r"""
    Bart Model with a linear layer on top of the hidden-states output to
    compute `span_start_logits` and `span_end_logits`, designed for question-answering tasks like SQuAD.

    Args:
        bart (:class:`BartModel`):
            An instance of BartModel.
    """

    def __init__(self, bart):
        super().__init__()
        self.bart = bart
        self.classifier = nn.Linear(self.bart.config["d_model"], 2)
        self.apply(self.init_weights)

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        decoder_input_ids=None,
        decoder_attention_mask=None,
        encoder_output=None,
        inputs_embeds=None,
        decoder_inputs_embeds=None,
        use_cache=False,
        cache=None,
        start_positions=None,
        end_positions=None,
        output_attentions=False,
        output_hidden_states=False,
        return_dict=False,
    ):
        r"""
        The BartForQuestionAnswering forward method, overrides the __call__() special method.

        Args:
            input_ids (Tensor, optional):
                See :class:`BartModel`.
            attention_mask (Tensor, optional):
                See :class:`BartModel`.
            decoder_input_ids (Tensor, `optional`):
                See :class:`BartModel`.
            decoder_attention_mask (Tensor, optional):
                See :class:`BartModel`.
            encoder_output (Tensor, optonal):
                See :class:`BartModel`.
            inputs_embeds (Tensor, optional):
                See :class:`BartModel`.
            decoder_inputs_embeds (Tensor, optional):
                See :class:`BartModel`.
            use_cache (bool, optional):
                See :class:`BartModel`. Forcely set to `False` when `start_positions` and `end_positions` are provided that can save memory during training.
            cache (Tensor, optional):
                See :class:`BartModel`.
            start_positions (Tensor, optional):
                Labels for position (index) of the start of the labelled span for computing the token classification loss.
                Positions are clamped to the length of the sequence (sequence_length). Position outside of the sequence
                are not taken into account for computing the loss.
                A tensor of shape `(batch_size, )`. Default to `None`.
            end_positions (Tensor, optional):
                Labels for position (index) of the end of the labelled span for computing the token classification loss.
                Positions are clamped to the length of the sequence (sequence_length). Position outside of the sequence
                are not taken into account for computing the loss.
                A tensor of shape `(batch_size, )`. Default to `None`.
            output_attentions (bool, optional):
                See :class:`BartModel`.
            output_hidden_states (bool, optional):
                See :class:`BartModel`.
            return_dict (bool, optional):
                See :class:`BartModel`.

        Returns:
            An instance of :class:`~paddlenlp.transformers.model_outputs.Seq2SeqQuestionAnsweringModelOutput` if
            `return_dict=True`. Otherwise it returns a tuple of tensors corresponding
            to ordered and not None (depending on the input arguments) fields of
            :class:`~paddlenlp.transformers.model_outputs.Seq2SeqQuestionAnsweringModelOutput`.
            Especially, When `return_dict=output_hidden_states=output_attentions=False` and `start_positions=end_positions=None`,
            returns tuple (`start_logits`, `end_logits`).

            With the fields:

            - `start_logits` (Tensor):
                A tensor of the input token classification logits, indicates the start position of the labelled span.
                Its data type should be float32 and its shape is [batch_size, sequence_length].

            - `end_logits` (Tensor):
                A tensor of the input token classification logits, indicates the end position of the labelled span.
                Its data type should be float32 and its shape is [batch_size, sequence_length].

        Example:
            .. code-block::

                import paddle
                from paddlenlp.transformers import BartForQuestionAnswering, BartTokenizer

                tokenizer = BartTokenizer.from_pretrained('bart-base')
                model = BartForQuestionAnswering.from_pretrained('bart-base')

                inputs = tokenizer("Welcome to use PaddlePaddle and PaddleNLP!")
                inputs = {k:paddle.to_tensor([v]) for (k, v) in inputs.items()}
                outputs = model(**inputs)
                start_logits = outputs[0]
                end_logits  =outputs[1]
        """
        if start_positions is not None and end_positions is not None:
            logger.warning(
                "The `use_cache` argument is changed to `False` since `start_positions` and `end_positions` are provided."
            )
            use_cache = False

        outputs = self.bart(
            input_ids,
            attention_mask,
            decoder_input_ids,
            decoder_attention_mask,
            encoder_output,
            inputs_embeds=inputs_embeds,
            decoder_inputs_embeds=decoder_inputs_embeds,
            use_cache=use_cache,
            cache=cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        logits = self.classifier(outputs[0])
        logits = paddle.transpose(logits, perm=[2, 0, 1])
        start_logits, end_logits = paddle.unstack(x=logits, axis=0)

        total_loss = None
        if start_positions is not None and end_positions is not None:
            # If we are on multi-GPU, split add a dimension
            if start_positions.ndim > 1:
                start_positions = start_positions.squeeze(-1)
            if start_positions.ndim > 1:
                end_positions = end_positions.squeeze(-1)
            # sometimes the start/end positions are outside our model inputs, we ignore these terms
            ignored_index = paddle.shape(start_logits)[1]
            start_positions = start_positions.clip(0, ignored_index)
            end_positions = end_positions.clip(0, ignored_index)

            loss_fct = nn.CrossEntropyLoss(ignore_index=ignored_index)
            start_loss = loss_fct(start_logits, start_positions)
            end_loss = loss_fct(end_logits, end_positions)
            total_loss = (start_loss + end_loss) / 2

        if not return_dict:
            outputs = (start_logits, end_logits) + (outputs[1:] if len(outputs) > 2 else ())
            return ((total_loss,) + outputs) if total_loss else outputs

        return Seq2SeqQuestionAnsweringModelOutput(
            loss=total_loss,
            start_logits=start_logits,
            end_logits=end_logits,
            past_key_values=outputs.past_key_values,
            decoder_hidden_states=outputs.decoder_hidden_states,
            decoder_attentions=outputs.decoder_attentions,
            cross_attentions=outputs.cross_attentions,
            encoder_last_hidden_state=outputs.encoder_last_hidden_state,
            encoder_hidden_states=outputs.encoder_hidden_states,
            encoder_attentions=outputs.encoder_attentions,
        )


class BartForConditionalGeneration(BartPretrainedModel):
    r"""
    Bart Model with a `language modeling` head on top.

    Args:
        bart (:class:`BartModel`):
            An instance of BartModel.
    """

    def __init__(self, bart):
        super().__init__()
        self.bart = bart
        self.lm_head_weight = self.create_parameter(
            shape=[self.bart.config["vocab_size"], self.bart.config["d_model"]],
            dtype=self.bart.shared.weight.dtype,
            is_bias=False,
        )
        self.register_buffer("final_logits_bias", paddle.zeros((1, self.bart.config["vocab_size"])))
        self.apply(self.init_weights)

    def get_encoder(self):
        return self.bart.get_encoder()

    def get_decoder(self):
        return self.bart.get_decoder()

    def prepare_faster_entry(self, kwargs):
        from paddlenlp.ops import FasterBART

        decode_strategy = kwargs.get("decode_strategy")
        use_fp16_decoding = kwargs.get("use_fp16_decoding", False)
        decoding_lib = kwargs.get("decoding_lib", None)
        enable_faster_encoder = kwargs.get("enable_faster_encoder", True)
        if decode_strategy == "sampling" and kwargs.get("top_k") != 0 and kwargs.get("top_p") != 1:
            raise AttributeError(
                "Only topk sampling or topp sampling are supported. "
                "Topk sampling and topp sampling cannot be both applied in the faster version."
            )
        if kwargs["repetition_penalty"] != 1.0:
            # not support for repetition_penalty yet in the faster version
            raise AttributeError("'repetition_penalty != 1' is not supported yet in the faster version")
        if kwargs["min_length"] != 0:
            # not support for min_length yet in the faster version
            raise AttributeError("'min_length != 0' is not supported yet in the faster version")
        if kwargs["forced_bos_token_id"] is not None:
            # not support for min_length yet in the faster version
            raise AttributeError("'forced_bos_token_id != None' is not supported yet in the faster version")
        self._faster_entry = FasterBART(
            self,
            use_fp16_decoding=use_fp16_decoding,
            decoding_lib=decoding_lib,
            enable_faster_encoder=enable_faster_encoder,
        ).forward
        return self._faster_entry

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        decoder_input_ids=None,
        decoder_attention_mask=None,
        encoder_output=None,
        inputs_embeds=None,
        decoder_inputs_embeds=None,
        use_cache=False,
        cache=None,
        labels=None,
        output_attentions=False,
        output_hidden_states=False,
        return_dict=False,
    ):
        r"""
        The BartForConditionalGeneration forward method, overrides the __call__() special method.

        Args:
            input_ids (Tensor, optional):
                See :class:`BartModel`.
            attention_mask (Tensor, optional):
                See :class:`BartModel`.
            decoder_input_ids (Tensor, `optional`):
                See :class:`BartModel`.
            decoder_attention_mask (Tensor, optional):
                See :class:`BartModel`.
            encoder_output (Tensor, optonal):
                See :class:`BartModel`.
            inputs_embeds (Tensor, optional):
                See :class:`BartModel`.
            decoder_inputs_embeds (Tensor, optional):
                See :class:`BartModel`.
            use_cache (bool, optional):
                See :class:`BartModel`.
            cache (Tensor, optional):
                See :class:`BartModel`.
            labels (Tensor, optional):
                Labels for computing the masked language modeling loss. Indices should either be in `[0, ...,
                vocab_size]` or -100 (see `input_ids` docstring). Tokens with indices set to `-100` are ignored
                (masked), the loss is only computed for the tokens with labels in `[0, ..., vocab_size]`.
                A tensor of shape `(batch_size, sequence_length)`. Default to `None`.
            output_attentions (bool, optional):
                See :class:`BartModel`.
            output_hidden_states (bool, optional):
                See :class:`BartModel`.
            return_dict (bool, optional):
                See :class:`BartModel`.

        Returns:
            An instance of :class:`~paddlenlp.transformers.model_outputs.Seq2SeqLMOutput` if
            `return_dict=True`. Otherwise it returns a tuple of tensors corresponding
            to ordered and not None (depending on the input arguments) fields of
            :class:`~paddlenlp.transformers.model_outputs.Seq2SeqLMOutput`.
            Especially, When `use_cache=return_dict=output_hidden_states=output_attentions=False` and labels=None,
            returns tensor `logits`, a tensor of the input text classification logits.

            With the fields:

            - `lm_logits` (Tensor):
                The generated sentence of the model.
                Its data type should be float32 and has a shape of [batch_size, sequence_length, vocab_size].

        Example:
            .. code-block::

                import paddle
                from paddlenlp.transformers import BartForConditionalGeneration, BartTokenizer

                tokenizer = BartTokenizer.from_pretrained('bart-base')
                model = BartForConditionalGeneration.from_pretrained('bart-base')

                inputs = tokenizer("Welcome to use PaddlePaddle and PaddleNLP!")
                inputs = {k:paddle.to_tensor([v]) for (k, v) in inputs.items()}
                outputs = model(**inputs)

        """
        if labels is not None:
            if use_cache:
                logger.warning("The `use_cache` argument is changed to `False` since `labels` is provided.")
            use_cache = False

        outputs = self.bart(
            input_ids,
            attention_mask,
            decoder_input_ids,
            decoder_attention_mask,
            encoder_output,
            inputs_embeds=inputs_embeds,
            decoder_inputs_embeds=decoder_inputs_embeds,
            use_cache=use_cache,
            cache=cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        lm_logits = paddle.tensor.matmul(outputs[0], self.lm_head_weight, transpose_y=True) + self.final_logits_bias

        masked_lm_loss = None
        if labels is not None:
            loss_fct = nn.CrossEntropyLoss()
            masked_lm_loss = loss_fct(lm_logits.reshape((-1, self.bart.config["vocab_size"])), labels.reshape((-1,)))

        if not return_dict:
            if len(outputs) == 2:
                return (masked_lm_loss, lm_logits) if masked_lm_loss is not None else lm_logits
            else:
                outputs = (lm_logits,) + outputs[1:]
                return ((masked_lm_loss,) + outputs) if masked_lm_loss is not None else outputs

        return Seq2SeqLMOutput(
            loss=masked_lm_loss,
            logits=lm_logits,
            past_key_values=outputs.past_key_values,
            decoder_hidden_states=outputs.decoder_hidden_states,
            decoder_attentions=outputs.decoder_attentions,
            cross_attentions=outputs.cross_attentions,
            encoder_last_hidden_state=outputs.encoder_last_hidden_state,
            encoder_hidden_states=outputs.encoder_hidden_states,
            encoder_attentions=outputs.encoder_attentions,
        )

    def prepare_decoder_input_ids_from_labels(self, labels):
        return shift_tokens_right(labels, self.bart.config["decoder_start_token_id"])

    def prepare_inputs_for_generation(
        self,
        decoder_input_ids,
        attention_mask=None,
        decoder_attention_mask=None,
        cache=None,
        use_cache=False,
        encoder_output=None,
        **kwargs
    ):
        # cut decoder_input_ids if past is used
        if cache is not None:
            decoder_input_ids = decoder_input_ids[:, -1].unsqueeze(-1)
            if decoder_attention_mask is not None:
                decoder_attention_mask = decoder_attention_mask[:, :, -1, :].unsqueeze(2)

        return {
            "input_ids": None,
            "decoder_input_ids": decoder_input_ids,
            "encoder_output": encoder_output,
            "decoder_attention_mask": decoder_attention_mask,
            "attention_mask": attention_mask,
            "use_cache": use_cache,
            "cache": cache,
        }

    def __getattr__(self, name):
        try:
            return super().__getattr__(name)
        except AttributeError as e:
            try:
                return getattr(getattr(self, self.base_model_prefix), name)
            except AttributeError:
                try:
                    return getattr(self, self.base_model_prefix).config[name]
                except KeyError:
                    raise e
