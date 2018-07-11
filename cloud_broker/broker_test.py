"""The file has unit tests for the cloud broker."""

import unittest
import pytest
from cloud_broker.broker import Broker


class BrokerTest(unittest.TestCase):
    """
    Tests for cloud broker.
    """

    def test_get_impl_object(self):
        """
        Tests that the get_impl_object method works as expected.
        """

        # Verify that a minion-manager object is returned for "aws"
        mgr = Broker.get_impl_object("aws", "mycluster", "us-west-2")
        assert mgr is not None, "No minion-manager returned!"

        # For non-aws clouds, a NotImplementedError is returned.
        with pytest.raises(NotImplementedError):
            mgr = Broker.get_impl_object("google", "mycluster", "")
