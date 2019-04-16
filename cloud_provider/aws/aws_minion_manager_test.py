"""The file has unit tests for the AWSMinionManager."""

import unittest
import mock
import pytest
import subprocess
import shlex
from cloud_provider.aws.aws_minion_manager import AWSMinionManager
from cloud_provider.aws.aws_bid_advisor import AWSBidAdvisor
from moto import mock_autoscaling, mock_sts, mock_ec2
import boto3
from bunch import bunchify
import time


class AWSMinionManagerTest(unittest.TestCase):
    """
    Tests for the AWSMinionManager.
    """
    cluster_name = "cluster"
    cluster_id = "abcd-c0ffeec0ffee"
    cluster_name_id = cluster_name + "-" + cluster_id
    asg_name = cluster_name_id + "-asg"
    lc_name = cluster_name_id + "-lc"
    insufficient_resource_message = "We currently do not have sufficient p2.xlarge capacity in the Availability Zone you requested (us-west-2b). Our system will be working on provisioning additional capacity. You can currently get p2.xlarge capacity by not specifying an Availability Zone in your request or choosing us-west-2c, us-west-2a."
    asg_waiting_for_spot_instance = 'Placed Spot instance request: sir-3j8r1t2p. Waiting for instance(s)'

    session = boto3.Session(region_name="us-west-2")
    autoscaling = session.client("autoscaling")
    ec2 = session.client("ec2")

    @mock_autoscaling
    @mock_sts
    def delete_mock_asgs(self):
        """ Deletes the moto resources. """
        self.autoscaling.delete_auto_scaling_group(
            AutoScalingGroupName=self.asg_name,
            ForceDelete=True
        )

        self.autoscaling.delete_launch_configuration(
            LaunchConfigurationName=self.lc_name,
        )

    @mock_ec2
    def get_ami(self):
        """
        Getting Mock Ami
        """
        response = self.ec2.describe_images()
        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200
        ami = response['Images'][0]['ImageId']
        return ami

    @mock_autoscaling
    @mock_sts
    def create_mock_asgs(self, minion_manager_tag="use-spot", not_terminate=False):
        """
        Creates mocked AWS resources.
        """
        if minion_manager_tag == "use-spot":
            response = self.autoscaling.create_launch_configuration(
                LaunchConfigurationName=self.lc_name, ImageId=self.get_ami(),
                SpotPrice="0.100", KeyName='kubernetes-some-key')
        else:
            response = self.autoscaling.create_launch_configuration(
                LaunchConfigurationName=self.lc_name, ImageId=self.get_ami(),
                KeyName='kubernetes-some-key')
        resp = bunchify(response)
        assert resp.ResponseMetadata.HTTPStatusCode == 200
        
        asg_tags = [{'ResourceId': self.cluster_name_id,
                     'Key': 'KubernetesCluster', 'Value': self.cluster_name_id},
                    {'ResourceId': self.cluster_name_id,
                     'Key': 'k8s-minion-manager', 'Value': minion_manager_tag},
                    {'ResourceId': self.cluster_name_id,
                     'PropagateAtLaunch': True,
                     'Key': 'Name', 'Value': "my-instance-name"},
                    ]
        
        if not_terminate:
            asg_tags.append({'ResourceId': self.cluster_name_id,
                             'Key': 'k8s-minion-manager/not-terminate', 'Value': 'True'})

        response = self.autoscaling.create_auto_scaling_group(
            AutoScalingGroupName=self.asg_name,
            LaunchConfigurationName=self.lc_name, MinSize=3, MaxSize=3,
            DesiredCapacity=3,
            AvailabilityZones=['us-west-2a'],
            Tags=asg_tags
        )
        resp = bunchify(response)
        assert resp.ResponseMetadata.HTTPStatusCode == 200

    def basic_setup_and_test(self, minion_manager_tag="use-spot", not_terminate=False):
        """
        Creates the mock setup for tests, creates the aws_mm object and does
        some basic sanity tests before returning it.
        """
        self.create_mock_asgs(minion_manager_tag, not_terminate)
        aws_mm = AWSMinionManager(self.cluster_name_id, "us-west-2", refresh_interval_seconds=50)
        assert len(aws_mm.get_asg_metas()) == 0, \
            "ASG Metadata already populated?"

        aws_mm.discover_asgs()
        assert aws_mm.get_asg_metas() is not None, "ASG Metadata not populated"
        assert len(aws_mm.get_asg_metas()) == 1, "ASG not discovered"

        for asg in aws_mm.get_asg_metas():
            assert asg.asg_info.AutoScalingGroupName == self.asg_name

        aws_mm.populate_current_config()
        return aws_mm

    @mock_autoscaling
    @mock_sts
    def test_discover_asgs(self):
        """
        Tests that the discover_asgs method works as expected.
        """
        self.basic_setup_and_test()

    @mock_autoscaling
    @mock_sts
    @mock_ec2
    def test_populate_instances(self):
        """
        Tests that existing info. about ASGs is populated correctly.
        """
        aws_mm = self.basic_setup_and_test()
        asg = aws_mm.get_asg_metas()[0]

        orig_instance_count = len(asg.get_instance_info())
        aws_mm.populate_instances(asg)
        assert len(asg.get_instance_info()) == orig_instance_count + 3

        instance = asg.get_instance_info().itervalues().next()
        assert asg.get_instance_name(instance) == "my-instance-name"

        assert asg.is_instance_running(instance) == True
        # Simulate that the instance has been terminated.
        instance.State.Code = 32
        instance.State.Name = "shutting-down"
        assert asg.is_instance_running(instance) == False

    @mock_autoscaling
    @mock_sts
    def test_populate_current_config(self):
        """
        Tests that existing instances are correctly populated by the
        populate_instances() method.
        """
        aws_mm = self.basic_setup_and_test()
        for asg_meta in aws_mm.get_asg_metas():
            assert asg_meta.get_lc_info().LaunchConfigurationName == \
                   self.lc_name
            assert asg_meta.get_bid_info()["type"] == "spot"
            assert asg_meta.get_bid_info()["price"] == "0.100"

    @mock_autoscaling
    @mock_sts
    @pytest.mark.skip(
        reason="Moto doesn't have some fields in it's LaunchConfig.")
    def test_update_cluster_spot(self):
        """
        Tests that the AWSMinionManager correctly creates launch-configs and
        updates the ASG.

        Note: Moto doesn't have the ClassicLinkVPCSecurityGroups and
        IamInstanceProfile fields in it's LaunchConfig. Running the test below
        required manually commenting out these fields in the call to
        create_launch_configuration :(
        """
        awsmm = self.basic_setup_and_test()
        bid_info = {}
        bid_info["type"] = "spot"
        bid_info["price"] = "10"
        awsmm.update_scaling_group(awsmm.get_asg_metas()[0], bid_info)

    @mock_autoscaling
    @mock_sts
    @pytest.mark.skip(
        reason="Moto doesn't have some fields in it's LaunchConfig.")
    def test_update_cluster_on_demand(self):
        """
        Tests that the AWSMinionManager correctly creates launch-configs and
        updates the ASG.

        Note: Moto doesn't have the ClassicLinkVPCSecurityGroups and
        IamInstanceProfile fields in it's LaunchConfig. Running the test below
        required manually commenting out these fields in the call to
        create_launch_configuration :(
        """
        awsmm = self.basic_setup_and_test()
        bid_info = {"type": "on-demand"}
        awsmm.update_scaling_group(awsmm.get_asg_metas()[0], bid_info)

    @mock_autoscaling
    @mock_sts
    @mock_ec2
    def test_update_needed(self):
        """
        Tests that the AWSMinionManager correctly checks if updates are needed.
        """
        # Try to use spot-instances.
        awsmm = self.basic_setup_and_test("use-spot")
        asg_meta = awsmm.get_asg_metas()[0]
        # Moto returns that all instances are running. No updates needed.
        assert awsmm.update_needed(asg_meta) is False
        # Simulate that the running instances are on-demand instances
        bid_info = {"type": "on-demand"}
        asg_meta.set_bid_info(bid_info)
        assert awsmm.update_needed(asg_meta) is True
        # Simulate that the running instances are spot instances
        bid_info = {"type": "spot"}
        asg_meta.set_bid_info(bid_info)
        assert awsmm.update_needed(asg_meta) is False

        # Try to ony use on-demand instances.
        awsmm = self.basic_setup_and_test("no-spot")
        asg_meta = awsmm.get_asg_metas()[0]
        assert awsmm.update_needed(asg_meta) is False

        # Simulate that the running instances are on-demand instances
        bid_info = {"type": "on-demand"}
        asg_meta.set_bid_info(bid_info)
        assert awsmm.update_needed(asg_meta) is False

        # Simulate that the running instances are spot-instances.
        bid_info = {"type": "spot"}
        asg_meta.set_bid_info(bid_info)
        assert awsmm.update_needed(asg_meta) is True

    @mock_autoscaling
    @mock_sts
    def test_bid_equality(self):
        """
        Tests that 2 bids are considered equal when their type and price match.
        Not equal otherwise.
        """
        a_bid = {}
        a_bid["type"] = "on-demand"
        b_bid = {}
        b_bid["type"] = "on-demand"
        b_bid["price"] = "100"
        awsmm = self.basic_setup_and_test()
        assert awsmm.are_bids_equal(a_bid, b_bid) is True

        # Change type of new bid to "spot".
        b_bid["type"] = "spot"
        assert awsmm.are_bids_equal(a_bid, b_bid) is False

        # Change the type of a_bid to "spot" but a different price.
        a_bid["type"] = "spot"
        a_bid["price"] = "90"
        assert awsmm.are_bids_equal(a_bid, b_bid) is False

        a_bid["price"] = "100"
        assert awsmm.are_bids_equal(a_bid, b_bid) is True

    @mock_autoscaling
    @mock_ec2
    @mock_sts
    def test_awsmm_instances(self):
        """
        Tests that the AWSMinionManager correctly tracks running instances.
        """
        awsmm = self.basic_setup_and_test()
        asg_meta = awsmm.get_asg_metas()[0]
        assert awsmm.check_scaling_group_instances(asg_meta)

        # Update the desired # of instances in the ASG. Verify that
        # minion-manager continues to account for the new instances.
        self.autoscaling.update_auto_scaling_group(
            AutoScalingGroupName=self.asg_name, MaxSize=4, DesiredCapacity=4)
        assert awsmm.check_scaling_group_instances(asg_meta)

    @mock_autoscaling
    @mock_ec2
    @mock_sts
    def test_instance_termination(self):
        """
        Tests that the AWSMinionManager schedules instance termination.
        """
        def _instance_termination_test_helper(minion_manager_tag, expected_kill_threads):
            awsmm = self.basic_setup_and_test(minion_manager_tag)
            assert len(awsmm.on_demand_kill_threads) == 0
            asg_meta = awsmm.get_asg_metas()[0]
            # Set instanceType since moto's instances don't have it.
            instance_type = "m3.medium"
            zone = "us-west-2b"
            awsmm.bid_advisor.on_demand_price_dict[instance_type] = "100"
            awsmm.bid_advisor.spot_price_list = [{'InstanceType': instance_type,
                                                'SpotPrice': '80',
                                                'AvailabilityZone': zone}]
            for instance in asg_meta.get_instances():
                instance.InstanceType = instance_type
            awsmm.populate_instances(asg_meta)
            awsmm.schedule_instance_termination(asg_meta)
            assert len(awsmm.on_demand_kill_threads) == expected_kill_threads

            time.sleep(15)
            assert len(awsmm.on_demand_kill_threads) == 0

        _instance_termination_test_helper("use-spot", 3)
        _instance_termination_test_helper("no-spot", 0)
        _instance_termination_test_helper("abcd", 0)
        
    @mock_autoscaling
    @mock_ec2
    @mock_sts
    def test_instance_not_termination(self):
        """
        Tests that the AWSMinionManager won't terminate instance with not-terminate tag.
        """
        def _instance_termination_test_helper(minion_manager_tag, expected_kill_threads):
            awsmm = self.basic_setup_and_test(minion_manager_tag, True)
            # Inject `k8s-minion-manager/not-terminate` to awsmm
            
            assert len(awsmm.on_demand_kill_threads) == 0
            asg_meta = awsmm.get_asg_metas()[0]
            # Set instanceType since moto's instances don't have it.
            instance_type = "m3.medium"
            zone = "us-west-2b"
            awsmm.bid_advisor.on_demand_price_dict[instance_type] = "100"
            awsmm.bid_advisor.spot_price_list = [{'InstanceType': instance_type,
                                                  'SpotPrice': '80',
                                                  'AvailabilityZone': zone}]
            for instance in asg_meta.get_instances():
                instance.InstanceType = instance_type
            awsmm.populate_instances(asg_meta)
            awsmm.schedule_instance_termination(asg_meta)
            assert len(awsmm.on_demand_kill_threads) == expected_kill_threads
        
            time.sleep(15)
            assert len(awsmm.on_demand_kill_threads) == 0
    
        _instance_termination_test_helper("use-spot", 0)

    # PriceReporter tests
    @mock_autoscaling
    @mock_ec2
    @mock_sts
    @mock.patch.object(AWSBidAdvisor, 'get_spot_instance_price')
    def test_price_reporter_basic(self, get_spot_instance_price_mock):
        """
        Tests that the PriceReporter populates the pricing info.
        """
        get_spot_instance_price_mock.return_value = "0.100"

        awsmm = self.basic_setup_and_test()
        asg_meta = awsmm.get_asg_metas()[0]
        awsmm.populate_instances(asg_meta)
        assert awsmm.price_reporter is not None

        assert len(awsmm.price_reporter.price_info) == 0
        awsmm.price_reporter.price_reporter_work()
        assert len(awsmm.price_reporter.price_info) == \
            len(asg_meta.get_instances())
        # Call price_reporter_work again. There should now be two values.
        awsmm.price_reporter.price_reporter_work()
        instance = asg_meta.get_instances()[0]
        assert len(awsmm.price_reporter.price_info[
            instance.InstanceId]) == 2

    # Setting semaphore Value
    @mock_autoscaling
    @mock_ec2
    @mock_sts
    def test_set_semaphore(self):
        """
        Testing Semaphore value based on terminate percentage
        """
        def _semaphore_helper(minion_manager_tag, percentage, outcome):
            awsmm = self.basic_setup_and_test(minion_manager_tag)
            asg_meta = awsmm.get_asg_metas()[0]
            awsmm.terminate_percentage = percentage
            get_semaphore = awsmm.set_semaphore(asg_meta)
            assert get_semaphore._Semaphore__value == outcome

        _semaphore_helper('use-spot', 1, 1)
        _semaphore_helper('use-spot', 30, 1)
        _semaphore_helper('use-spot', 60, 2)
        _semaphore_helper('use-spot', 100, 3)

    @mock.patch('subprocess.check_call')
    @mock.patch('cloud_provider.aws.aws_minion_manager.AWSMinionManager.get_name_for_instance')
    @mock_autoscaling
    @mock_ec2
    @mock_sts
    def test_cordon(self, mock_get_name_for_instance, mock_check_call):
        awsmm = self.basic_setup_and_test()
        mock_get_name_for_instance.return_value = "ip-of-fake-node-name"
        awsmm.cordon_node("ip-of-fake-node")
        mock_check_call.assert_called_with(['kubectl', 'drain', 'ip-of-fake-node-name',
            '--ignore-daemonsets=true', '--delete-local-data=true', '--force', '--grace-period=-1'])

        mock_check_call.side_effect = [Exception("Test"), True]
        awsmm.cordon_node("ip-of-fake-node")
        mock_check_call.assert_called_with(['kubectl', 'uncordon', 'ip-of-fake-node-name'])

    @mock.patch('cloud_provider.aws.aws_minion_manager.AWSMinionManager.describe_asg_activities_with_retries')
    @mock_autoscaling
    @mock_ec2
    @mock_sts
    def test_asg_activities_all_done(self, mock_get_name_for_instance):
        mock_get_name_for_instance.return_value = bunchify({'Activities': [{'StatusMessage': 'dummy ok message', 'Progress': 100}, {'StatusMessage': 'dummy ok message2', 'Progress': 100}]})
        
        awsmm = self.basic_setup_and_test()
        asg_meta = awsmm.get_asg_metas()[0]
        assert not awsmm.check_insufficient_capacity(asg_meta)


    @mock.patch('cloud_provider.aws.aws_minion_manager.AWSMinionManager.describe_asg_activities_with_retries')
    @mock_autoscaling
    @mock_ec2
    @mock_sts
    def test_asg_activity_without_statusMessage(self, mock_get_name_for_instance):
        mock_get_name_for_instance.return_value = bunchify({'Activities': [{'Progress': 20}, {'StatusMessage': 'dummy ok message2', 'Progress': 100}]})
    
        awsmm = self.basic_setup_and_test()
        asg_meta = awsmm.get_asg_metas()[0]
        assert not awsmm.check_insufficient_capacity(asg_meta)

    @mock.patch('cloud_provider.aws.aws_minion_manager.AWSMinionManager.describe_asg_activities_with_retries')
    @mock_autoscaling
    @mock_ec2
    @mock_sts
    def test_asg_done_activity_with_insufficient_resource(self, mock_get_name_for_instance):
        mock_get_name_for_instance.return_value = bunchify({'Activities': [{'StatusMessage': self.insufficient_resource_message, 'Progress': 100}]})
    
        awsmm = self.basic_setup_and_test()
        asg_meta = awsmm.get_asg_metas()[0]
        assert not awsmm.check_insufficient_capacity(asg_meta)

    @mock.patch('cloud_provider.aws.aws_minion_manager.AWSMinionManager.describe_asg_activities_with_retries')
    @mock_autoscaling
    @mock_ec2
    @mock_sts
    def test_asg_activity_with_insufficient_resource(self, mock_get_name_for_instance):
        mock_get_name_for_instance.return_value = bunchify({'Activities': [{'StatusMessage': self.insufficient_resource_message, 'Progress': 20}]})
    
        awsmm = self.basic_setup_and_test()
        asg_meta = awsmm.get_asg_metas()[0]
        assert awsmm.check_insufficient_capacity(asg_meta)
        
    @mock.patch('cloud_provider.aws.aws_minion_manager.AWSMinionManager.describe_spot_request_with_retries')
    @mock.patch('cloud_provider.aws.aws_minion_manager.AWSMinionManager.describe_asg_activities_with_retries')
    @mock_autoscaling
    @mock_ec2
    @mock_sts
    def test_spot_request_capacity_oversubscribed(self, mock_get_name_for_instance, mock_spot_request):
        mock_get_name_for_instance.return_value = bunchify({'Activities': [{'StatusMessage': self.asg_waiting_for_spot_instance, 'Progress': 20}]})
        mock_spot_request.return_value = bunchify({'SpotInstanceRequests': [{'Status': {'Code': 'capacity-oversubscribed'}}]})
    
        awsmm = self.basic_setup_and_test()
        asg_meta = awsmm.get_asg_metas()[0]
        assert awsmm.check_insufficient_capacity(asg_meta)

    @mock.patch('cloud_provider.aws.aws_minion_manager.AWSMinionManager.describe_spot_request_with_retries')
    @mock.patch('cloud_provider.aws.aws_minion_manager.AWSMinionManager.describe_asg_activities_with_retries')
    @mock_autoscaling
    @mock_ec2
    @mock_sts
    def test_spot_request_capacity_not_available(self, mock_get_name_for_instance, mock_spot_request):
        mock_get_name_for_instance.return_value = bunchify({'Activities': [{'StatusMessage': self.asg_waiting_for_spot_instance, 'Progress': 20}]})
        mock_spot_request.return_value = bunchify({'SpotInstanceRequests': [{'Status': {'Code': 'capacity-not-available'}}]})
        
        awsmm = self.basic_setup_and_test()
        asg_meta = awsmm.get_asg_metas()[0]
        assert awsmm.check_insufficient_capacity(asg_meta)

    @mock.patch('cloud_provider.aws.aws_minion_manager.AWSMinionManager.describe_spot_request_with_retries')
    @mock.patch('cloud_provider.aws.aws_minion_manager.AWSMinionManager.describe_asg_activities_with_retries')
    @mock_autoscaling
    @mock_ec2
    @mock_sts
    def test_spot_request_other_message(self, mock_get_name_for_instance, mock_spot_request):
        mock_get_name_for_instance.return_value = bunchify({'Activities': [{'StatusMessage': self.asg_waiting_for_spot_instance, 'Progress': 20}]})
        mock_spot_request.return_value = bunchify({'SpotInstanceRequests': [{'Status': {'Code': 'other-message'}}]})
    
        awsmm = self.basic_setup_and_test()
        asg_meta = awsmm.get_asg_metas()[0]
        assert not awsmm.check_insufficient_capacity(asg_meta)
