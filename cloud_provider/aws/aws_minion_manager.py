"""Minion Manager implementation for AWS."""

import logging
import re
import sys
import os
import time
import base64
from datetime import datetime
from threading import Timer, Semaphore
import boto3
from botocore.exceptions import ClientError
from retrying import retry
from bunch import bunchify
import pytz
import shlex
import subprocess
from constants import SECONDS_PER_MINUTE, SECONDS_PER_HOUR
from cloud_provider.aws.aws_bid_advisor import AWSBidAdvisor
from cloud_provider.aws.price_info_reporter import AWSPriceReporter
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from ..base import MinionManagerBase
from .asg_mm import AWSAutoscalinGroupMM, MINION_MANAGER_LABEL

logger = logging.getLogger("aws_minion_manager")
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s " +
                    "%(threadName)s: %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S",
                    stream=sys.stdout, level=logging.INFO)
logging.getLogger('boto3').setLevel(logging.WARNING)
logging.getLogger('botocore').setLevel(logging.WARNING)


class AWSMinionManager(MinionManagerBase):
    """
    This class implements the minion-manager functionality for AWS.
    """

    def __init__(self, cluster_name, region, refresh_interval_seconds=300, **kwargs):
        super(AWSMinionManager, self).__init__(region)
        self._cluster_name = cluster_name
        aws_profile = kwargs.get("aws_profile", None)
        if aws_profile:
            boto_session = boto3.Session(region_name=region,
                                         profile_name=aws_profile)
        else:
            boto_session = boto3.Session(region_name=region)

        self.incluster = kwargs.get("incluster", True)
        self._ac_client = boto_session.client('autoscaling')
        self._ec2_client = boto_session.client('ec2')
        self._events_only = kwargs.get("events_only", False)

        self._refresh_interval_seconds = refresh_interval_seconds
        self._asg_metas = []
        self.instance_type = None
        # Setting default termination to one instance at a time
        self.terminate_percentage = 1

        self.on_demand_kill_threads = {}
        self.minions_ready_checker_thread = None

        self.bid_advisor = AWSBidAdvisor(
            on_demand_refresh_interval=4 * SECONDS_PER_HOUR,
            spot_refresh_interval=15 * SECONDS_PER_MINUTE, region=region)

        self.price_reporter = AWSPriceReporter(
            self._ec2_client, self.bid_advisor, self._asg_metas)

    @staticmethod
    @retry(wait_exponential_multiplier=1000, stop_max_attempt_number=3)
    def describe_asg_with_retries(ac_client, asgs=[]):
        """
        AWS describe_auto_scaling_groups with retries.
        """
        response = ac_client.describe_auto_scaling_groups(
            AutoScalingGroupNames=asgs)
        return bunchify(response)

    @staticmethod
    @retry(wait_exponential_multiplier=1000, stop_max_attempt_number=3)
    def describe_asg_activities_with_retries(ac_client, asg):
        """
        AWS describe_auto_scaling_groups with retries.
        """
        response = ac_client.describe_scaling_activities(
            AutoScalingGroupName=asg)
        return bunchify(response)

    @staticmethod
    @retry(wait_exponential_multiplier=1000, stop_max_attempt_number=3)
    def get_instances_with_retries(ec2_client, instance_ids):
        """
        AWS describe_instances with retries.
        """
        response = ec2_client.describe_instances(
            InstanceIds=instance_ids)
        return bunchify(response)

    @staticmethod
    @retry(wait_exponential_multiplier=1000, stop_max_attempt_number=3)
    def get_asgs_with_tags(cluster_name, ac_client):
        """
        Get AWS describe_auto_scaling_groups with k8s-minion-manager tags.
        """
        response = {}
        response["AutoScalingGroups"] = []
        resp = ac_client.describe_auto_scaling_groups(MaxRecords=100)
        for r in resp["AutoScalingGroups"]:
            is_candidate = False
            # Scan for KubernetesCluster name. If the value matches the cluster_name
            # provided in the input, set 'is_candidate'.
            for tag in r['Tags']:
                if tag['Key'] == 'KubernetesCluster' and tag['Value'] == cluster_name:
                    is_candidate = True
            if not is_candidate:
                continue
            for tag in r['Tags']:
                if tag['Key'] == MINION_MANAGER_LABEL:
                    response["AutoScalingGroups"].append(r)
                    break
        return bunchify(response)

    @staticmethod
    @retry(wait_exponential_multiplier=1000, stop_max_attempt_number=3)
    def describe_spot_request_with_retries(ec2_client, request_ids):
        response = ec2_client.describe_spot_instance_requests(
            SpotInstanceRequestIds=request_ids)
        return bunchify(response)

    def discover_asgs(self):
        """ Query AWS and get metadata about all required ASGs. """
        response = AWSMinionManager.get_asgs_with_tags(self._cluster_name, self._ac_client)
        for asg in response.AutoScalingGroups:
            asg_mm = AWSAutoscalinGroupMM()
            asg_mm.set_asg_info(asg)
            self._asg_metas.append(asg_mm)
            logger.info("Adding asg %s (%s). Can manager terminate instance: %s", asg_mm.get_name(),
                        asg_mm.get_mm_tag(), "no " if asg_mm.not_terminate_instance() else "yes")

    def populate_current_config(self):
        """
        Queries AWS to get current bid_price for all ASGs and stores it
        in AWSAutoscalinGroupMM.
        """
        @retry(wait_exponential_multiplier=1000, stop_max_attempt_number=3)
        def _describe_launch_configuration():
            response = self._ac_client.describe_launch_configurations(
                LaunchConfigurationNames=[asg.LaunchConfigurationName])
            assert len(response["LaunchConfigurations"]) == 1
            return bunchify(response).LaunchConfigurations[0]

        for asg_meta in self._asg_metas:
            asg = asg_meta.asg_info

            # Get current launch configuration.
            launch_config = _describe_launch_configuration()
            asg_meta.set_lc_info(launch_config)
            bid_info = {}
            if "SpotPrice" in launch_config.keys():
                bid_info["type"] = "spot"
                bid_info["price"] = launch_config.SpotPrice
            else:
                bid_info["type"] = "on-demand"
            asg_meta.set_bid_info(bid_info)
            logger.info("ASG %s using launch-config %s with bid-info %s",
                        asg.AutoScalingGroupName,
                        launch_config.LaunchConfigurationName, bid_info)

    def log_k8s_event(self, asg_name, price="", useSpot=False):
        msg_str = '{"apiVersion":"v1alpha1","spotPrice":"' + price + '", "useSpot": ' + str(useSpot).lower() + '}'
        event_namespace = os.getenv('EVENT_NAMESPACE', 'default')
        if not self.incluster:
            logger.info(msg_str)
            return

        try:
            config.load_incluster_config()
            v1 = client.CoreV1Api()
            event_timestamp = datetime.now(pytz.utc)
            event_name = "spot-instance-update"
            new_event = client.V1Event(
                count=1,
                first_timestamp=event_timestamp,
                involved_object=client.V1ObjectReference(
                    kind="SpotPriceInfo",
                    name=asg_name,
                    namespace=event_namespace,
                ),
                last_timestamp=event_timestamp,
                metadata=client.V1ObjectMeta(
                    generate_name=event_name,
                ),
                message=msg_str,
                reason="SpotRecommendationGiven",
                source=client.V1EventSource(
                    component="minion-manager",
                ),
                type="Normal",
            )

            v1.create_namespaced_event(namespace=event_namespace, body=new_event)
            logger.info("Spot price info event logged")
        except Exception as e:
            logger.info("Failed to log event: " + str(e))

    def get_new_bid_info(self, asg_meta):
        """ get new bid price. """
        new_bid_info = self.bid_advisor.get_new_bid(
                        zones=asg_meta.asg_info.AvailabilityZones,
                        instance_type=asg_meta.lc_info.InstanceType)
        return new_bid_info

    def update_needed(self, asg_meta):
        """ Checks if an ASG needs to be updated. """
        try:
            asg_tag = asg_meta.get_mm_tag()
            bid_info = asg_meta.get_bid_info()
            if not bid_info.get("price"):
                current_price = self.get_new_bid_info(asg_meta).get("price") or ""
            else:
                current_price = bid_info.get("price")

            if asg_tag == "no-spot":
                if bid_info["type"] == "spot":
                    logger.info("ASG %s configured with on-demand but currently using spot. Update needed", asg_meta.get_name())
                    # '{"apiVersion":"v1alpha1","spotPrice":bid_info["price"], "useSpot": False}'
                    self.log_k8s_event(asg_meta.get_name(), current_price, False)
                    return True
                elif bid_info["type"] == "on-demand":
                    logger.info("ASG %s configured with on-demand and currently using on-demand. No update needed", asg_meta.get_name())
                    # '{"apiVersion":"v1alpha1","spotPrice":"", "useSpot": False}'
                    self.log_k8s_event(asg_meta.get_name(), "", False)
                    return False

            # The asg_tag is "spot".
            if bid_info["type"] == "on-demand":
                logger.info("ASG %s configured with spot but currently using on-demand. Update needed", asg_meta.get_name())
                # '{"apiVersion":"v1alpha1","spotPrice":"", "useSpot": true}'
                self.log_k8s_event(asg_meta.get_name(), current_price, True)
                return True
            else:
                # Continue to use spot
                self.log_k8s_event(asg_meta.get_name(), current_price, True)
            assert bid_info["type"] == "spot"
            if self.check_scaling_group_instances(asg_meta):
                # Desired # of instances running. No updates needed.
                logger.info("Desired number of instances running in ASG %s. No update needed", asg_meta.get_name())
                return False
            else:
                # Desired # of instances are not running.
                logger.info("Desired number of instance not running in ASG %s. Update needed", asg_meta.get_name())
                return True
        except Exception as ex:
            logger.error("Failed while checking minions in %s: %s",
                         asg_meta.get_name(), str(ex))
            return False

    def are_bids_equal(self, cur_bid_info, new_bid_info):
        """
        Returns True if the new bid_info is the same as the current one.
        False otherwise.
        """
        if cur_bid_info["type"] != new_bid_info["type"]:
            return False
        # If you're here, it means that the bid types are equal.
        if cur_bid_info["type"] == "on-demand":
            return True

        if cur_bid_info["price"] == new_bid_info["price"]:
            return True

        return False

    @retry(wait_exponential_multiplier=1000, stop_max_attempt_number=3)
    def create_lc_with_spot(self, new_lc_name, launch_config, spot_price):
        """ Creates a launch-config for using spot-instances. """
        try:
            if hasattr(launch_config, "AssociatePublicIpAddress"):
                response = self._ac_client.create_launch_configuration(
                    LaunchConfigurationName=new_lc_name,
                    ImageId=launch_config.ImageId,
                    KeyName=launch_config.KeyName,
                    SecurityGroups=launch_config.SecurityGroups,
                    ClassicLinkVPCSecurityGroups=launch_config.
                    ClassicLinkVPCSecurityGroups,
                    UserData=base64.b64decode(launch_config.UserData),
                    InstanceType=launch_config.InstanceType,
                    BlockDeviceMappings=launch_config.BlockDeviceMappings,
                    InstanceMonitoring=launch_config.InstanceMonitoring,
                    SpotPrice=spot_price,
                    IamInstanceProfile=launch_config.IamInstanceProfile,
                    EbsOptimized=launch_config.EbsOptimized,
                    AssociatePublicIpAddress=launch_config.
                    AssociatePublicIpAddress)
            else:
                response = self._ac_client.create_launch_configuration(
                    LaunchConfigurationName=new_lc_name,
                    ImageId=launch_config.ImageId,
                    KeyName=launch_config.KeyName,
                    SecurityGroups=launch_config.SecurityGroups,
                    ClassicLinkVPCSecurityGroups=launch_config.
                    ClassicLinkVPCSecurityGroups,
                    UserData=base64.b64decode(launch_config.UserData),
                    InstanceType=launch_config.InstanceType,
                    BlockDeviceMappings=launch_config.BlockDeviceMappings,
                    InstanceMonitoring=launch_config.InstanceMonitoring,
                    SpotPrice=spot_price,
                    IamInstanceProfile=launch_config.IamInstanceProfile,
                    EbsOptimized=launch_config.EbsOptimized)				
            assert response is not None, \
                "Failed to create launch-config {}".format(new_lc_name)
            assert response["HTTPStatusCode"] == 200, \
                "Failed to create launch-config {}".format(new_lc_name)
            logger.info("Created LaunchConfig for spot instances: %s",
                        new_lc_name)
        except ClientError as ce:
            if "AlreadyExists" in str(ce):
                logger.info("LaunchConfig %s already exists. Reusing it.",
                            new_lc_name)
                return
            raise ce

    @retry(wait_exponential_multiplier=1000, stop_max_attempt_number=3)
    def create_lc_on_demand(self, new_lc_name, launch_config):
        """ Creates a launch-config for using on-demand instances. """
        try:
            if hasattr(launch_config, "AssociatePublicIpAddress"):
                response = self._ac_client.create_launch_configuration(
                    LaunchConfigurationName=new_lc_name,
                    ImageId=launch_config.ImageId,
                    KeyName=launch_config.KeyName,
                    SecurityGroups=launch_config.SecurityGroups,
                    ClassicLinkVPCSecurityGroups=launch_config.
                    ClassicLinkVPCSecurityGroups,
                    UserData=base64.b64decode(launch_config.UserData),
                    InstanceType=launch_config.InstanceType,
                    BlockDeviceMappings=launch_config.BlockDeviceMappings,
                    InstanceMonitoring=launch_config.InstanceMonitoring,
                    IamInstanceProfile=launch_config.IamInstanceProfile,
                    EbsOptimized=launch_config.EbsOptimized,
                    AssociatePublicIpAddress=launch_config.
                    AssociatePublicIpAddress)
            else:
                response = self._ac_client.create_launch_configuration(
                    LaunchConfigurationName=new_lc_name,
                    ImageId=launch_config.ImageId,
                    KeyName=launch_config.KeyName,
                    SecurityGroups=launch_config.SecurityGroups,
                    ClassicLinkVPCSecurityGroups=launch_config.
                    ClassicLinkVPCSecurityGroups,
                    UserData=base64.b64decode(launch_config.UserData),
                    InstanceType=launch_config.InstanceType,
                    BlockDeviceMappings=launch_config.BlockDeviceMappings,
                    InstanceMonitoring=launch_config.InstanceMonitoring,
                    IamInstanceProfile=launch_config.IamInstanceProfile,
                    EbsOptimized=launch_config.EbsOptimized)
            assert response is not None, \
                "Failed to create launch-config {}".format(new_lc_name)
            assert response["HTTPStatusCode"] == 200, \
                "Failed to create launch-config {}".format(new_lc_name)
            logger.info("Created LaunchConfig for on-demand instances: %s",
                        new_lc_name)
        except ClientError as ce:
            if "AlreadyExists" in str(ce):
                logger.info("LaunchConfig %s already exists. Reusing it.",
                            new_lc_name)
                return
            raise ce

    def update_scaling_group(self, asg_meta, new_bid_info):
        """
        Updates the AWS AutoScalingGroup. Makes the next_bid_info as the new
        bid_info.
        """
        if self._events_only:
            logger.info("Minion-manager configured for only generating events. No changes to launch config will be made.")
            return

        logger.info("Updating ASG: %s, Bid: %s", asg_meta.get_name(),
                    new_bid_info)
        launch_config = asg_meta.get_lc_info()

        orig_launch_config_name = launch_config.LaunchConfigurationName
        assert new_bid_info.get("type", None) is not None, \
            "Bid info has no bid type"
        if new_bid_info["type"] == "spot":
            spot_price = new_bid_info["price"]
        else:
            spot_price = None
        logger.info("ASG(%s): New bid price %s", asg_meta.get_name(),
                    spot_price)

        if launch_config.LaunchConfigurationName[-2:] == "-0":
            new_lc_name = launch_config.LaunchConfigurationName[:-2]
        else:
            new_lc_name = launch_config.LaunchConfigurationName + "-0"
        logger.info("ASG(%s): New launch-config name: %s",
                    asg_meta.get_name(), new_lc_name)

        if spot_price is None:
            self.create_lc_on_demand(new_lc_name, launch_config)
        else:
            self.create_lc_with_spot(new_lc_name, launch_config, spot_price)

        @retry(wait_exponential_multiplier=1000, stop_max_attempt_number=3)
        def _update_asg_in_aws(asg_name, launch_config_name):
            self._ac_client.update_auto_scaling_group(
                AutoScalingGroupName=asg_name,
                LaunchConfigurationName=launch_config_name)
            logger.info("Updated ASG %s with new LaunchConfig: %s",
                        asg_name, launch_config_name)

        _update_asg_in_aws(asg_meta.get_name(), new_lc_name)

        @retry(wait_exponential_multiplier=1000, stop_max_attempt_number=3)
        def _delete_launch_config(lc_name):
            self._ac_client.delete_launch_configuration(
                LaunchConfigurationName=lc_name)
            logger.info("Deleted launch-configuration %s", lc_name)

        _delete_launch_config(orig_launch_config_name)

        # Update asg_meta.
        launch_config.LaunchConfigurationName = new_lc_name
        if spot_price is None:
            launch_config.pop('SpotPrice', None)
        else:
            launch_config['SpotPrice'] = spot_price
        asg_meta.set_lc_info(launch_config)
        asg_meta.set_bid_info(new_bid_info)

        logger.info("Updated ASG %s, new launch-config %s, bid-info %s",
                    asg_meta.get_name(), launch_config.LaunchConfigurationName,
                    new_bid_info)
        return

    def wait_for_all_running(self, asg_meta):
        """
        Wating for all instances in ASG to be running state.
        """
        asg_name = asg_meta.get_name()
        all_done = False
        while not all_done:
            resp = self._ac_client.describe_auto_scaling_groups(
                AutoScalingGroupNames=[asg_name])
            desired_instances = resp["AutoScalingGroups"][0]["DesiredCapacity"]
            running_instances = 0
            for i in resp["AutoScalingGroups"][0]["Instances"]:
                if i["HealthStatus"] == "Healthy":
                    running_instances += 1

            if running_instances == desired_instances:
                logger.info("ASG %s has all running instances", asg_name)
                all_done = True
            else:
                logger.info("Desired %s, Running %s",
                            desired_instances, running_instances)
                all_done = False
                time.sleep(60)

    def get_name_for_instance(self, instance):
        config.load_incluster_config()
        v1 = client.CoreV1Api()
        for item in v1.list_node().items:
            if instance.InstanceId in item.spec.provider_id:
                logger.info("Instance name for %s in Kubernetes clusters is %s",
                    instance.InstanceId, item.metadata.name)
                return item.metadata.name
        return None

    def cordon_node(self, instance):
        """" Runs 'kubectl drain' to actually drain the node."""
        instance_name = self.get_name_for_instance(instance)
        if instance_name:
            try:
                cmd = "kubectl drain " + instance_name + " --ignore-daemonsets=true --delete-local-data=true --force --grace-period=-1"
                subprocess.check_call(shlex.split(cmd))
                logger.info("Drained instance %s", instance_name)
            except Exception as ex:
                logger.info("Failed to drain node: " + str(ex) + ". Will try to uncordon")
                cmd = "kubectl uncordon " + instance_name
                subprocess.check_call(shlex.split(cmd))
                logger.info("Uncordoned node " + instance_name)
        else:
            logger.info("Instance %s not found in Kubernetes cluster. Will not drain the instance.",
                instance.InstanceId)
        return True

    @retry(wait_exponential_multiplier=1000, stop_max_attempt_number=3)
    def run_or_die(self, instance, asg_meta, asg_semaphore):
        """ Terminates the given instance. """
        zones = asg_meta.asg_info.AvailabilityZones
        bid_info = self.bid_advisor.get_new_bid(zones, instance.InstanceType)
        is_spot_instance = 'InstanceLifecycle' in instance
        is_on_demand_instance = not is_spot_instance
        with asg_semaphore:
            try:
                # If the instance is spot and the ASG is spot: don't kill the instance.
                if asg_meta.get_mm_tag() == "use-spot" and is_spot_instance:
                    logger.info("Instance %s (%s) is spot and ASG %s is spot. Ignoring termination.",
                                asg_meta.get_instance_name(instance), instance.InstanceId, asg_meta.get_name())
                    return False

                # If the instance is on-demand and the ASG is on-demand: don't kill the instance.
                if asg_meta.get_mm_tag() == "no-spot" and is_on_demand_instance:
                    logger.info("Instance %s (%s) is on-demand and ASG %s is on-demand. Ignoring termination.",
                                asg_meta.get_instance_name(instance), instance.InstanceId, asg_meta.get_name())
                    return False

                # If the instance is on-demand and ASG is spot; check if the bid recommendation. If the bid_recommendation is spot, terminate the instance.
                if asg_meta.get_mm_tag() == "use-spot" and is_on_demand_instance:
                    if bid_info["type"] == "on-demand":
                        logger.info("Instance %s (%s) is on-demand and ASG %s is spot. However, current recommendation is to use on-demand instances. Ignoring termination.",
                                    asg_meta.get_instance_name(instance), instance.InstanceId, asg_meta.get_name())
                        return False

                # Cordon and drain the node first
                self.cordon_node(instance)
                
                # Terminate EC2 uisng autoscaling client
                self._ac_client.terminate_instance_in_auto_scaling_group(InstanceId=instance.InstanceId,
                                                                         ShouldDecrementDesiredCapacity=False)
                logger.info("Terminated instance %s", instance.InstanceId)
                asg_meta.remove_instance(instance.InstanceId)
                logger.info("Removed instance %s from ASG %s", instance.InstanceId, asg_meta.get_name())
                logger.info("Sleeping 180s before checking ASG")
                time.sleep(180)
                self.wait_for_all_running(asg_meta)
                return True
            except Exception as ex:
                logger.error("Failed in run_or_die: %s", str(ex))
            finally:
                self.on_demand_kill_threads.pop(instance.InstanceId, None)

    def set_semaphore(self, asg_meta):
        """
        Update no of instances can be terminated based on percentage.
        """
        asg_name = asg_meta.get_name()
        asg_semaphore = 'semaphore' + asg_name
        resp = self._ac_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        desired_instances = resp["AutoScalingGroups"][0]["DesiredCapacity"]
        if self.terminate_percentage > 100:
            self.terminate_percentage = 100
        elif self.terminate_percentage <= 0:
            self.terminate_percentage = 1
        # Get no of instance can parallel be rotated
        svalue = int(round(desired_instances * (self.terminate_percentage/100.0)))
        if svalue == 0:
            svalue = 1
        logger.info("Maximum %d instance will be rotated at a time for ASG %s", svalue, asg_name)
        asg_semaphore = Semaphore(value=svalue)
        return asg_semaphore

    def schedule_instance_termination(self, asg_meta):
        """
        Checks whether any of the instances in the asg need to be terminated.
        """
        instances = asg_meta.get_instances()
        if len(instances) == 0:
            return
        
        # Check if ASG set not to terminate instance
        if asg_meta.not_terminate_instance():
            return

        # Check if the minion-manager is only configured to log events.
        if self._events_only:
            logger.info("Minion-manager configured for only generating events. No instances will be terminated.")
            return

        # If the ASG is configured to use "no-spot" or the required tag does not exist,
        # do not schedule any instance termination.
        asg_tag = asg_meta.get_mm_tag()

        # Setting Semaphore per ASG base on instance count and terminate_percentage
        asg_semaphore = self.set_semaphore(asg_meta)

        for instance in instances:
            # On-demand instances don't have the InstanceLifecycle field in
            # their responses. Spot instances have InstanceLifecycle=spot.

            # If the instance type and the ASG tag match, do not terminate the instance.
            is_spot = 'InstanceLifecycle' in instance
            if is_spot and asg_tag == "use-spot":
                logger.debug("Instance %s is spot and ASG %s is configured for spot. Ignoring termination request", instance.InstanceId, asg_meta.get_name())
                continue

            if asg_tag == "no-spot" and not is_spot:
                logger.debug("Instance %s is on-demand and ASG %s is configured for on-demand. Ignoring termination request", instance.InstanceId, asg_meta.get_name())
                continue

            if not asg_meta.is_instance_running(instance):
                logger.debug("Instance %s not running. Ignoring termination request", instance.InstanceId)
                continue

            launch_time = instance.LaunchTime
            current_time = datetime.utcnow().replace(tzinfo=pytz.utc)
            elapsed_seconds = (current_time - launch_time). \
                total_seconds()

            # If the instance is running for hours, only the seconds in
            # the current hour need to be used.
            # elapsed_seconds_in_hour = elapsed_seconds % \
            #    SECONDS_PER_HOUR
            # Start a thread that will check whether the instance
            # should continue running ~40 minutes later.

            # Earlier, the instances were terminated at approx. the boundary of 1 hour since
            # EC2 prices were for every hour. However, it has changed now and pricing is
            # per minute.
            # seconds_before_check = abs((40.0 + randint(0, 19)) *
            #                            SECONDS_PER_MINUTE -
            #                            elapsed_seconds_in_hour)
            # TODO: Make this time configurable!
            seconds_before_check = 10
            instance_id = instance.InstanceId
            if instance_id in self.on_demand_kill_threads.keys():
                continue

            logger.info("Scheduling termination thread for %s (%s) in ASG %s (%s) after %s seconds",
                        asg_meta.get_instance_name(instance), instance_id, asg_meta.get_name(), asg_tag, seconds_before_check)
            args = [instance, asg_meta, asg_semaphore]
            timed_thread = Timer(seconds_before_check, self.run_or_die,
                                    args=args)
            timed_thread.setDaemon(True)
            timed_thread.start()
            self.on_demand_kill_threads[instance_id] = timed_thread
        return

    def populate_instances(self, asg_meta):
        """ Populates info about all instances running in the given ASG. """
        response = AWSMinionManager.describe_asg_with_retries(
            self._ac_client, [asg_meta.get_name()])
        instance_ids = []
        asg = response.AutoScalingGroups[0]
        for instance in asg.Instances:
            instance_ids.append(instance.InstanceId)

        if len(instance_ids) <= 0:
            return

        response = self.get_instances_with_retries(self._ec2_client, instance_ids)
        running_instances = []
        for resv in response.Reservations:
            for instance in resv.Instances:
                if asg_meta.is_instance_running(instance):
                    running_instances.append(instance)
        asg_meta.add_instances(running_instances)

    def minion_manager_work(self):
        """ The main work for dealing with spot-instances happens here. """
        logger.info("Running minion-manager...")
        if self._events_only:
            logger.info("Only logging events\n")
        while True:
            try:
                # Iterate over all asgs and update them if needed.
                for asg_meta in self._asg_metas:
                    # Populate info. about all instances in the ASG
                    self.populate_instances(asg_meta)

                    # Check if any of these are instances that need to be terminated.
                    self.schedule_instance_termination(asg_meta)

                    if not self.update_needed(asg_meta):
                        continue

                    # Some update is needed. This can mean:
                    # 1. The desired # of instances are not running
                    # 2. The ASG tag and the type of running instances do not match.
                    # 3.
                    bid_info = asg_meta.get_bid_info()
                    if asg_meta.get_mm_tag() == "no-spot" and bid_info["type"] == "spot":
                        new_bid_info = self.create_on_demand_bid_info()
                        logger.info("ASG %s configured with no-spot but currently using spot. Updating...", asg_meta.get_name())
                        self.update_scaling_group(asg_meta, new_bid_info)
                        continue

                    new_bid_info = self.get_new_bid_info(asg_meta)
                    
                    # Change ASG to on-demand if insufficient capacity
                    if self.check_insufficient_capacity(asg_meta):
                        new_bid_info = self.create_on_demand_bid_info()
                        logger.info("ASG %s spot instance have not sufficient resource. Updating to on-demand...", asg_meta.get_name())
                        self.update_scaling_group(asg_meta, new_bid_info)
                        continue

                    # Update ASGs iff new bid is different from current bid.
                    if self.are_bids_equal(asg_meta.bid_info, new_bid_info):
                        logger.info("No change in bid info for %s",
                                   asg_meta.get_name())
                        continue
                    logger.info("Got new bid info from BidAdvisor: %s", new_bid_info)

                    self.update_scaling_group(asg_meta, new_bid_info)
            except Exception as ex:
                logger.exception("Failed while checking instances in ASG: " +
                                 str(ex))
            finally:
                # Cooling off period. TODO: Make this configurable!
                time.sleep(self._refresh_interval_seconds)

                try:
                    # Discover and populate the correct ASGs.
                    del self._asg_metas[:]
                    self.discover_asgs()
                    self.populate_current_config()
                except Exception as ex:
                    raise Exception("Failed to discover/populate current ASG info: " + str(ex))

    def create_on_demand_bid_info(self):
        new_bid_info = {}
        new_bid_info["type"] = "on-demand"
        new_bid_info["price"] = ""
        return new_bid_info

    def run(self):
        """Entrypoint for the AWS specific minion-manager."""
        logger.info("Running AWS Minion Manager")

        try:
            # Discover and populate the correct ASGs.
            self.discover_asgs()
            self.populate_current_config()
        except Exception as ex:
            raise Exception("Failed to discover/populate current ASG info: " +
                            str(ex))

        self.bid_advisor.run()

        self.price_reporter.run()

        self.minion_manager_work()
        return

    def check_scaling_group_instances(self, scaling_group):
        """
        Checks whether desired number of instances are running in an ASG.
        Also, schedules termination of "on-demand" instances.
        """
        asg_meta = scaling_group
        attempts_to_converge = 3
        while attempts_to_converge > 0:
            asg_info = asg_meta.get_asg_info()
            response = AWSMinionManager.describe_asg_with_retries(
                self._ac_client, [asg_info.AutoScalingGroupName])
            asg = response.AutoScalingGroups[0]

            if asg.DesiredCapacity <= len(asg.Instances):
                # The DesiredCapacity can be <= actual number of instances.
                # This can happen during scale down. The autoscaler may have
                # reduced the DesiredCapacity. But it can take sometime before
                # the instances are actually terminated. If this check happens
                # during that time, the DesiredCapacity may be < actual number
                # of instances.
                return True
            else:
                # It is possible that the autoscaler may have just increased
                # the DesiredCapacity but AWS is still in the process of
                # spinning up new instances. To given enough time to AWS to
                # spin up these new instances (i.e. for the desired state and
                # actual state to converge), sleep for 1 minute and try again.
                # If the state doesn't converge even after retries, return
                # False.
                logger.info("Desired number of instances not running in asg %s." +
                            "Desired %d, actual %d", asg_meta.get_name(), asg.DesiredCapacity,
                            len(asg.Instances))
                attempts_to_converge = attempts_to_converge - 1

                # Wait for sometime before checking again.
                time.sleep(60)
        return False
    
    def check_insufficient_capacity(self, scaling_group):
        """
        Checks whether not completed ASG activities got not have sufficient capacity error message.
        """
        # This error message from https://docs.aws.amazon.com/autoscaling/ec2/userguide/ts-as-capacity.html#ts-as-capacity-1
        INSUFFICIENT_CAPACITY_MESSAGE = ['We currently do not have sufficient',
                                           'capacity in the Availability Zone you requested']
        
        WAITING_SPOT_INSTANCE_MESSAGE = ['Placed Spot instance request:', 'Waiting for instance(s)']
        
        asg_info = scaling_group.get_asg_info()
        response = AWSMinionManager.describe_asg_activities_with_retries(
            self._ac_client, asg_info.AutoScalingGroupName)
        activities = response.Activities
        
        for activity in activities:
            if activity.Progress == 100:
                continue
            if 'StatusMessage' in activity and len([message for message in INSUFFICIENT_CAPACITY_MESSAGE if message in activity.StatusMessage]) == len(INSUFFICIENT_CAPACITY_MESSAGE):
                return True
            
            # Check spot request status code
            if 'StatusMessage' in activity and len([message for message in WAITING_SPOT_INSTANCE_MESSAGE if message in activity.StatusMessage]) == len(WAITING_SPOT_INSTANCE_MESSAGE):
                spot_req_regex = re.compile('Placed Spot instance request: (?P<spot_req_id>sir-[a-zA-Z0-9]+)\. Waiting for instance\(s\)')
                spot_req_re_result = spot_req_regex.search(activity.StatusMessage)
                if spot_req_re_result is not None and \
                        self.check_spot_request_insufficient_capacity(spot_req_re_result.group('spot_req_id')):
                    return True
            
        return False
    
    def check_spot_request_insufficient_capacity(self, spot_request):
        OVERSUBSCRIBED_MESSAGE = 'capacity-oversubscribed'
        CAPACITY_NOT_AVAILABLE = 'capacity-not-available'
        
        response = AWSMinionManager.describe_spot_request_with_retries(self._ec2_client, [spot_request])
        requests = response.SpotInstanceRequests
        for request in requests:
            if 'Status' in request and 'Code' in request.Status:
                if OVERSUBSCRIBED_MESSAGE == request.Status.Code or CAPACITY_NOT_AVAILABLE == request.Status.Code:
                    return True
                
        return False
        
    def get_asg_metas(self):
        """ Return all asg_meta """
        return self._asg_metas
