#  ------------------------------------------------------------------------------------------
#  Copyright (c) Microsoft Corporation. All rights reserved.
#  Licensed under the MIT License (MIT). See LICENSE in the repo root for license information.
#  ------------------------------------------------------------------------------------------
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import conda_merge
import ruamel.yaml
from azureml._restclient.constants import RunStatus
from azureml.core import Experiment, Run, Workspace, get_run
from azureml.core._serialization_utils import _serialize_to_dict
from azureml.core.conda_dependencies import CondaDependencies
from azureml.train.estimator import Estimator

from InnerEye.Common import fixed_paths

DEFAULT_CROSS_VALIDATION_SPLIT_INDEX = -1
EXPERIMENT_RUN_SEPARATOR = ":"
EFFECTIVE_RANDOM_SEED_KEY_NAME = "effective_random_seed"
NUMBER_OF_CROSS_VALIDATION_SPLITS_PER_FOLD_KEY_NAME = "number_of_cross_validation_splits_per_fold"
RUN_RECOVERY_ID_KEY_NAME = "run_recovery_id"
RUN_RECOVERY_FROM_ID_KEY_NAME = "recovered_from"
IS_ENSEMBLE_KEY_NAME = "is_ensemble"
MODEL_ID_KEY_NAME = "model_id"
# The name of the key used to store the cross validation index of the dataset for the run
CROSS_VALIDATION_SPLIT_INDEX_TAG_KEY = "cross_validation_split_index"
CROSS_VALIDATION_SUB_FOLD_SPLIT_INDEX_TAG_KEY = "cross_validation_sub_fold_split_index"
PARENT_RUN_ID_KEY_NAME = "parent_run_id"

# This is the folder structure that AzureML generates to store all results for an experiment run.
# azureml is the name of the container
AZUREML_RUN_FOLDER_PREFIX = "dcid."
AZUREML_RUN_FOLDER = "azureml/ExperimentRun/" + AZUREML_RUN_FOLDER_PREFIX

# Global variables for the Run context, to avoid repeated HTTP calls to get it.
RUN_CONTEXT = Run.get_context()
# The Run context of the Hyperdrive parent run. This must be cached to avoid issues with the AzureML SDK,
# which creates worker pools for each call to .parent.
PARENT_RUN_CONTEXT = getattr(RUN_CONTEXT, "parent", None)

INNEREYE_SDK_NAME = "innereye"
INNEREYE_SDK_VERSION = "1.0"


def get_results_blob_path(run_id: str) -> str:
    """
    Creates the name of the top level folder that contains the results for a given AzureML run.
    :param run_id: The AzureML run ID for which the folder should be created.
    :return: A full Azure blob storage path, starting with the container name.
    """
    return AZUREML_RUN_FOLDER + run_id


def create_run_recovery_id(run: Run) -> str:
    """
   Creates an recovery id for a run so it's checkpoints could be recovered for training/testing

   :param run: an instantiated run.
   :return: recovery id for a given run in format: [experiment name]:[run id]
   """
    return str(run.experiment.name + EXPERIMENT_RUN_SEPARATOR + run.id)


def split_recovery_id(id: str) -> Tuple[str, str]:
    """
    Splits a run ID into the experiment name and the actual run.
    The argument can be in the format 'experiment_name:run_id',
    or just a run ID like user_branch_abcde12_123. In the latter case, everything before the last
    two alphanumeric parts is assumed to be the experiment name.
    :param id:
    :return: experiment name and run name
    """
    components = id.strip().split(EXPERIMENT_RUN_SEPARATOR)
    if len(components) > 2:
        raise ValueError("recovery_id must be in the format: 'experiment_name:run_id', but got: {}".format(id))
    elif len(components) == 2:
        return components[0], components[1]
    else:
        recovery_id_regex = r"^(\w+)_\d+_[0-9a-f]+$|^(\w+)_\d+$"
        match = re.match(recovery_id_regex, id)
        if not match:
            raise ValueError("The recovery ID was not in the expected format: {}".format(id))
        return (match.group(1) or match.group(2)), id


def fetch_run(workspace: Workspace, run_recovery_id: str) -> Run:
    """
    Finds an existing run in an experiment, based on a recovery ID that contains the experiment ID
    and the actual RunId. The run can be specified either in the experiment_name:run_id format,
    or just the run_id.
    :param workspace: the configured AzureML workspace to search for the experiment.
    :param run_recovery_id: The Run to find. Either in the full recovery ID format, experiment_name:run_id
    or just the run_id
    :return: The AzureML run.
    """
    experiment, run = split_recovery_id(run_recovery_id)
    try:
        experiment_to_recover = Experiment(workspace, experiment)
    except Exception:
        raise (Exception("Unable to retrieve experiment {}".format(experiment)))
    run_to_recover = fetch_run_for_experiment(experiment_to_recover, run)
    logging.info("Fetched run #{} {} from experiment {}.".format(run, run_to_recover.number, experiment))
    return run_to_recover


def fetch_run_for_experiment(experiment_to_recover: Experiment, run_id_or_number: str) -> Run:
    """
    :param experiment_to_recover: an experiment
    :param run_id_or_number: a string representing the Run ID or Run Number of one of the runs of the experiment
    :return: the run matching run_id_or_number; raises an exception if not found
    """
    available_runs = experiment_to_recover.get_runs()
    try:
        run_number = int(run_id_or_number)
        for run in available_runs:
            if run.number == run_number:
                return run
    except ValueError:
        # will be raised in run_id_or_number does not represent a number
        pass
    try:
        return get_run(experiment=experiment_to_recover, run_id=run_id_or_number, rehydrate=True)
    except Exception:
        available_ids = ", ".join([run.id for run in available_runs])
        raise (Exception(
            "Run {} not found for experiment: {}. Available runs are: {}".format(
                run_id_or_number, experiment_to_recover.name, available_ids)))


def fetch_runs(experiment: Experiment, filters: List[str]) -> List[Run]:
    """
    Fetch the runs in an experiment.
    :param experiment: the experiment to fetch runs from
    :param filters: a list of run status to include. Must be subset of [Running, Completed, Failed, Canceled].
    :return: the list of runs in the experiment
    """
    exp_runs = list(experiment.get_runs())

    if len(filters) != 0:
        if set.issubset(set(filters), ["Running", "Completed", "Failed", "Canceled"]):
            runs = [run for run in exp_runs if run.status in filters]
            exp_runs = runs

    return exp_runs


def fetch_child_runs(run: Run, status: Optional[str] = None,
                     expected_number_cross_validation_splits: int = 0) -> List[Run]:
    """
    Fetch child runs for the provided runs that have the provided AML status (or fetch all by default)
    and have a run_recovery_id tag value set (this is to ignore superfluous AML infrastructure platform runs).
    :param run: parent run to fetch child run from
    :param status: if provided, returns only child runs with this status
    :param expected_number_cross_validation_splits: when recovering child runs from AML hyperdrive
    sometimes the get_children function fails to retrieve all children. If the number of child runs
    retrieved by AML is lower than the expected number of splits, we try to retrieve them manually.
    """
    if is_ensemble_run(run):
        run_recovery_id = run.get_tags().get(RUN_RECOVERY_FROM_ID_KEY_NAME, None)
        if run_recovery_id:
            run = fetch_run(run.experiment.workspace, run_recovery_id)
        elif PARENT_RUN_CONTEXT:
            run = PARENT_RUN_CONTEXT
    children_runs = list(run.get_children(tags=RUN_RECOVERY_ID_KEY_NAME))
    if 0 < expected_number_cross_validation_splits != len(children_runs):
        logging.warning(
            f"The expected number of child runs was {expected_number_cross_validation_splits}."
            f"Fetched only: {len(children_runs)} runs. Now trying to fetch them manually.")
        run_ids_to_evaluate = [f"{create_run_recovery_id(run)}_{i}"
                               for i in range(expected_number_cross_validation_splits)]
        children_runs = [fetch_run(run.experiment.workspace, id) for id in run_ids_to_evaluate]
    if status is not None:
        children_runs = [child_run for child_run in children_runs if child_run.get_status() == status]
    return children_runs


def is_ensemble_run(run: Run) -> bool:
    """Checks if the run was an ensemble of multiple models"""
    return run.get_tags().get(IS_ENSEMBLE_KEY_NAME) == 'True'


def update_run_tags(run: Run, tags: Dict[str, Any]) -> None:
    """Updates tags for the given run with the provided dictionary"""
    run.set_tags({**run.get_tags(), **tags})


def to_azure_friendly_string(x: Optional[str]) -> Optional[str]:
    """
    Given a string, ensure it can be used in Azure by replacing everything apart from a-zA-Z0-9_ with _,
    and replace multiple _ with a single _.
    """
    if x is None:
        return x
    else:
        return re.sub('_+', '_', re.sub(r'\W+', '_', x))


def estimator_to_string(estimator: Estimator) -> Optional[str]:
    """
    Convert a given AzureML estimator object to a string with its run configurations
    """
    return ruamel.yaml.round_trip_dump(_serialize_to_dict(estimator.run_config))


def to_azure_friendly_container_path(path: Path) -> str:
    """
    Converts a path an Azure friendly container path by replacing "\\", "//" with "/" so it can be in the form: a/b/c.
    :param path: Original path
    :return: Converted path
    """
    return str(path).replace("\\", "/").replace("//", "/").strip("/")


def is_offline_run_context(run_context: Run) -> bool:
    """
    Tells if a run_context is offline by checking if it has an experiment associated with it.
    :param run_context: Context of the run to check
    :return:
    """
    return not hasattr(run_context, 'experiment')


def get_run_context_or_default(run: Optional[Run] = None) -> Run:
    """
    Returns the context of the run, if run is not None. If run is None, returns the context of the current run.
    :param run: Run to retrieve context for. If None, retrieve ocntext of current run.
    :return: Run context
    """
    return run if run else Run.get_context()


def get_run_id(run: Optional[Run] = None) -> str:
    """
    Gets the id of a run handling both offline and online runs.
    :param run: If offline run, a Run object must be provided,
    for online runs if a run object is not provided the current run's context is used.
    :return: id of the run
    """
    run_context = get_run_context_or_default(run)
    if is_offline_run_context(run_context) and run:
        return run.id
    else:
        return run_context.id


def get_cross_validation_split_index(run: Run) -> int:
    """
    Gets the cross validation index from the run's tags or returns the default
    :param run: Run context from which to get index
    :return: The cross validation split index
    """
    if is_offline_run_context(run):
        return DEFAULT_CROSS_VALIDATION_SPLIT_INDEX
    else:
        return int(run.get_tags().get(CROSS_VALIDATION_SPLIT_INDEX_TAG_KEY, DEFAULT_CROSS_VALIDATION_SPLIT_INDEX))


def is_cross_validation_child_run(run: Run) -> bool:
    """
    Checks the provided run's tags to determine if it is a cross validation child run
    (which is the case if the split index >=0)
    :param run: Run to check.
    :return: True if cross validation run. False otherwise.
    """
    return get_cross_validation_split_index(run) > DEFAULT_CROSS_VALIDATION_SPLIT_INDEX


def strip_prefix(string: str, prefix: str) -> str:
    """
    Returns the string without the prefix if it has the prefix, otherwise the string unchanged.
    :param string: Input string.
    :param prefix: Prefix to remove from input string.
    :return: Input string with prefix removed.
    """
    if string.startswith(prefix):
        return string[len(prefix):]
    return string


def has_input_datasets(run_context: Any) -> bool:
    """
    Chceks if the run context has any input datasets defined.
    :param run_context: Run context to check.
    :return: True if the run context has any input datasets defined. False otherwise.
    """
    return hasattr(run_context, "input_datasets")


def _log_conda_dependencies_stats(conda: CondaDependencies, message_prefix: str) -> None:
    """
    Write number of conda and pip packages to logs.
    :param conda: A conda dependencies object
    :param message_prefix: A message to prefix to the log string.
    """
    conda_packages_count = len(list(conda.conda_packages))
    pip_packages_count = len(list(conda.pip_packages))
    logging.info(f"{message_prefix}: {conda_packages_count} conda packages, {pip_packages_count} pip packages")
    logging.debug("  Conda packages:")
    for p in conda.conda_packages:
        logging.debug(f"    {p}")
    logging.debug("  Pip packages:")
    for p in conda.pip_packages:
        logging.debug(f"    {p}")


def merge_conda_files(files: List[Path], result_file: Path) -> None:
    """
    Merges the given Conda environment files using the conda_merge package, and writes the merged file to disk.
    :param files: The Conda environment files to read.
    :param result_file: The location where the merge results should be written.
    """
    # This code is a slightly modified version of conda_merge. That code can't be re-used easily
    # it defaults to writing to stdout
    env_definitions = [conda_merge.read_file(str(f)) for f in files]
    unified_definition = {}
    NAME = "name"
    CHANNELS = "channels"
    DEPENDENCIES = "dependencies"
    name = conda_merge.merge_names(env.get(NAME) for env in env_definitions)
    if name:
        unified_definition[NAME] = name
    try:
        channels = conda_merge.merge_channels(env.get(CHANNELS) for env in env_definitions)
    except conda_merge.MergeError:
        logging.error("Failed to merge channel priorities.")
        raise
    if channels:
        unified_definition[CHANNELS] = channels
    deps = conda_merge.merge_dependencies(env.get(DEPENDENCIES) for env in env_definitions)
    if deps:
        unified_definition[DEPENDENCIES] = deps
    with result_file.open("w") as f:
        ruamel.yaml.dump(unified_definition, f, indent=2, default_flow_style=False)


def merge_conda_dependencies(files: List[Path]) -> CondaDependencies:
    """
    Creates a CondaDependencies object from the Conda environments specified in one or more files.
    The resulting object contains the union of the Conda and pip packages in the files, where merging
    is done via the conda_merge package.
    :param files: The Conda environment files to read.
    :return: A CondaDependencies object that contains packages from all the files.
    """
    for file in files:
        _log_conda_dependencies_stats(CondaDependencies(file), f"Conda environment in {file}")
    merged_file = tempfile.NamedTemporaryFile(delete=False)
    merge_conda_files(files, result_file=Path(merged_file.name))
    merged_dependencies = CondaDependencies(merged_file.name)
    _log_conda_dependencies_stats(merged_dependencies, "Merged Conda environment")
    merged_file.close()
    return merged_dependencies


def tag_values_all_distinct(runs: List[Run], tag: str) -> bool:
    """
    Returns True iff the runs all have the specified tag and all the values are different.
    """
    seen = set()
    for run in runs:
        value = run.get_tags().get(tag, None)
        if value is None or value in seen:
            return False
        seen.add(value)
    return True


def is_parent_run(run: Run) -> bool:
    return PARENT_RUN_CONTEXT and run.id == PARENT_RUN_CONTEXT.id


def download_outputs_from_run(blobs_path: Path,
                              destination: Path,
                              run: Optional[Run] = None,
                              is_file: bool = False,
                              append_prefix: bool = False) -> Path:
    """
    Download the blobs from the run's default output directory: DEFAULT_AML_UPLOAD_DIR.
    Silently returns for offline runs.
    :param blobs_path: Blobs path in DEFAULT_AML_UPLOAD_DIR to download from
    :param run: Run to download from (default to current run if None)
    :param destination: Local path to save the downloaded blobs to
    :param is_file: Set to True if downloading a single file.
    :param append_prefix: An optional flag whether to append the specified prefix from the final output file path.
    If False then the prefix is removed from the output file path.
    :return: Destination path to the downloaded file(s)
    """
    run = run or Run.get_context()
    blobs_root_path = str(fixed_paths.DEFAULT_AML_UPLOAD_DIR / blobs_path)
    if is_file:
        destination = destination / blobs_path.name
        logging.info(f"Downloading single file from run {run.id}: {blobs_root_path} -> {str(destination)}")
        run.download_file(blobs_root_path, str(destination), _validate_checksum=True)
    else:
        logging.info(f"Downloading multiple files from run {run.id}: {blobs_root_path} -> {str(destination)}")
        run.download_files(blobs_root_path, str(destination), append_prefix=append_prefix)
    return destination


def is_running_on_azure_agent() -> bool:
    """
    Returns True if the code appears to be running on an Azure build agent, and False otherwise.
    """
    # Guess by looking at the AGENT_OS variable, that all Azure hosted agents define.
    return bool(os.environ.get("AGENT_OS", None))


def is_run_and_child_runs_completed(run: Run) -> bool:
    """
    Checks if the given run has successfully completed. If the run has child runs, it also checks if the child runs
    completed successfully.
    :param run: The AzureML run to check.
    :return: True if the run and all child runs completed successfully.
    """

    def is_completed(run: Run) -> bool:
        status = run.get_status()
        if run.status == RunStatus.COMPLETED:
            return True
        logging.info(f"Run {run.id} in experiment {run.experiment.name} finished with status {status}.")
        return False

    runs = list(run.get_children())
    runs.append(run)
    return all(is_completed(run) for run in runs)
