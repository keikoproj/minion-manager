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


def validate_usr_args(usr_args):
    """
    Validates the arguments provided by the user.
    """
    assert usr_args.cloud.lower() == "aws", "Only AWS is currently supported."
    assert len(usr_args.scaling_groups) > 0, "At least 1 scaling group " + \
                                             "is needed"
    if "profile" not in usr_args:
        usr_args.profile = None


def run():
    """
    Parses user provided arguments and validates them. Asserts if any of
    the provided arguments is incorrect.
    """
    parser = argparse.ArgumentParser(description="Manage the minions in a " +
                                     "K8S cluster")
    parser.add_argument('--scaling-groups', required=True, nargs="+",
                        help="Names of the scaling groups to manage")
    parser.add_argument("--region", help="Region in which the cluster exists",
                        required=True)
    parser.add_argument("--cloud", default="aws",
                        help="Cloud provider (only AWS as of now)")
    parser.add_argument("--profile", help="Credentials profile to use")
    parser.add_argument("--monitor-nodes", default=False,
                        help="Check if nodes are 'Ready' and terminate if not")

    usr_args = parser.parse_args()
    validate_usr_args(usr_args)

    logger.info("Starting minion-manager for scaling groups: %s, in region " +
                "%s for cloud provider %s", usr_args.scaling_groups,
                usr_args.region, usr_args.cloud)

    if usr_args.cloud == "aws":
        minion_manager = Broker.get_impl_object(
            usr_args.cloud, usr_args.scaling_groups, usr_args.region,
            aws_profile=usr_args.profile, monitor_nodes=usr_args.monitor_nodes)
        minion_manager.run()

# A journey of a thousand miles ...
if __name__ == "__main__":
    run()
