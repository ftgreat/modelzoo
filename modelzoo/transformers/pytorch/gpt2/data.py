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

import sys

from modelzoo.transformers.pytorch.gpt2.input.HuggingFaceDataProcessorEli5 import (  # noqa
    HuggingFaceDataProcessorEli5,
)
from modelzoo.transformers.pytorch.gpt2.input.HuggingFaceIterableDataProcessorEli5 import (  # noqa
    HuggingFaceIterableDataProcessorEli5,
)

from modelzoo.transformers.pytorch.gpt2.input.DummyDataProcessor import (  # noqa
    DummyDataProcessor,
)
from modelzoo.transformers.pytorch.gpt2.input.DummyIterableDataProcessor import (  # noqa
    DummyIterableDataProcessor,
)

from modelzoo.transformers.pytorch.gpt2.input.GptHDF5DataProcessor import (  # noqa
    GptHDF5DataProcessor,
)
from modelzoo.transformers.pytorch.gpt2.input.GptTextDataProcessor import (  # noqa
    GptTextDataProcessor,
)
from modelzoo.transformers.pytorch.gpt2.input.GptHDF5MapDataProcessor import (  # noqa
    GptHDF5MapDataProcessor,
)


def train_input_dataloader(params):
    return getattr(
        sys.modules[__name__], params["train_input"]["data_processor"]
    )(params["train_input"]).create_dataloader()


def eval_input_dataloader(params):
    return getattr(
        sys.modules[__name__], params["eval_input"]["data_processor"]
    )(params["eval_input"]).create_dataloader()


def inference_input_dataloader(params):
    return getattr(
        sys.modules[__name__], params["inference_input"]["data_processor"]
    )(params["inference_input"]).create_dataloader()
