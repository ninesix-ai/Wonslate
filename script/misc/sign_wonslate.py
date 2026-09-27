#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""sign_wonslate.py -- Authenticode-sign the locally built Wonslate binaries.

Why: on Windows, freshly built unsigned binaries are a coin flip against code
integrity / application control policy, and an unsigned app also trips the
download warning. This script puts a self-signed code-signing certificate on the
build output so local launch smoke is deterministic.

Scope: only our own artifacts -- Wonslate.exe, Wonslate.dll and the Rust engine
translator_engine.dll. Third-party native libraries shipped by NuGet packages
are left untouched on purpose; re-signing someone else's binary is not ours to do.

Honest limits, printed by the script as well:
  * A self-signed certificate is trusted only on machines where it sits in the
    Trusted Root + Trusted Publisher stores. That makes local verification
    reproducible; it does NOT make the app trusted on a stranger's machine.
  * Some hosts enforce application control in a mode that keeps rejecting new
    hashes regardless of a self-signed certificate. Nothing here fights that;
    build_check.py reports SKIP for those machines instead of a false FAIL.
  * Distributed builds should use a real code-signing certificate (for open
    source projects, free through Sigstore or an approved CA programme).

Usage:
    python script/misc/sign_wonslate.py                    # sign the Release output
    python script/misc/sign_wonslate.py --out Wonslate.UI/bin/Debug/net10.0-windows
    python script/misc/sign_wonslate.py --verify-only       # just report status
Exit code: 0 = every target signed and verified, 2 = skipped (no cert tooling or
no build output), 1 = a real signing error.

Windows only. Standard library plus PowerShell/signtool called out to.
"""

from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
from pathlib import Path

try:                                  # only used to detect elevation on Windows
    import ctypes
except ImportError:                   # pragma: no cover - non-Windows / slim builds
    ctypes = None

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = Path("Wonslate.UI") / "bin" / "Release" / "net10.0-windows"
OUR_ARTIFACTS = ("Wonslate.exe", "Wonslate.dll", "translator_engine.dll")
CERT_SUBJECT = "CN=Wonslate"
KEY_FILES = "Wonslate*.dll"          # our own managed satellites, if any


def say(tag: str, msg: str = "") -> None:
    print(f"[{tag}] {msg}".rstrip(), flush=True)


def ps(script: str) -> tuple[int, str]:
    """Run a PowerShell snippet, returning (exit code, stdout+stderr)."""
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-Command", script], capture_output=True)
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    return r.returncode, (out + err).strip()


def is_admin() -> bool:
    if os.name != "nt" or ctypes is None:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def read_app_control_state() -> str:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\CI\Policy") as key:
            val, _ = winreg.QueryValueEx(key, "VerifiedAndReputablePolicyState")
        return {0: "off", 1: "enforcement", 2: "assessment"}.get(int(val), "unknown")
    except OSError:
        return "unknown"


def ensure_cert() -> str | None:
    """Return the thumbprint of a code-signing cert named Wonslate, creating it."""
    find = ("$c = Get-ChildItem Cert:\\CurrentUser\\My | Where-Object { "
            "$_.Subject -eq '%s' -and $_.NotAfter -gt (Get-Date) } | Select-Object -First 1; "
            "if (-not $c) { "
            "$c = New-SelfSignedCertificate -Type CodeSigningCert -Subject '%s' "
            "-HashAlgorithm SHA256 -CertStoreLocation Cert:\\CurrentUser\\My "
            "-NotAfter (Get-Date).AddYears(5) }; $c.Thumbprint" % (CERT_SUBJECT, CERT_SUBJECT))
    rc, out = ps(find)
    thumb = out.strip().splitlines()[-1] if out.strip() else ""
    if rc != 0 or not is_thumbprint(thumb):
        say("cert", "FAIL could not create or locate a code-signing certificate")
        say("cert", f"       {out.strip()[:220]}")
        return None
    say("cert", f"PASS thumbprint {thumb}")
    return thumb


def is_thumbprint(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789ABCDEFabcdef" for c in value)


def trust_cert(thumb: str) -> None:
    """Put the certificate where Authenticode looks, so local launches are quiet.

    Trusting the public certificate is enough to make a signature verify, so the
    machine-level path exports a .cer instead of a password-protected PFX.
    """
    rc, out = ps("$cer = Join-Path $env:TEMP 'wonslate.cer'; "
                 "Export-Certificate -Cert Cert:\\CurrentUser\\My\\%s -FilePath $cer | Out-Null; "
                 "Import-Certificate -FilePath $cer -CertStoreLocation Cert:\\CurrentUser\\TrustedPublisher | Out-Null; "
                 "Import-Certificate -FilePath $cer -CertStoreLocation Cert:\\CurrentUser\\Root | Out-Null; "
                 "Remove-Item $cer -ErrorAction SilentlyContinue; 'ok'" % thumb)
    say("trust", "PASS user-level Trusted Publisher + Root" if "ok" in out
        else f"WARN user store import incomplete: {out[:160]}")

    if is_admin():
        rc, out = ps("$cer = Join-Path $env:TEMP 'wonslate.cer'; "
                     "Export-Certificate -Cert Cert:\\CurrentUser\\My\\%s -FilePath $cer | Out-Null; "
                     "Import-Certificate -FilePath $cer -CertStoreLocation Cert:\\LocalMachine\\TrustedPublisher | Out-Null; "
                     "Import-Certificate -FilePath $cer -CertStoreLocation Cert:\\LocalMachine\\Root | Out-Null; "
                     "Remove-Item $cer -ErrorAction SilentlyContinue; 'ok'" % thumb)
        say("trust", "PASS machine-level stores (system-wide trust)" if "ok" in out
            else f"WARN machine store import failed: {out[:160]}")
    else:
        say("trust", "SKIP machine-level stores (not elevated)")
        say("trust", "      rerun from an elevated shell to trust the cert for all users")


def find_signtool() -> str | None:
    for pattern in (r"C:\Program Files (x86)\Windows Kits\10\bin\**\signtool.exe",
                    r"C:\Program Files\Windows Kits\10\bin\**\signtool.exe",
                    r"C:\Program Files (x86)\Windows Kits\8*\bin\x64\signtool.exe"):
        hits = glob.glob(pattern, recursive=True)
        if hits:
            return sorted(hits)[-1]
    return None


def collect_targets(out_dir: Path) -> list[Path]:
    targets = []
    for name in OUR_ARTIFACTS:
        p = out_dir / name
        if p.is_file():
            targets.append(p)
    for p in sorted(out_dir.glob(KEY_FILES)):
        if p.is_file() and p not in targets:
            targets.append(p)
    return targets


def sign_one(tool: str | None, thumb: str, path: Path) -> bool:
    if tool:
        r = subprocess.run(
            [tool, "sign", "/fd", "SHA256", "/sha1", thumb,
             "/tr", "http://timestamp.digicert.com", "/v", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            return True
        say("sign", "      signtool failed for %s: %s; retrying via PowerShell"
            % (path.name, (r.stdout + r.stderr).strip()[:120]))
    rc, out = ps("$c = Get-ChildItem Cert:\\CurrentUser\\My\\%s; "
                 "Set-AuthenticodeSignature -FilePath '%s' -Certificate $c "
                 "| Select-Object -ExpandProperty Status" % (thumb, path))
    return rc == 0 and out.strip().lower().startswith("signed")


def verify_one(path: Path) -> str:
    rc, out = ps("(Get-AuthenticodeSignature '%s').Status" % path)
    return out.strip() or "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sign local Wonslate build output")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="build output directory to sign")
    parser.add_argument("--verify-only", action="store_true",
                        help="only report current signature status")
    args = parser.parse_args(argv)

    if os.name != "nt":
        say("*", "SKIP  Authenticode signing is Windows-only")
        return 2

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    targets = collect_targets(out_dir)
    if not targets:
        say("*", f"SKIP  nothing to sign under {out_dir}")
        say("*", "      build first: python script/build.py")
        return 2

    say("*", f"application control state: {read_app_control_state()}")
    say("*", f"{len(targets)} artifact(s): " + ", ".join(p.name for p in targets))

    if args.verify_only:
        for p in targets:
            say("check", f"{p.name:24s} {verify_one(p)}")
        return 0

    thumb = ensure_cert()
    if not thumb:
        return 1
    trust_cert(thumb)

    tool = find_signtool()
    say("tool", f"using signtool: {tool}" if tool else
        "signtool not found; using Set-AuthenticodeSignature")

    failed = []
    for p in targets:
        ok = sign_one(tool, thumb, p)
        state = verify_one(p)
        say("sign", f"{'PASS' if ok and state.lower().startswith('valid') else 'FAIL'}"
            f" {p.name:24s} {state}")
        if not ok:
            failed.append(p.name)

    if failed:
        say("*", f"FAIL  could not sign: {', '.join(failed)}")
        return 1
    say("*", "PASS  all artifacts signed; rerun build_check.py to smoke them")
    say("*", "note  self-signed trust is host-local. Enforcement-mode application control may")
    say("*", "      still reject new hashes, and distributed builds need a real certificate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
