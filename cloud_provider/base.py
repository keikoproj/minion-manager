#!/usr/bin/env python

"""
Define the base class for the minion-manager. Cloud provider specific
implementations should derive from this.
"""

import abc


class MinionManagerBase(object):
    """ Base class for MinionManager. """
    __metaclass__ = abc.ABCMeta
    _region = None

    def __init__(self, region):
        self._region = region

    @abc.abstractmethod
    def run(self):
        """Main method for the minion-manager functionality."""
        return

    @abc.abstractmethod
    def check_scaling_group_instances(self, scaling_group):
        """
        Checks whether desired number of instances are running in a scaling
        group.
        """
        return

    @abc.abstractmethod
    def update_scaling_group(self, scaling_group, new_bid_info):
        """
        Updates the scaling group config.
        """
        return
