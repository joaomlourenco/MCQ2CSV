"""What mcq2csv.py needs, whether it is installed, and how to install it.

Only the standard library is used here, so this works when nothing else is
installed.  mcq2csv.py --check calls check(), mcq2csv.py --install calls
install().

Needs: Python 3.8+, numpy, Pillow, and a PDF reader: poppler's pdftoppm or
the pypdfium2 package (either one; page images need neither).

Installing, by platform:
- Python packages go in with pip, unless this Python is managed by the
  system (Homebrew on macOS, the distribution on Linux), which refuses pip;
  then the system's package manager installs them.
- The PDF reader is poppler from Homebrew on macOS and from the
  distribution on Linux; on Windows it is pypdfium2 from pip, which needs no
  separate program.
"""
import importlib.util
import os
import platform
import shlex
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

MIN_PYTHON = (3, 8)
PYTHON = "py" if os.name == "nt" else "python3"      # what to type to run a script

# Linux package managers: (command, install command, package names)
LINUX = [
    ("apt-get", ["sudo", "apt-get", "install", "-y"],
     {"numpy": "python3-numpy", "Pillow": "python3-pil", "poppler": "poppler-utils"}),
    ("dnf", ["sudo", "dnf", "install", "-y"],
     {"numpy": "python3-numpy", "Pillow": "python3-pillow", "poppler": "poppler-utils"}),
    ("pacman", ["sudo", "pacman", "-S", "--needed", "--noconfirm"],
     {"numpy": "python-numpy", "Pillow": "python-pillow", "poppler": "poppler"}),
    ("zypper", ["sudo", "zypper", "install", "-y"],
     {"numpy": "python3-numpy", "Pillow": "python3-Pillow", "poppler": "poppler-tools"}),
]


def version_of(package):
    try:
        import importlib.metadata                # Python 3.8+
        return importlib.metadata.version(package)
    except Exception:
        return "installed"


def has_module(module):
    return importlib.util.find_spec(module) is not None


def pdftoppm_version(path):
    out = subprocess.run([path, "-v"], capture_output=True, text=True)
    lines = (out.stderr or out.stdout).splitlines()
    return lines[0] if lines else ""


def status():
    """[(name, ok, detail)] for everything this program needs."""
    py_ok = sys.version_info >= MIN_PYTHON
    need = ".".join(map(str, MIN_PYTHON))
    rows = [("Python", py_ok, platform.python_version() + ("" if py_ok else f" (needs {need} or later)"))]
    for module, package in (("numpy", "numpy"), ("PIL", "Pillow")):
        found = has_module(module)
        rows.append((package, found, version_of(package) if found else "not installed"))
    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm:
        rows.append(("PDF reader", True, f"{pdftoppm_version(pdftoppm)} ({pdftoppm})"))
    elif has_module("pypdfium2"):
        rows.append(("PDF reader", True, f"pypdfium2 {version_of('pypdfium2')}"))
    else:
        rows.append(("PDF reader", False,
                     "neither pdftoppm (poppler) nor pypdfium2; needed for PDF input only"))
    return rows


def missing():
    return [name for name, ok, _ in status() if not ok]


def check():
    """Print what is installed and what is not; return the exit code."""
    print(f"Python: {sys.executable}")
    rows = status()
    for name, ok, detail in rows:
        print(f"  {'ok     ' if ok else 'MISSING'}  {name:<10} {detail}")
    if any(not ok for _, ok, _ in rows):
        print(f"\nTo install what is missing:  {PYTHON} {Path(sys.argv[0]).name} --install")
        return 1
    print("\nEverything needed is installed.")
    return 0


# ---------------------------------------------------------------- installing

def python_kind():
    """'pip' when pip may install into this Python, 'system' when the OS manages it."""
    if sys.prefix != sys.base_prefix:            # a virtual environment
        return "pip"
    marker = Path(sysconfig.get_path("stdlib")) / "EXTERNALLY-MANAGED"
    return "system" if marker.exists() else "pip"


PIP = (sys.executable, "-m", "pip", "install")


def plan(todo):
    """(commands, notes): what to run to install the missing items in todo."""
    commands, notes = {}, []                     # installer command -> packages, in order

    def add(installer, *packages):
        commands.setdefault(tuple(installer), []).extend(packages)

    system = platform.system()
    packages = [p for p in ("numpy", "Pillow") if p in todo]
    want_pdf = "PDF reader" in todo
    pip_ok = python_kind() == "pip"
    brew = shutil.which("brew")
    linux = next(((cmd, names) for tool, cmd, names in LINUX if shutil.which(tool)), None)

    if "Python" in todo:
        need = ".".join(map(str, MIN_PYTHON))
        notes.append(f"Python {need} or later is needed; install it, then run this again with it:\n"
                     + {"Darwin": "    brew install python",
                        "Windows": "    winget install Python.Python.3.13   (or python.org)",
                        }.get(system, "    your distribution's python3 package"))
        return [], notes

    venv_hint = "use a virtual environment:  python3 -m venv .venv  and run with .venv/bin/python3"
    if system == "Darwin":
        if packages and pip_ok:
            add(PIP, *packages)
        elif packages and brew:
            add(["brew", "install"], *[p.lower() for p in packages])
            notes.append("Homebrew's numpy and pillow are for Homebrew's default python3; "
                         "for another Python, " + venv_hint + ".")
        elif packages:
            notes.append("This Python refuses pip and Homebrew is not installed; " + venv_hint + ".")
        if want_pdf and brew:
            add(["brew", "install"], "poppler")
        elif want_pdf and pip_ok:
            notes.append("Homebrew is not installed (https://brew.sh); pypdfium2 will read PDFs instead.")
            add(PIP, "pypdfium2")
        elif want_pdf:
            notes.append("Install Homebrew (https://brew.sh), then run --install again for poppler.")
    elif system == "Linux":
        if packages and pip_ok:
            add(PIP, *packages)
        elif packages and linux:
            add(linux[0], *[linux[1][p] for p in packages])
        elif packages:
            notes.append("This Python refuses pip and no known package manager was found; "
                         + venv_hint + ".")
        if want_pdf and linux:
            add(linux[0], linux[1]["poppler"])
        elif want_pdf and pip_ok:
            add(PIP, "pypdfium2")
        elif want_pdf:
            notes.append("Install poppler (pdftoppm) with your package manager.")
    else:                                        # Windows: everything from pip
        add(PIP, *packages, *(["pypdfium2"] if want_pdf else []))
    return [list(cmd) + pkgs for cmd, pkgs in commands.items() if pkgs], notes


def show(cmd):
    return subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)


def install():
    """Propose commands for what is missing, run the ones the user accepts; return the exit code."""
    todo = missing()
    if not todo:
        print("Everything needed is already installed.")
        return 0
    print("Missing: " + ", ".join(todo))
    commands, notes = plan(todo)
    for note in notes:
        print(f"\nNote: {note}")
    if not commands:
        return 1
    print("\nProposed:")
    for cmd in commands:
        print(f"  {show(cmd)}")
    if not sys.stdin.isatty():
        print("\nNot run (no terminal to ask in). Run the commands above yourself.")
        return 1
    for cmd in commands:
        try:
            answer = input(f"\nRun  {show(cmd)}  ? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nStopped.")
            return 1
        if answer not in ("y", "yes"):
            print("Skipped.")
            continue
        if subprocess.run(cmd).returncode != 0:
            print("That command failed; fix the problem above and run --install again.")
            return 1
    importlib.invalidate_caches()
    print()
    return check()
