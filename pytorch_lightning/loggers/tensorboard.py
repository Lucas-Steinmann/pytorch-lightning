# Copyright The PyTorch Lightning team.
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

"""
TensorBoard
-----------
"""

import os
from argparse import Namespace
from typing import Optional, Dict, Union, Any
from warnings import warn

import torch
from pkg_resources import parse_version
from torch.utils.tensorboard import SummaryWriter

from pytorch_lightning import _logger as log
from pytorch_lightning.core.saving import save_hparams_to_yaml
from pytorch_lightning.loggers.base import LightningLoggerBase, rank_zero_experiment
from pytorch_lightning.utilities import rank_zero_only, rank_zero_warn
from pytorch_lightning.utilities.cloud_io import gfile, makedirs
from pytorch_lightning.core.lightning import LightningModule

try:
    from omegaconf import Container, OmegaConf
except ImportError:
    OMEGACONF_AVAILABLE = False
else:
    OMEGACONF_AVAILABLE = True


class TensorBoardLogger(LightningLoggerBase):
    r"""
    Log to local file system in `TensorBoard <https://www.tensorflow.org/tensorboard>`_ format.
    Implemented using :class:`~torch.utils.tensorboard.SummaryWriter`. Logs are saved to
    ``os.path.join(save_dir, name, version)``. This is the default logger in Lightning, it comes
    preinstalled.

    Example:
        >>> from pytorch_lightning import Trainer
        >>> from pytorch_lightning.loggers import TensorBoardLogger
        >>> logger = TensorBoardLogger("tb_logs", name="my_model")
        >>> trainer = Trainer(logger=logger)

    Args:
        save_dir: Save directory
        name: Experiment name. Defaults to ``'default'``. If it is the empty string then no per-experiment
            subdirectory is used.
        version: Experiment version. If version is not specified the logger inspects the save
            directory for existing versions, then automatically assigns the next available version.
            If it is a string then it is used as the run-specific subdirectory name,
            otherwise ``'version_${version}'`` is used.
        log_graph: Adds the computational graph to tensorboard. This requires that
            the user has defined the `self.example_input_array` attribute in their
            model.
        default_hp_metric: Enables a placeholder metric with key `hp_metric` when `log_hyperparams` is
            called without a metric (otherwise calls to log_hyperparams without a metric are ignored).
        \**kwargs: Other arguments are passed directly to the :class:`SummaryWriter` constructor.

    """
    NAME_HPARAMS_FILE = 'hparams.yaml'

    def __init__(
        self,
        save_dir: str,
        name: Optional[str] = "default",
        version: Optional[Union[int, str]] = None,
        log_graph: bool = False,
        default_hp_metric: bool = True,
        **kwargs
    ):
        super().__init__()
        self._save_dir = save_dir
        self._name = name or ''
        self._version = version
        self._log_graph = log_graph
        self._default_hp_metric = default_hp_metric

        self._experiment = None
        self.hparams = {}
        self._kwargs = kwargs

    @property
    def root_dir(self) -> str:
        """
        Parent directory for all tensorboard checkpoint subdirectories.
        If the experiment name parameter is ``None`` or the empty string, no experiment subdirectory is used
        and the checkpoint will be saved in "save_dir/version_dir"
        """
        if self.name is None or len(self.name) == 0:
            return self.save_dir
        else:
            return os.path.join(self.save_dir, self.name)

    @property
    def log_dir(self) -> str:
        """
        The directory for this run's tensorboard checkpoint. By default, it is named
        ``'version_${self.version}'`` but it can be overridden by passing a string value
        for the constructor's version parameter instead of ``None`` or an int.
        """
        # create a pseudo standard path ala test-tube
        version = self.version if isinstance(self.version, str) else f"version_{self.version}"
        log_dir = os.path.join(self.root_dir, version)
        return log_dir

    @property
    def save_dir(self) -> Optional[str]:
        return self._save_dir

    @property
    @rank_zero_experiment
    def experiment(self) -> SummaryWriter:
        r"""
        Actual tensorboard object. To use TensorBoard features in your
        :class:`~pytorch_lightning.core.lightning.LightningModule` do the following.

        Example::

            self.logger.experiment.some_tensorboard_function()

        """
        if self._experiment is not None:
            return self._experiment

        assert rank_zero_only.rank == 0, 'tried to init log dirs in non global_rank=0'
        if self.root_dir and not gfile.exists(str(self.root_dir)):
            makedirs(self.root_dir)
        self._experiment = SummaryWriter(log_dir=self.log_dir, **self._kwargs)
        return self._experiment

    @rank_zero_only
    def log_hyperparams(self, params: Union[Dict[str, Any], Namespace],
                        metrics: Optional[Dict[str, Any]] = None) -> None:
        params = self._convert_params(params)

        # store params to output
        if OMEGACONF_AVAILABLE and isinstance(params, Container):
            self.hparams = OmegaConf.merge(self.hparams, params)
        else:
            self.hparams.update(params)

        # format params into the suitable for tensorboard
        params = self._flatten_dict(params)
        params = self._sanitize_params(params)

        if parse_version(torch.__version__) < parse_version("1.3.0"):
            warn(
                f"Hyperparameter logging is not available for Torch version {torch.__version__}."
                " Skipping log_hyperparams. Upgrade to Torch 1.3.0 or above to enable"
                " hyperparameter logging."
            )
        else:
            from torch.utils.tensorboard.summary import hparams

            if metrics is None:
                if self._default_hp_metric:
                    metrics = {"hp_metric": -1}
            elif not isinstance(metrics, dict):
                metrics = {"hp_metric": metrics}

            if metrics:
                self.log_metrics(metrics, 0)
                exp, ssi, sei = hparams(params, metrics)
                writer = self.experiment._get_file_writer()
                writer.add_summary(exp)
                writer.add_summary(ssi)
                writer.add_summary(sei)

    @rank_zero_only
    def log_metrics(self, metrics: Dict[str, float], step: Optional[int] = None) -> None:
        assert rank_zero_only.rank == 0, 'experiment tried to log from global_rank != 0'

        for k, v in metrics.items():
            if isinstance(v, torch.Tensor):
                v = v.item()
            self.experiment.add_scalar(k, v, step)

    @rank_zero_only
    def log_graph(self, model: LightningModule, input_array=None):
        if self._log_graph:
            if input_array is None:
                input_array = model.example_input_array

            if input_array is not None:
                input_array = model.transfer_batch_to_device(input_array, model.device)
                self.experiment.add_graph(model, input_array)
            else:
                rank_zero_warn('Could not log computational graph since the'
                               ' `model.example_input_array` attribute is not set'
                               ' or `input_array` was not given',
                               UserWarning)

    @rank_zero_only
    def save(self) -> None:
        super().save()
        dir_path = self.log_dir
        if not gfile.isdir(dir_path):
            dir_path = self.save_dir

        # prepare the file path
        hparams_file = os.path.join(dir_path, self.NAME_HPARAMS_FILE)

        # save the metatags file
        save_hparams_to_yaml(hparams_file, self.hparams)

    @rank_zero_only
    def finalize(self, status: str) -> None:
        self.save()

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> int:
        if self._version is None:
            self._version = self._get_next_version()
        return self._version

    def _get_next_version(self):
        root_dir = os.path.join(self.save_dir, self.name)

        if not gfile.isdir(root_dir):
            log.warning('Missing logger folder: %s', root_dir)
            return 0

        existing_versions = []
        for d in gfile.listdir(root_dir):
            if gfile.isdir(os.path.join(root_dir, d)) and d.startswith("version_"):
                existing_versions.append(int(d.split("_")[1]))

        if len(existing_versions) == 0:
            return 0

        return max(existing_versions) + 1

    def __getstate__(self):
        state = self.__dict__.copy()
        state["_experiment"] = None
        return state
