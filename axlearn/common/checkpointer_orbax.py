# Copyright © 2024 Apple Inc.

"""Checkpointing utilities using orbax.

See also checkpointer.py for other checkpointing utilities and checkpointer_test.py for tests.
"""

import asyncio
import copy
import dataclasses
import functools
from concurrent import futures
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import jax
import orbax.checkpoint as ocp
import tensorflow as tf
from absl import logging

from axlearn.common import utils
from axlearn.common.checkpointer import (
    STEP_NUM_DIGITS,
    STEP_PREFIX,
    BaseCheckpointer,
    CheckpointValidationType,
    check_state_structure,
    restore_tf_savables,
    save_tf_savables,
)
from axlearn.common.config import config_class
from axlearn.common.module import Module
from axlearn.common.utils import Nested, Tensor, TensorSpec


class _TfIteratorHandler(ocp.pytree_checkpoint_handler.TypeHandler):
    """Serializes tf.data.Iterator.

    Reference:
    https://orbax.readthedocs.io/en/latest/custom_handlers.html#custom-serialization-deserialization

    Note that since handlers are global instances (potentially shared by multiple checkpointer
    instances), we construct and cleanup the executor per-serialize/deserialize call.
    """

    # Must be a subclass of RestoreArgs for `PyTreeRestore` to recognize it.
    @dataclasses.dataclass
    class RestoreArgs(ocp.type_handlers.RestoreArgs):
        item: Optional[tf.data.Iterator] = None

    def typestr(self) -> str:
        return "TfIterator"

    async def serialize(
        self,
        values: Sequence[tf.data.Iterator],
        infos: Sequence[ocp.type_handlers.ParamInfo],
        args: Optional[Sequence[ocp.args.PyTreeSave]],
    ) -> List[futures.Future]:
        """Serializes `values` into corresponding `info.path`s."""
        del args  # Unused.
        futs = []
        with futures.ThreadPoolExecutor(max_workers=1) as executor:
            for value, info in zip(values, infos):
                # Make sure the per-key directory is present as OCDBT doesn't create one.
                info.path.mkdir(exist_ok=True)
                futs.append(
                    executor.submit(functools.partial(save_tf_savables, value, dir=info.path))
                )
        return futs

    async def deserialize(
        self,
        infos: Sequence[ocp.type_handlers.ParamInfo],
        args: Optional[Sequence[RestoreArgs]] = None,
    ) -> tf.data.Iterator:
        if args is None:
            raise ValueError(f"{self.RestoreArgs.__name__} should be supplied as args.")
        with futures.ThreadPoolExecutor(max_workers=1) as executor:
            futs = [
                asyncio.get_event_loop().run_in_executor(
                    executor,
                    functools.partial(restore_tf_savables, arg.item, dir=info.path),
                )
                for arg, info in zip(args, infos)
            ]
        return await asyncio.gather(*futs)

    async def metadata(
        self, infos: Sequence[ocp.type_handlers.ParamInfo]
    ) -> Sequence[ocp.metadata.Metadata]:
        return [ocp.metadata.Metadata(name=info.name, directory=info.path) for info in infos]


ocp.type_handlers.register_type_handler(tf.data.Iterator, _TfIteratorHandler(), override=True)


class OrbaxCheckpointer(BaseCheckpointer):
    """A checkpointer that uses orbax CheckpointManager.

    NOTE: While this class uses index files to do additional validation on checkpoint state, the
    index file is not used for committing checkpoints (i.e., it is not reliable to check for the
    presence of 'index' file). In Orbax, checkpoints are committed differently on different
    filesystems. If the filesystem supports atomic renames, un-committed checkpoints are represented
    by a "tmp" prefix. OTOH, if the filesystem does not support atomic renames (such as GCS as of
    writing), committed checkpoints are represented by a commit file with the name
    "commit_success.txt". Thus, an incomplete checkpoint can still contain an "index" file (note
    that valid checkpoints should always contain an "index" file). In general, users should instead
    use `checkpoint_paths` to identify committed checkpoints.
    """

    @config_class
    class Config(BaseCheckpointer.Config):
        """Configures OrbaxCheckpointer.

        Attributes:
            keep_last_n: Keep this many past ckpts.
            validation_type: Checkpoint validation during restore.
            async_timeout_secs: Timeout for async barrier in seconds.
        """

        keep_last_n: int = 1
        validation_type: CheckpointValidationType = CheckpointValidationType.EXACT
        async_timeout_secs: int = 300

    @classmethod
    def checkpoint_paths(cls, base_dir: str) -> List[str]:
        """See `BaseCheckpointer.checkpointer_paths`."""
        return [str(path) for path in ocp.utils.checkpoint_steps_paths(base_dir)]

    def __init__(self, cfg: Config, *, parent: Optional[Module]):
        super().__init__(cfg, parent=parent)

        cfg: OrbaxCheckpointer.Config = self.config
        save_policy = cfg.save_policy.instantiate()

        # self._eval_summaries will be set in save() and used by save_fn_with_summaries() to decide
        # whether to save at the step.
        #
        # We need this because Orbax only provides (current_step, last_saved_step) as args to the
        # save policy, so we use `self._eval_summaries` to capture evaler summaries.
        #
        # While we can check the eval summaries in `save()`, some Orbax features like
        # save-on-preemption requires the user to call `ocp.CheckpointManager.save()` at every step,
        # even if the verdict from `cfg.save_policy` is negative.
        self._eval_summaries = None

        def save_fn_with_summaries(step: int, last_saved_step: Optional[int]) -> bool:
            del last_saved_step
            return save_policy(step=step, evaler_summaries=self._eval_summaries)

        self._name_format = ocp.step.standard_name_format(
            step_prefix=STEP_PREFIX,
            step_format_fixed_length=STEP_NUM_DIGITS,
        )
        self._manager = ocp.CheckpointManager(
            directory=cfg.dir,
            options=ocp.CheckpointManagerOptions(
                create=True,
                max_to_keep=cfg.keep_last_n,
                enable_async_checkpointing=True,
                step_name_format=self._name_format,
                should_save_fn=save_fn_with_summaries,
                enable_background_delete=True,
                async_options=ocp.options.AsyncOptions(timeout_secs=cfg.async_timeout_secs),
            ),
            item_handlers={
                # NOTE: we make a relatively weak assumption that index files are JSON serialized
                # for simplicity. The test cases ensure that this is compatible with
                # `read_index_file`.
                "index": ocp.JsonCheckpointHandler(filename="index"),
                # TODO(markblee): Add save/restore_concurrent_gb when available.
                # Note that this defaults to use_ocdb=True. Note also that custom `TypeHandler`s are
                # ignored by `StandardCheckpointHandler`, so we use `PyTreeCheckpointHandler`.
                "state": ocp.PyTreeCheckpointHandler(),
            },
        )

    def _get_spec(self, *, step: int, state: Nested[Any]) -> Nested[Any]:
        spec = {"index": [("step", step)]}
        for path, value in utils.flatten_items(state):
            if isinstance(value, (Tensor, TensorSpec)):
                dtype = getattr(value.dtype, "dtype", value.dtype)
                spec["index"].append(
                    (path, {"dtype": str(dtype), "shape": str(tuple(value.shape))})
                )
            elif isinstance(value, tf.data.Iterator):
                spec["index"].append((path, str(type(value))))
            else:
                spec["index"].append((path, value))
        return spec

    # pylint: disable-next=redefined-builtin
    def ckpt_dir(self, step: int, dir: Optional[str] = None) -> str:
        """Obtains the checkpoint dir for the given step."""
        if dir is None:
            dir = self._manager.directory
        return str(ocp.step.build_step_path(dir, self._name_format, step))

    def save(
        self, *, step: int, state: Nested[Tensor], evaler_summaries: Optional[Dict[str, Any]] = None
    ):
        """See `BaseCheckpointer.save` for details.

        Checkpoint saving is handled by `orbax` checkpoint manager.
        """
        spec = self._get_spec(step=step, state=state)
        assert self._eval_summaries is None, self._eval_summaries
        self._eval_summaries = copy.deepcopy(evaler_summaries or {})
        try:
            # Note that save() waits for prior serialization to finish.
            self._manager.save(
                step=step,
                # The input iterator is saved as part of `save_tf_savables`.
                args=ocp.args.Composite(
                    index=ocp.args.JsonSave(spec["index"]),
                    # TODO(markblee): Investigate save_args for chunk_byte_size and
                    # ocdbt_target_data_file_size:
                    # https://orbax.readthedocs.io/en/latest/optimized_checkpointing.html#custom-chunk-sizes
                    # https://orbax.readthedocs.io/en/latest/optimized_checkpointing.html#customizing-data-file-size
                    state=ocp.args.PyTreeSave(item=state),
                ),
            )
            # Exit early after pre-emption, equivalent to sys.exit():
            # https://orbax.readthedocs.io/en/latest/preemption_checkpointing.html
            if self._manager.reached_preemption(step):
                self._manager.wait_until_finished()
                raise SystemExit(f"Exiting after saving checkpoint at {step=} due to pre-emption.")
        finally:
            self._eval_summaries = None

    def restore(
        self,
        *,
        step: Optional[int] = None,
        state: Union[Nested[Tensor], Nested[TensorSpec]],
    ) -> Tuple[Optional[int], Nested[Tensor]]:
        """See `BaseCheckpointer.restore` for details."""

        cfg: OrbaxCheckpointer.Config = self.config

        def _restore_args(x: Any) -> ocp.RestoreArgs:
            if isinstance(x, (Tensor, TensorSpec)):
                return ocp.checkpoint_utils.construct_restore_args(
                    jax.ShapeDtypeStruct(shape=x.shape, dtype=x.dtype, sharding=x.sharding)
                )
            elif isinstance(x, tf.data.Iterator):
                return _TfIteratorHandler.RestoreArgs(item=x)
            else:
                return None

        restore_args = jax.tree_util.tree_map(_restore_args, state)

        try:
            composite_state = self._manager.restore(
                step,
                args=ocp.args.Composite(
                    index=ocp.args.JsonRestore(None),
                    state=ocp.args.PyTreeRestore(item=state, restore_args=restore_args),
                ),
            )
        except TypeError as e:
            # Orbax hits TypeError if there are no checkpoints, since it attempts to format `None`
            # as the step dir.
            if step is not None:
                raise ValueError(f"Failed to restore at step {step}.") from e
            logging.info("Could not find any completed checkpoints under %s: %s", cfg.dir, e)
            return None, state  # Return the input state.

        restored_index = composite_state["index"]
        restored_state = composite_state["state"]

        # If we successfully restored from step=None, use the restored step.
        if step is None:
            for k, v in restored_index:
                if k == "step":
                    step = v
                    break

        # Validate ckpt structure.
        check_state_structure(
            restored_index,
            target_structure=self._get_spec(step=step, state=state)["index"],
            validation=cfg.validation_type,
        )
        return step, restored_state

    def wait_until_finished(self):
        """See `BaseCheckpointer.wait_until_finished` docstring for details."""
        self._manager.wait_until_finished()

    def stop(self):
        """See `BaseCheckpointer.stop` for details."""
        self._manager.close()