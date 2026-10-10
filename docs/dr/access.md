# DR runbook: host baseline and admin access (VPN, bastion, DNS)

**Depends on:** hosts reachable through the provider console or out-of-band access, the inventory.
**RPO:** n.a. (configuration from git). **RTO:** 2 h.
**Backup:** none needed. Everything comes from the repository and the inventory. Session recordings
are in the SIEM. `/etc` of every host is in the `host-etc` backup declaration, for forensics only.

## Rebuild

1. Create the hosts with the same names and addresses. Put the data paths on encrypted volumes
   (docs/disk-encryption.md). With clevis/Tang, rebuild the Tang servers first, or unlock with the
   LUKS recovery key from the envelope.
2. From an admin machine with the repository and the inventory, using the provider's initial SSH
   key or console:

   ```sh
   ansible-playbook -i inventories/<env> playbooks/rbac.yml playbooks/baseline.yml playbooks/container_hosts.yml
   ansible-playbook -i inventories/<env> playbooks/access.yml
   ```

3. Until OpenBao is back (step 3 of the rebuild order), SSH certificates cannot be issued. Use the
   break-glass key from the envelope through the bastion, and record each use.
4. Hand out new WireGuard configurations if the gateway keys were not restored from `host-etc`
   (`playbooks/access.yml` prints which peers need one).

## Verify

- `ssh` through the bastion works with an OpenBao-signed certificate (after step 3 of the order).
- DNS resolves internal names from every zone. The firewall default-deny is active (`nft list ruleset`).
- Session recording starts on the bastion (IAM-07).

## Drill

No data to restore. The yearly game day rebuilds the bastion and one VPN gateway in the lab
inventory and times it.
