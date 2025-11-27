#!/usr/bin/env python3
"""
Full Kubernetes control-plane setup (fixed: selects CRI socket when multiple runtimes exist).
Default preference: containerd, unless CRI_PREFER=crio is set in environment.
Run as root: sudo ./setup_kube_cp1_fix.py
"""
import subprocess, time, os, sys
from pathlib import Path

def run(cmd, check=True):
    print(f"\n>>> {cmd}\n")
    r = subprocess.run(cmd, shell=True, text=True)
    if check and r.returncode != 0:
        print(f"ERROR: command failed: {cmd}")
        sys.exit(1)
    return r

def exists(path):
    return Path(path).exists()

# -------------------------
# CRI detection helper
# -------------------------
def detect_cri_socket():
    containerd_sock = "/var/run/containerd/containerd.sock"
    crio_sock = "/var/run/crio/crio.sock"
    found = []
    if exists(containerd_sock):
        found.append(("containerd", containerd_sock))
    if exists(crio_sock):
        found.append(("crio", crio_sock))

    if not found:
        return "/var/run/containerd/containerd.sock", []

    if len(found) == 1:
        return found[0][1], found

    prefer = os.environ.get("CRI_PREFER", "").lower()
    if prefer == "crio":
        chosen = next((s for name,s in found if name=="crio"), found[0][1])
    else:
        chosen = next((s for name,s in found if name=="containerd"), found[0][1])
    return chosen, found

# -------------------------
# BEGIN main flow
# -------------------------
if os.geteuid() != 0:
    print("Run as root (sudo). Exiting.")
    sys.exit(1)

print("STEP 1 - Hostname")
run("hostnamectl set-hostname kube-cp-1.kube.lan")

print("STEP 2 - Update/Upgrade")
run("apt-get update -y")
run("apt-get upgrade -y")

# ==============================
# STEP 3 — Enable root SSH login & set password
# ==============================
root_pass = "123"
ssh_config_changed = False

run("apt install -y sshpass")

# Set root password (always)
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

print("STEP 4 - Install prereqs")
run("apt-get install -y apt-transport-https ca-certificates curl gnupg lsb-release software-properties-common")

# -------------------------
# Install containerd (idempotent)
# -------------------------
print("STEP 5 - Install containerd")
run("mkdir -p /etc/apt/keyrings || true")
run("curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc || true")
run("chmod a+r /etc/apt/keyrings/docker.asc || true")
run('sh -c "echo \\\"deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable\\\" > /etc/apt/sources.list.d/docker.list"')
run("apt-get update -y")
run("apt-get install -y containerd.io || true")
run("mkdir -p /etc/containerd && containerd config default > /etc/containerd/config.toml || true")
run("sed -i 's/SystemdCgroup = false/SystemdCgroup = true/g' /etc/containerd/config.toml || true")
run("systemctl restart containerd || true")
run("systemctl enable containerd || true")

# -------------------------
# Install Kubernetes packages
# -------------------------
print("STEP 6 - Install Kubernetes packages")
run("curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.34/deb/Release.key | gpg --dearmor -o /etc/apt/keyrings/kubernetes-1-34.gpg || true")
run('sh -c "echo \\"deb [signed-by=/etc/apt/keyrings/kubernetes-1-34.gpg] https://pkgs.k8s.io/core:/stable:/v1.34/deb/ /\\" > /etc/apt/sources.list.d/kubernetes.list"')
run("apt-get update -y")
run("apt-get install -y kubelet kubeadm kubectl || true")
run("systemctl enable kubelet || true")

print("STEP 7 - Disable swap")
run("swapoff -a || true")
run("sed -i '/\\bswap\\b/d' /etc/fstab || true")

print("STEP 8 - Kernel modules & sysctl")
run('cat <<EOF > /etc/modules-load.d/k8s.conf\noverlay\nbr_netfilter\nEOF')
run("modprobe overlay || true")
run("modprobe br_netfilter || true")
run('cat <<EOF > /etc/sysctl.d/k8s.conf\nnet.bridge.bridge-nf-call-ip6tables = 1\nnet.bridge.bridge-nf-call-iptables = 1\nnet.ipv4.ip_forward = 1\nEOF')
run("sysctl --system || true")

# -------------------------
# Detect CRI sockets and choose one
# -------------------------
cri_socket, found_list = detect_cri_socket()
if found_list:
    print(f"Detected CRI sockets on host: {', '.join(f'{n}:{s}' for n,s in found_list)}")
    print(f"Choosing CRI socket: {cri_socket} (override with CRI_PREFER=crio)")
else:
    print("No CRI socket found yet on the host. Defaulting to:", cri_socket)
    print("If kubeadm fails, ensure a CRI runtime is installed and its socket exists.")

# -------------------------
# kubeadm init with explicit cri-socket
# -------------------------
kubeadm_cmd = (
    "kubeadm init "
    f"--control-plane-endpoint '10.0.0.1:6443' "
    f"--apiserver-advertise-address=10.0.0.201 "
    f"--upload-certs "
    f"--pod-network-cidr=10.244.0.0/16 "
    f"--cri-socket {cri_socket}"
)
print("Running kubeadm init with explicit CRI socket to avoid multiple-CRI ambiguity.")
run(kubeadm_cmd, check=True)

# -------------------------
# kubeconfig copy
# -------------------------
sudo_user = os.environ.get("SUDO_USER")
if sudo_user:
    home = Path("/home")/sudo_user
else:
    home = Path(os.environ.get("HOME", "/root"))
kube_dir = home/".kube"
kube_dir.mkdir(parents=True, exist_ok=True)
run(f"cp -i /etc/kubernetes/admin.conf {kube_dir/'config'}")
if os.environ.get("SUDO_UID") and os.environ.get("SUDO_GID"):
    run(f"chown {os.environ.get('SUDO_UID')}:{os.environ.get('SUDO_GID')} {kube_dir/'config'}")
else:
    run(f"chown {os.getuid()}:{os.getgid()} {kube_dir/'config'}")

os.environ["KUBECONFIG"] = "/etc/kubernetes/admin.conf"

# -------------------------
# Apply Calico and Ingress
# -------------------------
print("Applying Calico v3.27.2")
run("kubectl apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.27.2/manifests/calico.yaml", check=True)

print("Applying ingress-nginx (baremetal)")
run("kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v0.49.0/deploy/static/provider/baremetal/deploy.yaml", check=True)

# -------------------------
# Install etcd client and check health
# -------------------------
print("Installing etcd client (if available)")
run("apt-get update -y")
run("apt-get install -y etcd-client || true")
run("ETCDCTL_API=3 etcdctl --endpoints=https://127.0.0.1:2379 --cacert=/etc/kubernetes/pki/etcd/ca.crt --cert=/etc/kubernetes/pki/etcd/peer.crt --key=/etc/kubernetes/pki/etcd/peer.key endpoint health || true")
run("ETCDCTL_API=3 etcdctl --endpoints=https://127.0.0.1:2379 --cacert=/etc/kubernetes/pki/etcd/ca.crt --cert=/etc/kubernetes/pki/etcd/peer.crt --key=/etc/kubernetes/pki/etcd/peer.key member list || true")

# -------------------------
# Wait for core pods
# -------------------------
def wait_for_pods(label_substrs, timeout=600, interval=5):
    start = time.time()
    while time.time() - start < timeout:
        out = subprocess.run("kubectl get pods -n kube-system -o wide --no-headers || true", shell=True, stdout=subprocess.PIPE, text=True).stdout
        ok = True
        for substr in label_substrs:
            if substr not in out:
                ok = False
                break
        if ok:
            if "ContainerCreating" in out or "CrashLoopBackOff" in out:
                ok = False
        if ok:
            return True
        print("...waiting for pods; sleeping", interval)
        time.sleep(interval)
    return False

if not wait_for_pods(["calico-node","calico-kube-controllers","coredns"], timeout=600, interval=6):
    print("Warning: core pods did not reach expected state within timeout. Check: kubectl get pods -n kube-system")
else:
    print("Core pods appear healthy (or at least present).")

# -------------------------
# Wait for node Ready
# -------------------------
print("Waiting for node kube-cp-1.kube.lan to be Ready...")
start = time.time()
while time.time() - start < 600:
    out = subprocess.run("kubectl get node kube-cp-1.kube.lan -o jsonpath='{.status.conditions[?(@.type==\"Ready\")].status}' || true",
                        shell=True, stdout=subprocess.PIPE, text=True).stdout.strip()
    if out == "True":
        print("Node is Ready.")
        break
    print("Node not Ready yet; sleeping 5s")
    time.sleep(5)
else:
    print("Timeout waiting for node Ready; inspect via: kubectl get pods -n kube-system; kubectl describe node kube-cp-1.kube.lan")

# -------------------------
# Final summary
# -------------------------
print("\nSummary (versions & services):")
run("crio --version || true")
run("containerd --version || true")
run("kubelet --version || true")
run("kubeadm version || true")
run("kubectl version --client || true")
run("systemctl status crio --no-pager || true")
run("systemctl status containerd --no-pager || true")
run("systemctl status kubelet --no-pager || true")

# ==============================
# Install NFS
# ==============================
run("apt install -y nfs-common")

print("\nScript finished.")
