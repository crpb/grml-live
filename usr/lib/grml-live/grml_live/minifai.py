# This is a spaghetti-code minimal reimplementation of the FAI API surface grml-live needs,
# for building Grml Live Linux. If you have additional API surface needs, please contribute.
# Please beware that this implementation is an interim step, and we may or may not continue
# with the FAI API.
#
import argparse
import contextlib
import datetime
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import Event, Thread

from .classes import ClassFileParsingFailed, parse_class_varfile
from .packages import PackageList, parse_class_packages

APT_DEBUG_ACQUIRE = "Debug::Acquire::http=true"


class FaiScriptFailed(Exception):
    pass


class FaiAction(StrEnum):
    BOOTSTRAP = "bootstrap"
    DIRINSTALL = "dirinstall"
    SOFTUPDATE = "softupdate"
    RECONFIGURE = "reconfigure"
    REBUILD = "rebuild"


@dataclass
class DynamicState:
    """Holds state that can change in FAI hooks, for example by calling "skiptask"."""

    def __init__(self):
        self.skip_tasks = set()


@dataclass(kw_only=True)
class CopyFilesArgs:
    mode: int
    recursive: bool
    ignore_missing: bool
    paths: list[str]


@dataclass(kw_only=True)
class BuildDirectories:
    # xxx_inside is always the path _inside_ the chroot ("relative", if chrooted).
    build_dir_inside: str
    build_dir: Path
    log_dir_inside: str
    log_dir: Path
    netboot_dir_inside: str
    netboot_dir: Path
    media_dir_inside: str
    media_dir: Path
    sources_dir_inside: str
    sources_dir: Path


def now_for_log() -> str:
    return datetime.datetime.now().isoformat()


def run_x(args, check: bool = True, **kwargs):
    """Run program. Output goes to stdout/stderr."""
    # str-ify Paths, not necessary, but for readability in logs.
    args = [arg if isinstance(arg, str) else str(arg) for arg in args]
    args_str = '" "'.join(args)
    if "env" in kwargs:
        # Always pass-through SOURCE_DATE_EPOCH
        env = {}
        if "SOURCE_DATE_EPOCH" in os.environ:
            env["SOURCE_DATE_EPOCH"] = os.environ["SOURCE_DATE_EPOCH"]
        kwargs["env"] = env | kwargs["env"]  # do not update original dict

    print(f'D: Running "{args_str}"', flush=True)
    return subprocess.run(args, check=check, **kwargs)


def run_chrooted(chroot_dir: Path, args, check: bool = True, **kwargs):
    """Run program with arguments in chroot chroot_dir."""
    kwargs["env"] = {
        "PATH": "/usr/sbin:/sbin:/usr/bin:/bin",
        "TERM": "dumb",
    } | kwargs.get("env", {})
    return run_x(["chroot", chroot_dir, *args], check=check, **kwargs)


def chrooted_dpkg_print_architecture(chroot_dir: Path) -> str:
    """Read dpkg --print-architecture of chroot"""
    result = run_chrooted(chroot_dir, ["dpkg", "--print-architecture"], capture_output=True)
    return result.stdout.strip().decode()


def chrooted_apt_install(chroot_dir: Path, install_list: list[str]):
    """Run apt install in chroot_dir."""
    env = {
        "DEBIAN_FRONTEND": "noninteractive",
    }
    args = [
        "apt",
        "-oapt::cmd::disable-script-warning=1",
        "install",
        "-q",
        "-y",
        "--no-install-recommends",
        *install_list,
    ]
    if os.environ.get("GRML_LIVE_DEBUG_APT", "") != "":
        args.insert(1, f"-o{APT_DEBUG_ACQUIRE}")
    run_chrooted(
        chroot_dir,
        args,
        env=env,
        stdin=subprocess.DEVNULL,
    )


def chrooted_debconf_set_selections(chroot_dir: Path, selections_file: Path):
    """Run debconf-set-selections in chroot_dir, piping in selections_file."""

    if not selections_file.exists():
        return

    env = {
        "DEBIAN_FRONTEND": "noninteractive",
    }
    print("I: Loading debconf selections from", selections_file)
    with selections_file.open("r") as selections_fd:
        run_chrooted(chroot_dir, ["debconf-set-selections", "-v"], env=env, stdin=selections_fd)


def run_script(chroot_dir: Path, script: Path, helper_tools_paths: list[Path], env: dict[str, str]):
    """
    Run a FAI hook script or class script, if it exists.
    PATH will include helper_tools_paths.
    Environment will include env.
    """

    if not script.exists():
        return

    env = {
        "target": str(chroot_dir),
        "ROOTCMD": f"chroot {chroot_dir!s} ",
        "PATH": ":".join([str(p) for p in helper_tools_paths] + [os.environ["PATH"]]),
    } | env
    print()
    print(f"I: *** Running script {script} ***")
    proc = run_x([script], check=False, env=env, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        print(f"E: Script {script} failed with exitcode {proc.returncode} - aborting.")
        raise FaiScriptFailed()
    print(f"I: Finished script {script}.")


def run_class_scripts(
    script_type: str,
    conf_dir: Path,
    chroot_dir: Path,
    class_name: str,
    helper_tools_paths: list[Path],
    env: dict[str, str],
):
    print()
    print(f'I: Running "{script_type}" for class {class_name}...')
    print()
    scripts_dir = conf_dir / script_type / class_name
    for script in sorted(scripts_dir.glob("*")):
        if script.name.endswith(".dpkg-old") or script.name.endswith(".dpkg-new"):
            print(f"W: Skipping {script} due to name suffix, please delete it")
            continue
        run_script(chroot_dir, script, helper_tools_paths, env)


def install_packages_for_classes(
    conf_dir: Path,
    chroot_dir: Path,
    classes: list[str],
    helper_tools_paths: list[Path],
    hook_env: dict,
    dynamic_state: DynamicState,
):
    """Run equivalent of "instsoft" task: set debconf selections and install packages listed in package lists."""

    # debconf is not Essential. Ensure it is installed, so we can use debconf-set-selections.
    chrooted_apt_install(chroot_dir, ["debconf"])

    dpkg_architecture = chrooted_dpkg_print_architecture(chroot_dir)

    # First pass: Parse all package configs and build merged list
    class_package_lists = {}
    full_package_list = PackageList()

    for class_name in classes:
        package_list = parse_class_packages(conf_dir, class_name)
        class_package_lists[class_name] = package_list
        full_package_list.merge(package_list)

    # Show what packages will be skipped if any
    skip_packages = full_package_list.skip_list_for_arch(dpkg_architecture)
    if skip_packages:
        print(f"I: Skipping {len(skip_packages)} packages: {', '.join(sorted(skip_packages))}")

    # Second pass: Install packages and run hooks for each class
    for class_name in classes:
        chrooted_debconf_set_selections(chroot_dir, conf_dir / "debconf" / class_name)

        run_script(chroot_dir, conf_dir / "hooks" / class_name / "instsoft", helper_tools_paths, hook_env)

        # Use the previously parsed package list and apply final skip rules
        package_list = class_package_lists[class_name]
        install_args = package_list.as_apt_params(restrict_to_arch=dpkg_architecture, exclude_from=full_package_list)
        if install_args:
            print(f"I: Installing packages for class {class_name}")
            chrooted_apt_install(chroot_dir, install_args)

    print()
    print("I: Installing all packages together to detect relationship errors")
    chrooted_apt_install(chroot_dir, full_package_list.as_apt_params(restrict_to_arch=dpkg_architecture))

    with (chroot_dir / "grml-live" / "log" / "install_packages.list").open("wt") as file:
        file.write("# List of packages installed by minifai\n")
        file.write("\n".join(full_package_list.list_for_arch(dpkg_architecture)))
        file.write("\n")


def show_env(log_text: str, env):
    print(f"D: Showing {log_text} ...")
    for k, v in dict(env).items():
        print(f"D: {log_text}: {k}={v}")
    print()


def do_fcopy_file(to_copy: Path, dest_root: Path, path: str, mode: int):
    dest_path = dest_root / path

    print(f"I: fcopy: Installing {to_copy} as {dest_path}.")
    try:
        dest_exists = bool(dest_path.lstat())
    except FileNotFoundError:
        dest_exists = False

    if dest_exists:
        print(f"W: fcopy: Destination {dest_path} already exists, removing.")
        dest_path.unlink()

    # this is probably fine, as we expect to run as root and do not support
    # different file/directory ownership.
    dest_path.parent.mkdir(exist_ok=True, parents=True)

    shutil.copy2(to_copy, dest_path, follow_symlinks=False)

    try:
        dest_path.chmod(mode, follow_symlinks=False)
    except NotImplementedError:
        pass

    os.chown(dest_path, 0, 0, follow_symlinks=False)

    return True


def do_fcopy_path(files_dir: Path, dest_root: Path, classes: list[str], path: str, mode: int) -> bool:
    to_copy = None
    for class_name in classes:
        class_path = files_dir / class_name / path
        if class_path.exists():
            to_copy = class_path

    if to_copy:
        do_fcopy_file(to_copy, dest_root, path, mode)
        return True
    else:
        return False


def do_fcopy_recursive(files_dir: Path, dest_root: Path, classes: list[str], path_root: str, mode: int):
    tree = {}

    for class_name in classes:
        class_files_dir = files_dir / class_name
        if not class_files_dir.exists():
            continue

        class_path_root = class_files_dir / path_root
        if not class_path_root.exists():
            continue

        files = [p.relative_to(class_files_dir) for p in class_path_root.glob("**/*") if not p.is_dir()]
        for file in files:
            tree[file] = class_name

    for path, class_name in tree.items():
        do_fcopy_file(files_dir / class_name / path, dest_root, path, mode)


def do_copy_files(
    conf_dir: Path, dest_root: Path, classes: list[str], files_name: str, copy_args: CopyFilesArgs
) -> int:
    print(f"D: copy_files {copy_args.recursive=} {copy_args.ignore_missing=} {copy_args.mode=} {copy_args.paths=}")
    rc = 0
    files_dir = conf_dir / files_name

    try:
        if copy_args.recursive:
            for path in copy_args.paths:
                do_fcopy_recursive(files_dir, dest_root, classes, path, copy_args.mode)

        else:
            for path in copy_args.paths:
                found = do_fcopy_path(files_dir, dest_root, classes, path, copy_args.mode)
                if not found and not copy_args.ignore_missing:
                    print(f"E: Source {path=} is missing")
                    rc = 1

    except Exception as except_inst:
        print(f"E: copy_files failed: {except_inst} - returning with exit code 130", flush=True)
        rc = 130

    return rc


def _parse_fcopy_args(fcopy_args: list[str]) -> CopyFilesArgs:
    user = "root"
    group = "root"
    mode = 0o644
    recursive = False
    ignore_missing = False
    paths = []

    # FAI fcopy parameters:
    # -B Remove backup files with suffix .pre_fcopy.
    # -r Copy recursively (traverse down the tree). Copy all files below SOURCE.
    #    These are all subdirectory leaves in the SOURCE tree.
    #    Ignore "ignored" directories (see "-I" for details).
    # -i Ignore warnings about no matching class and non-existing source directories.
    #    These warnings will not set the exit code to 1.
    # -v verbose
    # -m user,group,mode Set user, group and mode for all copied files (mode as octal
    #    number, user and group numeric id or name). If not specified, use file
    #    file-modes or data of source file.
    # -M Use default values for user, group and mode. This is equal to -m root,root,0644

    parse_m = False
    for index, arg in enumerate(fcopy_args):
        if parse_m:
            # TODO: handle errors
            user, group, mode = arg.split(",")
            mode = int(mode, 8)
            parse_m = False
        elif arg in ["-B", "-v"]:
            # defaulted / ignored
            pass
        elif arg == "-m":
            parse_m = True
        elif arg == "-M":
            user, group = "root", "root"
            mode = 0o644
        elif arg == "-r":
            recursive = True
        elif arg == "-i":
            ignore_missing = True
        elif not arg.startswith("-"):
            paths = fcopy_args[index:]
            break
        else:
            raise ValueError(f"fcopy: param {arg} not understood")

    if not paths:
        raise ValueError("fcopy: no paths given")

    paths = [path.lstrip("/") for path in paths]

    if user != "root" or group != "root":
        raise ValueError("E: When copying files, user/group must be root/root")

    return CopyFilesArgs(
        mode=mode,
        recursive=recursive,
        ignore_missing=ignore_missing,
        paths=paths,
    )


def do_fcopy(conf_dir: Path, chroot_dir: Path, classes: list[str], remaining_args: list[str]) -> int:
    try:
        copy_args = _parse_fcopy_args(remaining_args)
    except Exception as except_inst:
        print(f"E: Parsing fcopy_args {remaining_args!r} failed: {except_inst}")
        return 1

    return do_copy_files(conf_dir, chroot_dir, classes, "files", copy_args)


def _parse_copy_media_files_args(args: list[str]) -> CopyFilesArgs:
    mode = 0o644
    recursive = False
    ignore_missing = False
    paths = []

    # Supported parameters:
    # -r Copy recursively (traverse down the tree). Copy all files below SOURCE.
    #    These are all subdirectory leaves in the SOURCE tree.
    # -i Ignore warnings about no matching class and non-existing source directories.
    #    These warnings will not set the exit code to 1.
    # -m mode Set mode for all copied files (mode as octal number).

    parse_m = False
    for index, arg in enumerate(args):
        if parse_m:
            mode = int(arg, 8)
            parse_m = False
        elif arg == "-m":
            parse_m = True
        elif arg == "-r":
            recursive = True
        elif arg == "-i":
            ignore_missing = True
        elif not arg.startswith("-"):
            paths = args[index:]
            break
        else:
            raise ValueError(f"copy-media-files: param {arg} not understood")

    if not paths:
        raise ValueError("copy-media-files: no paths given")

    paths = [path.lstrip("/") for path in paths]

    return CopyFilesArgs(
        mode=mode,
        recursive=recursive,
        ignore_missing=ignore_missing,
        paths=paths,
    )


def do_copy_media_files(conf_dir: Path, chroot_dir: Path, classes: list[str], remaining_args: list[str]) -> int:
    if not remaining_args:
        raise ValueError("copy-media-files: need 2 or more parameters")

    target = remaining_args.pop(0)
    if target not in ("media", "netboot"):
        raise ValueError(f"copy-media-files: target {target} not understood")

    try:
        copy_args = _parse_copy_media_files_args(remaining_args)
    except Exception as except_inst:
        print(f"E: Parsing fcopy_args {remaining_args!r} failed: {except_inst}")
        return 1

    dest_dir = chroot_dir / "grml-live" / target

    return do_copy_files(conf_dir, dest_dir, classes, "media-files", copy_args)


def do_skiptask(dynamic_state: DynamicState, skiptask_args: list[str]) -> int:
    if not skiptask_args:
        return 0
    print(f"I: Requesting skipping of tasks: {' '.join(skiptask_args)}")
    dynamic_state.skip_tasks.update(skiptask_args)
    return 0


def helper_socket_thread(
    tempdir: Path, conf_dir: Path, chroot_dir: Path, classes: list[str], exit_event: Event, dynamic_state: DynamicState
):
    address_family = socket.AF_UNIX
    socket_type = socket.SOCK_STREAM
    request_queue_size = 5

    listen_socket = socket.socket(address_family, socket_type)
    listen_socket.bind(f"{tempdir}/sock")
    listen_socket.listen(request_queue_size)
    listen_socket.settimeout(1)

    while not exit_event.is_set():
        try:
            request_socket, _ = listen_socket.accept()
        except TimeoutError:
            continue

        try:
            request_socket.settimeout(5 * 60)  # 5 minutes
            orig_req = request_socket.recv(4096).decode()
            req = orig_req.split("\n")
            rc = 120
            if len(req) != 2 and req[1] != "":
                print("W: socket thread: got message:", repr(orig_req))
                print("W: socket thread: no newline, message truncated?")
            else:
                req = req[0].split(" ")
                if req[0] == "fcopy":
                    rc = do_fcopy(conf_dir, chroot_dir, classes, req[1:])
                elif req[0] == "copy-media-files":
                    rc = do_copy_media_files(conf_dir, chroot_dir, classes, req[1:])
                elif req[0] == "skiptask":
                    rc = do_skiptask(dynamic_state, req[1:])
                else:
                    print("W: socket thread: request not understood:", repr(orig_req))

            request_socket.send(f"{rc!s}\n".encode())
            request_socket.close()

        except Exception:
            print(f"E: {now_for_log()} helper_socket_thread caught fatal exception", flush=True)
            traceback.print_exc()
            break

    listen_socket.close()


def write_helper_tool(tools_path: Path, tool_name: str, body: str):
    with (tools_path / tool_name).open("wt") as file:
        file.write(body)
        os.fchmod(file.fileno(), 0o755)


@contextlib.contextmanager
def helper_tools(conf_dir: Path, chroot_dir: Path, classes: list[str], dynamic_state: DynamicState):
    tempdir = Path(tempfile.mkdtemp())

    write_helper_tool(
        tempdir,
        "grml-live-command",
        f"""#!/bin/sh
PN=$(basename "$0")
if [ "$PN" = "grml-live-command" ]; then
  PN="$1"
  shift
fi
echo "D: minifai $PN: $(date +%FT%T) requesting $@"
RC=$(echo $PN "$@" | socat -t3600 - UNIX-CONNECT:{tempdir}/sock,forever)
if [ -z "$RC" ]; then
  echo "E: minifai $PN: $(date +%FT%T) got no reply from server"
  exit 119
elif [ "$RC" != "0" ]; then
  echo "E: minifai $PN: server sent error code $RC"
  exit "$RC"
fi
exit 0
""",
    )

    (tempdir / "fcopy").symlink_to(tempdir / "grml-live-command")
    (tempdir / "skiptask").symlink_to(tempdir / "grml-live-command")

    write_helper_tool(
        tempdir,
        "ifclass",
        f"""#!/bin/bash
haystack=:{":".join(classes)}:
if [[ ":$haystack:" = *:$1:* ]]; then
    echo "I: ifclass $1: yes."
    exit 0
else
    echo "I: ifclass $1: no."
    exit 1
fi
""",
    )

    exit_event = Event()
    thread = Thread(
        target=helper_socket_thread,
        args=(tempdir, conf_dir, chroot_dir, classes, exit_event, dynamic_state),
        daemon=False,
    )
    thread.start()
    try:
        yield tempdir
    finally:
        exit_event.set()
        thread.join()
        shutil.rmtree(tempdir, ignore_errors=True)


@contextlib.contextmanager
def policy_rcd(chroot_dir: Path):
    marker = "!MINIFAI!"
    print("I: Installing temporary policy-rc.d")
    program = chroot_dir / "usr" / "sbin" / "policy-rc.d"
    with program.open("wt") as file:
        file.write(f"#!/bin/sh\n# Installed by grml-live minifai {marker}\nexit 101\n")
        os.fchmod(file.fileno(), 0o755)

    try:
        yield
    finally:
        try:
            if marker in program.read_text():
                print(f"I: Cleaning up {program}")
                program.unlink()
            else:
                print(f"I: Not cleaning up {program} - our marker went missing")
        except Exception:
            print(f"W: Failed cleaning up {program}")


def read_envvars_for_classes(conf_dir: Path, classes: list[str]) -> dict:
    """Read environment variable files"""
    env = {}

    for class_name in classes:
        varfile = conf_dir / "env" / class_name
        if varfile.exists():
            env.update(parse_class_varfile(varfile))

    return env


def install_base(conf_dir: Path, chroot_dir: Path, classes, debian_suite: str, mirror_url: str):
    """Install Debian base system from given mirror"""
    print(f'I: Installing Debian base system for suite "{debian_suite}" using mmdebstrap')

    # Work around APT bug: http://bugs.debian.org/1092164
    included_packages = ["netbase"]

    # Allow using https:// sources. Do this unconditionally, so sources added with
    # fcopy /etc/apt just work.
    included_packages.append("ca-certificates")

    # Find keyring to use for mmdebstrap
    keyring_dir = conf_dir / "bootstrap-keyring"
    keyring_file = None
    for class_name in classes:
        if (keyring_dir / class_name).exists():
            keyring_file = keyring_dir / class_name

    # Should use delete_on_close=False, but needs Python >= 3.12
    with tempfile.NamedTemporaryFile(delete=False, dir=chroot_dir) as keyring_tempfile:
        keyring_tempfile.write(keyring_file.read_bytes())
    os.chmod(keyring_tempfile.name, 0o644)
    run_x(["ls", "-la", keyring_tempfile.name])

    args = [
        "mmdebstrap",
        "--format=directory",
        "--variant=required",
        "--verbose",
        "--skip=check/empty",  # grml-live pre-creates directories in chroot, skip emptyness check.
        f"--keyring={keyring_tempfile.name}",
        f"--include={','.join(included_packages)}",
        debian_suite,
        chroot_dir,
        mirror_url,
    ]

    if os.environ.get("GRML_LIVE_DEBUG_APT", "") != "":
        args.insert(1, f"--aptopt={APT_DEBUG_ACQUIRE}")
        args.insert(1, "--chrooted-customize-hook=rm /etc/apt/apt.conf.d/99mmdebstrap")

    run_x(args)
    os.unlink(keyring_tempfile.name)

    # Mark most leaf packages as automatically installed, so autoremove could remove them if possible.
    run_chrooted(chroot_dir, ["apt-mark", "auto", "~i ?not(~prequired) ?not(~pimportant) ?not(~pstandard)"])


def should_skip_task(dynamic_state: DynamicState, task: str) -> bool:
    if task in dynamic_state.skip_tasks:
        print(f'I: Skipping FAI task "{task}", as dynamically requested')
        return True
    return False


def task_updatebase(chroot_dir: Path, dynamic_state: DynamicState):
    if should_skip_task(dynamic_state, "updatebase"):
        return
    run_chrooted(chroot_dir, ["apt", "-oapt::cmd::disable-script-warning=1", "--error-on=any", "update", "-q"])


def _create_dirs(chroot_dir: Path) -> BuildDirectories:
    """Create required directories _inside_ the chroot."""
    # This code is as ugly as it looks.
    build_dir_relative = "grml-live"
    build_dir = chroot_dir / build_dir_relative
    if build_dir.exists():
        print(f'I: Deleting build directory "{build_dir_relative}" from previous run: {build_dir}')
        shutil.rmtree(build_dir)
    print(f"I: Creating build directory: {build_dir}")
    build_dir.mkdir()

    log_dir_name = "log"
    log_dir = build_dir / log_dir_name
    log_dir.mkdir()

    media_dir_name = "media"
    media_dir = build_dir / media_dir_name
    media_dir.mkdir()

    netboot_dir_name = "netboot"
    netboot_dir = build_dir / netboot_dir_name
    netboot_dir.mkdir()

    sources_dir_name = "grml_sources"
    sources_dir = build_dir / sources_dir_name
    sources_dir.mkdir()

    return BuildDirectories(
        build_dir_inside=f"/{build_dir_relative}/",
        build_dir=build_dir,
        log_dir_inside=f"/{build_dir_relative}/{log_dir_name}/",
        log_dir=log_dir,
        media_dir_inside=f"/{build_dir_relative}/{media_dir_name}/",
        media_dir=media_dir,
        netboot_dir_inside=f"/{build_dir_relative}/{netboot_dir_name}/",
        netboot_dir=media_dir,
        sources_dir_inside=f"/{build_dir_relative}/{sources_dir_name}/",
        sources_dir=sources_dir,
    )


def install_class_helper_tools(conf_dir: Path, build_dir: Path, classes: list[str]) -> Path:
    """
    Copy class-config helpers into chroot.

    Later classes will overwrite earlier classes' files. This is intentional.
    """

    class_helper_tools_path = build_dir / "tools"
    class_helper_tools_path.mkdir()
    for class_name in classes:
        class_path = conf_dir / "tools" / class_name
        if not class_path.exists():
            continue
        shutil.copytree(class_path, class_helper_tools_path, symlinks=False, dirs_exist_ok=True)

    return class_helper_tools_path


def _run_tasks(
    conf_dir: Path, chroot_dir: Path, classes: list[str], grml_live_config: Path, fai_action: str, skip_tasks: list[str]
) -> int:
    dynamic_state = DynamicState()
    directories = _create_dirs(chroot_dir)

    # Create a file in there, so grml-live does not complain.
    (directories.log_dir / "minifai").write_text(
        "This chroot was created by grml-live minifai. Not all FAI features are supported.\n"
    )

    # duplicate grml_live_config into the chroot, so chrooted scripts can use it.
    grml_live_config_chroot = directories.build_dir / "config"
    grml_live_config_chroot.write_bytes(grml_live_config.read_bytes())

    do_skiptask(dynamic_state, skip_tasks)

    env = {
        "GRML_LIVE_CONFIG": str(grml_live_config_chroot),
        "GRML_LIVE_BUILDDIR": directories.build_dir_inside,
        "GRML_LIVE_MEDIADIR": directories.media_dir_inside,
        "GRML_LIVE_NETBOOTDIR": directories.netboot_dir_inside,
        "GRML_LIVE_SOURCESDIR": directories.sources_dir_inside,
        "LOGDIR": str(directories.log_dir),
    } | read_envvars_for_classes(conf_dir, classes)
    show_env("Merged class variables", env)

    with helper_tools(conf_dir, chroot_dir, classes, dynamic_state) as helper_tools_path:
        class_helper_tools_path = install_class_helper_tools(conf_dir, directories.build_dir, classes)

        helper_tools_paths = [helper_tools_path, class_helper_tools_path]

        hook_env = env | {"FAI_ACTION": fai_action}
        for class_name in classes:
            run_script(chroot_dir, conf_dir / "hooks" / class_name / "updatebase", helper_tools_paths, hook_env)

        with policy_rcd(chroot_dir):
            task_updatebase(chroot_dir, dynamic_state)

            if not should_skip_task(dynamic_state, "instsoft"):
                install_packages_for_classes(conf_dir, chroot_dir, classes, helper_tools_paths, hook_env, dynamic_state)

        if not should_skip_task(dynamic_state, "configure"):
            for class_name in classes:
                run_class_scripts("scripts", conf_dir, chroot_dir, class_name, helper_tools_paths, env)

        if not should_skip_task(dynamic_state, "build"):
            for class_name in classes:
                run_class_scripts("media-scripts", conf_dir, chroot_dir, class_name, helper_tools_paths, env)

    return 0


def create_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    # path to fai classes, scripts, ...
    parser.add_argument("config", type=Path)
    parser.add_argument("classes")
    parser.add_argument(
        "action",
        choices=[value.value for value in FaiAction.__members__.values()],
        metavar="ACTION",
        help="FAI action to execute (choices: %(choices)s)",
    )
    parser.add_argument("chroot_dir", type=Path)
    parser.add_argument("grml_live_config", type=Path)
    parser.add_argument("debian_suite", type=str)
    parser.add_argument("mirror_url", type=str)
    return parser


def _main(program_name: str, argv: list[str]) -> int:
    print(f"I: {program_name} started with {argv=}")
    args = create_argparser().parse_args(argv[1:])
    print(f"I: {program_name} parsed args: {args}")
    classes = args.classes.split(",")
    print(f"I: Using classes: {classes}")
    conf_dir = args.config.absolute()
    print(f"I: Using conf_dir: {conf_dir}")
    chroot_dir: Path = args.chroot_dir.absolute()
    print(f"I: Using chroot_dir: {args.chroot_dir}")

    if not conf_dir.exists():
        raise ValueError(f"Config directory {conf_dir} does not exist")
    if not chroot_dir.exists():
        raise ValueError(f"Chroot directory {chroot_dir} does not exist")

    try:
        if args.action == FaiAction.BOOTSTRAP:
            install_base(conf_dir, chroot_dir, classes, args.debian_suite, args.mirror_url)
            rc = _run_tasks(conf_dir, chroot_dir, classes, args.grml_live_config, args.action, ["configure"])
        elif args.action == FaiAction.DIRINSTALL:
            install_base(conf_dir, chroot_dir, classes, args.debian_suite, args.mirror_url)
            rc = _run_tasks(conf_dir, chroot_dir, classes, args.grml_live_config, args.action, [])
        elif args.action == FaiAction.SOFTUPDATE:
            rc = _run_tasks(conf_dir, chroot_dir, classes, args.grml_live_config, args.action, [])
        elif args.action == FaiAction.RECONFIGURE:
            rc = _run_tasks(
                conf_dir, chroot_dir, classes, args.grml_live_config, args.action, ["updatebase", "instsoft"]
            )
        elif args.action == FaiAction.REBUILD:
            rc = _run_tasks(
                conf_dir,
                chroot_dir,
                classes,
                args.grml_live_config,
                args.action,
                ["updatebase", "instsoft", "configure"],
            )
        else:
            print(f"E: minifai: Unknown fai action: {args.action!r}")
            rc = 1
    except (ClassFileParsingFailed, FaiScriptFailed):
        # assume exception site already printed relevant info
        rc = 3
    except Exception:
        print(f"E: {now_for_log()} minifai main caught fatal exception")
        traceback.print_exc()
        rc = 2

    print(f"I: minifai exiting with exit code {rc}")
    return rc


def main() -> int:
    return _main(sys.argv[0], sys.argv)


if __name__ == "__main__":
    sys.exit(main())
