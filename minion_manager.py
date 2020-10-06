#!/usr/bin/env python

"""The main entry point for the minion-manager."""

import argparse
import sys
import logging

from cloud_broker import Broker

logger = logging.getLogger("minion_manager")
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s " +
                    "%(threadName)s: %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S",
                    stream=sys.stdout, level=logging.INFO)


def run():
    """Parses user provided arguments and validates them.
    Asserts if any of the provided arguments is incorrect.
    """
    parser = argparse.ArgumentParser(description="Manage the minions in a K8S cluster")
    parser.add_argument("--region", help="Region in which the cluster exists",
                        required=True)
    parser.add_argument("--cloud", default="aws", choices=['aws'], type=str.lower,
                        help="Cloud provider (only AWS as of now)")
    parser.add_argument("--profile", help="Credentials profile to use")
    parser.add_argument("--refresh-interval-seconds", default="300",
                        help="Interval in seconds at which to query AWS")
    parser.add_argument("--cluster-name", required=True,
                        help="Name of the Kubernetes cluster. Get's used for identifying ASGs")
    parser.add_argument("--events-only", action='store_true', required=False,
                        help="Whether minion-manager should only emit events and *not* actually do spot/on-demand changes to launch-config")

    usr_args = parser.parse_args()

    logger.info("Starting minion-manager for cluster: %s, in region %s for cloud provider %s",
                usr_args.cluster_name,
                usr_args.region,
                usr_args.cloud)

    if usr_args.cloud == "aws":
        minion_manager = Broker.get_impl_object(
            usr_args.cloud, usr_args.cluster_name, usr_args.region, int(usr_args.refresh_interval_seconds),
            aws_profile=usr_args.profile, events_only=usr_args.events_only)
        minion_manager.run()

# A journey of a thousand miles ...
if __name__ == "__main__":
    run()
