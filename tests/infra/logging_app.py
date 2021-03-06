# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the Apache 2.0 License.

import infra.checker
import time
import http
import random
import ccf.clients
import ccf.commit
from collections import defaultdict


from loguru import logger as LOG


class LoggingTxsVerifyException(Exception):
    """
    Exception raised if a LoggingTxs instance cannot successfully verify all
    entries previously issued.
    """


def sample_list(l, n):
    if n > len(l):
        # Return all elements
        return l
    elif n == 0:
        return []
    elif n == 1:
        # Return last element only
        return l[-1:]
    elif n == 2:
        # Return first and last elements
        return l[:1] + l[-1:]
    else:
        # Return first, last, and random sample of values in-between
        return l[:1] + random.sample(l[1:-1], n - 2) + l[-1:]


class LoggingTxs:
    def __init__(self, user_id=0):
        self.pub = defaultdict(list)
        self.priv = defaultdict(list)
        self.idx = 0
        self.user = f"user{user_id}"
        self.network = None

    def get_last_tx(self, priv=True, idx=None):
        if idx is None:
            idx = self.idx
        txs = self.priv if priv else self.pub
        msgs = txs[idx]
        return (idx, msgs[-1])

    def issue(
        self,
        network,
        number_txs=1,
        on_backup=False,
        repeat=False,
        idx=None,
        wait_for_sync=True,
    ):
        self.network = network
        remote_node, _ = network.find_primary()
        if on_backup:
            remote_node = network.find_any_backup()

        LOG.info(f"Applying {number_txs} logging txs to node {remote_node.node_id}")

        with remote_node.client(self.user) as c:
            check_commit = infra.checker.Checker(c)

            for _ in range(number_txs):
                if not repeat and idx is None:
                    self.idx += 1

                target_idx = idx
                if target_idx is None:
                    target_idx = self.idx

                priv_msg = f"Private message at idx {target_idx} [{len(self.priv[target_idx])}]"
                rep_priv = c.post(
                    "/app/log/private",
                    {
                        "id": target_idx,
                        "msg": priv_msg,
                    },
                )
                self.priv[target_idx].append(
                    {"msg": priv_msg, "seqno": rep_priv.seqno, "view": rep_priv.view}
                )

                pub_msg = (
                    f"Public message at idx {target_idx} [{len(self.pub[target_idx])}]"
                )
                rep_pub = c.post(
                    "/app/log/public",
                    {
                        "id": target_idx,
                        "msg": pub_msg,
                    },
                )
                self.pub[target_idx].append(
                    {"msg": pub_msg, "seqno": rep_pub.seqno, "view": rep_pub.view}
                )
            if number_txs and wait_for_sync:
                check_commit(rep_pub, result=True)

        if wait_for_sync:
            network.wait_for_node_commit_sync()

    def verify(self, network=None, node=None, timeout=3):
        LOG.info("Verifying all logging txs")
        if network is not None:
            self.network = network
        if self.network is None:
            raise ValueError(
                "Network object is not yet set - txs should be issued before calling verify"
            )

        sample_count = 5
        nodes = self.network.get_joined_nodes() if node is None else [node]
        for node in nodes:
            for pub_idx, pub_value in self.pub.items():
                # As public records do not yet handle historical queries,
                # only verify the latest entry
                entry = pub_value[-1]
                self._verify_tx(
                    node,
                    pub_idx,
                    entry["msg"],
                    entry["seqno"],
                    entry["view"],
                    priv=False,
                    timeout=timeout,
                )

            for priv_idx, priv_value in self.priv.items():
                # Verifying all historical transactions is expensive, verify only a sample
                for v in sample_list(priv_value, sample_count):
                    self._verify_tx(
                        node,
                        priv_idx,
                        v["msg"],
                        v["seqno"],
                        v["view"],
                        priv=True,
                        historical=(v != priv_value[-1]),
                        timeout=timeout,
                    )

    def _verify_tx(
        self, node, idx, msg, seqno, view, priv=True, historical=False, timeout=3
    ):
        if historical and not priv:
            raise ValueError(
                "Historical queries are only implemented with private records"
            )

        cmd = "/app/log/private" if priv else "/app/log/public"
        headers = {}
        if historical:
            cmd = "/app/log/private/historical"
            headers = {
                ccf.clients.CCF_TX_VIEW_HEADER: str(view),
                ccf.clients.CCF_TX_SEQNO_HEADER: str(seqno),
            }

        found = False
        start_time = time.time()
        while time.time() < (start_time + timeout):
            with node.client(self.user) as c:
                ccf.commit.wait_for_commit(c, seqno, view, timeout)

                rep = c.get(f"{cmd}?id={idx}", headers=headers)
                if rep.status_code == http.HTTPStatus.OK:
                    expected_result = {"msg": msg}
                    assert (
                        rep.body.json() == expected_result
                    ), "Expected {}, got {}".format(expected_result, rep.body)
                    found = True
                    break
                elif rep.status_code == http.HTTPStatus.NOT_FOUND:
                    LOG.warning("User frontend is not yet opened")
                    continue

                if historical:
                    if rep.status_code == http.HTTPStatus.ACCEPTED:
                        retry_after = rep.headers.get("retry-after")
                        if retry_after is None:
                            raise ValueError(
                                f"Response with status {rep.status_code} is missing 'retry-after' header"
                            )
                        sleep_time = 0.5
                        LOG.info(
                            f"Sleeping for {sleep_time}s waiting for historical query processing..."
                        )
                        time.sleep(sleep_time)
                    elif rep.status_code == http.HTTPStatus.NO_CONTENT:
                        raise ValueError(
                            f"Historical query response claims there was no write to {idx} at {view}.{seqno}"
                        )
                    else:
                        raise ValueError(
                            f"Unexpected response status code {rep.status_code}: {rep.body}"
                        )
                time.sleep(0.1)

        if not found:
            raise LoggingTxsVerifyException(
                f"Unable to retrieve entry at {idx} (seqno: {seqno}, view: {view}) after {timeout}s"
            )
