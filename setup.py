"""Reject stale compiled help in both wheel and source-distribution builds."""

import runpy
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist


def check_user_help() -> None:
    script = Path(__file__).parent / "tools" / "build_user_help.py"
    runpy.run_path(str(script))["check"]()


class CheckedBuildPy(build_py):
    def run(self) -> None:
        check_user_help()
        super().run()


class CheckedSdist(sdist):
    def run(self) -> None:
        check_user_help()
        super().run()


setup(cmdclass={"build_py": CheckedBuildPy, "sdist": CheckedSdist})
