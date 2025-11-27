#!/usr/bin/env python3
import os
import re
import subprocess
from pathlib import Path

HOSTNAME = "kube-w-2.kube.lan"
TIMEZONE = "Asia/Kolkata"
CHRONY_SERVER = "10.0.0.1"
CRIO_VERSION = "v1.34"

# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def info(msg):
    print(f"[INFO] {msg}")

def ok(msg):
    print(f"[OK] {msg}")

def fatal(msg):
    print(f"[FATAL] {msg}")
    exit(1)

def run(cmd, check=True, capture_output=False):
    if capture_output:
        result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if check and result.returncode != 0:
            fatal(f"Command failed: {cmd}\n{result.stderr}")
        return result.stdout.strip()
    else:
        result = subprocess.run(cmd, shell=True)
        if check and result.returncode != 0:
            fatal(f"Command failed: {cmd}")
        return None

# ---------------------------------------------------------
# Package installer (fixed reliable version)
# ---------------------------------------------------------

def ensure_package_installed(pkg):
    cmd = f"dpkg-query -W -f='${{Status}}' {pkg} 2>/dev/null | grep -c 'ok installed' || true"
    installed = run(cmd, capture_output=True)

    if installed == "1":
        ok(f"Package '{pkg}' already installed")
        return

    info(f"Installing package '{pkg}' ...")
    run(f"DEBIAN_FRONTEND=noninteractive apt-get install -y {pkg}")
    ok(f"'{pkg}' installed")

# ---------------------------------------------------------
# Hostname
# ---------------------------------------------------------

def set_hostname():
    current = run("hostnamectl --static", capture_output=True)
    if current.strip() == HOSTNAME:
        ok(f"Hostname already set to {HOSTNAME}")
        return
    info(f"Setting hostname to {HOSTNAME}")
    run(f"hostnamectl set-hostname {HOSTNAME}")
    ok("Hostname updated")

# ---------------------------------------------------------
# Update & Upgrade
# ---------------------------------------------------------

def system_update():
    info("Updating system packages ...")
    run("apt update && apt upgrade -y")
    ok("System updated")

# ---------------------------------------------------------
# Root SSH enable + passwd
# ---------------------------------------------------------

def enable_root_ssh():
    ensure_package_installed("sshpass")

    # root pass
    info("Setting root password to 123")
    run("echo 'root:123' | chpasswd")

    # PermitRootLogin
    sshd_conf = "/etc/ssh/sshd_config"
    content = Path(sshd_conf).read_text()

    new_content = re.sub(r"#?\s*PermitRootLogin.*", "PermitRootLogin yes", content)
    new_content = re.sub(r"#?\s*PasswordAuthentication.*", "PasswordAuthentication yes", new_content)

    if new_content != content:
        Path(sshd_conf).write_text(new_content)
        info("Updated /etc/ssh/sshd_config")
        run("systemctl reload ssh")
    else:
        ok("SSH config already enabled for root login")

# ---------------------------------------------------------
# Disable Swap
# ---------------------------------------------------------

def disable_swap():
    run("sed -i '/\\s*swap\\s/s/^/#/' /etc/fstab")
    run("swapoff -a")
    ok("Swap disabled")

# ---------------------------------------------------------
# Disable UFW
# ---------------------------------------------------------

def disable_ufw():
    status = run("systemctl is-active ufw || true", capture_output=True)
    if status == "active":
        info("Stopping UFW...")
        run("systemctl stop ufw")
        run("systemctl disable ufw")
    else:
        ok("UFW already disabled")

# ---------------------------------------------------------
# Sysctl net.ipv4.ip_forward
# ---------------------------------------------------------

def configure_ip_forwarding():
    path = "/etc/sysctl.d/99-kubernetes-cri.conf"
    line = "net.ipv4.ip_forward=1\n"

    write_needed = True
    if Path(path).exists():
        if line in Path(path).read_text():
            write_needed = False

    if write_needed:
        Path(path).write_text(line)
        info("Configured net.ipv4.ip_forward")

    run("sysctl --system")
    ok("IP forwarding applied")

# ---------------------------------------------------------
# Install Chrony (fixed)
# ---------------------------------------------------------

def install_chrony():
    ensure_package_installed("chrony")

    # Timezone
    tz = run("timedatectl show -p Timezone --value", capture_output=True)
    if tz.strip() != TIMEZONE:
        info(f"Setting timezone to {TIMEZONE}")
        run(f"timedatectl set-timezone {TIMEZONE}")
    else:
        ok("Timezone already correct")

    conf = Path("/etc/chrony/chrony.conf")
    content = conf.read_text()
    changed = False

    default_pools = [
        r"^\s*pool ntp.ubuntu.com.*",
        r"^\s*pool 0.ubuntu.pool.ntp.org.*",
        r"^\s*pool 1.ubuntu.pool.ntp.org.*",
        r"^\s*pool 2.ubuntu.pool.ntp.org.*",
    ]

    for p in default_pools:
        updated = re.sub(p, lambda m: "#" + m.group(0), content, flags=re.M)
        if updated != content:
            content = updated
            changed = True

    server_line = f"server {CHRONY_SERVER} iburst"
    if server_line not in content:
        content += f"\n{server_line}\n"
        changed = True

    if changed:
        conf.write_text(content)
        info("Chrony config updated")

    run("systemctl restart chrony")
    ok("Chrony restarted")

# ---------------------------------------------------------
# Install NFS client (fixed)
# ---------------------------------------------------------

def install_nfs():
    ensure_package_installed("nfs-common")
    ok("NFS client ready")

# ---------------------------------------------------------
# Install CRIO
# ---------------------------------------------------------

def install_crio():
    info("Installing CRI-O...")
    run("apt update && apt upgrade -y")
    run("mkdir -p /etc/apt/keyrings")

    run(
        f"curl -fsSL https://download.opensuse.org/repositories/isv:/cri-o:/stable:/{CRIO_VERSION}/deb/Release.key "
        f"| gpg --dearmor -o /etc/apt/keyrings/cri-o-apt-keyring.gpg"
    )

    repo_line = (
        f"deb [signed-by=/etc/apt/keyrings/cri-o-apt-keyring.gpg] "
        f"https://download.opensuse.org/repositories/isv:/cri-o:/stable:/{CRIO_VERSION}/deb/ /"
    )
    Path("/etc/apt/sources.list.d/cri-o.list").write_text(repo_line + "\n")

    run("apt update")
    ensure_package_installed("cri-o")

    run("systemctl enable --now crio")
    run("systemctl restart crio")
    ok("CRI-O installed and running")

# ---------------------------------------------------------
# Install Kubernetes
# ---------------------------------------------------------

def install_kubernetes():
    run("apt update && apt upgrade -y")
    ensure_package_installed("apt-transport-https")
    ensure_package_installed("ca-certificates")
    ensure_package_installed("curl")
    ensure_package_installed("gpg")

    run(
        "curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.34/deb/Release.key "
        "| gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg"
    )

    repo = (
        "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] "
        "https://pkgs.k8s.io/core:/stable:/v1.34/deb/ /"
    )

    Path("/etc/apt/sources.list.d/kubernetes.list").write_text(repo + "\n")

    run("apt-get update")
    ensure_package_installed("kubelet")
    ensure_package_installed("kubeadm")
    ensure_package_installed("kubectl")

    run("apt-mark hold kubelet kubeadm kubectl")
    ok("Kubernetes installed successfully")

# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

def main():
    set_hostname()
    system_update()
    enable_root_ssh()
    disable_swap()
    disable_ufw()
    configure_ip_forwarding()
    install_chrony()
    install_nfs()
    install_crio()
    install_kubernetes()

    ok("ALL TASKS COMPLETED SUCCESSFULLY")

if __name__ == "__main__":
    main()
