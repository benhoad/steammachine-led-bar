#!/usr/bin/env python3
"""Simulate a Steam download by writing a fake appmanifest.

Creates ``<root>/steamapps/appmanifest_<appid>.acf`` and updates it every
half second the way the Steam client does, so ``ledbar`` can be exercised
end-to-end without a real download:

    python3 tools/fake_steam.py --root /tmp/fakesteam --seconds 20 &
    LEDBAR_STEAM_PATHS=/tmp/fakesteam LEDBAR_REQUIRE_STEAM_PROCESS=0 \\
        python3 -m ledbar --backend terminal run
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

FLAG_UPDATE_REQUIRED = 2
FLAG_FULLY_INSTALLED = 4
FLAG_UPDATE_STARTED = 1024
FLAG_DOWNLOADING = 1048576
FLAG_STAGING = 2097152
FLAG_COMMITTING = 4194304


def write_manifest(path: Path, appid: int, name: str, flags: int, to_dl: int, dl: int, to_stage: int, staged: int) -> None:
    text = (
        '"AppState"\n{\n'
        f'\t"appid"\t\t"{appid}"\n'
        '\t"Universe"\t\t"1"\n'
        f'\t"name"\t\t"{name}"\n'
        f'\t"StateFlags"\t\t"{flags}"\n'
        f'\t"installdir"\t\t"{name}"\n'
        f'\t"LastUpdated"\t\t"{int(time.time())}"\n'
        f'\t"SizeOnDisk"\t\t"{to_dl}"\n'
        f'\t"BytesToDownload"\t\t"{to_dl}"\n'
        f'\t"BytesDownloaded"\t\t"{dl}"\n'
        f'\t"BytesToStage"\t\t"{to_stage}"\n'
        f'\t"BytesStaged"\t\t"{staged}"\n'
        '\t"UserConfig"\n\t{\n\t\t"language"\t\t"english"\n\t}\n'
        '}\n'
    )
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default="/tmp/fakesteam", help="fake Steam root (default /tmp/fakesteam)")
    parser.add_argument("--appid", type=int, default=440)
    parser.add_argument("--name", default="Demo Game")
    parser.add_argument("--seconds", type=float, default=20.0, help="download duration")
    parser.add_argument("--stage-seconds", type=float, default=4.0, help="install (staging) duration")
    parser.add_argument("--size-gb", type=float, default=12.0)
    parser.add_argument("--interval", type=float, default=0.5, help="manifest rewrite interval")
    parser.add_argument("--pause-at", type=float, default=None, help="pause for 5s at this fraction (0..1)")
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()

    root = Path(args.root)
    steamapps = root / "steamapps"
    steamapps.mkdir(parents=True, exist_ok=True)
    (steamapps / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t\t"label"\t\t""\n\t}\n}\n' % root, encoding="utf-8"
    )
    manifest = steamapps / f"appmanifest_{args.appid}.acf"
    downloading = steamapps / "downloading" / str(args.appid)
    total = int(args.size_gb * 1024 ** 3)
    print(f"fake Steam root: {root}")
    print(f"run:  LEDBAR_STEAM_PATHS={root} LEDBAR_REQUIRE_STEAM_PROCESS=0 python3 -m ledbar --backend terminal run")

    while True:
        write_manifest(manifest, args.appid, args.name, FLAG_UPDATE_REQUIRED | FLAG_UPDATE_STARTED, total, 0, 0, 0)
        downloading.mkdir(parents=True, exist_ok=True)
        time.sleep(1.0)
        started = time.time()
        paused_done = False
        while True:
            fraction = min(1.0, (time.time() - started) / args.seconds)
            if args.pause_at is not None and not paused_done and fraction >= args.pause_at:
                print("paused")
                write_manifest(manifest, args.appid, args.name, FLAG_UPDATE_REQUIRED | FLAG_UPDATE_STARTED | 512,
                               total, int(total * fraction), 0, 0)
                time.sleep(5.0)
                started += 5.0
                paused_done = True
                print("resumed")
            flags = FLAG_UPDATE_REQUIRED | FLAG_UPDATE_STARTED | FLAG_FULLY_INSTALLED | FLAG_DOWNLOADING
            write_manifest(manifest, args.appid, args.name, flags, total, int(total * fraction), 0, 0)
            sys.stdout.write(f"\rdownloading {fraction * 100:5.1f}%")
            sys.stdout.flush()
            if fraction >= 1.0:
                break
            time.sleep(args.interval)
        print()
        started = time.time()
        while True:
            fraction = min(1.0, (time.time() - started) / args.stage_seconds)
            flags = FLAG_UPDATE_REQUIRED | FLAG_UPDATE_STARTED | FLAG_FULLY_INSTALLED | FLAG_STAGING
            write_manifest(manifest, args.appid, args.name, flags, total, total, total, int(total * fraction))
            sys.stdout.write(f"\rinstalling  {fraction * 100:5.1f}%")
            sys.stdout.flush()
            if fraction >= 1.0:
                break
            time.sleep(args.interval)
        print()
        write_manifest(manifest, args.appid, args.name, FLAG_UPDATE_STARTED | FLAG_FULLY_INSTALLED | FLAG_COMMITTING, total, total, total, total)
        time.sleep(1.0)
        write_manifest(manifest, args.appid, args.name, FLAG_FULLY_INSTALLED, total, total, 0, 0)
        shutil.rmtree(downloading, ignore_errors=True)
        print("installed")
        if not args.loop:
            return 0
        time.sleep(6.0)


if __name__ == "__main__":
    raise SystemExit(main())
