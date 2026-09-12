"""Inventory installed locked packages for PyPI advisory matching without resolution."""

import importlib.metadata
import sysconfig
from pathlib import Path


def inventory():
    requirements = []
    for package in importlib.metadata.distributions(path=[sysconfig.get_paths()["purelib"]]):
        name, version = package.metadata["Name"], package.version
        if name.lower() == "citestack":
            # Local project source is covered by repository review/tests, not PyPI.
            continue
        if name.lower() == "torch" and version.endswith("+cpu"):
            # PyPI advisories describe the upstream release, not its CPU wheel suffix.
            version = version.removesuffix("+cpu")
        requirements.append(f"{name}=={version}")
    return sorted(requirements)


if __name__ == "__main__":
    destination = Path("data/audit-requirements.txt")
    destination.parent.mkdir(exist_ok=True)
    destination.write_text("\n".join(inventory()) + "\n")
