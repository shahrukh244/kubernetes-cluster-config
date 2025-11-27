def generate_ssh_key():
    """Generate ed25519 SSH key ONLY if not already present."""
    key_path = Path.home() / ".ssh/id_ed25519"
    pub_path = Path.home() / ".ssh/id_ed25519.pub"

    if key_path.exists() and pub_path.exists():
        ok("SSH key already exists (~/.ssh/id_ed25519)")
        return

    info("Generating new SSH key (ed25519)")
    run(f'ssh-keygen -t ed25519 -N "" -C "$(whoami)@$(hostname)" -f {key_path}')
    ok("SSH key generated")


def ping_ip(ip):
    """Ping IP once; return True if reachable."""
    out = run(f"ping -c 1 -W 1 {ip} >/dev/null 2>&1 || true", capture_output=True)
    # exit code lost because using shell; rely on output
    # but safer: retry ping using subprocess
    rc = subprocess.call(f"ping -c 1 -W 1 {ip} >/dev/null 2>&1", shell=True)
    return rc == 0


def distribute_ssh_keys():
    """Ping IP list, then ssh-copy-id for reachable hosts."""
    targets = [
        "10.0.0.1",
        "10.0.0.201",
        "10.0.0.202",
        "10.0.0.203",
        "10.0.0.211",
        "10.0.0.212"
    ]

    info("Checking reachability for target IPs...")

    reachable = []
    unreachable = []

    for ip in targets:
        if ping_ip(ip):
            ok(f"{ip} reachable")
            reachable.append(ip)
        else:
            warn(f"{ip} NOT reachable")
            unreachable.append(ip)

    if unreachable:
        warn("Some IPs unreachable — SSH key distribution WILL NOT proceed.")
        warn("Unreachable IPs:")
        for ip in unreachable:
            warn(f" - {ip}")
        return

    # All IPs are reachable → distribute keys
    info("All target IPs reachable. Running ssh-copy-id...")
    for ip in reachable:
        info(f"Copying SSH key to {ip}")
        run(
            f"sshpass -p '123' ssh-copy-id -o StrictHostKeyChecking=no "
            f"-i ~/.ssh/id_ed25519.pub root@{ip}"
        )
        ok(f"SSH key installed on {ip}")
