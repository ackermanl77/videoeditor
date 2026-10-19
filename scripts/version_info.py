"""Render the Windows version resource (version_info.txt) from ``core.__version__``.

build.spec calls :func:`write_version_file` so the exe's Properties > Details
always show the app's real version; ``version_info.txt`` in the repo is only
the template (``{filevers}`` / ``{version}`` placeholders).
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "version_info.txt")


def app_version() -> str:
    """``__version__`` read from core/__init__.py without importing the package."""
    with open(os.path.join(ROOT, "core", "__init__.py"), "r", encoding="utf-8") as f:
        match = re.search(r'^__version__\s*=\s*"([^"]+)"', f.read(), re.MULTILINE)
    if not match:
        raise RuntimeError("core/__init__.py defines no __version__")
    return match.group(1)


def version_tuple(version: str) -> tuple:
    """``"0.2.0"`` -> ``(0, 2, 0, 0)`` (four numeric parts, as VSVersionInfo needs)."""
    parts = [int(p) for p in re.findall(r"\d+", version)[:4]]
    return tuple(parts + [0] * (4 - len(parts)))


def render(version: str, template_path: str = TEMPLATE) -> str:
    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()
    numbers = version_tuple(version)
    return template.replace("{filevers}", repr(numbers)).replace("{version}", ".".join(map(str, numbers)))


# K8: PyInstaller's default manifest plus activeCodePage UTF-8 (narrow-API callers -- FFmpeg's
# own argument parsing included -- see non-ASCII paths intact) and PerMonitorV2 DPI awareness
# (Qt sets it too; declaring it means the very first window is already sharp).
MANIFEST_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security>
      <requestedPrivileges>
        <requestedExecutionLevel level="asInvoker" uiAccess="false"></requestedExecutionLevel>
      </requestedPrivileges>
    </security>
  </trustInfo>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">
    <application>
      <supportedOS Id="{e2011457-1546-43c5-a5fe-008deee3d3f0}"></supportedOS>
      <supportedOS Id="{35138b9a-5d96-4fbd-8e2d-a2440225f93a}"></supportedOS>
      <supportedOS Id="{4a2f28e3-53b9-4441-ba9c-d69d4a4a6e38}"></supportedOS>
      <supportedOS Id="{1f676c76-80e1-4239-95bb-83d0f6d0da78}"></supportedOS>
      <supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"></supportedOS>
    </application>
  </compatibility>
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings>
      <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
      <activeCodePage xmlns="http://schemas.microsoft.com/SMI/2019/WindowsSettings">UTF-8</activeCodePage>
      <dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true/pm</dpiAware>
      <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2</dpiAwareness>
    </windowsSettings>
  </application>
  <dependency>
    <dependentAssembly>
      <assemblyIdentity type="win32" name="Microsoft.Windows.Common-Controls" version="6.0.0.0" processorArchitecture="*" publicKeyToken="6595b64144ccf1df" language="*"></assemblyIdentity>
    </dependentAssembly>
  </dependency>
</assembly>
"""


def write_manifest(out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "VideoEditor.manifest")
    with open(path, "w", encoding="utf-8") as f:
        f.write(MANIFEST_XML)
    return path


def write_version_file(out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "version_info.generated.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render(app_version()))
    return path


def check_tag(tag: str) -> int:
    """Release guard: ``v0.2.0`` must match ``core.__version__``; prints and returns an exit code."""
    expected = "v" + app_version()
    if tag != expected:
        print(f"Tag {tag!r} doesn't match core.__version__ ({expected!r}). Bump core/__init__.py or retag.")
        return 1
    print(f"Tag {tag} matches core.__version__.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--check-tag":
        sys.exit(check_tag(sys.argv[2]))
    print(app_version())
