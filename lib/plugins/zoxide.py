import shutil
import sys
from pathlib import Path

parent_dir = Path(__file__).parent.parent.parent
if str(parent_dir) not in sys.path:
    sys.path.insert(0, str(parent_dir))

from lib.lib import FormatKwargs, Plugin


def _copy(plugin: Plugin, src: Path, dst: Path, format_kwargs: FormatKwargs):
    # src = src / f"{format_kwargs['filename'].rstrip('.tar.gz')}/"
    if not src.exists():
        raise Exception(f"Source path {src} does not exist")
    dst = dst / "bin"
    shutil.copytree(src, dst, dirs_exist_ok=True)
    cmd = dst / plugin.cmd
    cmd.chmod(0o755)


PLUGIN = Plugin(
    name="zoxide",
    cmd="zoxide",
    repo_name="ajeetdsouza/zoxide",
    filename_template="zoxide-{normalize_version}-{arch}-{platform}.tar.gz",
    bin_path=lambda kwargs: f"{kwargs['filename'].rstrip('.tar.gz')}/zoxide",
    platform_map={
        "darwin": "apple-darwin",
        "linux": "unknown-linux-musl",
    },
    recover_raw_version=lambda x: f"v{x}",
    custom_copy=_copy,
)
