# Copyright 2022 Cerebras Systems.
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

from __future__ import annotations

import copy
import inspect
import json
import logging
import os
import pickle
import re
from abc import ABC, abstractmethod
from collections import OrderedDict
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Type, Union

import torch
import yaml
from tqdm import tqdm

import cerebras_pytorch as cstorch
from modelzoo.common.pytorch.model_utils.checkpoint_converters.streaming_checkpoints import (
    OnDemandDictionaryConverter,
    StreamingCSWriter,
    StreamingShardedHFReader,
    StreamingShardedHFWriter,
)


class EquivalentSubkey:
    r"""EquivalentSubkey defines the bidirectional relationship between subkeys of a model's
    checkpoint. This class is simply a 2-tuple with index bounds checking.

    For example if the normalization layer in one model is named "norm" and "ln" in the other,
    the relationship can be represented as EquivalentSubkey("norm", "ln").
    """

    def __init__(self, a: str, b: str) -> None:
        self.keys = [a, b]

    def __getitem__(self, idx: int) -> str:
        assert (
            idx == 0 or idx == 1
        ), "Invalid index into EquivalentSubkey object: {}".format(idx)
        return self.keys[idx]

    def __setitem__(self, idx: int, value) -> str:
        assert (
            idx == 0 or idx == 1
        ), "Invalid index into EquivalentSubkey object: {}".format(idx)
        self.keys[idx] = value

    def __repr__(self) -> str:
        return "EquivalentSubkey(\"{}\", \"{}\")".format(*self.keys)


class ConversionRule:
    r"""ConversionRule defines a "rule" which:
        1. a key can be matched against
        2. procedure for converting this old key to a new one upon a successful match
        3. and an action to be taken once the new key is created (ex: updating the
           state dictionary)

    A rule consists of a sequence of regex pattern (supplied as a string),
    EquivalentSubkey object, and (possibly) a BaseDictionaryConverter as long as this
    object is last in the sequence. It also contains an "exists" argument which
    can be set to "left", "both", or "right". The "left" and "right" arguments
    are used to describe if a key exists in one checkpoint format but not the
    other and should be ignored. Without this behavior, keys that exist in one
    but not the other wouldn't be matched by any conversion rules, causing a failure
    as drop_unmatched_keys is disabled by default.

    Example:
    The following describes the conversion rule for mapping HF's layer normalization
    key to CS layer normalization in the GPT2 model.
        >>> ConversionRule(
        >>>     [
        >>>         EquivalentSubkey("h", "transformer_decoder.layers"),
        >>>         "\.\d+\.",
        >>>         EquivalentSubkey("ln_1", "norm1"),
        >>>         "\.(weight|bias)",
        >>>     ],
        >>>     action=BaseCheckpointConverter.replaceKey,
        >>> )
    This should be interpreted as:
        1. HF uses 'h' to represent the decoder name while CS uses 'transformer_decoder.layers'
        2. Both will have keys that follow with a dot, the decoder number, and then another dot
        3. HF uses 'ln_1' for the first layer norm while CS names it 'norm1'
        4. Both will have keys that follow with a dot and then either weight or bias
    This representation should make it easy to see how we can 1) build a regex which
    matches against old keys, and 2) use the matched result & EquivalentSubkey information
    to create a new key. Finally, once the new key is constructed the conversion rule
    will apply the 'action' described by the user in order to complete the conversion
    (in this case simply copying the value at old_state's old key into the new_state at the
    new key).

    As previously mentioned, a conversion rule object can also contain a checkpoint
    converter at the end of the sequence. This is used to create a new checkpoint
    converter which uses another converter to handle a portion of the conversion.
    Doing so reduces the amount of copy & pasted conversion rules. For example,
    many models have base model classes which are extended with additional layers
    for fine-tuning. For example, HF's GP2Model doesn't contain a language model head
    while GP2LMHeadModel does. Rather than copying the conversion rules, we could
    instead define a new checkpoint converter as follows:

    >>> class Converter_GPT2LMHeadModel_HF_CS17(BaseDictionaryConverter):
    >>>     def __init__(self):
    >>>         super().__init__()
    >>>         self.rules = [
    >>>             ConversionRule(
    >>>                 ["lm_head\.(weight|bias)"],
    >>>                 action=BaseCheckpointConverter.replaceKey,
    >>>             ),
    >>>             ConversionRule(
    >>>                 [
    >>>                     EquivalentSubkey("transformer.", ""),
    >>>                     Converter_GPT2Model_HF_CS17(),
    >>>                 ],
    >>>                 action=None,
    >>>             ),
    >>>         ]

    The first rule simply notates that the lm_head key now exists (and is named
    the same in both models). The second rule notates that if the "transformer."
    prefix is encountered, we should try all of the GPT2Model HF -> CS 1.7
    conversion rules.
    """

    def __init__(
        self,
        segments: List[Union[str, EquivalentSubkey, BaseDictionaryConverter]],
        exists: str = "both",
        action: Optional[
            Callable[[str, OrderedDict, str, OrderedDict, int], None]
        ] = None,
    ) -> None:
        assert isinstance(segments, list), "Expected segments to be list"
        for elm in segments:
            assert isinstance(
                elm, (str, EquivalentSubkey, BaseDictionaryConverter)
            ), f"ConversionRule segment doesn't support type {type(elm)}"
        assert exists in ["left", "both", "right"]

        self.segments = segments
        self.exists = exists
        self.action = action
        self.validate_segments()

    def __repr__(self) -> str:
        single_line = len(self.segments) < 2
        out = "ConversionRule(["
        if not single_line:
            out += "\n"
        for i in range(len(self.segments)):
            mod_str = repr(self.segments[i])
            if not single_line:
                mod_str = _addindent(mod_str, 4) + ",\n"
            out += mod_str
        action_name = "self." + self.action.__name__ if self.action else "None"
        out += "], action={})".format(action_name)
        return out

    @staticmethod
    def segment_is_converter(
        elm: Union[str, EquivalentSubkey, BaseDictionaryConverter]
    ) -> bool:
        return isinstance(elm, BaseDictionaryConverter)

    def validate_segments(self):
        for seg in self.segments:
            if isinstance(seg, str):
                pattern = re.compile(seg)
                assert (
                    pattern.groups == 0
                ), "The following regex isn't supported: {}\n\
                    Compile rule's regex cannot contain capture groups.\n\
                    Use (?:a|b) instead of (a|b)".format(
                    seg
                )

    def convert_key(
        self,
        old_key: str,
        old_state_dict: OrderedDict,
        new_state_dict: OrderedDict,
        from_index: int,
        match_start: int = 0,
        prefix: str = "",
        action_fn_args: Optional[dict] = None,
        debug: bool = False,
    ) -> bool:
        regex_str = ""
        maybe_escape = (
            lambda elm, idx: re.escape(elm[idx])
            if isinstance(elm, EquivalentSubkey)
            else elm
        )
        regex_str = ""
        chained_converter = ConversionRule.segment_is_converter(
            self.segments[-1]
        )
        candidate_segments = len(self.segments)
        if chained_converter:
            candidate_segments -= 1

        for i in range(candidate_segments):
            elm = self.segments[i]
            assert not ConversionRule.segment_is_converter(
                elm
            ), "Checkpoint convert objects can only be placed at the end of rules"
            regex_str += "({})".format(maybe_escape(elm, from_index))

        pattern = re.compile(regex_str)
        match_result = (
            pattern.fullmatch(old_key, match_start)
            if not chained_converter
            else pattern.match(old_key, match_start)
        )

        if match_result is None:
            return False

        converted = prefix
        to_index = 1 - from_index
        for i in range(candidate_segments):
            if isinstance(self.segments[i], EquivalentSubkey):
                converted += self.segments[i][to_index]
            else:
                converted += match_result.group(
                    i + 1
                )  # Index 0 always contains full match

        if chained_converter:
            converter = self.segments[-1]
            return converter.convert_key(
                old_key,
                old_state_dict,
                new_state_dict,
                from_index,
                match_start=match_result.span()[1],
                prefix=converted,
                action_fn_args=action_fn_args,
                debug=debug,
            )
        else:
            if debug:
                print(
                    "Matched {} -> {} action: {}".format(
                        old_key,
                        converted,
                        self.action.__name__ if self.action else "None",
                    )
                )
            if self.action:
                self.action(
                    old_key,
                    converted,
                    old_state_dict,
                    new_state_dict,
                    from_index,
                    action_fn_args,
                )
            return True

    def exists_in_index(self, to_index: int) -> bool:
        return (
            self.exists == "both"
            or (self.exists == "left" and to_index == 0)
            or (self.exists == "right" and to_index == 1)
        )


class FormatVersions:
    def __init__(self, *versions) -> None:
        self.formats = [*versions]

    def __len__(self):
        return len(self.formats)

    def __getitem__(self, i):
        return self.formats[i]

    def __contains__(self, key):
        return key in self.formats

    def __str__(self) -> str:
        return ", ".join(self.formats)

    def __repr__(self) -> str:
        return "FormatVersions" + repr(self.formats)


class BaseDictionaryConverter(ABC):
    r"""A dictionary converter represents a pair of two dictionary formats that
        can be converted between each other. The converter object defines a list
        of conversion rules which should be applied when converting one dict
        format to the other (and vice-versa).

        In order to make your own dictionary converter, simply:
        1. Create a new converter class which inherits from BaseDictionaryConverter
        2. Supply a list of conversion rules (self.rules)
        3. Override the pre_model_convert or post_model_convert hooks if you
           need to execute arbitrary behavior before/after the conversion.
    """

    def __init__(self, pbar_desc=None):
        self.pbar_desc = pbar_desc

    def __repr__(self) -> str:
        out = "BaseDictionaryConverter([\n"
        for i in range(len(self.rules)):
            out += _addindent(repr(self.rules[i]), 4) + ",\n"
        out += "])"
        return out

    @staticmethod
    @abstractmethod
    def formats() -> Tuple[FormatVersions, FormatVersions]:
        pass

    @classmethod
    def supports_conversion(cls, src_fmt, tgt_fmt):
        return cls.get_from_index(src_fmt, tgt_fmt) is not None

    @classmethod
    def get_from_index(cls, src_fmt, tgt_fmt):
        formats = cls.formats()
        assert (
            formats is not None
        ), "Class {} hasn't provided formats() which is required.".format(
            cls.__name__
        )
        if src_fmt in formats[0] and tgt_fmt in formats[1]:
            return 0
        elif src_fmt in formats[1] and tgt_fmt in formats[0]:
            return 1
        else:
            return None

    @staticmethod
    def replaceKey(
        old_key: str,
        new_key: str,
        old_state_dict: OrderedDict,
        new_state_dict: OrderedDict,
        from_index: int,
        action_fn_args: Optional[dict] = None,
    ) -> None:
        r"""
        Copies value that exists at old_state_dict's old_key to new_state_dict's
        new_key.
        """
        new_state_dict[new_key] = old_state_dict[old_key]

    def convert_key(
        self,
        old_key: str,
        old_state_dict: OrderedDict,
        new_state_dict: OrderedDict,
        from_index: int,
        match_start: int = 0,
        prefix: str = "",
        action_fn_args: Optional[dict] = None,
        debug: bool = False,
    ) -> None:
        r"""
        Attempts to convert the old key by matching against the list of
        conversion rules. The first rule to match is used for conversion (i.e.
        even if multiple rules *would* match, the latter ones are never used).
        Returns True if a conversion occurred.
        """
        assert hasattr(
            self, "rules"
        ), "Converter must have a list of conversion rules"
        for rule in self.rules:
            did_convert = rule.convert_key(
                old_key,
                old_state_dict,
                new_state_dict,
                from_index,
                match_start,
                prefix,
                action_fn_args,
                debug=debug,
            )
            if did_convert:
                return True
        return False

    def convert_all_keys(
        self,
        old_state_dict: OrderedDict,
        new_state_dict: OrderedDict,
        from_index: int,
        action_fn_args: Optional[dict] = None,
        no_progress_bar: bool = True,
        debug: bool = False,
        suppress_unmatched_key_warning: bool = False,
    ):
        if not no_progress_bar:
            pbar = tqdm(total=len(old_state_dict), desc=self.pbar_desc)

        matched_all_keys = True
        for key in old_state_dict.keys():
            matched_current_key = self.convert_key(
                key,
                old_state_dict,
                new_state_dict,
                from_index,
                action_fn_args=action_fn_args,
                debug=debug,
            )
            if not matched_current_key and not suppress_unmatched_key_warning:
                logging.warning("Key not matched: {}".format(key))
            if not no_progress_bar:
                pbar.update(1)
            matched_all_keys = matched_all_keys and matched_current_key
        return matched_all_keys


class BaseCheckpointConverter(BaseDictionaryConverter, ABC):
    r"""Converts between checkpoint state_dict formats."""

    def __init__(self):
        super().__init__(pbar_desc="Converting Checkpoint")

    @staticmethod
    @abstractmethod
    def file_formats() -> Tuple[str, str]:
        pass

    @staticmethod
    @abstractmethod
    def get_config_converter_class() -> BaseConfigConverter:
        pass

    @classmethod
    @abstractmethod
    def load(cls, file: str, from_index: int, **kwargs) -> OrderedDict:
        pass

    @classmethod
    @abstractmethod
    def save(
        cls,
        file_without_ext: str,
        checkpoint: OrderedDict,
        from_index: int,
        **kwargs,
    ) -> str:
        pass

    @classmethod
    @abstractmethod
    def init_output_checkpoint(
        cls, file_without_ext: str, from_index: int, **kwargs,
    ) -> str:
        r"""
        (Pre)Initializes the output checkpoint at a supplied path. This is used
        in streaming conversion when the checkpoint is written to file as
        conversion is performed rather than accumulating the full checkpoint
        in memory and saving to file at the very end.
        """

    @classmethod
    def convert(
        cls,
        input_checkpoint,
        configs,
        checkpoint_from_index,
        output_checkpoint=OrderedDict(),
        **kwargs,
    ):
        instance = cls()
        output_checkpoint = instance.convert_helper(
            input_checkpoint,
            configs,
            checkpoint_from_index,
            output_checkpoint=output_checkpoint,
            **kwargs,
        )
        return output_checkpoint

    def convert_helper(
        self,
        input_checkpoint,
        configs: Tuple[dict, dict],
        from_index: int,
        output_checkpoint=OrderedDict(),
        drop_unmatched_keys: bool = False,
        no_progress_bar: bool = True,
        debug: bool = False,
    ):
        r"""
        Converts all keys in a checkpoint from `from_index` format to the other
        format. Conversion will fail if at least one of the keys did not match
        on any conversion rules and drop_unmatched_keys is not enabled. Returns
        the newly converted checkpoint.
        """
        self.pre_checkpoint_convert(
            input_checkpoint, output_checkpoint, configs, from_index
        )

        old_state_dict, new_state_dict = self.extract_model_dict(
            input_checkpoint, output_checkpoint, configs, from_index,
        )

        self.pre_model_convert(
            old_state_dict,
            new_state_dict,
            configs,
            from_index,
            drop_unmatched_keys,
        )

        matched_all_keys = self.convert_all_keys(
            old_state_dict,
            new_state_dict,
            from_index,
            action_fn_args={"configs": configs},
            no_progress_bar=no_progress_bar,
            debug=debug,
        )

        self.post_model_convert(
            old_state_dict,
            new_state_dict,
            configs,
            from_index,
            drop_unmatched_keys,
        )

        if not matched_all_keys and not drop_unmatched_keys:
            assert matched_all_keys, (
                "Unable to match all keys. If you want to proceed by dropping keys that couldn't "
                "matched, rerun with --drop-unmatched-keys"
            )
        elif not matched_all_keys:
            logging.warning(
                "proceeding even though some keys weren't matched because of --drop-unmatched-keys"
            )

        self.post_checkpoint_convert(
            input_checkpoint, output_checkpoint, configs, from_index
        )
        return output_checkpoint

    def pre_model_convert(
        self,
        old_state_dict: OrderedDict,
        new_state_dict: OrderedDict,
        configs: Tuple[dict, dict],
        from_index: int,
        drop_unmatched_keys: bool,
    ):
        r"""
        Hook executes right before model conversion.
        """

    def post_model_convert(
        self,
        old_state_dict: OrderedDict,
        new_state_dict: OrderedDict,
        configs: Tuple[dict, dict],
        from_index: int,
        drop_unmatched_keys: bool,
        key_prefix: str = "",
    ):
        r"""
        Hook executes right after model conversion.
        """

    def pre_checkpoint_convert(
        self,
        input_checkpoint,
        output_checkpoint,
        configs: Tuple[dict, dict],
        from_index: int,
    ):
        r"""
        Hook executes before checkpoint conversion.
        """

    def extract_model_dict(
        self,
        input_checkpoint,
        output_checkpoint,
        configs: Tuple[dict, dict],
        from_index: int,
    ):
        r"""
        Hook to extract model state dicts out of the input/output checkpoint
        """
        return input_checkpoint, output_checkpoint

    def post_checkpoint_convert(
        self,
        input_checkpoint,
        output_checkpoint,
        configs: Tuple[dict, dict],
        from_index: int,
    ):
        r"""
        Hook executes after checkpoint conversion.
        """


class BaseCheckpointConverter_CS_CS(BaseCheckpointConverter):
    @staticmethod
    def file_formats() -> Tuple[str, str]:
        return ("mdl", "mdl")

    @classmethod
    def load(cls, file: str, from_index: int) -> OrderedDict:
        return cstorch.load(file, map_location="cpu")

    @classmethod
    def save(
        cls, file_without_ext: str, checkpoint: OrderedDict, from_index: int,
    ) -> OrderedDict:

        if isinstance(checkpoint, StreamingCSWriter):
            checkpoint.save()
            return checkpoint.checkpoint_file
        else:
            to_index = from_index - 1
            output_file_format = cls.file_formats()[to_index]
            file = file_without_ext + "." + output_file_format
            cstorch.save(checkpoint, file)
            return file

    @classmethod
    def init_output_checkpoint(
        cls, file_without_ext: str, from_index: int, **kwargs
    ) -> str:
        if kwargs.get("export_safetensors", False):
            logging.warning(
                "--export-safetensors flag will be ignored as we are converting"
                " to a CS format which uses Cerebras H5 checkpoints."
            )
        to_index = from_index - 1
        output_file_format = cls.file_formats()[to_index]
        file = file_without_ext + "." + output_file_format
        return StreamingCSWriter(file)

    def pre_checkpoint_convert(
        self,
        input_checkpoint,
        output_checkpoint,
        configs: Tuple[dict, dict],
        from_index: int,
    ):
        # Copy non model keys like optimizer state:
        for category in input_checkpoint:
            if category != "model":
                output_checkpoint[category] = input_checkpoint[category]
        output_checkpoint["model"] = {}

        # check to see if we need to run dataloader iter state conversion
        old_config = configs[from_index]
        worker_data_iter_files_dir = old_config.get("cerebras", {}).get(
            "save_iter_state_path", ""
        )
        if worker_data_iter_files_dir:
            # try to extract dataloader_type
            data_processor = old_config.get("train_input", {}).get(
                "data_processor", ""
            )
            if data_processor == "GptHDF5DataProcessor":
                dataloader_type = "iterable"
            elif data_processor == "GptHDF5MapDataProcessor":
                dataloader_type = "map"
            else:
                raise ValueError(
                    "DataLoader state conversion requires `train_input.data_processor` to be "
                    "specified as either 'GptHDF5DataProcessor' or 'GptHDF5MapDataProcessor', but "
                    f"instead got: '{data_processor}'."
                )
            convert_dataloader_checkpoint(
                output_checkpoint,
                worker_data_iter_files_dir,
                dataloader_type=dataloader_type,
                shuffle_seed=0,
            )

    def extract_model_dict(
        self,
        input_checkpoint,
        output_checkpoint,
        configs: Tuple[dict, dict],
        from_index: int,
    ):
        return input_checkpoint["model"], output_checkpoint["model"]


class BaseCheckpointConverter_HF_CS(BaseCheckpointConverter_CS_CS):
    r"""HF checkpoints contain model only while CS checkpoints package model,
    optimizer, and lr_scheduler into a single checkpoint. This class overrides
    the post_checkpoint_convert to automatically extract/package the state_dict
    correctly.
    """

    @staticmethod
    def file_formats() -> Tuple[str, str]:
        return ("bin", "mdl")

    @classmethod
    def load(cls, file: str, from_index: int,) -> OrderedDict:
        if os.path.isdir(file):
            raise AssertionError(
                """
                You have passed in a directory instead of a file. Some
                converters support this behavior, so if this was intended
                check the model you have entered.
                """
            )

        if file.endswith(".index.json"):
            assert (
                from_index == 0
            ), ".index.json files are only supported when doing HF -> CS conversion"
            return StreamingShardedHFReader(file)
        else:
            assert not file.endswith(".safetensors"), (
                ".safetensor files are only supported for sharded checkpoints due to safetensor's "
                "weight sharing restrictions."
            )
            # Any other type of checkpoint
            return cstorch.load(file, map_location="cpu")

    @classmethod
    def save(
        cls, file_without_ext: str, checkpoint: OrderedDict, from_index: int,
    ) -> OrderedDict:
        if isinstance(checkpoint, StreamingCSWriter):
            checkpoint.save()
            return checkpoint.checkpoint_file
        elif isinstance(checkpoint, StreamingShardedHFWriter):
            checkpoint.save()
            return checkpoint.checkpoint_dir
        else:
            to_index = from_index - 1
            output_file_format = cls.file_formats()[to_index]
            file = file_without_ext + "." + output_file_format
            if from_index == 0:
                torch.save(checkpoint, file)
            else:
                cstorch.save(checkpoint, file)
            return file

    @classmethod
    def init_output_checkpoint(
        cls,
        file_without_ext: str,
        from_index: int,
        hf_shard_size: Union[str, int] = "10GB",
        export_safetensors: bool = False,
        **kwargs,
    ) -> str:
        to_index = from_index - 1
        output_file_format = cls.file_formats()[to_index]
        file = file_without_ext + "." + output_file_format

        if from_index == 0:
            # HF -> CS
            if export_safetensors:
                logging.warning(
                    "--export-safetensors flag will be ignored as we are converting"
                    " to a CS format which uses Cerebras H5 checkpoints."
                )
            return StreamingCSWriter(file)
        else:
            # CS -> HF
            return StreamingShardedHFWriter(
                file_without_ext,
                shard_size=hf_shard_size,
                export_safetensors=export_safetensors,
            )

    def pre_checkpoint_convert(
        self,
        input_checkpoint,
        output_checkpoint,
        configs: Tuple[dict, dict],
        from_index: int,
    ):
        if from_index == 0:
            output_checkpoint["model"] = {}

    def extract_model_dict(
        self,
        input_checkpoint,
        output_checkpoint,
        configs: Tuple[dict, dict],
        from_index: int,
    ):
        if from_index == 0:
            return input_checkpoint, output_checkpoint["model"]
        else:
            from modelzoo.common.pytorch.model_utils.checkpoint_converters.mup import (
                Converter_sP_muP,
            )

            to_state_dict = input_checkpoint["model"]

            if self.attempt_mup_to_sp() and Converter_sP_muP.is_mup(configs[1]):
                if not self.supports_mup_conversion():
                    raise ConfigConversionError(
                        "This model currently does not support muP checkpoint conversion to HF."
                    )
                to_state_dict = OnDemandDictionaryConverter(
                    to_state_dict, Converter_sP_muP, {"configs": configs}
                )

            return to_state_dict, output_checkpoint

    def post_model_convert(
        self,
        old_state_dict: OrderedDict,
        new_state_dict: OrderedDict,
        configs: Tuple[dict, dict],
        from_index: int,
        drop_unmatched_keys: bool,
        key_prefix: str = "",
    ):
        if from_index == 1:
            cs_config = configs[from_index]
            if cs_config.get("sparsity", {}).get("type") == "sideband":
                # Finalize the CS sparsity in CS -> HF conversion.
                logging.info(
                    "Finalizing sparsity. The output checkpoint will be dense."
                )
                for key, weight in new_state_dict.items():
                    weight[weight.isnan()] = 0
                    new_state_dict[key] = weight

    def supports_mup_conversion(self) -> bool:
        return False

    def attempt_mup_to_sp(self) -> bool:
        return True


class BaseCheckpointConverter_UnpackedHF_PackedCS(
    BaseCheckpointConverter_HF_CS
):
    r"""Converter between a set of unpacked HF checkpoints and a single packed CS
    checkpoint.

    Some CS models consist of separate components which we want to initialize
    from existing HF checkpoints. For example, initializing the image encoder and
    text decoder of a multimodal model. This converter class provides an abstraction
    for using existing HF <-> CS checkpoint converters.

    In particular, we specify a list of `BaseCheckpointConverter_HF_CS` classes
    through `converters()` corresponding to each model component. Similarly,
    we specify another list of directory names through `component_names()`
    corresponding to the name of the subdirectory containing the model checkpoint.

    During conversion, this converter applies the i-th component converter to the
    component checkpoint found in the i-th subdirectory name.
    """

    @classmethod
    def load(cls, path: str, from_index: int,) -> OrderedDict:
        def find_checkpoint_file(directory: str) -> str:
            for filename in os.listdir(directory):
                if filename.endswith(".index.json"):
                    return os.path.join(directory, filename)

            if "pytorch_model.bin" in os.listdir(directory):
                return os.path.join(directory, "pytorch_model.bin")

            error_message = (
                "HF -> CS Converter assumes that the input file will "
                "be a directory that contains the sub-directory: {} "
                "It will run the standard checkpoint loading on this "
                "sub-directory, assuming the following convention. "
                "It will check if an .index.json exists in the sub-dir, "
                "and use this if found. Next, if will use a pytorch_model.bin in "
                "sub-dir if found. Otherwise it will return an error. "
            ).format(directory)
            raise AssertionError(error_message)

        # HF --> CS
        if from_index == 0:
            # read in files
            checkpoints = []
            for name in cls.component_names():
                dir = os.path.join(path, name)
                file = find_checkpoint_file(dir)
                checkpoints.append(super().load(file, from_index))
            return checkpoints
        # CS --> HF
        else:
            return super().load(path, from_index)

    @classmethod
    def init_output_checkpoint(
        cls,
        file_without_ext: str,
        from_index: int,
        hf_shard_size: Union[str, int] = "10GB",
        export_safetensors: bool = False,
        **kwargs,
    ) -> str:
        to_index = from_index - 1
        output_file_format = cls.file_formats()[to_index]
        file = file_without_ext + "." + output_file_format
        if from_index == 0:
            # HF -> CS
            if export_safetensors:
                logging.warn(
                    "--export-safetensors flag will be ignored as we are converting"
                    " to a CS format which uses Cerebras H5 checkpoints."
                )
            dir, file_name = os.path.split(file_without_ext)
            file_name = file_name.split("_")[1:]
            out_file = "pytorch_model_" + "_".join(file_name) + ".mdl"
            out_file = os.path.join(dir, out_file)
            return StreamingCSWriter(out_file)
        else:
            # CS -> HF
            dir = os.path.dirname(file)
            dirs = [os.path.join(dir, name) for name in cls.component_names()]
            return [
                StreamingShardedHFWriter(
                    model_dir,
                    shard_size=hf_shard_size,
                    export_safetensors=export_safetensors,
                )
                for model_dir in dirs
            ]

    def convert_helper(
        self,
        input_checkpoint,
        configs: Tuple[List[dict, dict], dict],
        from_index: int,
        output_checkpoint=OrderedDict(),
        drop_unmatched_keys: bool = False,
        no_progress_bar: bool = True,
        debug: bool = False,
    ):
        hf_config = configs[0]
        cs_config = configs[1]
        cs_configs = [
            {"model": cs_config["model"][name]}
            for name in self.component_names()
        ]

        # HF -> CS
        if from_index == 0:
            assert isinstance(input_checkpoint, list), (
                "When converting from HF to CS, the Converter expects "
                f"{len(self.converters())} input checkpoints for each "
                "model component."
            )
            if output_checkpoint is None:
                output_checkpoint = OrderedDict()
        else:
            if output_checkpoint is None:
                output_checkpoint = [OrderedDict() for _ in self.converters()]

            assert isinstance(output_checkpoint, list), (
                "When converting from CS -> HF, the converter expects "
                f"{len(self.converters())} output checkpoints for each "
                "model component."
            )

        self.pre_checkpoint_convert(
            input_checkpoint, output_checkpoint, configs, from_index
        )

        if from_index == 0:
            for i, converter_cls in enumerate(self.converters()):
                instance = converter_cls()
                output_checkpoint = instance.convert_helper(
                    input_checkpoint[i],
                    [hf_config[i], cs_configs[i]],
                    from_index,
                    output_checkpoint=output_checkpoint,
                    drop_unmatched_keys=drop_unmatched_keys,
                    no_progress_bar=no_progress_bar,
                    debug=debug,
                )
        else:
            for i, converter_cls in enumerate(self.converters()):
                instance = converter_cls()
                output_checkpoint[i] = instance.convert_helper(
                    input_checkpoint,
                    [hf_config[i], cs_configs[i]],
                    from_index,
                    output_checkpoint=output_checkpoint[i],
                    drop_unmatched_keys=drop_unmatched_keys,
                    no_progress_bar=no_progress_bar,
                    debug=debug,
                )

        self.post_checkpoint_convert(
            input_checkpoint, output_checkpoint, configs, from_index
        )
        return output_checkpoint

    @classmethod
    def save(
        cls,
        file_without_ext: str,
        checkpoint: List[OrderedDict],
        from_index: int,
        **kwargs,
    ) -> str:
        if from_index == 0:
            dir, file_name = os.path.split(file_without_ext)
            file_name = file_name.split("_")[1:]
            out_file = "pytorch_model_" + "_".join(file_name)
            out_file = os.path.join(dir, out_file)
            return super().save(out_file, checkpoint, from_index, **kwargs)
        else:
            outputs = []
            for ckpt in checkpoint:
                output = super().save(
                    file_without_ext, ckpt, from_index, **kwargs
                )
                outputs.append(output)
            return outputs

    @classmethod
    def converter_note(cls) -> str:
        src_fmt, tgt_fmt = cls.formats()[0], cls.formats()[1]
        src_arch, tgt_arch = cls.architectures()[0], cls.architectures()[1]
        return (
            f"{src_fmt} ({', '.join(src_arch)}) <-> {tgt_fmt} {tgt_arch}. "
            "Note that we expect the following structure for HF checkpoints: "
            "one outer directory that contains sub-directories named "
            f"({', '.join(cls.component_names())}). Each of these directories "
            "are expected to have either a pytorch_model.bin or .index.json "
            "file, as well as config.json. "
            "Please pass the path to the outer directory to both the "
            "--config argument and checkpoint_file argument. The converter will "
            "then find the correct config and checkpoint files assuming the file "
            "structure above. "
            "For CS -> HF, --config will refer to .yaml config and checkpoint "
            "path will refer to .mdl checkpoint as usual. "
        )

    @staticmethod
    @abstractmethod
    def converters() -> List[Type[BaseCheckpointConverter]]:
        pass

    @staticmethod
    @abstractmethod
    def component_names() -> List[str]:
        pass

    @staticmethod
    @abstractmethod
    def architectures() -> Tuple[List[str], str]:
        pass


class ConfigConversionError(Exception):
    "Raised when a config cannot be converted"


class BaseConfigConverter(BaseDictionaryConverter, ABC):
    def __init__(self):
        super().__init__(pbar_desc="Converting Config")
        self.pre_convert_defaults = [{}, {}]
        self.post_convert_defaults = [{}, {}]

    @staticmethod
    @abstractmethod
    def file_formats() -> Tuple[str, str]:
        pass

    @classmethod
    def load(cls, file: str, from_index: int) -> dict:
        input_file_format = cls.file_formats()[from_index]
        if input_file_format == "json":
            with open(file, "r") as f:
                return json.load(f)
        elif input_file_format == "yaml":
            with open(file, "r") as f:
                return yaml.load(f, Loader=yaml.SafeLoader)
        else:
            raise ValueError(
                "Unsupported input file format: {}".format(input_file_format())
            )

    @classmethod
    def save(cls, file_without_ext: str, config: dict, from_index: int) -> str:
        to_index = (from_index + 1) % 2
        output_file_format = cls.file_formats()[to_index]
        file = file_without_ext + "." + output_file_format
        if output_file_format == "json":
            with open(file, "w") as f:
                f.write(json.dumps(config, indent=4))
        elif output_file_format == "yaml":
            with open(file, "w") as f:
                f.write(yaml.dump(config, indent=4))
        else:
            raise ValueError(
                "Unsupported input file format: {}".format(output_file_format())
            )
        return file

    @classmethod
    def convert(
        cls,
        config,
        from_index: int,
        drop_unmatched_keys: bool = False,
        no_progress_bar: bool = True,
        debug: bool = False,
    ):
        instance = cls()
        return instance.convert_helper(
            config,
            from_index,
            drop_unmatched_keys=drop_unmatched_keys,
            no_progress_bar=no_progress_bar,
            debug=debug,
        )

    def convert_helper(
        self,
        config,
        from_index: int,
        drop_unmatched_keys: bool = False,
        no_progress_bar: bool = True,
        debug: bool = False,
    ):
        r"""
        Converts all keys in a config from `from_index` format to the other
        format. Conversion will fail if at least one of the keys did not match
        on any conversion rules and drop_unmatched_keys is not enabled. Returns
        the newly converted config.
        """

        old_config = self.pre_config_convert(config, from_index)
        new_config = {}

        matched_all_keys = self.convert_all_keys(
            old_config,
            new_config,
            from_index,
            no_progress_bar=no_progress_bar,
            debug=debug,
            suppress_unmatched_key_warning=drop_unmatched_keys,
        )

        if not matched_all_keys and not drop_unmatched_keys:
            assert matched_all_keys, "Unable to match all keys in config."

        final_config = self.post_config_convert(
            config, old_config, new_config, from_index, drop_unmatched_keys
        )
        return final_config

    def pre_config_convert(
        self, config, from_index,
    ):
        for key in self.pre_convert_defaults[from_index]:
            if key not in config:
                config[key] = self.pre_convert_defaults[from_index][key]
            elif isinstance(self.pre_convert_defaults[from_index][key], dict):
                for subkey, subvalue in self.pre_convert_defaults[from_index][
                    key
                ].items():
                    if subkey not in config[key]:
                        config[key][subkey] = subvalue
        return config

    def post_config_convert(
        self,
        original_config,
        old_config,
        new_config,
        from_index,
        drop_unmatched_keys,
    ):
        to_index = 1 - from_index
        for key in self.post_convert_defaults[to_index]:
            if key not in new_config:
                new_config[key] = self.post_convert_defaults[to_index][key]
            elif isinstance(self.post_convert_defaults[to_index][key], dict):
                for subkey, subvalue in self.post_convert_defaults[to_index][
                    key
                ].items():
                    if subkey not in new_config[key]:
                        new_config[key][subkey] = subvalue
        return new_config

    @staticmethod
    def assert_factory_fn(assert_index, assert_value):
        def assert_factory_wrapper(
            old_key,
            new_key,
            old_state_dict,
            new_state_dict,
            from_index,
            action_fn_args,
        ):

            if from_index != assert_index:
                raise ConfigConversionError(
                    f"{old_key} should not appear in the config"
                )

            if old_state_dict[old_key] != assert_value:
                raise ConfigConversionError(
                    "Can't convert config with {}={}. Only {} is supported.".format(
                        old_key, old_state_dict[old_key], assert_value
                    )
                )

        return assert_factory_wrapper


class BaseConfigConverter_HF_CS(BaseConfigConverter):
    r"""CS packages model, optimizer, and lr_scheduler into a single config.
    This class overrides the [pre|post]_config_convert fn to automatically
    extract/package the model configuration correctly.
    """

    def __init__(self):
        super().__init__()
        self.post_convert_defaults[1]["mixed_precision"] = True

    @staticmethod
    def file_formats() -> Tuple[str, str]:
        return ("json", "yaml")

    def pre_config_convert(
        self, config, from_index,
    ):
        from modelzoo.common.pytorch.model_utils.checkpoint_converters.mup import (
            ConfigConverter_sP_muP,
        )

        model_config = config["model"] if from_index == 1 else config

        if (
            from_index == 1
            and self.attempt_mup_to_sp()
            and ConfigConverter_sP_muP.is_mup(model_config)
        ):
            if not self.supports_mup_conversion():
                raise ConfigConversionError(
                    "This model currently does not support muP config conversion to HF."
                )
            model_config = ConfigConverter_sP_muP.convert(
                model_config, from_index,
            )

        return super().pre_config_convert(model_config, from_index)

    def post_config_convert(
        self,
        original_config,
        old_config,
        new_config,
        from_index,
        drop_unmatched_keys,
    ):
        model_config = super().post_config_convert(
            original_config,
            old_config,
            new_config,
            from_index,
            drop_unmatched_keys,
        )

        # Starting from cs-2.1, we no longer use `use_bfloat16` and use `fp16_type` instead.
        # This block takes care of conversion from HF use_bfloat16 <-> CS fp16_type.
        # Note that we don't check versions since we don't have info on what exact version we're
        # converting here, but also it doesn't really matter to have an extra unused flag for
        # previous releases.
        if from_index == 0 and "use_bfloat16" in original_config:
            model_config["fp16_type"] = (
                "bfloat16" if original_config["use_bfloat16"] else "float16"
            )
        elif from_index == 1 and "fp16_type" in original_config:
            if original_config["fp16_type"] == "cbfloat16":
                model_config["use_bfloat16"] = True  # Proxy dtype
            elif original_config["fp16_type"] == "bfloat16":
                model_config["use_bfloat16"] = True
            elif original_config["fp16_type"] == "float16":
                model_config["use_bfloat16"] = False
            else:
                raise ValueError(
                    f"Invalid `fp16_type` value: {original_config['fp16_type']}"
                )

        if from_index == 0:
            return {"model": model_config}
        else:
            return model_config

    def supports_mup_conversion(self) -> bool:
        r"""Determines whether muP -> sP conversion is supported for this model"""
        return False

    def attempt_mup_to_sp(self) -> bool:
        r"""Determines whether muP -> sP conversion is should be attempted.
        Some HF models (such as BTLM) should not attempt muP -> sP conversion
        since they can natively handle muP.
        """
        return True


class BaseConfigConverter_UnpackedHF_PackedCS(BaseConfigConverter_HF_CS):
    r"""Converter between a set of unpacked HF configs and a single packed CS
    configs.

    Some CS models consist of separate components which we want to initialize
    from existing HF checkpoints. For example, initializing the image encoder and
    text decoder of a multimodal model. This converter class provides an abstraction
    for using existing HF <-> CS checkpoint converters.

    In particular, we specify a list of `BaseConfigConverter_HF_CS` classes
    through `converters()` corresponding to each model component. Similarly,
    we specify another list of directory names through `component_names()`
    corresponding to the name of the subdirectory containing the model config.

    During conversion, this converter applies the i-th component converter to the
    component config found in the i-th subdirectory name.
    """

    @staticmethod
    @abstractmethod
    def converters() -> List[Type[BaseCheckpointConverter]]:
        pass

    @staticmethod
    @abstractmethod
    def component_names() -> List[str]:
        pass

    @classmethod
    def load(cls, path: str, from_index: int,) -> OrderedDict:
        # HF --> CS
        if from_index == 0:
            configs = []
            for name in cls.component_names():
                file = os.path.join(path, name, "config.json")
                assert os.path.exists(file)
                config = super().load(file, from_index)
                configs.append(config)
            return configs
        # CS --> HF
        else:
            return super().load(path, from_index)

    @classmethod
    def save(
        cls,
        file_without_ext: str,
        config: OrderedDict,
        from_index: int,
        **kwargs,
    ) -> str:
        # saving CS requires only saving once
        if from_index == 0:
            dir, file_name = os.path.split(file_without_ext)
            file_name = file_name.split("_")[1:]
            out_file = "config_" + "_".join(file_name)
            out_file = os.path.join(dir, out_file)
            return super().save(out_file, config, from_index, **kwargs)
        # saving HF requires separating encoders and saving both
        else:
            save_files = []
            dir = os.path.dirname(file_without_ext)
            for i, name in enumerate(cls.component_names()):
                path = os.path.join(dir, name, "config")
                save_file = super().save(path, config[i], from_index, **kwargs)
                save_files.append(save_file)
            return save_files

    def convert_helper(
        self,
        config,
        from_index: int,
        drop_unmatched_keys: bool = False,
        no_progress_bar: bool = True,
        debug: bool = False,
    ):
        old_config = self.pre_config_convert(config, from_index)

        if from_index == 0:
            input_config = [config for config in old_config]
        else:
            input_config = [
                {"model": config["model"][name]}
                for name in self.component_names()
            ]

        new_config = []
        for i, converter_cls in enumerate(self.converters()):
            instance = converter_cls()
            new_config.append(
                instance.convert_helper(
                    input_config[i],
                    from_index,
                    drop_unmatched_keys,
                    no_progress_bar=no_progress_bar,
                    debug=debug,
                )
            )

        final_config = self.post_config_convert(
            config, old_config, new_config, from_index, drop_unmatched_keys
        )
        return final_config

    def post_config_convert(
        self,
        original_config,
        old_config,
        new_config,
        from_index,
        drop_unmatched_keys,
    ):
        if from_index == 0:
            new_config = {
                name: config["model"]
                for name, config in zip(self.component_names(), new_config)
            }
        return super().post_config_convert(
            original_config,
            old_config,
            new_config,
            from_index,
            drop_unmatched_keys,
        )


class BaseConfigConverter_CS_CS(BaseConfigConverter):
    r"""CS packages model, optimizer, and lr_scheduler into a single config.
    This class overrides the [pre|post]_config_convert fn to automatically
    extract/package the model configuration correctly.
    """

    def __init__(self):
        super().__init__()
        self.post_convert_defaults[0]["mixed_precision"] = True
        self.post_convert_defaults[1]["mixed_precision"] = True

    @staticmethod
    def file_formats() -> Tuple[str, str]:
        return ("yaml", "yaml")

    def pre_config_convert(
        self, config, from_index,
    ):
        return super().pre_config_convert(config["model"], from_index)

    def post_config_convert(
        self,
        original_config,
        old_config,
        new_config,
        from_index,
        drop_unmatched_keys,
    ):
        final_config = {
            key: copy.deepcopy(original_config[key])
            for key in original_config
            if key != "model"
        }
        final_config["model"] = super().post_config_convert(
            original_config,
            old_config,
            new_config,
            from_index,
            drop_unmatched_keys,
        )

        # delete the cerebras key (including the save_iter_state_path key)
        final_config.pop("cerebras", "")

        format_versions = self.formats()
        to_index = 1 - from_index

        # When converting configs, remove runconfig params that have been deprecated across releases
        if "runconfig" in final_config and (
            "cs-1.9" in format_versions[from_index]
            or "cs-2.0" in format_versions[from_index]
        ):
            self._remove_deprecated_runconfig_params(final_config["runconfig"])

        # csconfig is an old section in the configs that's not been used since 1.9.
        if "csconfig" in final_config and (
            "cs-1.9" in format_versions[from_index]
            or "cs-2.0" in format_versions[from_index]
        ):
            del final_config["csconfig"]

        # In CS 2.0, some optimizer and LRS params were renamed and/or refactored to match the
        # vanilla PyTorch API. However, the old values were still accepted. In CS 2.1, these params
        # are no longer accepted. This base class takes care of applying the rename/refactor to such
        # params when moving from CS 2.0 to CS 2.1.
        if (
            "cs-2.0" in format_versions[from_index]
            and "cs-2.1" in format_versions[to_index]
        ):
            if "optimizer" in final_config:
                self._apply_optimizer_transforms(final_config["optimizer"])
                self._apply_lrs_transforms(final_config["optimizer"])

            self._apply_pol_transforms(final_config)
            self._apply_fp16_transforms(final_config["model"])

        return final_config

    def _remove_deprecated_runconfig_params(self, config):
        """Deletes deprecated runconfig params when moving from 1.9 or 2.0."""
        if "num_act_servers" in config or "num_wgt_servers" in config:
            logging.warning(
                "Removing 'num_act_servers' or 'num_wgt_servers' found in runconfig. "
                "Release 2.0 and later chooses optimal values for these params."
            )
        keys_to_delete = [
            "experimental_api",
            "multireplica",
            "num_replicas",
            "save_losses",
            "service_dir",
            "use_appliance_data",
            "num_act_servers",
            "num_wgt_servers",
        ]
        for key in keys_to_delete:
            config.pop(key, None)

        # if is_pretrained_checkpoint was True, replace it with load_checkpoint_states
        if config.pop("is_pretrained_checkpoint", False):
            config["load_checkpoint_states"] = ["model", "dataloader"]

        # fixing unused parameter keep_checkpoint_max to use intended parameter max_checkpoints
        max_checkpoints = config.pop("keep_checkpoint_max", None)
        if max_checkpoints is not None and "max_checkpoints" not in config:
            config["max_checkpoints"] = max_checkpoints

    def _apply_pol_transforms(self, config):
        if "precision_opt_level" in config["model"] and "runconfig" in config:
            # Only override runconfig if it doesn't already exist
            if "precision_opt_level" not in config["runconfig"]:
                config["runconfig"]["precision_opt_level"] = config["model"][
                    "precision_opt_level"
                ]
            # Pop POL from model since it's no longer supported
            config["model"].pop("precision_opt_level")

    def _apply_fp16_transforms(self, config):
        if "use_bfloat16" in config:
            config["fp16_type"] = (
                "bfloat16" if config["use_bfloat16"] else "float16"
            )
            # Pop use_bfloat16 from model since it's no longer supported
            config.pop("use_bfloat16")

    def _apply_optimizer_transforms(self, config):
        optimizer_type = config.get("optimizer_type", None)
        if not optimizer_type:
            return

        optimizer_map = self._get_optimizer_lrs_signatures()["optimizers"]

        if optimizer_type.lower() not in optimizer_map:
            return

        cls_signature: inspect.Signature = optimizer_map[optimizer_type.lower()]

        aliases = {
            "weight_decay": ["weight_decay_rate"],
            "betas": [["beta1", "beta2"]],
            "eps": [["eps1", "eps2"]],
            "etas": [["eta1", "eta2"]],
            "step_sizes": [["step_size_min", "step_size_max"]],
        }

        # Replace all aliases with the new key
        for name in cls_signature.parameters.keys():
            if (
                name
                not in ("self", "params", "lr", "learning_rate", *config.keys())
                and name in aliases
            ):
                for alias in aliases[name]:
                    if isinstance(alias, str) and alias in config:
                        config[name] = config.pop(alias)
                        break
                    elif isinstance(alias, (list, tuple)) and all(
                        a in config for a in alias
                    ):
                        config[name] = type(alias)(config.pop(a) for a in alias)
                        break

    def _apply_lrs_transforms(self, config):
        learning_rate = config.get("learning_rate", None)

        if isinstance(learning_rate, dict):
            learning_rate_dicts = [learning_rate]
        elif isinstance(learning_rate, (list, tuple)):
            learning_rate_dicts = learning_rate
        else:
            return

        lr_scheduler_map = self._get_optimizer_lrs_signatures()["lr_schedulers"]

        # common aliases
        aliases = {
            "total_iters": ["steps", "decay_steps"],
            "initial_learning_rate": ["learning_rate", "base_lr"],
            "base_lr": ["learning_rate", "initial_learning_rate"],
            "learning_rates": ["values"],
            "milestones": ["boundaries"],
            "T_max": ["t_max"],
            "T_0": ["t_0"],
            "T_mult": ["t_mult"],
        }

        # Replace all aliases with the new key
        for lr_params in learning_rate_dicts:
            scheduler = lr_params.get("scheduler").lower()

            for name in (scheduler, f"{scheduler}lr"):
                if name in lr_scheduler_map:
                    cls_signature: inspect.Signature = lr_scheduler_map[name]
                    break
            else:
                continue

            for name in cls_signature.parameters.keys():
                if (
                    name not in ("self", "optimizer", *lr_params.keys())
                    and name in aliases
                ):
                    for alias in aliases[name]:
                        if alias in lr_params:
                            lr_params[name] = lr_params.pop(alias)
                            break

    def _get_optimizer_lrs_signatures(self):
        artifact_dir = get_artifact_dir("cs-2.0")
        with open(artifact_dir / "optimizer_lrs_signatures.pkl", "rb") as f:
            signatures = pickle.load(f)
        return signatures


def _addindent(s_, numSpaces):
    s = s_.split('\n')
    s = [(numSpaces * ' ') + line for line in s]
    s = '\n'.join(s)
    return s


def get_artifact_dir(version) -> Path:
    artifact_dir: Path = Path(__file__).parent / "artifacts" / version
    if not artifact_dir.exists():
        raise NotADirectoryError(f"{artifact_dir} is not a directory.")
    return artifact_dir


def convert_dataloader_checkpoint(
    checkpoint_state_dict: dict,
    data_checkpoints_dir: str,
    dataloader_type: str = "map",
    shuffle_seed: int = 0,
):
    """Converts DataLoader state files saved in release 1.9 to DataLoader checkpoint format for the 
    new map and iterable DataLoaders in MZ in release 2.0. This is useful to provide backwards 
    comptability for deterministic restart on 2.0 runs from old dataloader state files.

    Args:
        checkpoint_state_dict: the state_dict of the 1.9 checkpoint
        data_checkpoints_dir: Path to directory containing data step file 
            `data_iter_checkpoint_state_file_global` and worker checkpoint files of the format 
            `data_iter_state_file_worker_*_step_*.txt`
        dataloader_type: The MZ DataLoader for which state is being converted. Use `map` for the 
            map-style dataloader and `iterable` for the iterable-style dataloader. Defaults to 
            map-style dataloader.
        shuffle_seed: The seed value to be captured in the DataLoader state for the map-style 
            dataloader. Note that the seed is only relevant for deterministically restarting the 
            map-style dataloader if dataset shuffling/mixing is enabled.
    """
    if "dataloader" in checkpoint_state_dict:
        logging.warning(
            "DataLoader state already exists in the checkpoint. R1.9 DataLoader state specified "
            "under config `cerebras.save_iter_state_path` will not be injected into the checkpoint."
        )
        return

    # Check if data_checkpoints_dir contains file specifying the data step
    global_data_iter_state_file = os.path.join(
        data_checkpoints_dir, "data_iter_checkpoint_state_file_global"
    )
    assert os.path.isfile(global_data_iter_state_file), (
        f"File `{global_data_iter_state_file}` does not exist. "
        f"Please ensure that the specified dir `{data_checkpoints_dir}` "
        "has file `data_iter_checkpoint_state_file_global` that records "
        "the data step for the R1.9 Dataloader state being converted."
    )

    # Read the data step
    with open(global_data_iter_state_file, "r") as f:
        data_step = int(f.readline())

    total_samples_streamed = 0
    wrk_states = []

    dir = Path(data_checkpoints_dir)

    for f in os.listdir(dir):
        if f.endswith('.txt'):
            # WRK data iter files names follow the format:
            # `data_iter_state_file_worker_{wrk_id}_step_{step}.txt`
            # where `wrk_id` and `step` are ints.
            file_name_split = f.split('_')
            wrk_id = int(file_name_split[-3])
            step = int(file_name_split[-1].split('.')[0])

            # Each worker will have a single checkpoint at the data step
            if step == data_step:
                wrk_ckpt_file = os.path.join(data_checkpoints_dir, f)

                with open(wrk_ckpt_file, "r") as ckpt:
                    samples_streamed = int(ckpt.readline())
                    if dataloader_type == "iterable":
                        wrk_state_dict = {
                            "samples_streamed": samples_streamed,
                            "shard_index": wrk_id,
                        }
                        wrk_states.append(wrk_state_dict)
                    total_samples_streamed += samples_streamed

    if (
        dataloader_type == "map"
    ):  # State dict aggregation of map-style dataloader
        aggregated_state_dict = {
            "samples_streamed": total_samples_streamed,
            "seed": shuffle_seed,
        }
    else:  # State dict aggregation of iterable-style dataloader
        wrk_states.sort(key=lambda x: x["shard_index"])
        aggregated_state_dict = {"all_worker_states": wrk_states}

    # Add DL state to previously loaded checkpoint state dict
    checkpoint_state_dict["dataloader"] = aggregated_state_dict
