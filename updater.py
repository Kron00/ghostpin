"""Release checks and macOS Sparkle integration (signed update installation)."""
import re
import sys
from pathlib import Path

import requests

VERSION = "2.2.1"
RELEASES_URL = "https://github.com/Kron00/ghostpin/releases/latest"


def check_release():
    response = requests.get(
        "https://api.github.com/repos/Kron00/ghostpin/releases/latest",
        headers={"Accept": "application/vnd.github+json"}, timeout=10,
    )
    if response.status_code == 404:
        return {"available": False, "current_version": VERSION, "message": "No published release yet."}
    response.raise_for_status()
    release = response.json()
    tag = release.get("tag_name", "")
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag)
    if not match or release.get("prerelease") or release.get("draft"):
        raise ValueError("The latest release has no supported stable version")
    available = tuple(map(int, match.groups())) > tuple(map(int, VERSION.split(".")))
    return {"available": available, "current_version": VERSION, "latest_version": tag,
            "url": RELEASES_URL}


class NativeUpdater:
    """Retain Sparkle's controller for the entire native app lifetime."""
    def __init__(self):
        self.controller = None
        self.error = None
        self.started = False

    def start(self):
        if self.started:
            return
        self.started = True
        if sys.platform != "darwin" or not getattr(sys, "frozen", False):
            return
        try:
            import objc
            from Foundation import NSBundle
            from PyObjCTools.AppHelper import callAfter
            bundle = NSBundle.bundleWithPath_(str(Path(sys.executable).parent.parent.parent))
            if not bundle.objectForInfoDictionaryKey_("SUPublicEDKey"):
                print("[!] No Sparkle public key in native bundle", file=sys.stderr)
                return
            framework = Path(sys.executable).parent.parent / "Frameworks" / "Sparkle.framework"
            def initialize():
                try:
                    objc.loadBundle("Sparkle", globals(), bundle_path=str(framework))
                    controller_class = objc.lookUpClass("SPUStandardUpdaterController")
                    self.controller = controller_class.alloc().initWithStartingUpdater_updaterDelegate_userDriverDelegate_(
                        True, None, None,
                    )
                    print("[+] Sparkle updater initialized", file=sys.stderr)
                except Exception as exc:
                    self.error = str(exc)
                    print(f"[!] Sparkle initialization failed: {exc}", file=sys.stderr)
            callAfter(initialize)
        except Exception as exc:
            self.error = str(exc)
            print(f"[!] Sparkle unavailable: {exc}", file=sys.stderr)

    def check(self):
        if self.controller is None:
            return {"handled": False, "error": self.error}
        from PyObjCTools.AppHelper import callAfter
        callAfter(self.controller.checkForUpdates_, None)
        return {"handled": True}
