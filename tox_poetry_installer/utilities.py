"""Helper utility functions, usually bridging Tox and Poetry functionality"""
# Silence this one globally to support the internal function imports for the proxied poetry module.
# See the docstring in 'tox_poetry_installer._poetry' for more context.
# pylint: disable=import-outside-toplevel
import sys
import typing
from pathlib import Path
from typing import List
from typing import Sequence
from typing import Set

import tox
from poetry.core.packages import Package as PoetryPackage
from tox.action import Action as ToxAction
from tox.venv import VirtualEnv as ToxVirtualEnv

from tox_poetry_installer import constants
from tox_poetry_installer import exceptions
from tox_poetry_installer.datatypes import PackageMap

if typing.TYPE_CHECKING:
    from tox_poetry_installer import _poetry


def install_to_venv(
    poetry: "_poetry.Poetry", venv: ToxVirtualEnv, packages: Sequence[PoetryPackage]
):
    """Install a bunch of packages to a virtualenv

    :param poetry: Poetry object the packages were sourced from
    :param venv: Tox virtual environment to install the packages to
    :param packages: List of packages to install to the virtual environment
    """
    from tox_poetry_installer import _poetry

    tox.reporter.verbosity1(
        f"{constants.REPORTER_PREFIX} Installing {len(packages)} packages to environment at {venv.envconfig.envdir}"
    )

    installer = _poetry.PipInstaller(
        env=_poetry.VirtualEnv(path=Path(venv.envconfig.envdir)),
        io=_poetry.NullIO(),
        pool=poetry.pool,
    )

    for dependency in packages:
        tox.reporter.verbosity1(f"{constants.REPORTER_PREFIX} Installing {dependency}")
        installer.install(dependency)


def identify_transients(
    packages: PackageMap, dependency_name: str, allow_missing: Sequence[str] = ()
) -> List[PoetryPackage]:
    """Using a poetry object identify all dependencies of a specific dependency

    :param packages: All packages from the lockfile to use for identifying dependency relationships.
    :param dependency_name: Bare name (without version) of the dependency to fetch the transient
                            dependencies of.
    :param allow_missing: Sequence of package names to allow to be missing from the lockfile. Any
                          packages that are not found in the lockfile but their name appears in this
                          list will be silently skipped from installation.
    :returns: List of packages that need to be installed for the requested dependency.

    .. note:: The package corresponding to the dependency named by ``dependency_name`` is included
              in the list of returned packages.
    """
    from tox_poetry_installer import _poetry

    transients: List[PoetryPackage] = []

    searched: Set[PoetryPackage] = set()

    def find_deps_of_deps(name: str):
        searched.add(name)

        if name in _poetry.Provider.UNSAFE_PACKAGES:
            tox.reporter.warning(
                f"{constants.REPORTER_PREFIX} Installing package '{name}' using Poetry is not supported and will be skipped"
            )
            tox.reporter.verbosity2(
                f"{constants.REPORTER_PREFIX} Skip {name}: designated unsafe by Poetry"
            )
            return

        try:
            package = packages[name]
        except KeyError as err:
            if name in allow_missing:
                tox.reporter.verbosity2(
                    f"{constants.REPORTER_PREFIX} Skip {name}: package is not in lockfile but designated as allowed to be missing"
                )
                return
            raise err

        if not package.python_constraint.allows(constants.PLATFORM_VERSION):
            tox.reporter.verbosity2(
                f"{constants.REPORTER_PREFIX} Skip {package}: incompatible Python requirement '{package.python_constraint}' for current version '{constants.PLATFORM_VERSION}'"
            )
        elif package.platform is not None and package.platform != sys.platform:
            tox.reporter.verbosity2(
                f"{constants.REPORTER_PREFIX} Skip {package}: incompatible platform requirement '{package.platform}' for current platform '{sys.platform}'"
            )
        else:
            for index, dep in enumerate(package.requires):
                tox.reporter.verbosity2(
                    f"{constants.REPORTER_PREFIX} Processing dependency {index + 1}/{len(package.requires)} for {package}: {dep.name}"
                )
                if dep.name not in searched:
                    find_deps_of_deps(dep.name)
                else:
                    tox.reporter.verbosity2(
                        f"{constants.REPORTER_PREFIX} Package with name '{dep.name}' has already been processed, skipping"
                    )
            tox.reporter.verbosity2(
                f"{constants.REPORTER_PREFIX} Including {package} for installation"
            )
            transients.append(package)

    try:
        find_deps_of_deps(packages[dependency_name].name)
    except KeyError:
        if dependency_name in _poetry.Provider.UNSAFE_PACKAGES:
            tox.reporter.warning(
                f"{constants.REPORTER_PREFIX} Installing package '{dependency_name}' using Poetry is not supported and will be skipped"
            )
            return []

        if any(
            delimiter in dependency_name
            for delimiter in constants.PEP508_VERSION_DELIMITERS
        ):
            raise exceptions.LockedDepVersionConflictError(
                f"Locked dependency '{dependency_name}' cannot include version specifier"
            ) from None

        raise exceptions.LockedDepNotFoundError(
            f"No version of locked dependency '{dependency_name}' found in the project lockfile"
        ) from None

    return transients


def check_preconditions(venv: ToxVirtualEnv, action: ToxAction) -> "_poetry.Poetry":
    """Check that the local project environment meets expectations"""
    # Skip running the plugin for the provisioning environment. The provisioned environment,
    # for alternative Tox versions and/or the ``requires`` meta dependencies is specially
    # handled by Tox and is out of scope for this plugin. Since one of the ways to install this
    # plugin in the first place is via the Tox provisioning environment, it quickly becomes a
    # chicken-and-egg problem.
    if action.name == venv.envconfig.config.provision_tox_env:
        raise exceptions.SkipEnvironment(
            f"Skipping Tox provisioning env '{action.name}'"
        )

    # Skip running the plugin for the packaging environment. PEP-517 front ends can handle
    # that better than we can, so let them do their thing. More to the point: if you're having
    # problems in the packaging env that this plugin would solve, god help you.
    if action.name == venv.envconfig.config.isolated_build_env:
        raise exceptions.SkipEnvironment(
            f"Skipping isolated packaging build env '{action.name}'"
        )

    from tox_poetry_installer import _poetry

    try:
        return _poetry.Factory().create_poetry(venv.envconfig.config.toxinidir)
    # Support running the plugin when the current tox project does not use Poetry for its
    # environment/dependency management.
    #
    # ``RuntimeError`` is dangerous to blindly catch because it can be (and in Poetry's case,
    # is) raised in many different places for different purposes.
    except RuntimeError:
        raise exceptions.SkipEnvironment(
            "Project does not use Poetry for env management, skipping installation of locked dependencies"
        ) from None


def find_project_dependencies(
    venv: ToxVirtualEnv, poetry: "_poetry.Poetry", packages: PackageMap
) -> List[PoetryPackage]:
    """Find the root package dependencies

    Recursively identify the root package dependencies

    :param venv: Tox virtual environment to install the packages to
    :param poetry: Poetry object the packages were sourced from
    :param packages: Mapping of package names to the corresponding package object
    """

    base_dependencies: List[PoetryPackage] = [
        packages[item.name]
        for item in poetry.package.requires
        if not item.is_optional()
    ]

    extra_dependencies: List[PoetryPackage] = []
    for extra in venv.envconfig.extras:
        tox.reporter.verbosity1(
            f"{constants.REPORTER_PREFIX} Processing project extra '{extra}'"
        )
        try:
            extra_dependencies += [
                packages[item.name] for item in poetry.package.extras[extra]
            ]
        except KeyError:
            raise exceptions.ExtraNotFoundError(
                f"Environment '{venv.name}' specifies project extra '{extra}' which was not found in the lockfile"
            ) from None

    dependencies: List[PoetryPackage] = []
    for dep in base_dependencies + extra_dependencies:
        dependencies += identify_transients(
            packages, dep.name.lower(), allow_missing=[poetry.package.name]
        )

    return dependencies


def find_dev_dependencies(
    poetry: "_poetry.Poetry", packages: PackageMap
) -> List[PoetryPackage]:
    """Find the dev dependencies

    Recursively identify the Poetry dev dependencies

    :param venv: Tox virtual environment to install the packages to
    :param poetry: Poetry object the packages were sourced from
    :param packages: Mapping of package names to the corresponding package object
    """
    dependencies: List[PoetryPackage] = []
    for dep_name in (
        poetry.pyproject.data["tool"]["poetry"].get("dev-dependencies", {}).keys()
    ):
        dependencies += identify_transients(
            packages, dep_name, allow_missing=[poetry.package.name]
        )

    return dependencies


def find_env_dependencies(
    venv: ToxVirtualEnv, poetry: "_poetry.Poetry", packages: PackageMap
) -> List[PoetryPackage]:
    """Find the environment dependencies

    Recursively identify the dependencies to install for the current environment

    :param venv: Tox virtual environment to install the packages to
    :param poetry: Poetry object the packages were sourced from
    :param packages: Mapping of package names to the corresponding package object
    """
    dependencies: List[PoetryPackage] = []
    for dep in venv.envconfig.locked_deps:
        dependencies += identify_transients(
            packages, dep.lower(), allow_missing=[poetry.package.name]
        )

    return dependencies
