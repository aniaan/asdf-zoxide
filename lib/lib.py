import gzip
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict, Union
from urllib.error import URLError
from urllib.request import Request, urlopen


PlatformType = Literal["darwin", "linux"]
ArchType = Literal["x86_64", "aarch64"]


class FormatKwargs(TypedDict):
    name: str
    repo_name: str
    version: str
    normalize_version: str
    platform: str
    arch: str
    filename: str
    checksum_filename: str


LibTemplate = Union[str, Callable[[FormatKwargs], str]]


def verify_by_sha256sum(file_path: Path, checksum_path: Path):
    print(f"Verifying checksum for {file_path.name}...")
    if not shutil.which("shasum") and not shutil.which("sha256sum"):
        raise Exception("shasum or sha256sum not found")
    with open(checksum_path) as f:
        expected = None
        lines = f.readlines()
        for line in lines:
            if file_path.name in line:
                expected = line.split()[0]
                break
        if not expected and len(lines) == 1:
            expected = lines[0].split()[0]
        if not expected:
            raise Exception(f"Checksum not found for {file_path.name}")

    cmd = ["shasum", "-a", "256"] if shutil.which("shasum") else ["sha256sum"]
    result = subprocess.run([*cmd, file_path], capture_output=True, text=True)
    actual = result.stdout.split()[0]

    if actual != expected:
        raise Exception(
            f"Checksum verification failed: {actual} != {expected} for {file_path.name}"
        )
    print("Checksum verification passed")


def _verify_by_minisign(
    bin_path: str, public_key: str, file_path: Path, signature_path: Path
):
    cmd = [bin_path, "-P", public_key, "-x", signature_path, "-Vm", file_path]
    result = subprocess.run([*cmd, file_path], capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"Error checking signature: {result.stderr}")


_MINISIGN_VERSION = "0.11"
_MINISIGN_CMD = "minisign"


def verify_by_minisign(
    public_key: str, file_path: Path, signature_path: Path, format_kwargs: FormatKwargs
):
    print(f"minisign: Verifying signature for {file_path.name}...")
    if shutil.which(_MINISIGN_CMD):
        _verify_by_minisign(_MINISIGN_CMD, public_key, file_path, signature_path)
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        install_path = Path(tmpdir)
        install_path.mkdir(exist_ok=True)

        install_version(_MINISIGN_CMD, _MINISIGN_VERSION, install_path.as_posix())
        bin_path = install_path / "bin" / _MINISIGN_CMD
        _verify_by_minisign(bin_path.as_posix(), public_key, file_path, signature_path)

    print(f"minisign: verification passed {file_path.name}")


publish_at_sort_version_key = lambda x: datetime.strptime(
    x["published_at"], "%Y-%m-%dT%H:%M:%SZ"
)


@dataclass(kw_only=True)
class Plugin:
    name: str
    cmd: str
    repo_name: str
    filename_template: LibTemplate = ""
    checksum_stage: Literal["download", "extract"] = "download"
    checksum_filename_template: LibTemplate = ""
    bin_path: LibTemplate = ""
    platform_map: dict[PlatformType, str] | None = None
    arch_map: dict[ArchType, str] | None = None
    recover_raw_version: Callable[[str], str] = lambda x: x
    normalize_version: Callable[[str], str] = lambda x: x.removeprefix("v")
    custom_copy: Callable[["Plugin", Path, Path, FormatKwargs], None] | None = None
    is_compressed: bool = True
    # list version filter
    release_filter: Callable[[dict], bool] = lambda _: True
    custom_checker: Callable[[Path, Path, FormatKwargs], None] | None = None
    sort_version_key: Callable[[dict], Any] = publish_at_sort_version_key


def get_plugin(plugin_name: str) -> Plugin:
    plugin_config_path = Path(__file__).parent / "plugins" / f"{plugin_name}.py"

    if not plugin_config_path.exists():
        raise Exception(f"Plugin config not found: {plugin_name}")

    import importlib.util

    spec = importlib.util.spec_from_file_location("plugin_config", plugin_config_path)
    module = importlib.util.module_from_spec(spec)  # type: ignore
    spec.loader.exec_module(module)  # type: ignore

    return module.PLUGIN


API_RELEASE_URL = "https://api.github.com/repos/{repo_name}/releases"
GITHUB_URL = "https://github.com/{repo_name}"
DOWNLOAD_BASE_URL = GITHUB_URL + "/releases/download/{version}"
BINARY_URL = DOWNLOAD_BASE_URL + "/{filename}"
CHECKSUM_URL = DOWNLOAD_BASE_URL + "/{checksum_filename}"


def list_repo_url(plugin_name: str) -> str:
    plugin = get_plugin(plugin_name)
    return GITHUB_URL.format(repo_name=plugin.repo_name)


def list_version(plugin_name: str, with_published_at: bool = False) -> str:
    plugin = get_plugin(plugin_name)
    url = API_RELEASE_URL.format(repo_name=plugin.repo_name)

    try:
        with urlopen(url) as response:
            releases = json.loads(response.read())

        sorted_releases = sorted(
            filter(plugin.release_filter, releases),
            key = plugin.sort_version_key,
            reverse=True,
        )

        recent_versions = sorted_releases[:10]

        if with_published_at:
            versions = [
                plugin.normalize_version(release["tag_name"])
                + "#"
                + release["published_at"]
                for release in recent_versions
            ]
        else:
            versions = [
                plugin.normalize_version(release["tag_name"])
                for release in recent_versions
            ]
        versions = list(reversed(versions))

        return "\n".join(versions)

    except URLError as e:
        raise Exception(f"get version failed: {str(e)}")


def get_system_info() -> tuple[PlatformType, ArchType]:
    system = platform.system().lower()
    if system == "darwin":
        plat = "darwin"
    elif system == "linux":
        plat = "linux"
    else:
        raise Exception(f"Unsupported platform: {system}")

    machine = platform.machine().lower()
    if machine in ["x86_64", "amd64"]:
        arch = "x86_64"
    elif machine in ["arm64", "aarch64"]:
        arch = "aarch64"
    else:
        raise Exception(f"Unsupported architecture: {machine}")

    return plat, arch


def get_normalize_version(version: str) -> str:
    return version.lstrip("v")


def format_template(template: LibTemplate, format_kwargs: FormatKwargs) -> str:
    if callable(template):
        return template(format_kwargs)
    return template.format(**format_kwargs)


def extract(download_path: Path, extract_path: Path, bin_path: str):
    filename = download_path.name
    if filename.endswith(".tar.gz"):
        with tarfile.open(download_path, mode="r:gz") as tar:
            tar.extractall(extract_path, filter="data")
    elif filename.endswith(".tar.xz"):
        with tarfile.open(download_path, mode="r:xz") as tar:
            tar.extractall(extract_path, filter="data")
    elif filename.endswith(".gz"):
        dst = extract_path / bin_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(download_path, "rb") as f_in:
            dst.write_bytes(f_in.read())
    elif filename.endswith(".zip"):
        with zipfile.ZipFile(download_path, "r") as zip_ref:
            zip_ref.extractall(extract_path)
    else:
        raise Exception(f"Unsupported file type: {filename}")


def _download_file(url: str, download_path: Path):
    print(f"Downloading {url} ...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    req = Request(url=url, headers=headers)

    with urlopen(req) as response:
        download_path.write_bytes(response.read())


def _get_checker(
    plugin: Plugin,
    download_dir: Path,
    checksum_filename: str,
    format_kwargs: FormatKwargs,
) -> Callable[[Path], None]:
    if not checksum_filename:
        return lambda _: None

    checksum_url = (
        checksum_filename
        if checksum_filename.startswith("https")
        else CHECKSUM_URL.format(
            **format_kwargs,
        )
    )
    checksum_path = download_dir / Path(checksum_filename).name
    _download_file(url=checksum_url, download_path=checksum_path)

    if plugin.custom_checker:
        checker = lambda file_path: plugin.custom_checker(
            file_path, checksum_path, format_kwargs
        )  # type: ignore
    else:
        checker = lambda file_path: verify_by_sha256sum(file_path, checksum_path)

    return checker


def install_version(plugin_name: str, normalize_version: str, install_path: str):
    plugin = get_plugin(plugin_name)
    plat, arch = get_system_info()

    platform_name = plugin.platform_map[plat] if plugin.platform_map else plat
    arch_name = plugin.arch_map[arch] if plugin.arch_map else arch

    version = plugin.recover_raw_version(normalize_version)

    format_kwargs: FormatKwargs = {
        "name": plugin.name,
        "repo_name": plugin.repo_name,
        "version": version,
        "normalize_version": normalize_version,
        "platform": platform_name,
        "arch": arch_name,
        "filename": "",
        "checksum_filename": "",
    }

    filename = format_template(plugin.filename_template, format_kwargs)
    format_kwargs["filename"] = filename
    checksum_filename = format_template(
        plugin.checksum_filename_template, format_kwargs
    )
    format_kwargs["checksum_filename"] = checksum_filename

    bin_path = format_template(plugin.bin_path, format_kwargs)

    download_url = (
        filename if filename.startswith("https") else BINARY_URL.format(**format_kwargs)
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        download_path = tmp_path / Path(filename).name

        _download_file(url=download_url, download_path=download_path)

        checker = _get_checker(
            plugin=plugin,
            download_dir=tmp_path,
            checksum_filename=checksum_filename,
            format_kwargs=format_kwargs,
        )

        if plugin.checksum_stage == "download":
            checker(download_path)

        extract_path = tmp_path / "extract"
        extract_path.mkdir(exist_ok=True)

        if plugin.is_compressed:
            extract(
                download_path=download_path,
                extract_path=extract_path,
                bin_path=bin_path,
            )
        else:
            shutil.copy2(download_path, extract_path / bin_path)

        if plugin.checksum_stage == "extract":
            checker(extract_path / bin_path)

        if not plugin.custom_copy:
            print(f"{plugin.name}: Using default copy function...")
            src = extract_path / bin_path
            if not src.exists():
                raise Exception(f"Binary file not found: {src}")

            dst = Path(install_path) / "bin"
            dst.mkdir(parents=True, exist_ok=True)
            dst = dst / plugin.cmd

            shutil.copy2(src, dst)
            dst.chmod(0o755)
        else:
            print(f"{plugin.name} Using custom copy function...")
            plugin.custom_copy(plugin, extract_path, Path(install_path), format_kwargs)

    print(f"{plugin.name} Installation completed successfully!")


def main():
    if len(sys.argv) < 3:
        print("Usage:")
        print("  list <plugin_name>")
        print("  install <plugin_name> <version> <install_path>")
        sys.exit(1)

    command = sys.argv[1]
    plugin_name = sys.argv[2]

    if command == "list":
        print(list_version(plugin_name))
    elif command == "install":
        if len(sys.argv) != 5:
            print("Usage: install <plugin_name> <version> <install_path>")
            sys.exit(1)
        version = sys.argv[3]
        install_path = os.path.abspath(sys.argv[4])
        install_version(plugin_name, version, install_path)
    else:
        print(f"Unknown command: {command}")
        print("Available commands: list, install")
        sys.exit(1)


if __name__ == "__main__":
    main()
