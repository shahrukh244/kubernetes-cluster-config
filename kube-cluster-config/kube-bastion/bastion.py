#!/usr/bin/env python3
"""
bastion_final_setup.py

- Requires root.
- Hard-coded hostname: kube-bastion.kube.lan
- Prepares bastion node, then waits for remote nodes to come online.
- As each remote node becomes reachable, copies SSH key to it immediately.
- Exits only after all nodes are reachable and have the key installed.
"""

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List

# ---------- CONFIG ----------
EXPECTED_HOSTNAME = "kube-bastion.kube.lan"
TIMEZONE = "Asia/Kolkata"
CHRONY_SERVER = "10.0.0.1"
ROOT_PASS = "123"
TARGET_IPS = [
    "10.0.0.1",
    "10.0.0.201",
    "10.0.0.202",
    "10.0.0.203",
    "10.0.0.211",
    "10.0.0.212",
]
PING_INTERVAL = 5            # seconds between ping rounds
SSH_COPY_TIMEOUT = 30        # seconds timeout for ssh-copy-id attempts
SSH_CONNECT_TIMEOUT = 8      # timeout for test ssh connections (seconds)
SSH_KEY_PATH = Path.home() / ".ssh" / "id_ed25519"
SSH_PUB_PATH = Path(str(SSH_KEY_PATH) + ".pub")
OC_URL = "https://mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp/4.14.9/openshift-client-linux.tar.gz"
OC_TAR_NAME = "openshift-client-linux.tar.gz"
OC_BIN = Path("/usr/local/bin/oc")
KUBECTL_BIN = Path("/usr/local/bin/kubectl")

# ---------- UTILITIES ----------
def info(msg: str):
    print(f"[INFO] {msg}")

def ok(msg: str):
    print(f"[ OK ] {msg}")

def warn(msg: str):
    print(f"[WARN] {msg}")

def fatal(msg: str):
    print(f"[FATAL] {msg}")
    sys.exit(1)

def run(cmd: str, check: bool = True, capture_output: bool = False, timeout: int = None):
    """
    Wrapper around subprocess.run — prints the command, returns CompletedProcess or raises.
    """
    print(f"\n[RUN] {cmd}")
    try:
        if capture_output:
            proc = subprocess.run(cmd, shell=True, check=check,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, timeout=timeout)
        else:
            proc = subprocess.run(cmd, shell=True, check=check, timeout=timeout)
        return proc
    except subprocess.CalledProcessError as e:
        # When check=False we don't get here; only when check=True and err happens.
        print(f"[ERROR] Command failed (returncode={e.returncode}). Cmd: {cmd}")
        if hasattr(e, "stdout") and e.stdout:
            print("STDOUT:", e.stdout)
        if hasattr(e, "stderr") and e.stderr:
            print("STDERR:", e.stderr)
        raise
    except subprocess.TimeoutExpired as e:
        print(f"[ERROR] Command timed out after {timeout}s: {cmd}")
        raise

# ---------- ROOT CHECK ----------
def ensure_root():
    if os.geteuid() != 0:
        fatal("This script must be run as root (sudo).")

# ---------- BASTION PREP STEPS ----------
def ensure_hostname():
    info("Checking current hostname...")
    cur = run("hostnamectl --static", capture_output=True).stdout.strip()
    info(f"Current hostname: {cur}")
    if cur == EXPECTED_HOSTNAME:
        ok(f"Hostname already set to {EXPECTED_HOSTNAME}")
        return
    info(f"Setting hostname to {EXPECTED_HOSTNAME}")
    run(f"hostnamectl set-hostname {EXPECTED_HOSTNAME}")
    ok("Hostname set. Note: some services may require restart or logout/login to pick this up.")

def apt_update_upgrade():
    info("Running apt update and checking for upgrades")
    run("apt-get update -y")
    upg = run("apt list --upgradable 2>/dev/null | grep -v Listing || true", capture_output=True).stdout.strip()
    if upg == "":
        ok("No upgrades available (skipping apt-get upgrade).")
    else:
        info("Upgrades available; performing apt-get upgrade -y")
        run("DEBIAN_FRONTEND=noninteractive apt-get upgrade -y")
        ok("System packages upgraded")

def disable_swap():
    info("Disabling swap in /etc/fstab (commenting swap lines) and running swapoff -a")
    fstab = Path("/etc/fstab")
    if fstab.exists():
        text = fstab.read_text()
        new_lines = []
        changed = False
        for line in text.splitlines():
            if not line.lstrip().startswith("#") and "swap" in line:
                new_lines.append("#" + line)
                changed = True
            else:
                new_lines.append(line)
        if changed:
            fstab.write_text("\n".join(new_lines) + "\n")
            ok("Commented swap entries in /etc/fstab")
        else:
            ok("No active swap lines found in /etc/fstab (already commented or none)")
    else:
        warn("/etc/fstab not found; skipping fstab editing")

    # Attempt to disable active swap
    out = run("swapon --noheadings --summary || true", capture_output=True).stdout.strip()
    if out == "":
        ok("No active swap detected")
    else:
        info("Active swap detected; running swapoff -a")
        run("swapoff -a")
        out2 = run("swapon --noheadings --summary || true", capture_output=True).stdout.strip()
        if out2 == "":
            ok("Swap successfully turned off")
        else:
            warn("Swap still active after swapoff -a; check manually")

def disable_ufw():
    info("Disabling UFW if present")
    svc_list = run("systemctl list-unit-files | grep -E '^ufw\\.service' || true", capture_output=True).stdout
    if "ufw.service" in svc_list:
        active = run("systemctl is-active ufw || true", capture_output=True).stdout.strip()
        if active == "active":
            info("Stopping and disabling ufw")
            run("systemctl stop ufw")
            run("systemctl disable ufw")
            ok("ufw stopped and disabled")
        else:
            ok("ufw not active; ensuring disabled")
            run("systemctl disable ufw", check=False)
    else:
        ok("ufw service not present; skipping")

def configure_ip_forwarding():
    info("Configuring net.ipv4.ip_forward=1 persistently")
    conf_path = Path("/etc/sysctl.d/99-kubernetes-cri.conf")
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    conf_path.write_text("net.ipv4.ip_forward=1\n")
    run("sysctl --system")
    cur = run("sysctl -n net.ipv4.ip_forward", capture_output=True).stdout.strip()
    if cur == "1":
        ok("IP forwarding enabled")
    else:
        warn("IP forwarding not seen as 1 after sysctl --system; check manually")

def install_base_packages():
    info("Installing base packages: chrony, nfs-common, sshpass, curl, wget, tar, gnupg")
    run("apt-get update -y")
    run("DEBIAN_FRONTEND=noninteractive apt-get install -y chrony nfs-common sshpass curl wget tar gnupg dirmngr apt-transport-https ca-certificates")

def configure_chrony():
    info("Configuring chrony")
    conf = Path("/etc/chrony/chrony.conf")
    if not conf.exists():
        warn("/etc/chrony/chrony.conf not found — chrony package may not have installed correctly")
        return
    content = conf.read_text()
    # Comment default ubuntu pools if present and append server line if missing
    patterns = [
        r"^\s*pool ntp\.ubuntu\.com.*",
        r"^\s*pool 0\.ubuntu\.pool\.ntp\.org.*",
        r"^\s*pool 1\.ubuntu\.pool\.ntp\.org.*",
        r"^\s*pool 2\.ubuntu\.pool\.ntp\.org.*",
    ]
    changed = False
    import re
    for pat in patterns:
        updated = re.sub(pat, lambda m: "#" + m.group(0), content, flags=re.M)
        if updated != content:
            content = updated
            changed = True
    server_line = f"server {CHRONY_SERVER} iburst"
    if server_line not in content:
        content = content + "\n" + server_line + "\n"
        changed = True
    if changed:
        conf.write_text(content)
        ok("chrony.conf updated")
    run("systemctl restart chrony")
    ok("chrony restarted (if chrony service exists)")

def install_oc_client():
    if OC_BIN.exists():
        ok("oc client already installed")
        return
    info("Installing OpenShift 'oc' client")
    tmp = "/tmp/oc_install"
    run(f"rm -rf {tmp} || true")
    run(f"mkdir -p {tmp}")
    tar_path = f"{tmp}/{OC_TAR_NAME}"
    run(f"wget -O {tar_path} {OC_URL}")
    run(f"tar -xzvf {tar_path} -C {tmp}")
    # Find oc binary
    found = None
    for p in Path(tmp).rglob("oc"):
        if p.is_file():
            found = p
            break
    if not found:
        warn("Could not find oc in extracted archive; please check download")
    else:
        run(f"cp {str(found)} /usr/local/bin/oc")
        run("chmod +x /usr/local/bin/oc")
        ok("oc installed to /usr/local/bin/oc")
    run(f"rm -rf {tmp}")

def install_kubectl():
    if KUBECTL_BIN.exists():
        ok("kubectl already installed")
        return
    info("Installing kubectl (stable)")
    ver = run("curl -L -s https://dl.k8s.io/release/stable.txt", capture_output=True).stdout.strip()
    if not ver:
        warn("Unable to fetch stable kubectl version; skipping kubectl install")
        return
    url = f"https://dl.k8s.io/release/{ver}/bin/linux/amd64/kubectl"
    tmpfile = "/tmp/kubectl.tmp"
    run(f"curl -L -o {tmpfile} {url}")
    run(f"chmod +x {tmpfile} && mv {tmpfile} /usr/local/bin/kubectl")
    ok("kubectl installed to /usr/local/bin/kubectl")
    # best-effort client version check
    run("kubectl version --client --short || true", check=False)

def enable_root_ssh():
    info("Enabling root SSH login and setting root password")
    # install sshpass ensured earlier
    run(f"echo 'root:{ROOT_PASS}' | chpasswd")
    sshd_conf = Path("/etc/ssh/sshd_config")
    if not sshd_conf.exists():
        warn("/etc/ssh/sshd_config not found; skipping sshd config edits")
    else:
        text = sshd_conf.read_text()
        # PermitRootLogin yes
        if "PermitRootLogin yes" not in text:
            # replace or append
            import re
            if re.search(r'^\s*#?\s*PermitRootLogin\s+.*', text, flags=re.M):
                text = re.sub(r'^\s*#?\s*PermitRootLogin\s+.*', 'PermitRootLogin yes', text, flags=re.M)
            else:
                text += "\nPermitRootLogin yes\n"
        # PasswordAuthentication yes
        if "PasswordAuthentication yes" not in text:
            import re
            if re.search(r'^\s*#?\s*PasswordAuthentication\s+.*', text, flags=re.M):
                text = re.sub(r'^\s*#?\s*PasswordAuthentication\s+.*', 'PasswordAuthentication yes', text, flags=re.M)
            else:
                text += "\nPasswordAuthentication yes\n"
        sshd_conf.write_text(text)
        run("systemctl reload ssh || systemctl reload sshd || true")
        ok("sshd config updated and reloaded")

# ---------- SSH KEY + DISTRIBUTION LOGIC ----------
def ensure_ssh_key():
    ssh_dir = SSH_KEY_PATH.parent
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    if SSH_KEY_PATH.exists() and SSH_PUB_PATH.exists():
        ok(f"SSH key pair exists at {SSH_KEY_PATH} and {SSH_PUB_PATH}")
        return
    info("Generating ed25519 SSH key (no passphrase)")
    run(f'ssh-keygen -t ed25519 -N "" -C "$(whoami)@$(hostname)" -f {SSH_KEY_PATH}')
    ok("SSH key generated")

def can_ssh_with_key(ip: str) -> bool:
    """
    Test if passwordless SSH with our key already works.
    Uses BatchMode=yes to fail fast if key not accepted.
    """
    cmd = (f'ssh -i "{SSH_KEY_PATH}" -o BatchMode=yes -o StrictHostKeyChecking=no '
           f'-o ConnectTimeout={SSH_CONNECT_TIMEOUT} root@{ip} echo __SSH_OK__')
    try:
        proc = run(cmd, check=False, capture_output=True, timeout=SSH_CONNECT_TIMEOUT+2)
        out = proc.stdout.strip() if proc and hasattr(proc, 'stdout') else ""
        return "__SSH_OK__" in out
    except Exception:
        return False

def is_host_reachable(ip: str) -> bool:
    """Ping host once with short timeout."""
    cmd = f"ping -c 1 -W 1 {ip} >/dev/null 2>&1"
    rc = subprocess.call(cmd, shell=True)
    return rc == 0

def copy_key_to_host(ip: str) -> bool:
    """
    Copies public key to host using ssh-copy-id wrapped by sshpass.
    Uses timeout so script won't block too long.
    Returns True on success.
    """
    # if already can SSH with key, skip
    if can_ssh_with_key(ip):
        ok(f"{ip}: passwordless SSH already works; skipping copy")
        return True

    # Ensure pub key exists
    if not SSH_PUB_PATH.exists():
        warn("Public key not found; cannot copy")
        return False

    cmd = (f"timeout {SSH_COPY_TIMEOUT} sshpass -p '{ROOT_PASS}' ssh-copy-id -o StrictHostKeyChecking=no "
           f"-i {SSH_PUB_PATH} root@{ip}")
    try:
        proc = run(cmd, check=False, capture_output=True, timeout=SSH_COPY_TIMEOUT+5)
        # ssh-copy-id returns 0 on success. We allow further verification.
        if proc.returncode == 0:
            # verify by trying passwordless ssh
            if can_ssh_with_key(ip):
                ok(f"{ip}: SSH key installed and verified")
                return True
            else:
                warn(f"{ip}: ssh-copy-id returned success but key verification failed")
                return False
        else:
            warn(f"{ip}: ssh-copy-id failed (returncode={proc.returncode}). STDERR: {proc.stderr.strip()}")
            return False
    except subprocess.TimeoutExpired:
        warn(f"{ip}: ssh-copy-id timed out after {SSH_COPY_TIMEOUT}s")
        return False
    except Exception as e:
        warn(f"{ip}: unexpected exception during ssh-copy-id: {e}")
        return False

# ---------- MAIN WAIT/LOOP LOGIC ----------
def distribute_keys_wait_until_all_online(targets: List[str]):
    info("Beginning ping loop: will wait until all nodes are online and copy keys as they come up.")
    # Track which hosts already had key installed
    installed = {ip: False for ip in targets}
    # If some hosts already accept key, mark them
    for ip in targets:
        if can_ssh_with_key(ip):
            installed[ip] = True
            ok(f"{ip}: already has key installed")

    all_online_and_installed = all(installed.values())
    if all_online_and_installed:
        ok("All hosts already have keys installed. No waiting necessary.")
        return

    info("Entering main loop. Press Ctrl+C to abort.")
    try:
        while True:
            # First, check reachability and attempt copy for reachable & not-installed hosts
            for ip in targets:
                if installed[ip]:
                    continue
                reachable = is_host_reachable(ip)
                if reachable:
                    info(f"{ip} is reachable — attempting to copy SSH key")
                    success = copy_key_to_host(ip)
                    if success:
                        installed[ip] = True
                else:
                    info(f"{ip} is not reachable yet")

            # Show progress
            online_count = sum(1 for ip in targets if is_host_reachable(ip))
            installed_count = sum(1 for ip, v in installed.items() if v)
            info(f"Progress: {installed_count}/{len(targets)} keys installed; {online_count}/{len(targets)} nodes currently pingable")

            if all(installed.values()):
                ok("All targets have keys installed. Exiting loop.")
                break

            # Next, check if all nodes are pingable (online); if not, continue waiting.
            all_pingable = all(is_host_reachable(ip) for ip in targets)
            if all_pingable:
                info("All nodes respond to ping. For any remaining hosts without keys, attempting final installs.")
                # Try remaining installs one more time before next wait
                for ip in targets:
                    if not installed[ip]:
                        info(f"Final attempt: copying key to {ip}")
                        if copy_key_to_host(ip):
                            installed[ip] = True
                if all(installed.values()):
                    ok("All keys installed after final attempts.")
                    break

            time.sleep(PING_INTERVAL)
    except KeyboardInterrupt:
        warn("Interrupted by user (Ctrl+C). Exiting loop. Some hosts may remain without keys.")
    # final summary
    info("Final summary:")
    for ip in targets:
        status = "INSTALLED" if installed[ip] else "MISSING"
        reach = "reachable" if is_host_reachable(ip) else "unreachable"
        print(f" - {ip}: {status} ({reach})")

# ---------- MAIN ----------
def main():
    ensure_root()
    info("Starting bastion preparation")

    ensure_hostname()
    apt_update_upgrade()
    disable_swap()
    disable_ufw()
    configure_ip_forwarding()
    install_base_packages()
    configure_chrony()
    install_oc_client()
    install_kubectl()
    enable_root_ssh()

    info("Preparing SSH key on bastion")
    ensure_ssh_key()

    info("Starting distribution of SSH keys to targets (will wait for nodes & copy as they come up)")
    distribute_keys_wait_until_all_online(TARGET_IPS)

    ok("All done. Please review the above output for any warnings.")


if __name__ == "__main__":
    main()
