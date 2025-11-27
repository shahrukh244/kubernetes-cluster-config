#!/usr/bin/env python3
import os
import subprocess

def run(cmd, check=True):
    """Run a shell command"""
    print(f">>> Running: {cmd}")
    result = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if result.returncode != 0 and check:
        print(f"❌ ERROR: {result.stderr.strip()}")
        exit(1)
    return result.stdout.strip()

# ==============================
# STEP 1 — Set Hostname
# ==============================
desired_hostname = "kube-cp-3.kube.lan"
current_hostname = run("hostnamectl --static")
if current_hostname != desired_hostname:
    run(f"hostnamectl set-hostname {desired_hostname}")
else:
    print(f"✅ Hostname already set to {desired_hostname}")

# ==============================
# STEP 2 — System Update
# ==============================
run("apt update && apt upgrade -y")

# ==============================
# STEP 3 — Enable root SSH login & set password
# ==============================
root_pass = "123"
ssh_config_changed = False

run("apt install -y sshpass")

# Set root password
current_pass_hash = run("sudo grep root /etc/shadow").split(":")[1]
if current_pass_hash == "":
    run(f"echo 'root:{root_pass}' | sudo chpasswd")
else:
    run(f"echo 'root:{root_pass}' | sudo chpasswd")

# Enable root SSH login
ssh_file = "/etc/ssh/sshd_config"
with open(ssh_file, "r") as f:
    ssh_content = f.read()

if "PermitRootLogin yes" not in ssh_content:
    run("sudo sed -i 's/#\\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config")
    ssh_config_changed = True

if "PasswordAuthentication yes" not in ssh_content:
    run("sudo sed -i 's/#\\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config")
    ssh_config_changed = True

if ssh_config_changed:
    run("sudo systemctl reload ssh")
else:
    print("✅ SSH root login already enabled")

# ==============================
# STEP 4 — Disable Swap
# ==============================
fstab_file = "/etc/fstab"
with open(fstab_file, "r") as f:
    fstab_content = f.read()

if "swap" in fstab_content:
    run("sed -i '/\\s*swap\\s/s/^/#/' /etc/fstab")
    run("swapoff -a")
    print("✅ Swap disabled")
else:
    print("✅ Swap already disabled")

# ==============================
# STEP 5 — Disable UFW
# ==============================
ufw_status = run("systemctl is-active ufw", check=False)
if ufw_status != "inactive":
    run("systemctl stop ufw")
    run("systemctl disable ufw")
    print("✅ UFW disabled")
else:
    print("✅ UFW already inactive")

# ==============================
# STEP 6 — Enable IP Forwarding
# ==============================
ip_forward_file = "/etc/sysctl.d/99-kubernetes-cri.conf"
if not os.path.exists(ip_forward_file):
    run('echo "net.ipv4.ip_forward=1" | sudo tee /etc/sysctl.d/99-kubernetes-cri.conf')
    run("sudo sysctl --system")
else:
    print("✅ IP forwarding already enabled")

# ==============================
# STEP 7 — Install NTP Client
# ==============================
run("timedatectl set-timezone Asia/Kolkata")
run("apt update")
run("apt install -y chrony")

# Update chrony config
chrony_conf = "/etc/chrony/chrony.conf"
with open(chrony_conf, "r") as f:
    content = f.read()
if "server 10.0.0.1 iburst" not in content:
    run(
        "sed -i -E '/^pool ntp.ubuntu.com[[:space:]]+iburst maxsources 4/s/^/#/;"
        "/^pool 0.ubuntu.pool.ntp.org[[:space:]]+iburst maxsources 1/s/^/#/;"
        "/^pool 1.ubuntu.pool.ntp.org[[:space:]]+iburst maxsources 1/s/^/#/;"
        "/^pool 2.ubuntu.pool.ntp.org[[:space:]]+iburst maxsources 2/s/^/#/;"
        "/^#pool 2.ubuntu.pool.ntp.org[[:space:]]+iburst maxsources 2/a\\"
        "server 10.0.0.1 iburst' /etc/chrony/chrony.conf"
    )
    run("systemctl restart chrony")
    run("chronyc sources")
    run("chronyc tracking")
else:
    print("✅ Chrony already configured with NTP server 10.0.0.1")

# ==============================
# STEP 8 — Install NFS
# ==============================
run("apt install -y nfs-common")

# ==============================
# STEP 9 — Install CRI-O
# ==============================
crio_version = "v1.34"
crio_keyfile = "/etc/apt/keyrings/cri-o-apt-keyring.gpg"
crio_sources = f"/etc/apt/sources.list.d/cri-o.list"

if not os.path.exists(crio_keyfile):
    run(f"mkdir -p /etc/apt/keyrings")
    run(f"curl -fsSL https://download.opensuse.org/repositories/isv:/cri-o:/stable:/{crio_version}/deb/Release.key | sudo gpg --dearmor -o {crio_keyfile}")
    run(f'echo "deb [signed-by={crio_keyfile}] https://download.opensuse.org/repositories/isv:/cri-o:/stable:/{crio_version}/deb/ /" | sudo tee {crio_sources}')
    run("apt update")

run("apt install -y cri-o")
run("systemctl enable --now crio")
run("systemctl restart crio")
print("✅ CRI-O installed and running")
run("crio --version")

# ==============================
# STEP 10 — Install Kubernetes
# ==============================
k8s_keyfile = "/etc/apt/keyrings/kubernetes-apt-keyring.gpg"
k8s_sources = "/etc/apt/sources.list.d/kubernetes.list"

if not os.path.exists(k8s_keyfile):
    run("apt-get install -y apt-transport-https ca-certificates curl gpg")
    run(f"curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.34/deb/Release.key | sudo gpg --dearmor -o {k8s_keyfile}")
    run(f'echo "deb [signed-by={k8s_keyfile}] https://pkgs.k8s.io/core:/stable:/v1.34/deb/ /" | sudo tee {k8s_sources}')
    run("apt-get update")

run("apt-get install -y kubelet kubeadm kubectl")
run("apt-mark hold kubelet kubeadm kubectl")
print("✅ Kubernetes installed and held")
