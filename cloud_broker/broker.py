"""
The Broker object takes a cloud provider name as input and returns the
appropriate object on which subsequent methods can be called.
"""

from cloud_provider.aws.aws_minion_manager import AWSMinionManager


class Broker(object):
    """ Create and return cloud provider specific objects """

    @staticmethod
    def get_impl_object(provider, scaling_groups, region, **kwargs):
        """
        Given a cloud provider name, return the cloud provider specific
        implementation.
        """
        if provider.lower() == "aws":
            return AWSMinionManager(scaling_groups, region, **kwargs)

        raise NotImplementedError
