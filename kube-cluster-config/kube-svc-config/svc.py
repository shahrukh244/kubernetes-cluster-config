
#!/usr/bin/env python3
"""
Full idempotent setup script for Ubuntu systems with robust BIND handling.

- Repo: /root/kubernetes-cluster-config
- Ensures SSH root/password login is enabled
- Validates BIND config and zones before restarting
- Uses 'named' where appropriate to avoid systemd alias errors
- Comments swap lines in /etc/fstab using sed (does not remove lines)
"""

import os
import re
import shutil
import subprocess
import filecmp
from pathlib import Path

# ---- Configuration ----
REPO_DIR = "/root/kubernetes-cluster-config"
NETPLAN_DIR = "/etc/netplan"
ZONES_DIR = "/etc/bind/zones"
ROOT_PASS = "123"
INTERFACES_NETPLAN = ["ens32", "ens34"]
NAT_SOURCE_NET = "10.0.0.0/24"
NAT_OUT_IF = "ens32"
NAT_IN_IF = "ens34"

# ---- Helpers ----
APT_UPDATED = False

def run(cmd, check=False):
    """Run a shell command; return stdout (str). If check True, raise on non-zero."""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed: {cmd}\n{result.stderr.strip()}")
    return result.stdout.strip()

def run_output(cmd):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return {"returncode": p.returncode, "stdout": p.stdout.strip(), "stderr": p.stderr.strip()}

def log_ok(msg): print(f"✔ {msg}")
def log_info(msg): print(f"➤ {msg}")
def log_warn(msg): print(f"✖ {msg}")

def ensure_apt_updated():
    global APT_UPDATED
    if not APT_UPDATED:
        log_info("Running apt-get update...")
        run("DEBIAN_FRONTEND=noninteractive apt-get update -y", check=True)
        APT_UPDATED = True

def ensure_package(pkg):
    ensure_apt_updated()
    rc = subprocess.call(f"dpkg -s {pkg} >/dev/null 2>&1", shell=True)
    if rc != 0:
        log_info(f"Installing {pkg}...")
        run(f"DEBIAN_FRONTEND=noninteractive apt-get install -y {pkg}", check=True)
        log_ok(f"{pkg} installed")
    else:
        log_ok(f"{pkg} already installed")

def backup_file(path):
    if os.path.exists(path):
        bak = f"{path}.bak"
        shutil.copy2(path, bak)
        log_ok(f"Backed up {path} -> {bak}")
    else:
        log_info(f"No file to backup at {path}")

def files_same(src, dst):
    return os.path.exists(src) and os.path.exists(dst) and filecmp.cmp(src, dst, shallow=False)

def ensure_file(src, dst, backup=True, mode=None):
    if not os.path.exists(src):
        log_warn(f"Source missing: {src}")
        return False
    dst_parent = os.path.dirname(dst)
    if dst_parent and not os.path.exists(dst_parent):
        os.makedirs(dst_parent, exist_ok=True)
    if not os.path.exists(dst) or not filecmp.cmp(src, dst, shallow=False):
        if backup and os.path.exists(dst):
            backup_file(dst)
        shutil.copy2(src, dst)
        if mode is not None:
            os.chmod(dst, mode)
        log_ok(f"Copied {src} -> {dst}")
        return True
    else:
        log_ok(f"{dst} already up to date")
        return False

def service_exists(svc):
    svc_unit = f"{svc}.service"
    paths = [
        f"/lib/systemd/system/{svc_unit}",
        f"/etc/systemd/system/{svc_unit}",
        f"/usr/lib/systemd/system/{svc_unit}",
        f"/run/systemd/system/{svc_unit}",
    ]
    for p in paths:
        if os.path.exists(p):
            return True
    out = run(f"systemctl list-unit-files --type=service | grep -w {svc_unit} || true")
    return bool(out.strip())

def restart_service(svc):
    if not service_exists(svc):
        log_warn(f"Service unit not found: {svc} (skipping restart/enable)")
        return False
    try:
        run(f"systemctl daemon-reload", check=False)
        run(f"systemctl restart {svc}", check=True)
        run(f"systemctl enable {svc}", check=True)
        log_ok(f"{svc} restarted and enabled")
        return True
    except RuntimeError as e:
        log_warn(f"Failed to restart/enable {svc}: {e}")
        return False

# ---- BIND-specific helpers ----
def choose_bind_service():
    """
    Prefer 'named' if present (Ubuntu 22.04+). Fallback to 'bind9'.
    Return tuple (service_name, used_alias_warning_flag)
    """
    if service_exists("named"):
        return ("named", False)
    if service_exists("bind9"):
        out = run("systemctl status bind9 || true")
        # if bind9 is an alias or masked prefer named
        if "alias" in out or "linked" in out or "Loaded: masked" in out:
            if service_exists("named"):
                return ("named", True)
        return ("bind9", False)
    return ("named", False)

def verify_bind_config_and_zones(bind_conf="/etc/bind/named.conf", zones=None):
    if shutil.which("named-checkconf") is None:
        log_warn("named-checkconf not present; skipping BIND config validation")
        return True
    res = run_output("named-checkconf /etc/bind/named.conf")
    if res["returncode"] != 0:
        log_warn(f"named-checkconf reported error:\n{res['stderr']}")
        return False
    log_ok("named-checkconf OK")
    if zones:
        if shutil.which("named-checkzone") is None:
            log_warn("named-checkzone not present; skipping zone validation")
            return True
        for zone_name, zone_path in zones.items():
            if not os.path.exists(zone_path):
                log_warn(f"Zone file missing: {zone_path} (skipping check for {zone_name})")
                continue
            zres = run_output(f"named-checkzone {zone_name} {zone_path}")
            if zres["returncode"] != 0:
                log_warn(f"named-checkzone failed for {zone_name}: {zres['stderr']}")
                return False
            log_ok(f"named-checkzone OK for {zone_name}")
    return True

def restart_bind_service_with_validation():
    svc, alias_flag = choose_bind_service()
    log_info(f"Using DNS service '{svc}' for restart (alias_flag={alias_flag})")
    zones = {
        "kube.lan": "/etc/bind/zones/db.kube.lan",
        "reverse": "/etc/bind/zones/db.reverse"
    }
    ok = verify_bind_config_and_zones(zones=zones)
    if not ok:
        log_warn("BIND validation failed; skipping restart. Fix config then restart 'named' or 'bind9' manually.")
        return False
    if svc == "bind9" and alias_flag and service_exists("named"):
        svc = "named"
        log_info("Switching to 'named' due to alias/enable restrictions")
    return restart_service(svc)

# ---- Step implementations ----
def ensure_hostname(desired="kube-svc-1.kube.lan"):
    try:
        current = run("hostnamectl --static")
    except Exception:
        current = run("hostname -s")
    if current.strip() != desired:
        log_info(f"Setting hostname -> {desired}")
        run(f"hostnamectl set-hostname {desired}", check=True)
        log_ok("Hostname set")
    else:
        log_ok("Hostname already correct")

def fix_ssh_config_and_root_password(root_pass=ROOT_PASS):
    log_info("Ensuring SSH allows root password login (PermitRootLogin/PasswordAuthentication/UsePAM)")
    sshd_conf = "/etc/ssh/sshd_config"
    if not os.path.exists(sshd_conf):
        log_warn(f"{sshd_conf} missing; creating minimal config lines")
        with open(sshd_conf, "w") as f:
            f.write("PermitRootLogin yes\nPasswordAuthentication yes\nUsePAM yes\n")
    else:
        with open(sshd_conf, "r", encoding="utf-8") as f:
            content = f.read()
        content = re.sub(r"^#?\s*PermitRootLogin\s+.*", "PermitRootLogin yes", content, flags=re.MULTILINE)
        content = re.sub(r"^#?\s*PasswordAuthentication\s+.*", "PasswordAuthentication yes", content, flags=re.MULTILINE)
        content = re.sub(r"^#?\s*UsePAM\s+.*", "UsePAM yes", content, flags=re.MULTILINE)
        for line in ("PermitRootLogin yes", "PasswordAuthentication yes", "UsePAM yes"):
            if line not in content:
                content += "\n" + line + "\n"
        backup_file(sshd_conf)
        with open(sshd_conf, "w", encoding="utf-8") as f:
            f.write(content)
        log_ok("sshd_config updated")
    run(f"echo 'root:{root_pass}' | chpasswd", check=True)
    log_ok("Root password set/updated")
    if not (restart_service("ssh") or restart_service("sshd")):
        log_warn("Could not restart ssh service; restart manually")

def disable_swap():
    r"""
    Comments swap lines in /etc/fstab using sed (does not delete) and turns off swap.
    Uses the exact sed pattern requested: '/\s*swap\s/s/^/#/' WITHOUT producing Python warnings.
    """
    fstab = "/etc/fstab"

    if os.path.exists(fstab):
        backup_file(fstab)

        # FIXED: escape \s properly to avoid SyntaxWarning
        run(r"sed -i '/\s*swap\s/s/^/#/' /etc/fstab")

        try:
            run(sed_cmd, check=True)
            log_ok("Swap lines commented in /etc/fstab using sed")
        except Exception as e:
            log_warn(f"sed commenting failed: {e}; falling back to python edit")

            with open(fstab, "r", encoding="utf-8") as f:
                lines = f.readlines()

            new_lines = []
            changed = False
            for ln in lines:
                if re.search(r"\sswap\s", ln) and not ln.strip().startswith("#"):
                    new_lines.append("#" + ln)
                    changed = True
                else:
                    new_lines.append(ln)

            if changed:
                backup_file(f"{fstab}.fallback")
                with open(fstab, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
                log_ok("Swap lines commented in /etc/fstab by fallback")
            else:
                log_ok("No active swap lines found in /etc/fstab")

    else:
        log_warn("/etc/fstab not found; cannot comment swap lines")

    try:
        run("swapoff -a", check=True)
        log_ok("Swap disabled now (swapoff -a)")
    except Exception as e:
        log_warn(f"swapoff failed or no active swap: {e}")


def disable_ufw():
    if shutil.which("ufw"):
        status = run("systemctl is-active ufw || true")
        if status.strip() != "inactive":
            run("systemctl stop ufw || true")
            run("systemctl disable ufw || true")
            log_ok("UFW stopped & disabled")
        else:
            log_ok("UFW already inactive")
    else:
        log_ok("ufw not installed")

def clone_repo():
    ensure_package("git")
    if not os.path.exists(REPO_DIR):
        log_info("Cloning repository...")
        run(f"git clone https://github.com/shahrukh244/kubernetes-cluster-config.git {REPO_DIR}", check=True)
        log_ok("Repository cloned")
    else:
        log_ok("Repository exists")

def apply_netplan():
    changed = False
    for iface in INTERFACES_NETPLAN:
        src = os.path.join(REPO_DIR, "network-ip", f"{iface}.yaml")
        dst = os.path.join(NETPLAN_DIR, f"{iface}.yaml")
        if os.path.exists(src):
            if ensure_file(src, dst, mode=0o600):
                changed = True
        else:
            log_warn(f"Missing netplan file in repo: {src}")
    if shutil.which("netplan"):
        try:
            run("netplan apply", check=True)
            log_ok("netplan applied")
        except RuntimeError as e:
            log_warn(f"netplan apply failed: {e}")
    else:
        log_warn("netplan not installed on this host")

def configure_bind9():
    ensure_package("bind9")
    os.makedirs(ZONES_DIR, exist_ok=True)
    mapping = {
        os.path.join(REPO_DIR, "dns", "named.conf.options"): "/etc/bind/named.conf.options",
        os.path.join(REPO_DIR, "dns", "named.conf.local"): "/etc/bind/named.conf.local",
        os.path.join(REPO_DIR, "dns", "zones", "db.kube.lan"): os.path.join(ZONES_DIR, "db.kube.lan"),
        os.path.join(REPO_DIR, "dns", "zones", "db.reverse"): os.path.join(ZONES_DIR, "db.reverse"),
    }
    changed = False
    for src, dst in mapping.items():
        if ensure_file(src, dst):
            changed = True
    if changed:
        restarted = restart_bind_service_with_validation()
        if not restarted:
            log_warn("BIND config changed but restart was not performed automatically.")
    else:
        log_ok("BIND9 config up to date")

def configure_dhcp():
    ensure_package("isc-dhcp-server")
    dhcp_default = "/etc/default/isc-dhcp-server"
    if os.path.exists(dhcp_default):
        txt = run(f"cat {dhcp_default}")
        if 'INTERFACESv4="ens34"' not in txt:
            backup_file(dhcp_default)
            if re.search(r"^INTERFACESv4=.*$", txt, flags=re.MULTILINE):
                new = re.sub(r"^INTERFACESv4=.*$", 'INTERFACESv4="ens34"', txt, flags=re.MULTILINE)
            else:
                new = txt + '\nINTERFACESv4="ens34"\n'
            with open(dhcp_default, "w", encoding="utf-8") as f:
                f.write(new)
            log_ok("Updated /etc/default/isc-dhcp-server")
        else:
            log_ok("/etc/default/isc-dhcp-server already configured")
    else:
        log_warn(f"{dhcp_default} not found")
    dhcp_src = os.path.join(REPO_DIR, "dhcp", "dhcpd.conf")
    dhcp_dst = "/etc/dhcp/dhcpd.conf"
    if ensure_file(dhcp_src, dhcp_dst):
        restart_service("isc-dhcp-server")
    else:
        # still try to start/enable the service if present
        restart_service("isc-dhcp-server")

def ensure_ip_forwarding():
    """
    Bulletproof IP forwarding setter: removes any existing ip_forward lines (commented or not)
    and appends a single 'net.ipv4.ip_forward=1' line, then applies it immediately.
    """
    sysctl_conf = "/etc/sysctl.conf"
    content = ""
    if os.path.exists(sysctl_conf):
        with open(sysctl_conf, "r", encoding="utf-8") as f:
            content = f.read()
    # build new content preserving non-ip_forward lines
    new_lines = []
    for line in content.splitlines():
        if re.match(r"^\s*#?\s*net\.ipv4\.ip_forward\s*=.*", line):
            continue
        new_lines.append(line)
    new_lines.append("net.ipv4.ip_forward=1")
    backup_file(sysctl_conf)
    with open(sysctl_conf, "w", encoding="utf-8") as f:
        f.write("\n".join(new_lines) + "\n")
    # apply immediately
    try:
        run("sysctl -w net.ipv4.ip_forward=1", check=False)
        run("sysctl -p /etc/sysctl.conf || true", check=False)
        log_ok("IP forwarding ensured (net.ipv4.ip_forward=1)")
    except Exception as e:
        log_warn(f"Failed to apply sysctl immediately: {e}")

def configure_nat_and_iptables():
    ensure_ip_forwarding()
    rules = [
        (f"-t nat -C POSTROUTING -s {NAT_SOURCE_NET} -o {NAT_OUT_IF} -j MASQUERADE",
         f"-t nat -A POSTROUTING -s {NAT_SOURCE_NET} -o {NAT_OUT_IF} -j MASQUERADE"),
        (f"-C FORWARD -i {NAT_IN_IF} -o {NAT_OUT_IF} -j ACCEPT",
         f"-A FORWARD -i {NAT_IN_IF} -o {NAT_OUT_IF} -j ACCEPT"),
        (f"-C FORWARD -i {NAT_OUT_IF} -o {NAT_IN_IF} -m state --state RELATED,ESTABLISHED -j ACCEPT",
         f"-A FORWARD -i {NAT_OUT_IF} -o {NAT_IN_IF} -m state --state RELATED,ESTABLISHED -j ACCEPT")
    ]
    for check_cmd, add_cmd in rules:
        if subprocess.call(f"iptables {check_cmd} >/dev/null 2>&1", shell=True) != 0:
            try:
                run(f"iptables {add_cmd}", check=True)
                log_ok(f"Applied iptables rule: {add_cmd}")
            except RuntimeError as e:
                log_warn(f"Failed to apply rule ({add_cmd}): {e}")
        else:
            log_ok(f"Iptables rule exists: {add_cmd}")
    ensure_package("iptables-persistent")
    run("netfilter-persistent save || true", check=False)
    run("netfilter-persistent reload || true", check=False)
    log_ok("Saved iptables rules (if netfilter-persistent available)")

def configure_haproxy():
    ensure_package("haproxy")
    src = os.path.join(REPO_DIR, "haproxy", "haproxy.cfg")
    dst = "/etc/haproxy/haproxy.cfg"
    if ensure_file(src, dst):
        out = run(f"haproxy -c -f {dst} || true")
        log_info(out.splitlines()[0] if out else "Checked haproxy config")
        restart_service("haproxy")
    else:
        restart_service("haproxy")

def configure_nfs():
    ensure_package("nfs-kernel-server")
    base = "/shares/kubernetes"
    p1 = os.path.join(base, "StorageClass", "Delete")
    p2 = os.path.join(base, "StorageClass", "Retain")
    os.makedirs(p1, exist_ok=True)
    os.makedirs(p2, exist_ok=True)
    run(f"chown -R 1000:1000 {base} || true")
    run(f"chmod -R 777 {base} || true")
    exports = f"""{p1}  10.0.0.0/24(rw,sync,all_squash,anonuid=1000,anongid=1000,no_subtree_check,no_wdelay)
{p2} 10.0.0.0/24(rw,sync,all_squash,anonuid=1000,anongid=1000,no_subtree_check,no_wdelay)
"""
    exports_file = "/etc/exports"
    write_exports = True
    if os.path.exists(exports_file):
        with open(exports_file, "r", encoding="utf-8") as f:
            if f.read().strip() == exports.strip():
                write_exports = False
    if write_exports:
        backup_file(exports_file)
        with open(exports_file, "w", encoding="utf-8") as f:
            f.write(exports)
        log_ok("Wrote /etc/exports")
    else:
        log_ok("/etc/exports already up to date")
    run("exportfs -ra || true")
    restart_service("nfs-kernel-server")

def configure_chrony_and_timezone():
    run("timedatectl set-timezone Asia/Kolkata || true")
    log_ok("Timezone set to Asia/Kolkata (or attempted)")
    ensure_package("chrony")
    src = os.path.join(REPO_DIR, "chrony", "chrony.conf")
    dst = "/etc/chrony/chrony.conf"
    if ensure_file(src, dst):
        restart_service("chrony")
    else:
        restart_service("chrony")

# ---- Main ----
def main():
    log_info("Starting full setup (Ubuntu-only) with BIND validation ...")
    ensure_hostname()
    fix_ssh_config_and_root_password()
    disable_swap()
    disable_ufw()
    clone_repo()
    apply_netplan()
    configure_bind9()
    configure_dhcp()
    configure_nat_and_iptables()
    configure_haproxy()
    configure_nfs()
    configure_chrony_and_timezone()

    print("\n✅ Manual Verification Commands:")
    print("grep -E 'PermitRootLogin|PasswordAuthentication|UsePAM' /etc/ssh/sshd_config")
    print("ssh -vvv root@<IP>  # to debug SSH")
    print("named-checkconf /etc/bind/named.conf  # check BIND config")
    print("named-checkzone kube.lan /etc/bind/zones/db.kube.lan  # check forward zone")
    print("named-checkzone reverse /etc/bind/zones/db.reverse  # check reverse zone")
    print("chronyc sources -v")
    print("ss -uln | grep ':123' || true")
    print("exportfs -rv || true")
    print("journalctl -xeu nfs-kernel-server || true")
    print("systemctl status named bind9 isc-dhcp-server haproxy nfs-kernel-server chrony || true")

    log_ok("Script completed (review warnings above).")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_warn(f"Script terminated with error: {e}")
        raise
