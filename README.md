
# Change HostName

hostnamectl set-hostname kube-svc-01.kube.lan
bash

apt update && apt upgrade -y


__________________________________________________________________________________________



# Disable swap 

sed -i '/\s*swap\s/s/^/#/' /etc/fstab
swapon -s
cat /etc/fstab | grep swap


__________________________________________________________________________________________



# Disable UFW

systemctl status ufw
systemctl stop ufw
systemctl disable ufw
systemctl status ufw


__________________________________________________________________________________________



# Install Git package

apt install -y git

# Clone Config Files
git clone https://github.com/shahrukh244/kubernetes-cluster-config.git


__________________________________________________________________________________________



# Change Network IP. For reference check ( kubernetes-cluster-config/network-ip/ ) 
# ens32 = 192.168.1.X
# ens34 = 10.0.0.1/24

cp ~/kubernetes-cluster-config/network-ip/ens32.yaml /etc/netplan/
cp ~/kubernetes-cluster-config/network-ip/ens34.yaml /etc/netplan/

netplan apply

reboot


__________________________________________________________________________________________



# Install DNS

apt install bind9 bind9-utils -y


cp ~/kubernetes-cluster-config/dns/named.conf.options /etc/bind/
cp ~/kubernetes-cluster-config/dns/named.conf.local /etc/bind/

mkdir /etc/bind/zones
cp ~/kubernetes-cluster-config/dns/zones/db.kube.lan /etc/bind/zones/
cp ~/kubernetes-cluster-config/dns/zones/db.reverse /etc/bind/zones/

systemctl restart bind9
systemctl status bind9


# Verification

journalctl -xeu bind9 -f


named-checkzone kube.lan /etc/bind/zones/db.kube.lan
named-checkzone 0.0.10.in-addr.arpa /etc/bind/zones/db.reverse
named-checkconf

dig @127.0.0.1 kube-svc01.kube.lan
dig @127.0.0.1 kube-w-1.lab.kube.lan

dig @127.0.0.1 -x 10.0.0.1
dig @127.0.0.1 -x 10.0.0.211



__________________________________________________________________________________________



# Install DHCP

apt install isc-dhcp-server -y

sed -i 's/^INTERFACESv4=""/INTERFACESv4="ens34"/' /etc/default/isc-dhcp-server

cp ~/kubernetes-cluster-config/dhcp/dhcpd.conf /etc/dhcp/

systemctl restart isc-dhcp-server
systemctl status isc-dhcp-server


# Verification

journalctl -u isc-dhcp-server -f
cat /var/lib/dhcp/dhcpd.leases



__________________________________________________________________________________________




# Enable NAT

sed -i 's/^#net\.ipv4\.ip_forward=1/net.ipv4.ip_forward=1/' /etc/sysctl.conf
sysctl -p /etc/sysctl.conf

iptables -t nat -A POSTROUTING -s 10.0.0.0/24 -o ens32 -j MASQUERADE
iptables -A FORWARD -i ens34 -o ens32 -j ACCEPT
iptables -A FORWARD -i ens32 -o ens34 -m state --state RELATED,ESTABLISHED -j ACCEPT

apt install iptables-persistent -y
netfilter-persistent save
netfilter-persistent reload


# Verification

iptables -t nat -L -n
# MASQUERADE  all  --  10.0.0.0/24  anywhere    # Output



__________________________________________________________________________________________
XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX



# Install apache

apt update && \
apt install -y apache2 && \
sed -i 's/Listen 80/Listen 0.0.0.0:8080/' /etc/apache2/ports.conf && \
sed -i 's/<VirtualHost \*:80>/<VirtualHost \*:8080>/' /etc/apache2/sites-enabled/000-default.conf && \
systemctl enable --now apache2


# Verification

systemctl status apache2
ss -tuln | grep 8080
LISTEN  0  128  0.0.0.0:8080  0.0.0.0:*     # Output

curl -I http://localhost:8080/
curl -I http://10.0.0.1:8080/     # Open In FirFox

tail -f /var/log/apache2/error.log


XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
__________________________________________________________________________________________



# Install Haproxy 

apt update && sudo apt install -y haproxy

cp ~/kubernetes-cluster-config/haproxy/haproxy.cfg  /etc/haproxy/


haproxy -c -f /etc/haproxy/haproxy.cfg

systemctl restart haproxy
systemctl enable haproxy


# Verification

curl -s http://localhost:9000/stats | head -5
http://10.0.0.1:9000/stats     # (In FireFox)



__________________________________________________________________________________________




# Install NFS


apt update
apt install -y nfs-kernel-server

mkdir -p /shares/kubernetes/StorageClass/Delete
mkdir -p /shares/kubernetes/StorageClass/Retain

chown -R nobody:nogroup /shares/kubernetes
chmod -R 777 /shares/kubernetes

echo "/shares/kubernetes/StorageClass/Delete  10.0.0.0/24(rw,sync,root_squash,no_subtree_check,no_wdelay)" | sudo tee /etc/exports
echo "/shares/kubernetes/StorageClass/Retain 10.0.0.0/24(rw,sync,root_squash,no_subtree_check,no_wdelay)" | sudo tee -a /etc/exports


# Verification

exportfs -rv

journalctl -xeu nfs-server



__________________________________________________________________________________________



# Install NTP


timedatectl set-timezone Asia/Kolkata
timedatectl


apt update
apt install -y chrony

cp ~/kubernetes-cluster-config/chrony/chrony.conf  /etc/chrony/

systemctl restart chrony
systemctl enable chrony


# Verification

chronyc sources -v
chronyc tracking

ss -uln | grep ':123'
chronyc clients



__________________________________________________________________________________________














