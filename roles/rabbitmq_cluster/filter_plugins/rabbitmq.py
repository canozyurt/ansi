# Copyright compliant-infra-ansible contributors
# SPDX-License-Identifier: Apache-2.0
"""Filters for the rabbitmq_cluster role."""

import base64
import hashlib


def rabbitmq_password_hash(password, seed=""):
    """Return a RabbitMQ password hash (rabbit_password_hashing_sha256) for the HTTP API.

    The format is base64(salt + sha256(salt + password)) with a 4-byte salt. The salt is derived
    from the seed and the password, so the same input always gives the same hash and the role can
    compare it with what the cluster stores (idempotent runs without sending the password again).
    """
    pw = str(password).encode("utf-8")
    salt = hashlib.sha256(str(seed).encode("utf-8") + b"\0" + pw).digest()[:4]
    return base64.b64encode(salt + hashlib.sha256(salt + pw).digest()).decode("ascii")


class FilterModule:
    """Ansible filter plugin."""

    def filters(self):
        return {"rabbitmq_password_hash": rabbitmq_password_hash}
