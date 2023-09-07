import os
import signal
import sys
import time
import traceback
from argparse import Namespace
from copy import deepcopy
from multiprocessing import set_start_method
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

import rich.traceback
from rich import get_console

from app.core import configuration
from app.core import definitions
from app.core import emitter
from app.core import logger
from app.core import utilities
from app.core import values
from app.core.args import parse_args
from app.core.configs.ConfigDataFactory import ConfigDataFactory
from app.core.configs.ConfigDataLoader import ConfigDataLoader
from app.core.configs.ConfigValidationSchemas import config_validation_schema
from app.core.configs.tasks_data.TaskConfig import TaskConfig
from app.core.configuration import Configurations
from app.core.task import task
from app.core.task.TaskProcessor import TaskList
from app.core.task.TaskProcessor import TaskProcessor
from app.core.task.typing import TaskType
from app.drivers.benchmarks.AbstractBenchmark import AbstractBenchmark
from app.drivers.tools.AbstractTool import AbstractTool
from app.notification import notification
from app.ui import ui


def create_output_directories():
    dir_list = [
        values.dir_logs,
        values.dir_output_base,
        values.dir_log_base,
        values.dir_artifacts,
        values.dir_results,
        values.dir_experiments,
        values.dir_summaries,
    ]

    for dir_i in dir_list:
        if not os.path.isdir(dir_i):
            os.makedirs(dir_i)


def timeout_handler(signum, frame):
    emitter.error("TIMEOUT Exception")
    raise Exception("end of time")


def shutdown(signum, frame):
    # global stop_event
    emitter.warning("Exiting due to Terminate Signal")
    # stop_event.set()
    raise SystemExit


def bootstrap(arg_list: Namespace):
    emitter.sub_title("Bootstrapping framework")
    config = Configurations()
    config.read_email_config_file()
    config.read_slack_config_file()
    config.read_discord_config_file()
    config.read_arg_list(arg_list)
    values.arg_pass = True
    config.update_configuration()
    config.print_configuration()


def create_task_image_identifier(
    benchmark: AbstractBenchmark,
    tool: AbstractTool,
    experiment_item: Dict[str, Any],
    tag: Optional[str] = None,
):
    bug_name = str(experiment_item[definitions.KEY_BUG_ID])
    subject_name = str(experiment_item[definitions.KEY_SUBJECT])
    image_args = [tool.name, benchmark.name, subject_name, bug_name]

    if tag and tag != "":
        image_args.append(tag)

    image_name = "-".join(image_args)
    return image_name.lower()


def create_bug_image_identifier(
    benchmark: AbstractBenchmark, experiment_item: Dict[str, Any]
):
    bug_name = str(experiment_item[definitions.KEY_BUG_ID])
    subject_name = str(experiment_item[definitions.KEY_SUBJECT])
    return "-".join([benchmark.name, subject_name, bug_name]).lower()


def create_task_identifier(
    benchmark: AbstractBenchmark,
    task_profile,
    container_profile,
    experiment_item,
    tool: AbstractTool,
    run_index: str,
    tool_tag: str,
):
    return "-".join(
        [
            benchmark.name,
            tool.name if tool_tag == "" else f"{tool.name}-{tool_tag}",
            experiment_item[definitions.KEY_SUBJECT],
            experiment_item[definitions.KEY_BUG_ID],
            task_profile[definitions.KEY_ID],
            container_profile[definitions.KEY_ID],
            run_index,
        ]
    )


iteration = 0


def construct_task_list(
    tool_list: List[AbstractTool],
    benchmark: AbstractBenchmark,
    task_profiles: Dict[str, Dict[str, Any]],
    container_profiles: Dict[str, Dict[str, Any]],
    task_type: TaskType,
) -> TaskList:

    task_config = TaskConfig(
        task_type,
        values.compact_results,
        values.dump_patches,
        values.docker_host,
        values.only_analyse,
        values.only_setup,
        values.only_instrument,
        values.only_setup,
        values.rebuild_all,
        values.rebuild_base,
        values.use_cache,
        values.use_container,
        values.use_gpu,
        values.use_purge,
        values.cpus,
        values.runs,
    )
    for task_profile_template in map(
        lambda task_profile_id: task_profiles[task_profile_id],
        values.task_profile_id_list,
    ):
        task_profile = deepcopy(task_profile_template)
        task_profile[definitions.KEY_TOOL_PARAMS] = values.tool_params
        task_profile[definitions.KEY_TOOL_TAG] = values.tool_tag
        for container_profile_template in map(
            lambda container_profile_id: container_profiles[container_profile_id],
            values.container_profile_id_list,
        ):
            container_profile = deepcopy(container_profile_template)
            for experiment_item in filter_experiment_list(benchmark):
                bug_index = experiment_item[definitions.KEY_ID]

                for tool in tool_list:
                    yield (
                        task_config,
                        (
                            deepcopy(benchmark),
                            deepcopy(tool),
                            experiment_item,
                            task_profile,
                            container_profile,
                            bug_index,
                        ),
                    )


def get_task_profiles() -> Dict[str, Dict[str, Any]]:
    emitter.normal("\t[framework] loading repair task profiles")
    task_profiles = configuration.load_profiles(values.file_task_profiles)
    for task_profile_id in values.task_profile_id_list:
        if task_profile_id not in task_profiles:
            utilities.error_exit("invalid task profile id {}".format(task_profile_id))
    return task_profiles


def get_container_profiles() -> Dict[str, Dict[str, Any]]:
    emitter.normal("\t[framework] loading container profiles")
    container_profiles = configuration.load_profiles(values.file_container_profiles)
    for container_profile_id in values.container_profile_id_list:
        if container_profile_id not in container_profiles:
            utilities.error_exit(
                "invalid container profile id {}".format(container_profile_id)
            )
    return container_profiles


def get_tools() -> List[AbstractTool]:
    tool_list: List[AbstractTool] = []
    if values.task_type.get() == "prepare":
        return tool_list
    for tool_name in values.tool_list:
        tool = configuration.load_tool(tool_name, values.task_type.get(None) or "boom")
        if not values.only_analyse:
            tool.check_tool_exists()
        tool_list.append(tool)
    emitter.highlight(
        f"\t[framework] {values.task_type.get()}-tool(s): "
        + " ".join([x.name for x in tool_list])
    )
    return tool_list


def get_benchmark() -> AbstractBenchmark:
    benchmark = configuration.load_benchmark(values.benchmark_name.lower())
    emitter.highlight(
        f"\t[framework] {values.task_type.get()}-benchmark: {benchmark.name}"
    )
    return benchmark


def filter_experiment_list(benchmark: AbstractBenchmark):
    filtered_list = []
    experiment_list = benchmark.get_list()
    for bug_index in range(1, benchmark.size + 1):
        experiment_item = experiment_list[bug_index - 1]
        subject_name = experiment_item[definitions.KEY_SUBJECT]
        bug_name = str(experiment_item[definitions.KEY_BUG_ID])
        if values.bug_id_list and bug_name not in values.bug_id_list:
            continue
        if values.bug_index_list and bug_index not in values.bug_index_list:
            continue
        if values.skip_index_list and str(bug_index) in values.skip_index_list:
            continue
        if values.start_index and bug_index < values.start_index:
            continue
        if values.subject_name and values.subject_name != subject_name:
            continue
        if values.end_index and bug_index > values.end_index:
            break
        filtered_list.append(experiment_item)
    return filtered_list


def process_configs(
    task_config: TaskConfig,
    benchmark: AbstractBenchmark,
    experiment_item,
    task_profile: Dict[str, Any],
    container_profile: Dict[str, Any],
):
    for (k, v) in task_config.__dict__.items():
        if k != "task_type" and v is not None:
            emitter.configuration(k, v)
            setattr(values, k, v)
    values.task_type.set(task_config.task_type)
    values.current_container_profile_id.set(container_profile[definitions.KEY_ID])
    values.current_task_profile_id.set(task_profile[definitions.KEY_ID])

    if values.use_container:
        values.job_identifier.set(
            create_bug_image_identifier(benchmark, experiment_item)
        )


def main():
    global iteration
    if not sys.warnoptions:
        import warnings

        warnings.simplefilter("ignore")

    rich.traceback.install(show_locals=True)
    parsed_args = parse_args()
    is_error = False
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.signal(signal.SIGTERM, shutdown)
    set_start_method("spawn")
    start_time = time.time()
    create_output_directories()
    logger.create_log_files()
    # TODO Do overwrite magic
    bootstrap(parsed_args)
    try:
        emitter.title(
            "Starting {} (Program Repair Framework) ".format(values.tool_name)
        )

        tasks = None
        if parsed_args.config_file:
            config = process_config_file(parsed_args)
            tasks = TaskProcessor.execute(config)
            # The tool and benchmark images are going to be created while enumerating
            process_tasks(tasks)
        else:
            if not parsed_args.task_type:
                utilities.error_exit(
                    "Configuration file was not passed. Please provide a task type!"
                )
            tasks = construct_task_list(
                get_tools(),
                get_benchmark(),
                get_task_profiles(),
                get_container_profiles(),
                parsed_args.task_type,
            )

        if values.use_parallel:
            info = sys.version_info
            if info.major < 3 or info.minor < 10:
                utilities.error_exit(
                    "Parallel mode is currently supported only for versions 3.10+"
                )
            iteration = ui.setup_ui(tasks)
        else:
            process_tasks(tasks)

    except (SystemExit, KeyboardInterrupt) as e:
        pass
    except Exception as e:
        is_error = True
        values.ui_active = False
        emitter.error("Runtime Error")
        emitter.error(str(e))
        logger.error(traceback.format_exc())
    finally:
        get_console().show_cursor(True)
        # Final running time and exit message
        # os.system("ps -aux | grep 'python' | awk '{print $2}' | xargs kill -9")
        total_duration = format((time.time() - start_time) / 60, ".3f")
        if not parsed_args.parallel:
            notification.end(total_duration, is_error)
        emitter.end(total_duration, iteration, is_error)


def process_config_file(parsed_args):
    values.arg_pass = True
    config_loader = ConfigDataLoader(
        file_path=parsed_args.config_file,
        validation_schema=config_validation_schema,
    )
    config_loader.load()
    config_loader.validate()
    config = ConfigDataFactory.create(config_data_dict=config_loader.get_config_data())
    values.debug = config.general.debug_mode
    values.secure_hash = config.general.secure_hash
    values.use_parallel = config.general.parallel_mode
    return config


def process_tasks(tasks: TaskList):
    for iteration, (task_config, task_data) in enumerate(tasks):
        (
            benchmark,
            tool,
            experiment_item,
            task_profile,
            container_profile,
            bug_index,
        ) = task_data
        process_configs(
            task_config,
            benchmark,
            experiment_item,
            task_profile,
            container_profile,
        )

        cpu = ",".join(
            map(
                str,
                range(
                    container_profile.get(
                        definitions.KEY_CONTAINER_CPU_COUNT, values.cpus
                    )
                ),
            )
        )
        experiment_image_id = task.prepare_experiment(benchmark, experiment_item, cpu)

        tool_tag = task_profile.get(definitions.KEY_TOOL_TAG, "")

        bug_name = str(experiment_item[definitions.KEY_BUG_ID])
        subject_name = str(experiment_item[definitions.KEY_SUBJECT])
        dir_info = task.generate_dir_info(benchmark.name, subject_name, bug_name)

        image_name = create_task_image_identifier(
            benchmark,
            tool,
            experiment_item,
            tool_tag,
        )
        task.prepare_experiment_tool(
            experiment_image_id,
            tool,
            dir_info,
            image_name,
            task_profile[definitions.KEY_TOOL_TAG],
        )

        for run_index in range(task_config.runs):
            iteration = iteration + 1
            emitter.sub_sub_title(
                "Experiment #{} - Bug #{} Run #{}".format(
                    iteration, bug_index, run_index + 1
                )
            )

            key = create_task_identifier(
                benchmark,
                task_profile,
                container_profile,
                experiment_item,
                tool,
                str(run_index),
                tool_tag,
            )

            if not values.only_setup:
                task.run(
                    benchmark,
                    tool,
                    experiment_item,
                    task_profile,
                    container_profile,
                    key,
                    cpu,
                    image_name,
                )
