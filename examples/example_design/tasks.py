"""Tasks for use with Invoke.

(c) 2020-2021 Network To Code
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at
  http://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import os
import sys

from invoke import Collection
from invoke import task as invoke_task


# Use pyinvoke configuration for default values, see http://docs.pyinvoke.org/en/stable/concepts/configuration.html
# Variables may be overwritten in invoke.yml or by the environment variables INVOKE_DESIGN_BUILDER_xxx
namespace = Collection("design_builder_designs")
namespace.configure({"design_builder_designs": {}})


def task(function=None, *args, **kwargs):
    """Task decorator to override the default Invoke task decorator and add each task to the invoke namespace."""

    def task_wrapper(function=None):
        """Wrapper around invoke.task to add the task to the namespace as well."""
        if args or kwargs:
            task_func = invoke_task(*args, **kwargs)(function)
        else:
            task_func = invoke_task(function)
        namespace.add_task(task_func)
        return task_func

    if function:
        # The decorator was called with no arguments
        return task_wrapper(function)
    # The decorator was called with arguments
    return task_wrapper


@task
def nbshell(context):
    """Launch an interactive nbshell session."""
    command = "nautobot-server nbshell"
    context.run(command)


@task
def shell_plus(context):
    """Launch an interactive shell_plus session."""
    command = "nautobot-server shell_plus"
    context.run(command)


@task(
    help={
        "user": "name of the superuser to create (default: admin)",
    }
)
def createsuperuser(context, user="admin"):
    """Create a new Nautobot superuser account (default: "admin"), will prompt for password."""
    command = f"nautobot-server createsuperuser --username {user}"
    context.run(command)


# ------------------------------------------------------------------------------
# TESTS
# ------------------------------------------------------------------------------
@task(
    help={
        "autoformat": "Apply formatting recommendations automatically, rather than failing if formatting is incorrect.",
    }
)
def black(context, autoformat=False):
    """Check Python code style with Black."""
    if autoformat:
        black_command = "black"
    else:
        black_command = "black --check --diff"

    command = f"{black_command} ."

    context.run(command)


@task
def flake8(context):
    """Check for PEP8 compliance and other style issues."""
    command = "flake8 designs/ jobs/"
    context.run(command)


@task(help={"file": "run pylint for a specific file"})
def pylint(context, file=None):
    """Run pylint code analysis."""
    command = 'pylint --ignore-patterns="^test_" --init-hook "import nautobot; nautobot.setup()" '
    if file is None:
        command += "designs"
    else:
        command += file
    context.run(command)


@task
def pydocstyle(context):
    """Run pydocstyle to validate docstring formatting adheres to NTC defined standards."""
    # We exclude the /migrations/ directory since it is autogenerated code
    command = "pydocstyle designs jobs"
    context.run(command)


@task(
    help={
        "keepdb": "save and re-use test database between test runs for faster re-testing.",
        "label": "specify a directory or module to test instead of running all Nautobot tests",
        "failfast": "fail as soon as a single test fails don't run the entire test suite",
        "buffer": "Discard output from passing tests",
    }
)
def unittest(context, keepdb=True, label="designs", failfast=False, buffer=True):
    """Run Nautobot unit tests."""
    command = f"coverage run --module nautobot.core.cli test {label}"

    if keepdb:
        command += " --keepdb"
    if failfast:
        command += " --failfast"
    if buffer:
        command += " --buffer"
    context.run(command)


@task(
    help={
        "failfast": "fail as soon as a single test fails don't run the entire test suite",
    }
)
def tests(context, failfast=False):
    """Run all tests for this plugin."""
    # Sorted loosely from fastest to slowest
    print("Running black...", file=sys.stderr)
    black(context)
    print("Running flake8...", file=sys.stderr)
    flake8(context)
    print("Running pydocstyle...", file=sys.stderr)
    pydocstyle(context)
    print("Running pylint...", file=sys.stderr)
    pylint(context)
    print("Running unit tests...", file=sys.stderr)
    unittest(context, failfast=failfast)
    print("All tests have passed!", file=sys.stderr)


@task
def log(context):
    """View logs for the running project."""
    compose_file = os.path.join(os.path.dirname(__file__), ".devcontainer", "docker-compose.yml")
    project_name = f"{os.path.basename(os.environ.get('LOCAL_WORKSPACE_FOLDER'))}_devcontainer"
    command = f"docker-compose -p {project_name} -f {compose_file} logs -f"
    context.run(command)


@task
def restart(context):
    """Restart the nautobot web and worker containers."""
    services = ["nautobot", "worker"]

    compose_file = os.path.join(os.path.dirname(__file__), ".devcontainer", "docker-compose.yml")
    project_name = f"{os.path.basename(os.environ.get('LOCAL_WORKSPACE_FOLDER'))}_devcontainer"
    command = f"docker-compose -p {project_name} -f {compose_file} restart {' '.join(services)}"
    print(command)
    context.run(command)
