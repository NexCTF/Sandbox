from nexctf.plugins import (
    ConfigDef,
    ConfigType,
    InputType,
    register_plugin_configs,
    solution_registry,
)

from nexctf_sandbox._sandbox import (
    DEFAULT_BASE_IMAGE,
    DEFAULT_CPUS,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_MEMORY_MIB,
    DEFAULT_NETWORK_ACCESS,
    DEFAULT_ROOT_DISK_MIB,
    NETWORK_ACCESS_CHOICES,
    PLUGIN_SLUG,
)
from nexctf_sandbox.solutions.runner import (
    RunnerSolution,
    RunnerSolutionCreate,
    RunnerSolutionRead,
    RunnerSolutionUpdate,
)
from nexctf_sandbox.solutions.script import (
    ScriptSolution,
    ScriptSolutionCreate,
    ScriptSolutionRead,
    ScriptSolutionUpdate,
)

solution_registry.register(
    "runner",
    model=RunnerSolution,
    create_schema=RunnerSolutionCreate,
    update_schema=RunnerSolutionUpdate,
    read_schema=RunnerSolutionRead,
    compatible_input_types=[InputType.CODE],
    description="Code runner — executes the submitted Python 3 code against a set of test cases.",
)

solution_registry.register(
    "script",
    model=ScriptSolution,
    create_schema=ScriptSolutionCreate,
    update_schema=ScriptSolutionUpdate,
    read_schema=ScriptSolutionRead,
    compatible_input_types=[InputType.INPUT, InputType.TEXT, InputType.CODE],
    description="Script checker — runs a custom Python function check(answer, team_id) → bool to validate the answer.",
)

register_plugin_configs(
    "Sandbox",
    ConfigDef(
        key="base_image",
        label="Base image",
        default=DEFAULT_BASE_IMAGE,
        type=ConfigType.STRING,
        description="OCI image every microVM boots from. It must provide python3 on PATH.",
    ),
    ConfigDef(
        key="cpus",
        label="vCPUs",
        default=DEFAULT_CPUS,
        description="Virtual CPUs given to each microVM.",
    ),
    ConfigDef(
        key="memory_mib",
        label="Memory (MiB)",
        default=DEFAULT_MEMORY_MIB,
        description="Guest RAM for each microVM. The root disk is added on top of this.",
    ),
    ConfigDef(
        key="root_disk_mib",
        label="Root disk (MiB)",
        default=DEFAULT_ROOT_DISK_MIB,
        description="Size of the tmpfs root disk. It is RAM-backed, so it is charged to guest memory.",
    ),
    ConfigDef(
        key="network_access",
        label="Network access",
        default=DEFAULT_NETWORK_ACCESS,
        type=ConfigType.CHOICE,
        choices=NETWORK_ACCESS_CHOICES,
        description=(
            "'disabled': no egress at all.\n"
            "'internet': public addresses, plus the host's DNS resolver on port 53.\n"
            "'all': unfiltered, including everything on the host's own network."
        ),
    ),
    ConfigDef(
        key="max_concurrent",
        label="Max concurrent microVMs",
        default=DEFAULT_MAX_CONCURRENT,
        description="Ceiling on microVMs running at once, across all challenges. Takes effect after a restart.",
    ),
    icon="box",
    plugin_slug=PLUGIN_SLUG,
)
