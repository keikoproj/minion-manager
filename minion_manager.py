#!/usr/bin/env python

"""The main entry point for the minion-manager."""

import argparse
import logging
import sys

import yaml

from cloud_broker import Broker

logger = logging.getLogger("minion_manager")
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s " +
                    "%(threadName)s: %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S",
                    stream=sys.stdout, level=logging.INFO)

CONFIG = "config.yml"

def read_config():
    """Read from local config file as a potential substitute for
    argument flags
    """
    settings = {}
    with open(CONFIG) as config:
        try:
            settings = yaml.safe_load(config)
        except yaml.YAMLError:
            pass
    return settings

def run():
    """Parses user provided arguments and validates them.
    Asserts if any of the provided arguments is incorrect.
    """
    config = read_config()
    
    parser = argparse.ArgumentParser(description="Manage the minions in a K8S cluster")
    parser.add_argument("--region", help="Region in which the cluster exists",
                        default=config.get("region"))
    parser.add_argument("--cloud", default="aws", choices=['aws'], type=str.lower,
                        help="Cloud provider (only AWS as of now)")
    parser.add_argument("--profile", default=config.get("profile"), help="Credentials profile to use")
    parser.add_argument("--refresh-interval-seconds", type=int, default=config.get("refresh_interval_seconds", 300),
                        help="Interval in seconds at which to query AWS")
    parser.add_argument("--cluster-name", default=config.get("cluster_name"),
                        help="Name of the Kubernetes cluster. Get's used for identifying ASGs")
    parser.add_argument("--events-only", action='store_true',
                        help="Whether minion-manager should only emit events and *not* actually do spot/on-demand changes to launch-config")

    usr_args = parser.parse_args()
    if usr_args.region is None:
        sys.exit("Region must be specified. To see help, you can run -h.")
    if usr_args.cluster_name is None:
        sys.exit("Name of the Kubernetes cluster must be specified. To see help, you can run -h.")

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
