# DR runbook: mail relay

**Depends on:** OpenBao (TLS, DKIM key), DNS records (SPF, DKIM, DMARC) at the DNS provider.
**RPO:** n.a. (the queue is transient). **RTO:** 1 h.
**Backup:** none. Configuration comes from the inventory. The DKIM private key is in OpenBao or
the vault. Mail still queued on a lost node is lost: senders retry, and alerts are re-sent while
they still fire.

## Rebuild

1. Restore the hosts. Run `ansible-playbook -i ../inventories/<env> playbooks/mail_relay.yml`.
2. Check that the DKIM selector in DNS matches the key the role installed. If the key had to be
   regenerated, publish the new selector before mail leaves.

## Verify

- Send a test message from a host. `MailRelayDown` and `MailRelayDkimDown` are clear. The DMARC
  report from the receiving side shows DKIM pass.

## Drill

No data to restore. The yearly game day rebuilds one relay in the lab.
