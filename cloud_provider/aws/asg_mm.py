""" Metadata for each Autoscaling group in AWS. """


class AWSAutoscalinGroupMM(object):
    """
    This class has metadata associated with an autoscaling group.
    """
    # 'asg_info' is populated with the returned value of
    # describe_autoscaling_groups() API.
    asg_info = None

    # 'lc_info' is the LaunchConfiguration info returned by
    # describe_launch_configurations() API.
    lc_info = None

    # 'bid_info' is a simple dictionary with keys 'type' and 'bid_price'.
    bid_info = {}

    # Metadata about all instances running in this ASG, keyed by instance-id.
    instance_info = {}

    def get_name(self):
        """ Convenience method to get the name of the ASG. """
        return self.asg_info.AutoScalingGroupName

    def set_asg_info(self, asg_info):
        """ Sets the asg_info. """
        assert asg_info is not None, "Can't set ASG info to None!"
        self.asg_info = asg_info

    def set_lc_info(self, lc_info):
        """ Sets the lc_info. """
        assert lc_info is not None, "Can't set lc_info info to None!"
        self.lc_info = lc_info

    def set_bid_info(self, bid_info):
        """ Sets the bif_info. """
        assert bid_info is not None, "Can't set bid_info info to None!"
        self.bid_info = bid_info

    def get_asg_info(self):
        """ Returns the asg_info. """
        return self.asg_info

    def get_lc_info(self):
        """ Returns the lc_info. """
        return self.lc_info

    def get_bid_info(self):
        """ Returns the bid_info. """
        return self.bid_info

    def add_instances(self, instances):
        """
        Adds the given instances to the 'instances' array if they're not
        present.
        """
        for instance in instances:
            if instance.InstanceId in self.instance_info.keys():
                continue
            else:
                self.instance_info[instance.InstanceId] = instance

    def remove_instance(self, instance_id):
        """
        Removes the given instance from the 'instances' array.
        """
        self.instance_info.pop(instance_id, None)

    def get_instance_info(self):
        """ Returns the instances. """
        return self.instance_info

    def get_instances(self):
        """ Returns the 'instance' objects. """
        return self.instance_info.values()
