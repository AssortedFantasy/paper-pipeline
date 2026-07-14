from __future__ import annotations

import argparse
import importlib.metadata
import site
import threading
from pathlib import Path

OLD_RASTERIZE_SNIPPET = "pypdfium2.PdfDocument(pdf)"
NEW_RASTERIZE_SNIPPET = "pypdfium2.PdfDocument(str(pdf))"
RASTERIZE_PILS_SNIPPET = "    pils = []\n"
RASTERIZE_OPEN_SNIPPET = (
    "        if isinstance(pdf, (str, Path)):\n"
    "            pdf = pypdfium2.PdfDocument(str(pdf))\n"
)
RASTERIZE_SAFE_OPEN_SNIPPET = (
    "        if isinstance(pdf, (str, Path)):\n"
    "            opened_pdf = pypdfium2.PdfDocument(str(pdf))\n"
    "            pdf = opened_pdf\n"
)
RASTERIZE_RETURN_SNIPPET = (
    "    except Exception as e:\n"
    "        logging.error(e)\n"
    "    if return_pil:\n"
    "        return pils\n"
)
RASTERIZE_SAFE_RETURN_SNIPPET = (
    "    except Exception as e:\n"
    "        logging.error(e)\n"
    "    finally:\n"
    "        if opened_pdf is not None:\n"
    "            try:\n"
    "                opened_pdf.close()\n"
    "            except OSError as exc:\n"
    "                logging.warning(\n"
    "                    'suppressed PdfDocument close failure during Nougat rasterization: %s',\n"
    "                    exc,\n"
    "                )\n"
    "    if return_pil:\n"
    "        return pils\n"
)
PACKAGE_NAMES = (
    "nougat-ocr",
    "transformers",
    "albumentations",
    "pypdfium2",
    "torch",
    "torchvision",
)

_ENSURE_LOCK = threading.Lock()
_ENSURE_STATUS: str | None = None


def find_site_packages() -> list[Path]:
    paths: list[Path] = []
    for value in site.getsitepackages():
        path = Path(value)
        if path.exists():
            paths.append(path)

    user_site = site.getusersitepackages()
    if user_site:
        path = Path(user_site)
        if path.exists() and path not in paths:
            paths.append(path)
    return paths


def patch_rasterize_file(rasterize_path: Path) -> str:
    content = rasterize_path.read_text(encoding="utf-8")
    if "suppressed PdfDocument close failure during Nougat rasterization" in content:
        return "already-patched"
    if OLD_RASTERIZE_SNIPPET in content:
        content = content.replace(OLD_RASTERIZE_SNIPPET, NEW_RASTERIZE_SNIPPET)

    if RASTERIZE_OPEN_SNIPPET not in content or RASTERIZE_RETURN_SNIPPET not in content:
        return "unexpected-content"

    if RASTERIZE_PILS_SNIPPET in content:
        content = content.replace(
            RASTERIZE_PILS_SNIPPET, "    pils = []\n    opened_pdf = None\n", 1
        )
    content = content.replace(RASTERIZE_OPEN_SNIPPET, RASTERIZE_SAFE_OPEN_SNIPPET, 1)
    content = content.replace(
        RASTERIZE_RETURN_SNIPPET, RASTERIZE_SAFE_RETURN_SNIPPET, 1
    )

    rasterize_path.write_text(
        content,
        encoding="utf-8",
    )
    return "patched"


def inspect_environment() -> dict[str, str]:
    versions: dict[str, str] = {}
    for package_name in PACKAGE_NAMES:
        try:
            version = importlib.metadata.version(package_name)
        except importlib.metadata.PackageNotFoundError:
            version = "missing"
        versions[package_name] = version
    return versions


def print_environment_report() -> None:
    for package_name, version in inspect_environment().items():
        print(f"{package_name}={version}")


def resolve_default_nougat_command(workspace_root: Path) -> str:
    from .runner import default_nougat_command

    return default_nougat_command(workspace_root)


def ensure_nougat_compatibility(*, workspace_root: Path | None = None) -> list[str]:
    """Verify the Nougat runtime and apply the known rasterize patch if needed.

    The intent is that normal users do not need to learn or remember an extra
    maintenance command. The patch is applied lazily the first time the runtime
    is actually needed.
    """

    global _ENSURE_STATUS

    with _ENSURE_LOCK:
        if _ENSURE_STATUS == "ready":
            return []

        versions = inspect_environment()
        missing = [name for name, version in versions.items() if version == "missing"]
        if missing:
            missing_text = ", ".join(missing)
            raise RuntimeError(
                "Nougat runtime is incomplete. Run `uv sync` to install the project dependencies. "
                f"Missing packages: {missing_text}."
            )

        candidates = [
            base / "nougat" / "dataset" / "rasterize.py"
            for base in find_site_packages()
        ]
        existing = [path for path in candidates if path.exists()]

        if not existing:
            raise RuntimeError(
                "Nougat is installed but nougat/dataset/rasterize.py was not found in the current environment."
            )

        messages: list[str] = []
        for rasterize_path in existing:
            result = patch_rasterize_file(rasterize_path)
            messages.append(f"{rasterize_path}: {result}")
            if result == "unexpected-content":
                raise RuntimeError(
                    "Nougat compatibility patch could not be applied because the upstream file format changed."
                )

        _ENSURE_STATUS = "ready"
        return messages


def doctor_report(workspace_root: Path | None = None) -> list[str]:
    workspace_root = (workspace_root or Path(".")).resolve()
    lines = ["Environment:"]
    versions = inspect_environment()
    for package_name in PACKAGE_NAMES:
        lines.append(f"- {package_name}: {versions[package_name]}")

    lines.append("")
    lines.append(
        f"Default nougat command: {resolve_default_nougat_command(workspace_root)}"
    )

    try:
        patch_messages = ensure_nougat_compatibility(workspace_root=workspace_root)
    except RuntimeError as exc:
        lines.append(f"Compatibility check: failed ({exc})")
        return lines

    if patch_messages:
        lines.append("Compatibility patch:")
        lines.extend(f"- {message}" for message in patch_messages)
    else:
        lines.append("Compatibility patch: already satisfied")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or repair the local Nougat runtime used by this project."
    )
    parser.add_argument(
        "--inspect-only",
        action="store_true",
        help="Print installed package versions without editing site-packages.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("."),
        help="Workspace root used when resolving the default nougat command.",
    )
    args = parser.parse_args()

    print_environment_report()
    if args.inspect_only:
        return 0

    try:
        for line in doctor_report(args.workspace.resolve()):
            print(line)
    except RuntimeError as exc:
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
