#!/usr/bin/env python3
"""Fetch and verify the current Windows release published by wow.aethus.pl.

This downloads official artifacts at run time. It does not compile the official client
from source and does not put the executable or game data into Git.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache" / "aethus-release"
OUT = ROOT / "out" / "live-release"
EXE_MANIFEST_URL = "https://wow.aethus.pl/exe/latest.json"
PATCH_MANIFEST_URL = "https://wow.aethus.pl/exe/data/patches.json"
EXE_PARTS_BASE = "https://wow.aethus.pl/exe/"
PATCH_PARTS_BASE = "https://wow.aethus.pl/exe/data/"
BLOCK_SIZE = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(BLOCK_SIZE):
            digest.update(block)
    return digest.hexdigest()


def curl_download(url: str, destination: Path, expected_size: int | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".download")
    temporary.unlink(missing_ok=True)

    command = [
        "curl", "--http1.1", "--fail", "--location", "--silent", "--show-error",
        "--retry", "2", "--connect-timeout", "20", "--max-time", "300",
        "--user-agent", "Mozilla/5.0", "--output", str(temporary), url,
    ]
    result = subprocess.run(command, check=False)
    if result.returncode:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"curl failed with exit code {result.returncode}: {url}")

    actual_size = temporary.stat().st_size
    if expected_size is not None and actual_size != expected_size:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            f"download size mismatch for {url}: got {actual_size}, expected {expected_size}"
        )
    os.replace(temporary, destination)


def fetch_json(url: str, destination: Path) -> dict:
    curl_download(url, destination)
    try:
        with destination.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid JSON from {url}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object from {url}")
    return value


def validated_parts(artifact: dict, description: str) -> list[dict]:
    parts = artifact.get("parts")
    if not isinstance(parts, list) or not parts:
        raise RuntimeError(f"{description}: manifest contains no parts")
    checked = []
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            raise RuntimeError(f"{description}: invalid part #{index}")
        relative_url = part.get("url")
        size = part.get("size")
        if not isinstance(relative_url, str) or not relative_url:
            raise RuntimeError(f"{description}: part #{index} has no URL")
        relative_path = PurePosixPath(relative_url)
        if relative_path.is_absolute() or ".." in relative_path.parts or "\\" in relative_url:
            raise RuntimeError(f"{description}: unsafe relative part URL: {relative_url!r}")
        if not isinstance(size, int) or size < 1:
            raise RuntimeError(f"{description}: invalid compressed size for part #{index}")
        checked.append({"url": relative_url, "size": size})
    return checked


def artifact_matches(path: Path, expected_size: int, expected_hash: str) -> bool:
    if not path.is_file() or path.stat().st_size != expected_size:
        return False
    return sha256_file(path).lower() == expected_hash.lower()


def reconstruct_gzip_artifact(
    *,
    kind: str,
    base_url: str,
    artifact: dict,
    output: Path,
    description: str,
) -> None:
    expected_size = artifact.get("size")
    expected_hash = artifact.get("sha256")
    if not isinstance(expected_size, int) or expected_size < 1:
        raise RuntimeError(f"{description}: invalid decompressed size in manifest")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise RuntimeError(f"{description}: invalid SHA-256 in manifest")

    parts = validated_parts(artifact, description)
    if artifact_matches(output, expected_size, expected_hash):
        print(f"Already current: {output.relative_to(ROOT)}")
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    cache_dir = CACHE / "parts" / kind / expected_hash.lower()
    cached_parts: list[Path] = []

    for part in parts:
        relative_path = PurePosixPath(part["url"])
        cached = cache_dir.joinpath(*relative_path.parts)
        url = urljoin(base_url, part["url"])
        if cached.is_file() and cached.stat().st_size == part["size"]:
            print(f"Using cached part: {part['url']}")
        else:
            print(f"Downloading {description} part: {part['url']}")
            curl_download(url, cached, part["size"])
        cached_parts.append(cached)

    for attempt in range(2):
        joined_name = None
        output_temporary = output.with_name(output.name + ".download")
        output_temporary.unlink(missing_ok=True)
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f"{kind}-", suffix=".joined.gz", dir=CACHE, delete=False
            ) as joined:
                joined_name = joined.name
                for cached in cached_parts:
                    with cached.open("rb") as part_stream:
                        shutil.copyfileobj(part_stream, joined, length=BLOCK_SIZE)

            digest = hashlib.sha256()
            total_size = 0
            with gzip.open(joined_name, "rb") as compressed, output_temporary.open("wb") as target:
                while block := compressed.read(BLOCK_SIZE):
                    target.write(block)
                    digest.update(block)
                    total_size += len(block)

            actual_hash = digest.hexdigest()
            if total_size == expected_size and actual_hash.lower() == expected_hash.lower():
                os.replace(output_temporary, output)
                print(f"Verified: {output.relative_to(ROOT)}")
                print(f"  size:   {total_size} bytes")
                print(f"  SHA256: {actual_hash}")
                return

            problem = (
                f"verification mismatch: size {total_size}/{expected_size}, "
                f"SHA-256 {actual_hash}/{expected_hash}"
            )
        except (OSError, EOFError) as exc:
            problem = f"gzip/decompression failed: {exc}"
        finally:
            output_temporary.unlink(missing_ok=True)
            if joined_name:
                Path(joined_name).unlink(missing_ok=True)

        if attempt == 0:
            print(f"Warning: {description} {problem}; refreshing cached parts and retrying.")
            for cached in cached_parts:
                cached.unlink(missing_ok=True)
            for part, cached in zip(parts, cached_parts):
                url = urljoin(base_url, part["url"])
                curl_download(url, cached, part["size"])
        else:
            raise RuntimeError(f"{description}: {problem}")


def main() -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)

    print("Fetching the current official release manifest...")
    release = fetch_json(EXE_MANIFEST_URL, CACHE / "latest.json")
    version = release.get("version")
    if not isinstance(version, str) or not version:
        raise RuntimeError("latest.json does not contain a valid version")
    print(f"Official version: {version}")

    reconstruct_gzip_artifact(
        kind="exe",
        base_url=EXE_PARTS_BASE,
        artifact=release,
        output=OUT / "AzerothSurvivors.exe",
        description=f"Windows executable {version}",
    )

    print("Fetching the official patch manifest...")
    patches_manifest = fetch_json(PATCH_MANIFEST_URL, CACHE / "patches.json")
    patches = patches_manifest.get("patches")
    if not isinstance(patches, list):
        raise RuntimeError("patches.json does not contain a patches array")

    for patch in patches:
        if not isinstance(patch, dict):
            raise RuntimeError("patches.json contains an invalid patch entry")
        name = patch.get("name")
        if not isinstance(name, str) or not name or Path(name).name != name or name in {".", ".."}:
            raise RuntimeError(f"unsafe patch filename: {name!r}")
        reconstruct_gzip_artifact(
            kind="patch",
            base_url=PATCH_PARTS_BASE,
            artifact=patch,
            output=OUT / "Data" / name,
            description=f"patch {name}",
        )

    shutil.copy2(CACHE / "latest.json", OUT / "latest.json")
    shutil.copy2(CACHE / "patches.json", OUT / "patches.json")
    readme = OUT / "README.txt"
    readme.write_text(
        "Official Azeroth Survivors files fetched from wow.aethus.pl.\n"
        f"Release version: {version}\n\n"
        "The executable and the listed patch files were verified against the SHA-256\n"
        "and size values in the official manifests. This folder does NOT include the\n"
        "base World of Warcraft client MPQs. Supply those files from a client you are\n"
        "entitled to use; they are deliberately not redistributed by this repository.\n\n"
        "This target downloads the published official Windows build; it does not\n"
        "compile that build from source. The repository's source build is separate.\n",
        encoding="utf-8",
    )
    print()
    print(f"Finished. Verified official artifacts are in: {OUT.relative_to(ROOT)}")
    print("Base WoW client MPQs are not included.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
