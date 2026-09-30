#!/usr/bin/env python3
"""Build the legacy or ESM entry point from the shared GNOME 42 source."""
import json
import shutil
import sys
from pathlib import Path


def build(source: Path, destination: Path, shell_version: int) -> None:
    if shell_version not in (42, 45, 46):
        raise ValueError(f"Unsupported GNOME Shell {shell_version}; supported: 42, 45, 46")

    script = (source / "extension.js").read_text()
    metadata = json.loads((source / "metadata.json").read_text())
    if shell_version >= 45:
        # Keep all UI logic in one source; only the module API differs in GNOME 45+.
        start = script.index("// 表示するプロバイダ。")
        end = script.index("\nclass Extension {", start)
        script = """import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Pango from 'gi://Pango';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

""" + script[start:end] + """

export default class RateLimitExtension extends Extension {
    enable() {
        this._indicator = new AiIndicator();
        Main.panel.addToStatusArea(this.uuid, this._indicator, 1, 'center');
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
"""
        metadata["shell-version"] = ["45", "46"]

    destination.mkdir(parents=True, exist_ok=True)
    (destination / "extension.js").write_text(script)
    (destination / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    shutil.copyfile(source / "stylesheet.css", destination / "stylesheet.css")


if __name__ == "__main__":
    try:
        build(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]))
    except (ValueError, OSError) as error:
        sys.exit(str(error))
